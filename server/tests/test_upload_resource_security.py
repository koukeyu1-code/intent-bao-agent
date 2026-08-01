from __future__ import annotations

import asyncio
import io
import os
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from starlette.datastructures import Headers, UploadFile

from app import models as m
from app import services
from app.main import create_app


@pytest.fixture()
def isolated_upload_context(tmp_path: Path):
    data_dir = tmp_path / "data"
    app = create_app(
        database_url=f"sqlite:///{(tmp_path / 'upload-security.db').as_posix()}",
        data_dir=data_dir,
        seed=True,
    )
    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login",
            json={
                "username": "planner",
                "password": os.environ["INTENT_BAO_USER_PASSWORD"],
            },
        )
        assert login.status_code == 200, login.text
        client.headers.update(
            {"Authorization": f"Bearer {login.json()['access_token']}"}
        )
        response = client.post(
            "/api/meeting-packages",
            json={
                "project_id": "project-mnt",
                "customer_id": "customer-a",
                "title": "上传资源安全隔离测试",
                "meeting_date": "2026-08-01",
                "data_scope": "模拟数据",
                "attendees": [],
            },
        )
        assert response.status_code == 201, response.text
        yield client, app, data_dir, response.json()


def _upload(
    client: TestClient,
    package_id: str,
    *,
    name: str,
    content: bytes,
    material_type: str = "人工纪要",
):
    return client.post(
        f"/api/meeting-packages/{package_id}/files",
        data={"material_type": material_type},
        files={"file": (name, content, "application/octet-stream")},
    )


def _material_count(app, package_id: str) -> int:
    with app.state.database.session() as session:
        return int(
            session.scalar(
                select(func.count())
                .select_from(m.MaterialFile)
                .where(m.MaterialFile.package_id == package_id)
            )
            or 0
        )


def test_material_specific_single_file_limit_rejects_before_write(
    isolated_upload_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app, data_dir, package = isolated_upload_context
    monkeypatch.setitem(services.MATERIAL_FILE_LIMITS_BYTES, "人工纪要", 4)

    response = _upload(
        client,
        package["id"],
        name="minutes.txt",
        content=b"valid text",
    )

    assert response.status_code == 413, response.text
    assert "人工纪要单文件不得超过4字节" in response.json()["detail"]
    assert _material_count(app, package["id"]) == 0
    source_dir = (
        data_dir
        / "projects"
        / package["project_id"]
        / package["id"]
        / "source"
    )
    assert not source_dir.exists()


def test_package_file_count_quota_rejects_additional_upload(
    isolated_upload_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app, _data_dir, package = isolated_upload_context
    monkeypatch.setattr(services, "MIN_FREE_DISK_BYTES", 0)
    monkeypatch.setattr(services, "MAX_FILES_PER_PACKAGE", 1)
    first = _upload(
        client, package["id"], name="first.txt", content=b"first"
    )
    assert first.status_code == 201, first.text

    rejected = _upload(
        client, package["id"], name="second.txt", content=b"second"
    )

    assert rejected.status_code == 413, rejected.text
    assert "最多允许上传1个文件" in rejected.json()["detail"]
    assert _material_count(app, package["id"]) == 1


def test_package_cumulative_byte_quota_rejects_additional_upload(
    isolated_upload_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app, _data_dir, package = isolated_upload_context
    monkeypatch.setattr(services, "MIN_FREE_DISK_BYTES", 0)
    monkeypatch.setattr(services, "MAX_BYTES_PER_PACKAGE", 10)
    first = _upload(
        client, package["id"], name="first.txt", content=b"alpha"
    )
    assert first.status_code == 201, first.text

    rejected = _upload(
        client, package["id"], name="second.txt", content=b"bravo!"
    )

    assert rejected.status_code == 413, rejected.text
    assert "累计容量不得超过10字节" in rejected.json()["detail"]
    assert _material_count(app, package["id"]) == 1


def test_disk_guard_rejects_before_creating_target(
    isolated_upload_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app, data_dir, package = isolated_upload_context
    monkeypatch.setattr(services, "MIN_FREE_DISK_BYTES", 10)
    monkeypatch.setattr(services, "_disk_free_bytes", lambda _path: 5)

    response = _upload(
        client, package["id"], name="minutes.txt", content=b"valid"
    )

    assert response.status_code == 507, response.text
    assert "开始上传前磁盘可用空间不足" in response.json()["detail"]
    assert _material_count(app, package["id"]) == 0
    source_dir = (
        data_dir
        / "projects"
        / package["project_id"]
        / package["id"]
        / "source"
    )
    assert source_dir.is_dir()
    assert not list(source_dir.iterdir())


def test_disk_guard_rechecks_during_stream_and_removes_partial_file(
    isolated_upload_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    _client, app, data_dir, package_data = isolated_upload_context
    monkeypatch.setattr(services, "MIN_FREE_DISK_BYTES", 10)
    free_values = iter((100, 5))
    monkeypatch.setattr(
        services, "_disk_free_bytes", lambda _path: next(free_values)
    )

    upload = UploadFile(
        file=io.BytesIO(b"valid text"),
        size=None,
        filename="streamed.txt",
        headers=Headers({"content-type": "text/plain"}),
    )
    with app.state.database.session() as session:
        package = session.get(m.MeetingPackage, package_data["id"])
        assert package is not None
        with pytest.raises(services.DomainError) as error:
            asyncio.run(
                services.save_uploaded_file(
                    session, app.state.settings, package, upload, "人工纪要"
                )
            )

    assert error.value.status_code == 507
    assert "上传过程中磁盘可用空间不足" in error.value.message
    source_dir = (
        data_dir
        / "projects"
        / package_data["project_id"]
        / package_data["id"]
        / "source"
    )
    assert source_dir.is_dir()
    assert not list(source_dir.iterdir())
    assert _material_count(app, package_data["id"]) == 0


def test_direct_model_text_read_is_byte_bounded_and_avoids_read_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "transcript.txt"
    source.write_bytes("你好世界和后续全文".encode("utf-8"))

    def forbidden_read_bytes(_self: Path) -> bytes:
        raise AssertionError("model text reader must not load the full file")

    monkeypatch.setattr(Path, "read_bytes", forbidden_read_bytes)
    text = services._read_model_text(source, max_text_bytes=7)

    # Seven bytes end in the middle of the third UTF-8 code point; the bounded
    # incremental decoder returns only the two complete characters.
    assert text == "你好"


def _high_compression_docx() -> bytes:
    output = io.BytesIO()
    document = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'
        + ("A" * (2 * 1024 * 1024))
        + "</w:t></w:r></w:p></w:body></w:document>"
    )
    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types '
            'xmlns="http://schemas.openxmlformats.org/package/2006/'
            'content-types"></Types>',
        )
        archive.writestr("word/document.xml", document)
    return output.getvalue()


def test_docx_with_abnormal_compression_ratio_is_rejected_before_parse(
    isolated_upload_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app, _data_dir, package = isolated_upload_context
    monkeypatch.setattr(services, "MIN_FREE_DISK_BYTES", 0)

    response = _upload(
        client,
        package["id"],
        name="compressed-bomb.docx",
        content=_high_compression_docx(),
    )

    assert response.status_code == 422, response.text
    assert "异常压缩比" in response.json()["detail"]
    assert "压缩炸弹" in response.json()["detail"]
    assert _material_count(app, package["id"]) == 0
