"""商家端 P1 契约补齐（03-API设计.md P1/P0 缺口）。

原则：只补「缺口端点」，已存在实现（msg-config/points-export/member-orders 等）
直接复用或仅增强（channels 校验/分页），不与 mall.py 现有路由重复注册。
鉴权统一走 merchant_ctx（SCOPE_MERCHANT + 员工权限回查）。
"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from app.api import mall as _mall
from app.core.response import ok, page

router = APIRouter(tags=["商家端-P1"])


def _csv_response(rows: list[list], headers: list[str]) -> StreamingResponse:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(headers)
    w.writerows(rows)
    return StreamingResponse(iter(["\ufeff" + out.getvalue()]),
                             media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=export.csv"})


# ---------- 订单导出 ----------
@router.post("/order/export")
def order_export(request: Request, status: str | None = None, channel: str | None = None):
    tid, _ = _mall.merchant_ctx(request)
    with _mall.SessionLocal() as s:
        q = s.query(_mall.OdOrder).filter(_mall.OdOrder.tenant_id == tid)
        if status:
            q = q.filter(_mall.OdOrder.status == status)
        if channel:
            q = q.filter(_mall.OdOrder.channel == channel)
        rows = []
        for o in q.order_by(_mall.OdOrder.id.desc()).all():
            rows.append([o.order_no, o.status, o.channel, o.receiver_name or "",
                         str(o.pay_amount), o.created_at.isoformat()])
        return _csv_response(rows, ["订单号", "状态", "渠道", "收货人", "实付", "下单时间"])


# ---------- 会员导出 ----------
@router.post("/member/export")
def member_export(request: Request):
    tid, _ = _mall.merchant_ctx(request)
    with _mall.SessionLocal() as s:
        rows = []
        for m in s.query(_mall.MbMember).filter(_mall.MbMember.tenant_id == tid).all():
            rows.append([m.member_no, m.nickname or "", m.phone_mask or "",
                         m.points_balance, str(m.total_amount), m.total_order_count])
        return _csv_response(rows, ["会员号", "昵称", "手机号", "积分", "消费额", "订单数"])


# ---------- 支付流水 ----------
@router.get("/payment")
def payment_list(request: Request, channel: str | None = None,
                 status: str | None = None,
                 pageNo: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100)):
    tid, _ = _mall.merchant_ctx(request)
    with _mall.SessionLocal() as s:
        q = s.query(_mall.OdPayment).filter(_mall.OdPayment.tenant_id == tid)
        if channel:
            q = q.filter(_mall.OdPayment.channel == channel)
        if status:
            q = q.filter(_mall.OdPayment.status == status)
        total = q.count()
        rows = q.order_by(_mall.OdPayment.id.desc()).offset((pageNo - 1) * size).limit(size).all()
        return page([{
            "id": p.id, "orderId": p.order_id, "transactionId": p.transaction_id,
            "payMethod": p.pay_method, "amount": str(p.amount), "points": p.points,
            "status": p.status, "paidAt": p.paid_at.isoformat() if p.paid_at else None,
        } for p in rows], total, pageNo, size)


@router.post("/payment/export")
def payment_export(request: Request, channel: str | None = None):
    tid, _ = _mall.merchant_ctx(request)
    with _mall.SessionLocal() as s:
        q = s.query(_mall.OdPayment).filter(_mall.OdPayment.tenant_id == tid)
        if channel:
            q = q.filter(_mall.OdPayment.channel == channel)
        rows = []
        for p in q.order_by(_mall.OdPayment.id.desc()).all():
            rows.append([str(p.order_id), p.transaction_id or "", p.pay_method, p.channel,
                         str(p.amount), p.points, p.status,
                         p.paid_at.isoformat() if p.paid_at else ""])
        return _csv_response(rows, ["订单号", "微信单号", "支付方式", "渠道", "金额", "积分", "状态", "支付时间"])


# ---------- 订单关闭（仅 PENDING_PAY → CLOSED，释放锁定库存）----------
@router.post("/order/{order_id}/close")
def order_close(order_id: int, request: Request):
    from app.services.inventory import release_lock

    tid, operator = _mall.merchant_ctx(request)
    with _mall.SessionLocal() as s:
        staff_store = _mall._staff_store_scope(s, tid, operator)
        o = s.query(_mall.OdOrder).filter_by(id=order_id, tenant_id=tid).first()
        if not o:
            from app.core.exceptions import NotFoundError
            raise NotFoundError("订单不存在")
        _mall._ensure_order_store_scope(s, o, staff_store)
        if o.status != "PENDING_PAY":
            from app.core.errors import BizCode
            from app.core.exceptions import BizError
            raise BizError(BizCode.ORDER_STATUS_INVALID, "仅待付款订单可关闭")
        items = s.query(_mall.OdOrderItem).filter_by(order_id=o.id, tenant_id=tid).all()
        for it in items:
            try:
                release_lock(s, items=[{"sku_id": it.sku_id, "channel": it.channel,
                                       "qty": it.quantity}], ref_id=str(o.id))
            except Exception:
                s.rollback()
                raise
        o.status = "CLOSED"
        s.commit()
        return ok({"id": o.id, "orderNo": o.order_no, "status": o.status})


# ---------- 支付配置（McPayConfig 每租户一行，注册租户模型）----------
_MASK_KEYS = ("wx_mch_id", "wx_api_key_enc", "sp_mch_id", "sub_mch_id", "sub_appid")


def _mask(v: str | None) -> str | None:
    if not v:
        return None
    v = str(v)
    return "****" + v[-4:] if len(v) > 4 else "****"


@router.get("/pay-config")
def pay_config_get(request: Request):
    from app.core.tenant_context import set_tenant
    from app.models.mc_config import McPayConfig

    tid, _ = _mall.merchant_ctx(request)
    set_tenant(tid)
    with _mall.SessionLocal() as s:
        cfg = s.query(McPayConfig).filter_by(tenant_id=tid).first()
        if not cfg:
            cfg = McPayConfig(tenant_id=tid, pay_mode="DIRECT", enabled=1)
            s.add(cfg)
            s.commit()
        return ok({
            "payMode": cfg.pay_mode,
            "wxMchId": _mask(cfg.wx_mch_id),
            "wxApiKey": _mask(cfg.wx_api_key_enc),
            "wxCertSerial": _mask(cfg.wx_cert_serial),
            "spMchId": _mask(cfg.sp_mch_id),
            "subMchId": _mask(cfg.sub_mch_id),
            "subAppid": _mask(cfg.sub_appid),
            "notifyUrl": cfg.notify_url,
            "enabled": cfg.enabled,
        })


@router.put("/pay-config")
def pay_config_put(payload: dict, request: Request):
    from app.core.errors import BizCode
    from app.core.exceptions import BizError
    from app.core.tenant_context import reset, set_tenant
    from app.models.mc_config import McPayConfig

    tid, _ = _mall.merchant_ctx(request)
    # 代客态（平台 impersonating）禁止写支付配置
    auth = _mall.get_auth_payload(request)
    if auth.get("impersonating"):
        raise BizError(BizCode.FORBIDDEN, "代客操作禁止修改支付配置")

    pay_mode = str(payload.get("payMode") or payload.get("pay_mode") or "DIRECT").upper()
    if pay_mode not in ("DIRECT", "PARTNER"):
        raise BizError(BizCode.PARAM_ERROR, "payMode 仅支持 DIRECT/PARTNER")
    if pay_mode == "PARTNER":
        for k in ("spMchId", "subMchId", "subAppid"):
            if not (payload.get(k) or payload.get(k.lower())):
                raise BizError(BizCode.PARAM_ERROR, f"PARTNER 模式必填 {k}")

    set_tenant(tid)
    try:
        with _mall.SessionLocal() as s:
            cfg = s.query(McPayConfig).filter_by(tenant_id=tid).first()
            if not cfg:
                cfg = McPayConfig(tenant_id=tid, pay_mode="DIRECT", enabled=1)
                s.add(cfg)
            cfg.pay_mode = pay_mode
            if "wxMchId" in payload:
                cfg.wx_mch_id = str(payload["wxMchId"])
            if "wxApiKey" in payload:
                cfg.wx_api_key_enc = str(payload["wxApiKey"])
            if "spMchId" in payload:
                cfg.sp_mch_id = str(payload["spMchId"])
            if "subMchId" in payload:
                cfg.sub_mch_id = str(payload["subMchId"])
            if "subAppid" in payload:
                cfg.sub_appid = str(payload["subAppid"])
            if "notifyUrl" in payload:
                cfg.notify_url = str(payload["notifyUrl"])
            if "enabled" in payload:
                cfg.enabled = 1 if payload["enabled"] else 0
            s.commit()
            return ok({"payMode": cfg.pay_mode, "enabled": cfg.enabled})
    finally:
        reset()


# ---------- 消息配置 ----------
# 注：mall.py 已实现 GET/PUT /msg-config、/message（兼容别名）。
# channels 校验（WX_SUBSCRIBE/INTERNAL）为 P1 增强项，已直接在
# mall.py merchant_msg_config_update 中补充（见 mall.py 改动），此处不重复注册。


# ---------- 会员订单/积分明细分页（既有 mc 实现已满足，无需重复注册）----------
# 注：mall.py merchant_router 已实现 GET /member/{member_id}/orders 与
# /member/{member_id}/points-log（非分页 list 形态，前端契约已定）。
# P1 分页形态如确需，应改 mall.py 原端点而非重复注册（FastAPI 先注册者生效）。
