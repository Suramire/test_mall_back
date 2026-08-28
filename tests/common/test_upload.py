"""公共文件上传接口验收（真实 API + 真实持久化，不走 mock）。"""
from __future__ import annotations

import io
import os

from sqlalchemy import select

from app.core import errors
from app.core.config import settings
from app.models.sys_common import SysFile
from tests.conftest import assert_biz_code


def _enable_sqlite_autoincrement() -> None:
    from sqlalchemy import BigInteger, Integer

    from app.db.base import Base

    for table in Base.metadata.tables.values():
        for col in table.primary_key.columns:
            if not isinstance(col.type, BigInteger):
                continue
            col.type = BigInteger().with_variant(Integer(), "sqlite")


_enable_sqlite_autoincrement()

PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-image-body"


def _patch_upload_dir(monkeypatch, tmp_path) -> str:
    d = str(tmp_path / "uploads")
    monkeypatch.setattr(settings, "UPLOAD_DIR", d)
    return d


def _merchant_headers(merchant_token) -> dict[str, str]:
    return {"Authorization": f"Bearer {merchant_token}"}


class TestUploadAuth:
    def test_upload_requires_login(self, client, monkeypatch, tmp_path):
        """未登录上传 -> 业务码 40100，且不落盘不落库。"""
        _patch_upload_dir(monkeypatch, tmp_path)
        resp = client.post(
            "/api/common/upload",
            files={"file": ("a.png", io.BytesIO(PNG_BYTES), "image/png")},
        )
        assert_biz_code(resp, errors.BizCode.UNAUTHORIZED)

    def test_platform_user_can_upload(self, client, monkeypatch, tmp_path, db_session, auth_headers):
        """任何已登录角色均可上传；平台端记录 tenant_id 为空。"""
        _patch_upload_dir(monkeypatch, tmp_path)
        resp = client.post(
            "/api/common/upload",
            headers=auth_headers,
            files={"file": ("pf.png", io.BytesIO(PNG_BYTES), "image/png")},
        )
        body = assert_biz_code(resp, errors.BizCode.OK)
        row = db_session.get(SysFile, body["data"]["id"])
        assert row is not None
        assert row.uploader_id == 1
        assert row.tenant_id is None


class TestUploadValidation:
    def test_rejects_mismatched_content_type(self, client, merchant_token, monkeypatch, tmp_path):
        _patch_upload_dir(monkeypatch, tmp_path)
        resp = client.post("/api/common/upload", headers=_merchant_headers(merchant_token),
                           files={"file": ("fake.png", io.BytesIO(PNG_BYTES), "text/plain")})
        assert_biz_code(resp, errors.BizCode.PARAM_ERROR)

    def test_rejects_disallowed_type(self, client, merchant_token, monkeypatch, tmp_path, db_session):
        """非白名单扩展名 -> 40001，不落盘不落库。"""
        upload_dir = _patch_upload_dir(monkeypatch, tmp_path)
        total_before = len(db_session.scalars(select(SysFile)).all())
        resp = client.post(
            "/api/common/upload",
            headers=_merchant_headers(merchant_token),
            files={"file": ("evil.txt", io.BytesIO(b"hello"), "text/plain")},
        )
        assert_biz_code(resp, errors.BizCode.PARAM_ERROR)
        assert not os.path.exists(upload_dir) or os.listdir(upload_dir) == []
        assert len(db_session.scalars(select(SysFile)).all()) == total_before

    def test_rejects_missing_extension(self, client, merchant_token, monkeypatch, tmp_path):
        _patch_upload_dir(monkeypatch, tmp_path)
        resp = client.post(
            "/api/common/upload",
            headers=_merchant_headers(merchant_token),
            files={"file": ("noext", io.BytesIO(b"x"), "application/octet-stream")},
        )
        assert_biz_code(resp, errors.BizCode.PARAM_ERROR)

    def test_rejects_oversize(self, client, merchant_token, monkeypatch, tmp_path, db_session):
        """超过 UPLOAD_MAX_BYTES -> 40001，半成品文件被清理。"""
        upload_dir = _patch_upload_dir(monkeypatch, tmp_path)
        monkeypatch.setattr(settings, "UPLOAD_MAX_BYTES", 8)
        resp = client.post(
            "/api/common/upload",
            headers=_merchant_headers(merchant_token),
            files={"file": ("big.png", io.BytesIO(PNG_BYTES), "image/png")},
        )
        assert_biz_code(resp, errors.BizCode.PARAM_ERROR)
        assert os.listdir(upload_dir) == []


class TestUploadSuccess:
    def test_success_persists_sysfile(self, client, merchant_token, monkeypatch, tmp_path, db_session):
        """成功上传 -> 落盘 uuid 文件名 + sys_file 行存在（uploader/tenant 正确）。"""
        upload_dir = _patch_upload_dir(monkeypatch, tmp_path)
        resp = client.post(
            "/api/common/upload",
            headers=_merchant_headers(merchant_token),
            files={"file": ("hello.png", io.BytesIO(PNG_BYTES), "image/png")},
        )
        body = assert_biz_code(resp, errors.BizCode.OK)
        data = body["data"]
        assert isinstance(data["id"], int)
        assert data["name"] == "hello.png"
        assert data["size"] == len(PNG_BYTES)
        assert data["url"].startswith("/api/common/upload/file/")
        assert data["url"].endswith(".png")

        stored = os.listdir(upload_dir)
        assert len(stored) == 1 and stored[0].endswith(".png") and stored[0] != "hello.png"

        row = db_session.get(SysFile, data["id"])
        assert row is not None
        assert row.uploader_id == 10
        assert row.tenant_id == 1001
        assert row.name == "hello.png"
        assert row.size == len(PNG_BYTES)
        assert row.url == data["url"]

    def test_returned_url_is_gettable(self, client, merchant_token, monkeypatch, tmp_path):
        """返回的 url 可直接 GET 且内容一致（无需登录）。"""
        _patch_upload_dir(monkeypatch, tmp_path)
        resp = client.post(
            "/api/common/upload",
            headers=_merchant_headers(merchant_token),
            files={"file": ("pic.webp", io.BytesIO(PNG_BYTES), "image/webp")},
        )
        body = assert_biz_code(resp, errors.BizCode.OK)
        got = client.get(body["data"]["url"])
        assert got.status_code == 200
        assert got.content == PNG_BYTES


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
