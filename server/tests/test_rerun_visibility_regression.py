"""重跑可见性与作用域回归（CODE_REVIEW_20261120 第一优先级 3/4/5）。

- P1-3：失败任务留下的隐藏草稿不得阻止资料包到达“已确认”。
- P1-4：新任务成功后，旧任务的未确认草稿被标记“已被后续版本取代”。
- P1-5：文本材料的说话人回退标签必须带任务作用域，重跑不得把新观察
  挂到已人工确认的说话人身上。
"""

from __future__ import annotations

from sqlalchemy import select

from app import models as m, services
from app.query_visibility import visible_observation_clause
from app.schemas import IntentConfirmRequest, SpeakerConfirmRequest
from test_media_completeness import (
    FakeAsr,
    FakeIntent,
    _install_asr,
    _install_diarization,
    _transcript,
    pipeline,
)
from test_pipeline_recovery import _new_run


def _seed_dictionaries(pipeline):
    technology = m.Technology(code="rerun-tech", name="重跑回归技术")
    function = m.CustomerFunction(code="rerun-func", name="重跑回归职能")
    pipeline.session.add_all([technology, function])
    pipeline.session.commit()
    pipeline.session.refresh(technology)
    pipeline.session.refresh(function)
    return technology, function


def _observation_intent(technology, function, quote="安排样机验证"):
    class OneObservation(FakeIntent):
        def analyze(self, context, output_budget=None):
            return {"observations": [{
                "technology_id": technology.id, "quote": quote,
                "attitude_level": 3, "confidence": 0.9,
                "function_id": function.id,
            }]}

    return OneObservation()


def test_new_completed_run_supersedes_older_draft_observations(pipeline, monkeypatch):
    """P1-4：重跑成功后旧草稿被取代，新旧两套观察不同时待确认。"""
    pipeline.add_file("rerun.wav")
    _install_diarization(monkeypatch)
    _install_asr(monkeypatch, FakeAsr(lambda *_a, **_k: _transcript("安排样机验证")))
    technology, _function = _seed_dictionaries(pipeline)

    assert pipeline.execute(_observation_intent(technology, _function)).status == "已完成"
    first_run_observations = pipeline.session.scalars(
        select(m.RawIntentObservation).where(
            m.RawIntentObservation.analysis_run_id == pipeline.run.id
        )
    ).all()
    assert len(first_run_observations) == 1
    assert first_run_observations[0].status == "待确认"

    _new_run(pipeline)
    assert pipeline.execute(_observation_intent(technology, _function)).status == "已完成"

    # 取代走的是批量 UPDATE（synchronize_session=False），使身份映射失效后
    # 再断言数据库里的真实状态。
    pipeline.session.expire_all()
    all_observations = pipeline.session.scalars(
        select(m.RawIntentObservation).where(
            m.RawIntentObservation.package_id == pipeline.package.id
        )
    ).all()
    assert len(all_observations) == 2
    by_run = {item.analysis_run_id: item for item in all_observations}
    superseded = [
        item for run_id, item in by_run.items() if run_id != pipeline.run.id
    ]
    assert [item.status for item in superseded] == ["已被后续版本取代"]
    assert by_run[pipeline.run.id].status == "待确认"
    # 可见集合里只剩最新一次的草稿
    visible = pipeline.session.scalars(
        select(m.RawIntentObservation).where(
            m.RawIntentObservation.package_id == pipeline.package.id,
            m.RawIntentObservation.status == "待确认",
            visible_observation_clause(),
        )
    ).all()
    assert [item.id for item in visible] == [by_run[pipeline.run.id].id]


def test_hidden_drafts_do_not_block_package_confirmation(pipeline, monkeypatch):
    """P1-3：失败运行留下的隐藏草稿不计入待处理，确认完可见条目后包到达“已确认”。"""
    pipeline.add_file("confirm.wav")
    _install_diarization(monkeypatch)
    _install_asr(monkeypatch, FakeAsr(lambda *_a, **_k: _transcript("安排样机验证")))
    technology, function = _seed_dictionaries(pipeline)

    assert pipeline.execute(_observation_intent(technology, function)).status == "已完成"

    # 模拟一次失败运行：意向先落库、身份阶段失败（观察保留但被隐藏）。
    failed_run = m.AnalysisRun(
        package_id=pipeline.package.id, adapter="ollama",
        model_name="test-intent", status="处理失败",
    )
    pipeline.session.add(failed_run)
    pipeline.session.flush()
    hidden_speaker = m.SpeakerCluster(
        package_id=pipeline.package.id, label="Speaker hidden",
        recommended_name="隐藏人员", status="待确认",
    )
    pipeline.session.add(hidden_speaker)
    pipeline.session.flush()
    visible_observation = pipeline.session.scalar(
        select(m.RawIntentObservation).where(
            m.RawIntentObservation.analysis_run_id == pipeline.run.id
        )
    )
    hidden_observation = m.RawIntentObservation(
        package_id=pipeline.package.id, analysis_run_id=failed_run.id,
        customer_id=pipeline.package.customer_id,
        technology_id=technology.id, function_id=function.id,
        speaker_id=hidden_speaker.id,
        evidence_id=visible_observation.evidence_id,
        raw_quote="安排样机验证", status="待确认",
    )
    pipeline.session.add(hidden_observation)
    pipeline.session.commit()

    # 确认可见说话人与观察后，包必须到达“已确认”（隐藏行不计数）。
    speaker = pipeline.session.get(m.SpeakerCluster, visible_observation.speaker_id)
    services.confirm_speaker(
        pipeline.session, speaker,
        SpeakerConfirmRequest(version=speaker.version, display_name="王工",
                              function_id=function.id),
    )
    observation = pipeline.session.scalars(
        select(m.RawIntentObservation).where(
            m.RawIntentObservation.analysis_run_id == pipeline.run.id
        )
    ).one()
    services.confirm_observation(
        pipeline.session, observation,
        IntentConfirmRequest(version=observation.version, attitude_level=3),
    )
    pipeline.session.refresh(pipeline.package)
    assert pipeline.package.status == "已确认"


def test_text_material_speakers_are_run_scoped(pipeline, monkeypatch):
    """P1-5：文本材料的回退说话人标签带任务作用域，重跑不挂到已确认身份。"""
    pipeline.add_file("安排样机验证纪要.txt", "会议资料")
    technology, function = _seed_dictionaries(pipeline)

    assert pipeline.execute(
        _observation_intent(technology, function, quote="安排样机验证")
    ).status == "已完成"
    first_observation = pipeline.session.scalars(
        select(m.RawIntentObservation).where(
            m.RawIntentObservation.analysis_run_id == pipeline.run.id
        )
    ).one()
    first_speaker = pipeline.session.get(m.SpeakerCluster, first_observation.speaker_id)
    # 人工确认第一次的身份
    first_speaker.status = "已确认"
    first_speaker.confirmed_name = "采购王工"
    pipeline.session.commit()

    _new_run(pipeline)
    assert pipeline.execute(
        _observation_intent(technology, function, quote="安排样机验证")
    ).status == "已完成"
    second_observation = pipeline.session.scalars(
        select(m.RawIntentObservation).where(
            m.RawIntentObservation.analysis_run_id == pipeline.run.id
        )
    ).one()
    second_speaker = pipeline.session.get(m.SpeakerCluster, second_observation.speaker_id)
    # 新观察必须落到新的（待确认）说话人，而不是复用已确认身份
    assert second_speaker.id != first_speaker.id
    assert second_speaker.status == "待确认"
