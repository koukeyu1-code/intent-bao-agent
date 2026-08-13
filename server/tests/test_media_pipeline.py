from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import wait_for_analysis

from app.evaluation import run_evaluation
from app.audio_adapters import LlamaCppAsrAdapter
from app.main import create_app
from app import models as m
from app.auth import AppUser, ROLE_VIEWER
from app.media_processing import (
    extract_pdf_text,
    parse_srt,
    parse_vtt,
    probe_media_duration,
)
from app.stage_adapters import OllamaLlmStageAdapter


def _client(tmp_path: Path):
    app = create_app(
        database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        data_dir=tmp_path / "data",
        seed=True,
    )
    test_client = TestClient(app)
    login = test_client.post(
        "/api/auth/login",
        json={
            "username": "admin",
            "password": os.environ["INTENT_BAO_ADMIN_PASSWORD"],
        },
    )
    assert login.status_code == 200, login.text
    test_client.headers.update(
        {"Authorization": f"Bearer {login.json()['access_token']}"}
    )
    return test_client


def test_parse_srt_and_vtt() -> None:
    srt = """1
00:00:01,000 --> 00:00:03,500
第一段转写内容

2
00:00:04,000 --> 00:00:06,000
第二段内容
"""
    segments = parse_srt(srt)
    assert len(segments) == 2
    assert segments[0].start_ms == 1000
    assert segments[0].end_ms == 3500
    assert segments[1].text == "第二段内容"

    vtt = """WEBVTT

00:00:01.000 --> 00:00:03.500
视频字幕第一段
"""
    vtt_segments = parse_vtt(vtt)
    assert len(vtt_segments) == 1
    assert vtt_segments[0].start_ms == 1000
    assert vtt_segments[0].end_ms == 3500


def test_extract_pdf_text_roundtrip(tmp_path: Path) -> None:
    import fitz

    pdf_path = tmp_path / "sample.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Mini LED 27英寸5K 意向验证")
    document.save(pdf_path)
    document.close()

    pages = extract_pdf_text(pdf_path)
    assert len(pages) == 1
    assert "Mini LED" in pages[0].text


def test_probe_media_duration_missing_file_returns_none(tmp_path: Path) -> None:
    assert probe_media_duration(tmp_path / "missing.wav") is None


def _identity_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "message": {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "speakers": [
                            {
                                "speaker_label": "Speaker A",
                                "display_name": "赵总",
                                "role": "产品规划负责人",
                                "organization": "产品规划部",
                                "function_id": "function-product",
                                "granularity": "完整",
                                "confidence": 0.9,
                                "reason": "自我介绍与发言内容一致",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
            }
        },
    )


def test_stage_adapter_recommend_identities() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _identity_response()

    adapter = OllamaLlmStageAdapter(
        base_url="http://127.0.0.1:11434",
        model_name="qwen-test:latest",
        transport=httpx.MockTransport(handler),
    )
    speakers = adapter.recommend_identities(
        segments=[{"id": "s1", "speaker_label": "Speaker A", "text": "样机可以验证"}],
        attendees=[{"display_name": "赵总", "role": "产品规划负责人"}],
        master_data={
            "functions": [
                {"id": "function-product", "name": "产品", "aliases": []}
            ]
        },
    )
    assert len(speakers) == 1
    assert speakers[0].display_name == "赵总"
    assert speakers[0].function_id == "function-product"


def test_stage_adapter_align_materials() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "alignments": [
                                {
                                    "segment_id": "s1",
                                    "target_type": "pdf_page",
                                    "target_id": "p12",
                                    "confidence": 0.8,
                                    "reason": "提到规格表",
                                }
                            ]
                        }
                    ),
                }
            },
        )

    adapter = OllamaLlmStageAdapter(
        base_url="http://127.0.0.1:11434",
        model_name="qwen-test:latest",
        transport=httpx.MockTransport(handler),
    )
    alignments = adapter.align_materials(
        segments=[{"id": "s1", "text": "AGLR增加结构厚度"}],
        pages=[{"id": "p12", "page_no": 12, "text": "结构厚度规格"}],
        frames=[],
    )
    assert alignments[0].target_id == "p12"
    assert alignments[0].target_type == "pdf_page"


def test_llamacpp_asr_adapter_handles_server_text_format(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "type": "transcript.text.done",
                "text": (
                    "language Chinese<asr_text>"
                    "这是测试转写结果，包含<|startofthink|>格式标记。"
                ),
                "usage": {"type": "tokens", "total_tokens": 71},
            },
        )

    adapter = LlamaCppAsrAdapter(
        base_url="http://127.0.0.1:8099",
        model_name="qwen3-asr",
        transport=httpx.MockTransport(handler),
    )
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"RIFF-test-wav")
    result = adapter.transcribe(
        audio_path,
        language="zh",
        hotwords=["Mini LED"],
        duration_ms=30_000,
    )
    assert len(result.segments) == 1
    assert result.segments[0].text == "这是测试转写结果，包含格式标记。"
    assert result.segments[0].end_ms == 30_000
    assert result.segments[0].language == "zh"


def test_clean_transcript_text_strips_model_formatting() -> None:
    from app.audio_adapters import _clean_transcript_text

    assert (
        _clean_transcript_text(
            "language Chinese<asr_text>空山不见人，但闻人语响。"
        )
        == "空山不见人，但闻人语响。"
    )
    assert (
        _clean_transcript_text(
            "<|startofthink|>推理<|endofthink|> language zh<asr_text> 正文 "
        )
        == "正文"
    )
    assert _clean_transcript_text("language zh 无标记转写") == "无标记转写"
    assert _clean_transcript_text("普通纯文本") == "普通纯文本"


def test_diarizer_falls_back_when_models_cache_missing(tmp_path: Path) -> None:
    from app.config import Settings
    from app.diarization import get_diarizer

    settings = Settings.load(data_dir=tmp_path / "data")
    # Disposable test environments have no FunASR model cache; the pipeline
    # must silently keep using whole-file transcription.
    assert get_diarizer(settings) is None


def test_model_evidence_reuses_asr_speaker_and_keeps_source_coordinates(
    tmp_path: Path,
) -> None:
    from app.model_adapters import MockIntentModelAdapter
    from app.schemas import IntentConfirmRequest
    from app.services import (
        DomainError,
        _persist_model_observations,
        confirm_observation,
    )

    client = _client(tmp_path)
    package = _create_package_with_minutes(client, "证据链与说话人复用")
    database = client.app.state.database
    with database.session() as session:
        package_row = session.get(m.MeetingPackage, package["id"])
        assert package_row is not None
        audio = session.scalar(
            select(m.MaterialFile).where(
                m.MaterialFile.package_id == package_row.id
            )
        )
        assert audio is not None
        audio.material_type = "录音"
        pdf = m.MaterialFile(
            package_id=package_row.id,
            original_name="review-deck.pdf",
            safe_name="review-deck.pdf",
            media_type="application/pdf",
            material_type="会议资料",
            relative_path=f"projects/{package_row.id}/review-deck.pdf",
            sha256="b" * 64,
            size_bytes=1024,
        )
        run = m.AnalysisRun(
            package_id=package_row.id,
            adapter="ollama",
            model_name="local-test-model",
            status="运行中",
        )
        asr_speaker = m.SpeakerCluster(
            package_id=package_row.id,
            label="SPEAKER_00",
            recommended_name="ASR待确认人员",
            confirmed_name="赵总",
            confirmed_role="产品规划负责人",
            confirmed_organization="产品部",
            confirmed_function_id="function-product",
            status="已确认",
        )
        session.add_all([pdf, run, asr_speaker])
        session.flush()

        timed_quote = "Mini LED可以进入样机验证"
        page_quote = "背光成本仍需进一步确认"
        speaker_count, observation_count, unresolved = (
            _persist_model_observations(
                session,
                package_row,
                run,
                [
                    {
                        "id": "transcript-derived-id",
                        "type": "转写片段",
                        "name": "SPEAKER_00",
                        "content": f"[00:00:12.000-00:00:18.500] {timed_quote}",
                        "source_material_file_id": audio.id,
                        "source_material_type": "录音",
                        "source_material_name": audio.safe_name,
                        "speaker_label": "SPEAKER_00",
                        "start_ms": 12_000,
                        "end_ms": 18_500,
                    },
                    {
                        "id": "pdf-page-derived-id",
                        "type": "PDF页面",
                        "name": "review-deck.pdf 第7页",
                        "content": page_quote,
                        "source_material_file_id": pdf.id,
                        "source_material_type": "会议资料",
                        "source_material_name": pdf.safe_name,
                        "page_no": 7,
                    },
                ],
                {
                    "observations": [
                        {
                            "technology_id": "tech-mini-led-backlight",
                            "function_id": "function-product",
                            "speaker_name": "赵总",
                            "quote": timed_quote,
                            "attitude_level": 4,
                            "confidence": 0.9,
                        },
                        {
                            "technology_id": "tech-mini-led-backlight",
                            "function_id": "function-purchase",
                            "quote": page_quote,
                            "attitude_level": 3,
                            "confidence": 0.8,
                        },
                    ]
                },
                MockIntentModelAdapter(),
                material_file_ids={audio.id, pdf.id},
            )
        )
        session.flush()

        assert (speaker_count, observation_count, unresolved) == (2, 2, [])
        clusters = session.scalars(
            select(m.SpeakerCluster).where(
                m.SpeakerCluster.package_id == package_row.id
            )
        ).all()
        assert len(clusters) == 2

        timed_observation = session.scalar(
            select(m.RawIntentObservation).where(
                m.RawIntentObservation.raw_quote == timed_quote
            )
        )
        page_observation = session.scalar(
            select(m.RawIntentObservation).where(
                m.RawIntentObservation.raw_quote == page_quote
            )
        )
        assert timed_observation is not None and page_observation is not None
        assert timed_observation.speaker_id == asr_speaker.id

        timed_evidence = session.get(
            m.EvidenceFragment, timed_observation.evidence_id
        )
        page_evidence = session.get(
            m.EvidenceFragment, page_observation.evidence_id
        )
        assert timed_evidence is not None and page_evidence is not None
        assert timed_evidence.material_file_id == audio.id
        assert (timed_evidence.start_ms, timed_evidence.end_ms) == (
            12_000,
            18_500,
        )
        assert timed_evidence.playback_available is True
        assert audio.safe_name in timed_evidence.locator
        assert page_evidence.material_file_id == pdf.id
        assert page_evidence.page_no == 7
        assert "第7页" in page_evidence.locator

        payload = IntentConfirmRequest(
            version=timed_observation.version,
            attitude_level=4,
        )
        run.status = "已完成"
        run.finished_at = m.utcnow()
        session.flush()

        original_speaker_id = timed_observation.speaker_id
        timed_observation.speaker_id = None
        session.flush()
        with pytest.raises(DomainError, match="缺少可回溯的说话人") as no_speaker:
            confirm_observation(session, timed_observation, payload)
        assert no_speaker.value.status_code == 422
        timed_observation.speaker_id = original_speaker_id

        asr_speaker.status = "待确认"
        session.flush()
        with pytest.raises(DomainError, match="身份仍待确认") as pending_speaker:
            confirm_observation(session, timed_observation, payload)
        assert pending_speaker.value.status_code == 422
        asr_speaker.status = "已确认"

        foreign_package = session.scalar(
            select(m.MeetingPackage).where(
                m.MeetingPackage.id != package_row.id
            )
        )
        assert foreign_package is not None
        foreign_speaker = m.SpeakerCluster(
            package_id=foreign_package.id,
            label="SPEAKER_FOREIGN",
            status="已确认",
        )
        session.add(foreign_speaker)
        session.flush()
        timed_observation.speaker_id = foreign_speaker.id
        with pytest.raises(DomainError, match="未关联本会议") as cross_package_speaker:
            confirm_observation(session, timed_observation, payload)
        assert cross_package_speaker.value.status_code == 422
        timed_observation.speaker_id = original_speaker_id

        run.status = "运行中"
        run.finished_at = None
        session.flush()
        with pytest.raises(DomainError, match="来源分析尚未成功完成") as active_run:
            confirm_observation(session, timed_observation, payload)
        assert active_run.value.status_code == 422
        run.status = "已完成"
        run.cancel_requested_at = m.utcnow()
        run.finished_at = m.utcnow()
        session.flush()
        with pytest.raises(DomainError, match="来源分析尚未成功完成") as cancelled_run:
            confirm_observation(session, timed_observation, payload)
        assert cancelled_run.value.status_code == 422
        run.cancel_requested_at = None

        timed_evidence.material_file_id = None
        session.flush()
        with pytest.raises(DomainError, match="原始材料") as missing_source:
            confirm_observation(session, timed_observation, payload)
        assert missing_source.value.status_code == 422

        timed_evidence.material_file_id = audio.id
        timed_evidence.start_ms = None
        timed_evidence.end_ms = None
        timed_evidence.page_no = None
        timed_evidence.frame_no = None
        timed_evidence.locator = ""
        session.flush()
        with pytest.raises(DomainError, match="时间戳、页码或文本位置") as no_locator:
            confirm_observation(session, timed_observation, payload)
        assert no_locator.value.status_code == 422


def _create_package_with_minutes(client, title: str) -> dict:
    created = client.post(
        "/api/meeting-packages",
        json={
            "customer_id": "customer-a",
            "title": title,
            "meeting_date": "2026-08-01",
            "data_scope": "模拟数据",
        },
    )
    assert created.status_code == 201, created.text
    package = created.json()
    quote = (
        "产品规划负责人表示，Mini LED可以进入样机验证，"
        "但还需要确认成本。"
    )
    uploaded = client.post(
        f"/api/meeting-packages/{package['id']}/files",
        data={"material_type": "人工纪要"},
        files={"file": ("minutes.txt", quote.encode("utf-8"), "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    return package


def test_export_endpoints(tmp_path: Path) -> None:
    client = _client(tmp_path)
    package = _create_package_with_minutes(client, "导出验证")
    analyzed = client.post(
        f"/api/meeting-packages/{package['id']}/analyze",
        json={"adapter": "mock"},
    )
    assert analyzed.status_code == 202, analyzed.text
    wait_for_analysis(
        client,
        package["id"],
        run_id=analyzed.json()["analysis_run"]["id"],
    )

    csv_response = client.get(
        f"/api/meeting-packages/{package['id']}/export?format=csv"
    )
    assert csv_response.status_code == 200
    assert "原始意向观察" in csv_response.text

    md_response = client.get(
        f"/api/meeting-packages/{package['id']}/export?format=md"
    )
    assert md_response.status_code == 200
    assert "正式职能状态" in md_response.text

    xlsx_response = client.get(
        f"/api/meeting-packages/{package['id']}/export?format=xlsx"
    )
    assert xlsx_response.status_code == 200
    assert xlsx_response.headers["content-type"].startswith(
        "application/vnd.openxmlformats"
    )


def test_package_export_excludes_other_meetings_and_denies_viewer(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    first = _create_package_with_minutes(client, "导出范围一")
    second = _create_package_with_minutes(client, "导出范围二")
    first_run = client.post(
        f"/api/meeting-packages/{first['id']}/analyze",
        json={"adapter": "mock"},
    )
    second_run = client.post(
        f"/api/meeting-packages/{second['id']}/analyze",
        json={"adapter": "mock"},
    )
    assert first_run.status_code == second_run.status_code == 202
    wait_for_analysis(client, first["id"], first_run.json()["analysis_run"]["id"])
    wait_for_analysis(client, second["id"], second_run.json()["analysis_run"]["id"])

    database = client.app.state.database
    with database.session() as session:
        first_observation = session.scalar(
            select(m.RawIntentObservation).where(
                m.RawIntentObservation.package_id == first["id"]
            )
        )
        second_observation = session.scalar(
            select(m.RawIntentObservation).where(
                m.RawIntentObservation.package_id == second["id"]
            )
        )
        assert first_observation is not None and second_observation is not None
        customer = session.get(m.MeetingPackage, first["id"]).customer_id
        for observation, marker in (
            (first_observation, "ONLY-FIRST-PACKAGE"),
            (second_observation, "ONLY-SECOND-PACKAGE"),
        ):
            session.add(
                m.FormalIntentStatus(
                    status_key=f"{marker}|{observation.id}",
                    customer_id=customer,
                    technology_id=observation.technology_id,
                    generic_platform_id=observation.generic_platform_id,
                    customer_project_id=observation.customer_project_id,
                    function_id=observation.function_id,
                    attitude_level=observation.suggested_attitude,
                    is_supporter=False,
                    is_resistance=False,
                    conditions=[],
                    bottlenecks=[marker],
                    source_observation_ids=[observation.id],
                    effective_at=session.get(
                        m.MeetingPackage, observation.package_id
                    ).meeting_date,
                    confirmed_by="测试",
                    version_no=1,
                    is_current=True,
                )
            )
        session.commit()

    scoped = client.get(f"/api/meeting-packages/{first['id']}/export?format=csv")
    assert scoped.status_code == 200
    assert "ONLY-FIRST-PACKAGE" in scoped.text
    assert "ONLY-SECOND-PACKAGE" not in scoped.text
    with database.session() as session:
        audit = session.scalar(
            select(m.ConfirmationRecord).where(
                m.ConfirmationRecord.target_type == "meeting_package_export",
                m.ConfirmationRecord.target_id == first["id"],
            )
        )
        assert audit is not None
        assert audit.action == "导出"
        assert audit.after_snapshot["format"] == "csv"

    with database.session() as session:
        admin = session.scalar(select(AppUser).where(AppUser.username == "admin"))
        assert admin is not None
        admin.role = ROLE_VIEWER
        session.commit()
    denied = client.get(f"/api/meeting-packages/{first['id']}/export?format=csv")
    assert denied.status_code == 403


def test_business_queries_only_expose_legacy_or_successful_run_observations(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    package = _create_package_with_minutes(client, "分析运行隔离验证")
    database = client.app.state.database
    observation_ids = {
        "legacy": "obs-visibility-legacy",
        "success": "obs-visibility-success",
        "failed": "obs-visibility-failed",
        "cancelled": "obs-visibility-cancelled",
    }
    speaker_ids = {
        key: f"speaker-visibility-{key}" for key in observation_ids
    }
    markers = {
        key: f"VISIBILITY-{key.upper()}" for key in observation_ids
    }

    with database.session() as session:
        stored_package = session.get(m.MeetingPackage, package["id"])
        assert stored_package is not None
        now = datetime.now(timezone.utc)
        session.add_all(
            [
                m.AnalysisRun(
                    id="run-visibility-success",
                    package_id=package["id"],
                    adapter="mock",
                    model_name="visibility-test",
                    status="已完成",
                    finished_at=now,
                ),
                m.AnalysisRun(
                    id="run-visibility-failed",
                    package_id=package["id"],
                    adapter="mock",
                    model_name="visibility-test",
                    status="处理失败",
                    finished_at=now,
                ),
                m.AnalysisRun(
                    id="run-visibility-cancelled",
                    package_id=package["id"],
                    adapter="mock",
                    model_name="visibility-test",
                    status="已完成",
                    cancel_requested_at=now,
                    finished_at=now,
                ),
            ]
        )
        for index, key in enumerate(observation_ids, start=1):
            session.add(
                m.SpeakerCluster(
                    id=speaker_ids[key],
                    package_id=package["id"],
                    label=f"Speaker {index}",
                    recommended_name=f"可见性测试{index}",
                    recommended_function_id="function-product",
                    confidence=0.9,
                )
            )
        session.flush()
        run_ids = {
            "legacy": None,
            "success": "run-visibility-success",
            "failed": "run-visibility-failed",
            "cancelled": "run-visibility-cancelled",
        }
        for key, observation_id in observation_ids.items():
            session.add(
                m.RawIntentObservation(
                    id=observation_id,
                    package_id=package["id"],
                    analysis_run_id=run_ids[key],
                    customer_id=stored_package.customer_id,
                    technology_id="tech-mini-led-backlight",
                    function_id="function-product",
                    speaker_id=speaker_ids[key],
                    raw_quote=markers[key],
                    suggested_attitude=4,
                    suggested_adoption_stage="内部评估",
                    support_reason=markers[key],
                    bottlenecks=[],
                    decision_influence="高",
                    confidence=0.9,
                    status="AI草稿",
                    model_name="visibility-test",
                )
            )
        session.commit()

    allowed_observation_ids = {
        observation_ids["legacy"],
        observation_ids["success"],
    }
    observations = client.get(
        "/api/intent-observations",
        params={"package_id": package["id"]},
    )
    assert observations.status_code == 200, observations.text
    assert {item["id"] for item in observations.json()["items"]} == (
        allowed_observation_ids
    )
    assert observations.json()["total"] == 2
    assert client.get(
        f"/api/intent-observations/{observation_ids['failed']}"
    ).status_code == 404

    speakers = client.get(
        "/api/speakers", params={"package_id": package["id"]}
    )
    assert speakers.status_code == 200, speakers.text
    assert {item["id"] for item in speakers.json()["items"]} == {
        speaker_ids["legacy"],
        speaker_ids["success"],
    }
    assert client.get(
        f"/api/speakers/{speaker_ids['cancelled']}"
    ).status_code == 404

    package_detail = client.get(f"/api/meeting-packages/{package['id']}")
    assert package_detail.status_code == 200, package_detail.text
    assert package_detail.json()["pending_observation_count"] == 2

    bootstrap = client.get("/api/bootstrap")
    assert bootstrap.status_code == 200, bootstrap.text
    bootstrap_ids = {
        item["id"]
        for item in bootstrap.json()["intents"]
        if item["id"].startswith("obs-visibility-")
    }
    assert bootstrap_ids == allowed_observation_ids
    bootstrap_speaker_ids = {
        item["id"]
        for item in bootstrap.json()["speakers"]
        if item["id"].startswith("speaker-visibility-")
    }
    assert bootstrap_speaker_ids == {
        speaker_ids["legacy"],
        speaker_ids["success"],
    }

    exported = client.get(
        f"/api/meeting-packages/{package['id']}/export?format=csv"
    )
    assert exported.status_code == 200, exported.text
    assert markers["legacy"] in exported.text
    assert markers["success"] in exported.text
    assert markers["failed"] not in exported.text
    assert markers["cancelled"] not in exported.text

    golden = [
        {
            "quote": markers[key],
            "technology_id": "tech-mini-led-backlight",
            "function_id": "function-product",
            "attitude_level": 4,
            "adoption_stage": "内部评估",
        }
        for key in observation_ids
    ]
    evaluation = client.post(
        "/api/eval/runs",
        json={"package_id": package["id"], "golden": golden},
    )
    assert evaluation.status_code == 200, evaluation.text
    report = evaluation.json()["evaluation"]
    assert report["total"] == 4
    assert report["correct"] == 2
    assert report["traceable"] == 2
    assert report["error_count"] == 2


def test_env_check_endpoint(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.get("/api/env-check")
    assert response.status_code == 200
    assert "ffmpeg" in response.json()
    assert "asr" in response.json()


def test_eval_endpoint_reports_accuracy(tmp_path: Path) -> None:
    client = _client(tmp_path)
    package = _create_package_with_minutes(client, "评测验证")
    analyzed = client.post(
        f"/api/meeting-packages/{package['id']}/analyze",
        json={"adapter": "mock"},
    )
    assert analyzed.status_code == 202, analyzed.text
    wait_for_analysis(
        client,
        package["id"],
        run_id=analyzed.json()["analysis_run"]["id"],
    )
    observations = client.get(
        "/api/intent-observations",
        params={"package_id": package["id"]},
    ).json()["items"]
    assert observations
    golden = [
        {
            "quote": item["raw_quote"],
            "technology_id": item["technology"]["id"],
            "function_id": item["function"]["id"] if item["function"] else None,
            "attitude_level": item["suggested_attitude"]["level"],
            "adoption_stage": item.get("suggested_adoption_stage"),
        }
        for item in observations
    ]
    response = client.post(
        "/api/eval/runs",
        json={"package_id": package["id"], "golden": golden},
    )
    assert response.status_code == 200, response.text
    evaluation = response.json()["evaluation"]
    assert evaluation["total"] == len(golden)
    assert evaluation["correct"] == len(golden)
    assert evaluation["structured_accuracy"] == 1.0


def test_speaker_merge_and_split(tmp_path: Path) -> None:
    client = _client(tmp_path)
    package = _create_package_with_minutes(client, "说话人操作验证")
    analyzed = client.post(
        f"/api/meeting-packages/{package['id']}/analyze",
        json={"adapter": "mock"},
    )
    assert analyzed.status_code == 202, analyzed.text
    wait_for_analysis(
        client,
        package["id"],
        run_id=analyzed.json()["analysis_run"]["id"],
    )
    speakers = client.get(
        "/api/speakers",
        params={"package_id": package["id"]},
    ).json()["items"]
    assert len(speakers) >= 3

    keep = speakers[0]
    targets = [item["id"] for item in speakers[1:3]]
    merged = client.patch(
        "/api/speakers/merge",
        json={
            "package_id": package["id"],
            "keep_speaker_id": keep["id"],
            "merge_speaker_ids": targets,
        },
    )
    assert merged.status_code == 200, merged.text
    after_merge = client.get(
        "/api/speakers",
        params={"package_id": package["id"]},
    ).json()["items"]
    assert len(after_merge) == len(speakers) - 2

    kept = next(
        item for item in after_merge if item["id"] == keep["id"]
    )
    split = client.post(
        f"/api/speakers/{kept['id']}/split",
        json={"package_id": package["id"], "segment_ids": []},
    )
    assert split.status_code == 422


def test_analysis_artifacts_endpoint(tmp_path: Path) -> None:
    client = _client(tmp_path)
    package = _create_package_with_minutes(client, "产物查询验证")
    analyzed = client.post(
        f"/api/meeting-packages/{package['id']}/analyze",
        json={"adapter": "mock"},
    )
    assert analyzed.status_code == 202, analyzed.text
    run_id = analyzed.json()["analysis_run"]["id"]
    wait_for_analysis(client, package["id"], run_id=run_id)
    artifacts = client.get(f"/api/analysis-runs/{run_id}/artifacts")
    assert artifacts.status_code == 200
    assert "transcripts" in artifacts.json()
    assert "alignments" in artifacts.json()


def test_evaluation_module_marks_unknown_misjudgment(tmp_path: Path) -> None:
    client = _client(tmp_path)
    package = _create_package_with_minutes(client, "未知评测")
    analyzed = client.post(
        f"/api/meeting-packages/{package['id']}/analyze",
        json={"adapter": "mock"},
    )
    assert analyzed.status_code == 202, analyzed.text
    wait_for_analysis(
        client,
        package["id"],
        run_id=analyzed.json()["analysis_run"]["id"],
    )
    observations = client.get(
        "/api/intent-observations",
        params={"package_id": package["id"]},
    ).json()["items"]
    # Golden says every observation must be unknown; mock always assigns 1-5.
    golden = [
        {
            "quote": item["raw_quote"],
            "technology_id": item["technology"]["id"],
            "function_id": item["function"]["id"] if item["function"] else None,
            "attitude_level": None,
        }
        for item in observations
    ]
    response = client.post(
        "/api/eval/runs",
        json={"package_id": package["id"], "golden": golden},
    )
    evaluation = response.json()["evaluation"]
    assert evaluation["unknown_total"] == len(golden)
    assert evaluation["unknown_misjudged"] == len(golden)
