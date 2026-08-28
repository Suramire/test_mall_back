"""公共文件上传 /api/common/upload。

任何已登录主体（platform/merchant/customer）均可上传；文件落本地
settings.UPLOAD_DIR，持久化 sys_file（平台级表，tenant_id 可空，不注册租户钩子）。
返回的 url 走同路由组下载端点，保证可 GET 访问且不依赖部署层静态挂载。
"""
from __future__ import annotations

import os
import uuid

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import get_auth_payload
from app.core.errors import BizCode
from app.core.exceptions import BizError, NotFoundError
from app.core.response import ok
from app.db.session import get_db
from app.models.sys_common import SysFile
from app.core.security import SCOPE_CUSTOMER, SCOPE_MERCHANT

router = APIRouter(prefix="/upload", tags=["公共-文件上传"])

_ALLOWED_EXTS = frozenset({"jpg", "jpeg", "png", "gif", "webp", "pdf"})
_CHUNK = 1024 * 1024
_EXT_MIMES = {
    "jpg": {"image/jpeg"}, "jpeg": {"image/jpeg"}, "png": {"image/png"},
    "gif": {"image/gif"}, "webp": {"image/webp"}, "pdf": {"application/pdf"},
}


def _safe_ext(filename: str | None) -> str:
    if not filename or "." not in filename:
        raise BizError(BizCode.PARAM_ERROR, "缺少文件扩展名")
    ext = os.path.splitext(filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1])[1][1:].lower()
    if ext not in _ALLOWED_EXTS:
        raise BizError(BizCode.PARAM_ERROR, "不支持的文件类型，仅允许 jpg/jpeg/png/gif/webp/pdf")
    return ext


@router.post("")
async def upload_file(
    file: UploadFile = File(...),
    biz_type: str = Form(""),
    payload: dict = Depends(get_auth_payload),
    db: Session = Depends(get_db),
):
    ext = _safe_ext(file.filename)
    # 不信任客户端伪造 MIME：扩展名与声明的 Content-Type 必须一致。
    if file.content_type and file.content_type.lower() not in _EXT_MIMES[ext]:
        raise BizError(BizCode.PARAM_ERROR, "文件扩展名与内容类型不匹配")

    stored_name = f"{uuid.uuid4().hex}.{ext}"
    upload_dir = settings.UPLOAD_DIR
    os.makedirs(upload_dir, exist_ok=True)
    dest_path = os.path.join(upload_dir, stored_name)

    max_bytes = settings.UPLOAD_MAX_BYTES
    size = 0
    try:
        with open(dest_path, "wb") as out:
            while True:
                chunk = await file.read(_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise BizError(
                        BizCode.PARAM_ERROR,
                        f"文件大小超过限制（最大 {max_bytes // (1024 * 1024)}MB）",
                    )
                out.write(chunk)
    except BizError:
        if os.path.exists(dest_path):
            os.remove(dest_path)
        raise
    finally:
        await file.close()

    tenant_id: int | None = None
    scope = payload.get("scope")
    if scope in (SCOPE_MERCHANT, SCOPE_CUSTOMER):
        tid = payload.get("tid")
        if tid is not None:
            tenant_id = int(tid)

    record = SysFile(
        tenant_id=tenant_id,
        biz_type=(biz_type or "")[:30],
        name=file.filename or stored_name,
        url=f"/api/common/upload/file/{stored_name}",
        size=size,
        mime=file.content_type or "",
        uploader_id=int(payload["sub"]) if payload.get("sub") is not None else None,
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    return ok({
        "id": record.id,
        "url": record.url,
        "name": record.name,
        "size": record.size,
    })


@router.get("/file/{stored_name}")
def download_file(stored_name: str):
    ext = stored_name.rsplit(".", 1)[-1].lower() if "." in stored_name else ""
    if (
        "/" in stored_name
        or "\\" in stored_name
        or ".." in stored_name
        or ext not in _ALLOWED_EXTS
    ):
        raise NotFoundError("文件不存在")
    path = os.path.join(settings.UPLOAD_DIR, stored_name)
    if not os.path.isfile(path):
        raise NotFoundError("文件不存在")
    media = {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "gif": "image/gif",
        "webp": "image/webp",
        "pdf": "application/pdf",
    }[ext]
    return FileResponse(path, media_type=media, filename=stored_name)
