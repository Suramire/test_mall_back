"""统一响应体 {code, message, data, traceId} 与分页封装。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from app.core.errors import BizCode

T = TypeVar("T")


class Response(BaseModel, Generic[T]):
    code: int = BizCode.OK
    message: str = "success"
    data: T | None = None
    traceId: str = ""


class PageOut(BaseModel, Generic[T]):
    """统一分页协议：{list,total,page,size,pages,hasMore}。"""

    list: list[T]
    total: int
    page: int
    size: int
    pages: int
    hasMore: bool


def ok(data: Any = None, message: str = "success") -> dict:
    return {"code": BizCode.OK, "message": message, "data": data}


def err(code: int, message: str, data: Any = None) -> dict:
    return {"code": code, "message": message, "data": data}


def page(list_: list[T], total: int, page: int, size: int) -> dict:
    pages = (total + size - 1) // size if size > 0 else 0
    return {
        "code": BizCode.OK,
        "message": "success",
        "data": {
            "list": list_,
            "total": total,
            "page": page,
            "size": size,
            "pages": pages,
            "hasMore": page * size < total,
        },
    }
