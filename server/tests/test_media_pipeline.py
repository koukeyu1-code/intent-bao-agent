from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import wait_for_analysis

from app.evaluation import run_evaluation
from app.audio_adapters import LlamaCppAsrAdapter
from app.main import create_app
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
