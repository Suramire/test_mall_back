"""统一分页查询参数与校验。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class PageQuery(BaseModel):
    page: int = Field(default=1, ge=1, description="页码，从1开始")
    size: int = Field(default=20, ge=1, le=100, description="每页条数，≤100")
    sortBy: str | None = Field(default=None, description="排序字段")
    sortOrder: str = Field(default="desc", pattern="^(asc|desc)$", description="asc/desc")

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.size
