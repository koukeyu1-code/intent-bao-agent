from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import models as m
from app.main import create_app


@pytest.fixture()
def isolated_upload_app(tmp_path: Path):
    data_dir = tmp_path / "data"
    app = create_app(
        database_url=f"sqlite:///{(tmp_path / 'preflight.db').as_posix()}",
        data_dir=data_dir,
        seed=True,
    )
    with TestClient(app) as client:
        package_response = client.post(
            "/api/meeting-packages",
            json={
                "project_id": "project-mnt",
                "customer_id": "customer-a",
                "title": "文件首层预检隔离测试",
                "meeting_date": "2026-07-31",
                "data_scope": "模拟数据",
                "attendees": [],
            },
        )
        assert package_response.status_code == 201, package_response.text
        yield client, app, data_dir, package_response.json()


def _upload(
    client: TestClient,
    package_id: str,
    *,
    name: str,
    content: bytes,
    material_type: str,
    content_type: str = "application/octet-stream",
):
    return client.post(
        f"/api/meeting-packages/{package_id}/files",
        data={"material_type": material_type},
        files={"file": (name, content, content_type)},
    )


def _minimal_docx(*, include_content_types: bool = True) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        if include_content_types:
            archive.writestr(
                "[Content_Types].xml",
                '<?xml version="1.0"?><Types '
                'xmlns="http://schemas.openxmlformats.org/package/2006/'
                'content-types"></Types>',
            )
        archive.writestr("word/document.xml", "<document/>")
    return output.getvalue()


@pytest.mark.parametrize(
    ("name", "content", "material_type"),
    [
        ("empty.txt", b"", "人工纪要"),
        ("blank.txt", b"  \r\n\t", "人工纪要"),
        ("binary.txt", b"\x00\x01\x02", "人工纪要"),
        ("fake.pdf", b"not a pdf", "会议资料"),
        ("broken.docx", b"not a zip", "人工纪要"),
        (
            "missing-content-types.docx",
            _minimal_docx(include_content_types=False),
            "人工纪要",
        ),
        ("fake.mp4", b"not an mp4", "录屏"),
        ("fake.mp3", b"not an mp3", "录音"),
        ("fake.wav", b"not a wav", "录音"),
        ("fake.m4a", b"not an m4a", "录音"),
    ],
)
def test_invalid_content_is_rejected_without_file_or_database_record(
    isolated_upload_app,
    name: str,
    content: bytes,
    material_type: str,
) -> None:
    client, app, data_dir, package = isolated_upload_app

    response = _upload(
        client,
        package["id"],
        name=name,
        content=content,
        material_type=material_type,
    )

    assert response.status_code == 422, response.text
    assert "文件内容预检失败" in response.json()["detail"]
    with app.state.database.session() as session:
        count = session.scalar(
            select(func.count())
            .select_from(m.MaterialFile)
            .where(m.MaterialFile.package_id == package["id"])
        )
        assert count == 0
    source_dir = (
        data_dir
        / "projects"
        / package["project_id"]
        / package["id"]
        / "source"
    )
    assert not source_dir.exists() or not list(source_dir.iterdir())


@pytest.mark.parametrize(
    ("name", "content", "material_type", "content_type"),
    [
        (
            "minutes.txt",
            "有效的会议纪要。".encode(),
            "人工纪要",
            "text/plain",
        ),
        (
            "slides.pdf",
            b"%PDF-1.4\n% first-layer signature only\n%%EOF\n",
            "会议资料",
            "application/pdf",
        ),
        (
            "minutes.docx",
            _minimal_docx(),
            "人工纪要",
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document",
        ),
        (
            "screen.mp4",
            b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isom",
            "录屏",
            "video/mp4",
        ),
        ("audio.mp3", b"ID3\x04\x00\x00\x00\x00\x00\x00", "录音", "audio/mpeg"),
        (
            "audio.wav",
            b"RIFF\x04\x00\x00\x00WAVEfmt ",
            "录音",
            "audio/wav",
        ),
        (
            "audio.m4a",
            b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00M4A ",
            "录音",
            "audio/mp4",
        ),
    ],
)
def test_recognized_first_layer_content_is_marked_preflight_passed(
    isolated_upload_app,
    name: str,
    content: bytes,
    material_type: str,
    content_type: str,
) -> None:
    client, _app, _data_dir, package = isolated_upload_app

    response = _upload(
        client,
        package["id"],
        name=name,
        content=content,
        material_type=material_type,
        content_type=content_type,
    )

    assert response.status_code == 201, response.text
    assert response.json()["file"]["parse_status"] == "预检通过"
