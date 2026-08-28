"""平台域模型：pf_msg_template（消息模板库）与 pf_sequence（编号序列）。"""
from __future__ import annotations

from sqlalchemy import JSON, BigInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TimestampMixin, SoftDeleteMixin


class PfMsgTemplate(Base, IdMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "pf_msg_template"

    template_no: Mapped[str] = mapped_column(String(20), nullable=False, comment="TM0001")
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    channel: Mapped[str] = mapped_column(String(20), nullable=False, comment="MsgChannel")
    wx_template_id: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    scene: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    variables: Mapped[list | None] = mapped_column(JSON, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ENABLED")

    __table_args__ = (
        {"comment": "平台消息模板库"},
    )


class PfSequence(Base, IdMixin, TimestampMixin):
    __tablename__ = "pf_sequence"

    seq_key: Mapped[str] = mapped_column(String(80), nullable=False, comment="tenant / t{tid}:member / ...")
    current_val: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    __table_args__ = (
        {"comment": "编号序列(Redis降级兜底)"},
    )
