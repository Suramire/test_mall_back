"""库存服务：三段式库存锁 + 商家库存调整。

三段式（docs/architecture/02-数据库设计.md §3 `gd_sku_stock` + 04 §2.2 时序图）：
1. 下单预锁   lock_stock    : available -= qty, locked += qty
2. 支付扣减   confirm_lock  : locked -= qty, sold += qty（available 不变）
3. 超时/关单  release_lock  : locked -= qty, available += qty
（退款返还 refund_return    : sold -= qty, available += qty）

恒等式 available = total - locked - sold 由本服务维护（DDL 注释口径）。

并发策略：条件 UPDATE（CAS）防超卖 —— `WHERE available_stock >= qty` 原子推进
两个计数器，rowcount==0 即不足/状态冲突。MySQL 下等价行锁效果，且不依赖
SELECT ... FOR UPDATE（SQLite 测试库无 FOR UPDATE，两方言行为一致）。

事务边界（02 §6 事务边界）：本服务**不 commit**。lock 随订单 INSERT 同事务、
confirm/release 随支付回调/关单任务事务，由调用方统一提交，失败整体回滚。

T-033 预留：lock_stock / confirm_lock / release_lock 的签名已锁定，
订单域传入 items([{sku_id, channel, qty}] 或 StockMoveItem) + ref_id(订单号) 即可。
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.errors import BizCode
from app.core.exceptions import BizError, ConflictError, NotFoundError, ParamError
from app.core.tenant_context import require_tenant_id
from app.models.gd_goods import GdSkuStock, GdStockLog

CHANNELS = ("NORMAL", "POINTS")
#: 商家库存调整三模式（03-API设计.md §3.3 PUT /goods/{id}/stock）
ADJUST_TYPES = ("INCREASE", "DECREASE", "SET")


class StockValueInvalidError(BizError):
    """42007 库存变更值非法。"""

    def __init__(self, message: str = "库存变更值非法") -> None:
        super().__init__(code=BizCode.STOCK_INVALID_VALUE, message=message)


class StockNotEnoughError(BizError):
    """42008 库存不足。"""

    def __init__(self, message: str = "库存不足") -> None:
        super().__init__(code=BizCode.STOCK_NOT_ENOUGH, message=message)


@dataclass(frozen=True)
class StockMoveItem:
    """订单链路库存移动明细（下单锁/支付扣/超时释放共用）。"""

    sku_id: int
    channel: str = "NORMAL"
    qty: int = 1


def _normalize_items(items: list[StockMoveItem | dict]) -> list[StockMoveItem]:
    out: list[StockMoveItem] = []
    for it in items or []:
        if isinstance(it, StockMoveItem):
            sku_id, channel, qty = it.sku_id, it.channel, it.qty
        else:
            sku_id = int(it.get("skuId") or it.get("sku_id") or 0)
            channel = str(it.get("channel") or "NORMAL")
            qty = int(it.get("qty") or it.get("quantity") or 0)
        if sku_id <= 0 or qty <= 0:
            raise ParamError(fields={"items": "skuId/qty 必填且为正"}, message="库存明细非法")
        if channel not in CHANNELS:
            raise ParamError(fields={"channel": "仅支持 NORMAL/POINTS"})
        out.append(StockMoveItem(sku_id=sku_id, channel=channel, qty=qty))
    if not out:
        raise ParamError(message="库存明细不能为空")
    return out


def _get_row(session: Session, sku_id: int, channel: str) -> GdSkuStock | None:
    tid = require_tenant_id()
    return session.scalar(
        select(GdSkuStock).where(
            GdSkuStock.tenant_id == tid,
            GdSkuStock.sku_id == sku_id,
            GdSkuStock.channel == channel,
        )
    )


def ensure_stock_row(
    session: Session,
    goods_id: int,
    sku_id: int,
    channel: str,
    total_stock: int = 0,
    warn_stock: int = 0,
) -> GdSkuStock:
    """取（或创建）SKU×渠道库存行。商品/SKU 创建时落初始库存。"""
    if channel not in CHANNELS:
        raise ParamError(fields={"channel": "仅支持 NORMAL/POINTS"})
    row = _get_row(session, sku_id, channel)
    if row is not None:
        return row
    tid = require_tenant_id()
    row = GdSkuStock(
        tenant_id=tid,
        goods_id=goods_id,
        sku_id=sku_id,
        channel=channel,
        total_stock=max(0, total_stock),
        available_stock=max(0, total_stock),
        warn_stock=max(0, warn_stock),
    )
    session.add(row)
    session.flush()
    return row


def _already_applied(
    session: Session, sku_id: int, channel: str, change_type: str, ref_id: str,
    ref_type: str = "ORDER",
) -> bool:
    """幂等：同一 ref_id(订单号) 同 SKU×渠道同动作已记账则跳过（防回调重放重复扣减）。

    ref_type 默认 ORDER（下单/支付/释放路径）；退款返还路径传 "REFUND"，
    与 _write_log 写入的 ref_type 保持一致，避免查不到自己的记录导致双返库。
    """
    tid = require_tenant_id()
    return (
        session.scalar(
            select(func.count(GdStockLog.id)).where(
                GdStockLog.tenant_id == tid,
                GdStockLog.sku_id == sku_id,
                GdStockLog.channel == channel,
                GdStockLog.change_type == change_type,
                GdStockLog.ref_type == ref_type,
                GdStockLog.ref_id == ref_id,
            )
        )
        or 0
    ) > 0


def _write_log(
    session: Session,
    row: GdSkuStock,
    change_type: str,
    before_avail: int,
    after_avail: int,
    ref_type: str,
    ref_id: str,
    remark: str = "",
    operator_id: int | None = None,
    operator_name: str | None = None,
) -> None:
    """追加库存流水。操作人未显式传入时取当前员工上下文（TenantGuard 已绑定）。"""
    from app.core.tenant_context import get_staff_id, get_staff_name

    session.add(
        GdStockLog(
            tenant_id=require_tenant_id(),
            goods_id=row.goods_id,
            sku_id=row.sku_id,
            channel=row.channel,
            change_type=change_type,
            before_val=before_avail,
            change_val=after_avail - before_avail,
            after_val=after_avail,
            ref_type=ref_type,
            ref_id=ref_id,
            operator_id=operator_id or get_staff_id(),
            operator_name=operator_name or get_staff_name() or "SYSTEM",
            remark=remark,
        )
    )


def _move_cas(
    session: Session,
    row: GdSkuStock,
    *,
    locked_delta: int = 0,
    sold_delta: int = 0,
    available_delta: int = 0,
    total_delta: int = 0,
    guard_column: str | None = None,
    guard_qty: int = 0,
) -> bool:
    """条件 UPDATE 原子推进计数器。guard_column 不足时返回 False（CAS 失败）。"""
    tid = require_tenant_id()
    values: dict = {}
    if locked_delta:
        values["locked_stock"] = GdSkuStock.locked_stock + locked_delta
    if sold_delta:
        values["sold_stock"] = GdSkuStock.sold_stock + sold_delta
    if available_delta:
        values["available_stock"] = GdSkuStock.available_stock + available_delta
    if total_delta:
        values["total_stock"] = GdSkuStock.total_stock + total_delta
    conditions = [GdSkuStock.id == row.id, GdSkuStock.tenant_id == tid]
    if guard_column == "available":
        conditions.append(GdSkuStock.available_stock >= guard_qty)
    elif guard_column == "locked":
        conditions.append(GdSkuStock.locked_stock >= guard_qty)
    elif guard_column == "sold":
        conditions.append(GdSkuStock.sold_stock >= guard_qty)
    res = session.execute(update(GdSkuStock).where(*conditions).values(**values))
    if res.rowcount:
        session.expire(row)  # 让调用方读到最新值
    return bool(res.rowcount)


# ---------------------------------------------------------------------------
# 三段式（T-033 订单链路预留，签名锁定）
# ---------------------------------------------------------------------------
def lock_stock(session: Session, items: list[StockMoveItem | dict], ref_id: str) -> None:
    """第一段·下单预锁：available -= qty, locked += qty。

    任一 SKU 不足 → StockNotEnoughError(42008)，调用方回滚整单事务。
    """
    for it in _normalize_items(items):
        row = _get_row(session, it.sku_id, it.channel)
        if row is None or not _move_cas(
            session, row,
            locked_delta=it.qty, available_delta=-it.qty,
            guard_column="available", guard_qty=it.qty,
        ):
            raise StockNotEnoughError(f"SKU {it.sku_id}({it.channel}) 库存不足")
        # CAS 已生效：row.available_stock 为扣减后值（expire 后自动刷新）
        _write_log(session, row, "ORDER_LOCK",
                   row.available_stock + it.qty, row.available_stock,
                   "ORDER", ref_id, remark="下单预锁")


def confirm_lock(session: Session, items: list[StockMoveItem | dict], ref_id: str) -> None:
    """第二段·支付成功：locked -= qty, sold += qty（available 不变），释放预锁。"""
    for it in _normalize_items(items):
        if _already_applied(session, it.sku_id, it.channel, "ORDER_PAY", ref_id):
            continue  # 幂等：支付回调可能重放
        row = _get_row(session, it.sku_id, it.channel)
        if row is None:
            raise ConflictError(f"SKU {it.sku_id}({it.channel}) 库存行不存在")
        before = row.available_stock
        if not _move_cas(
            session, row,
            locked_delta=-it.qty, sold_delta=it.qty,
            guard_column="locked", guard_qty=it.qty,
        ):
            raise ConflictError(f"SKU {it.sku_id}({it.channel}) 预锁不足，无法确认扣减")
        _write_log(session, row, "ORDER_PAY", before, before, "ORDER", ref_id,
                   remark="支付扣减并释放预锁")


def release_lock(session: Session, items: list[StockMoveItem | dict], ref_id: str) -> None:
    """第三段·超时/关单：locked -= qty, available += qty，归还预锁。"""
    for it in _normalize_items(items):
        if _already_applied(session, it.sku_id, it.channel, "ORDER_RELEASE", ref_id):
            continue  # 幂等：关单任务可能重入
        row = _get_row(session, it.sku_id, it.channel)
        if row is None:
            continue
        before = row.available_stock
        if not _move_cas(
            session, row,
            locked_delta=-it.qty, available_delta=it.qty,
            guard_column="locked", guard_qty=it.qty,
        ):
            raise ConflictError(f"SKU {it.sku_id}({it.channel}) 无可释放的预锁")
        _write_log(session, row, "ORDER_RELEASE", before, before + it.qty,
                   "ORDER", ref_id, remark="超时/关单释放预锁")


def refund_return(session: Session, items: list[StockMoveItem | dict], ref_id: str) -> None:
    """退款通过返还：sold -= qty, available += qty（02 §6 退款通过事务）。"""
    for it in _normalize_items(items):
        if _already_applied(session, it.sku_id, it.channel, "REFUND_RETURN", ref_id,
                            ref_type="REFUND"):
            continue
        row = _get_row(session, it.sku_id, it.channel)
        if row is None:
            continue
        before = row.available_stock
        if not _move_cas(
            session, row,
            sold_delta=-it.qty, available_delta=it.qty,
            guard_column="sold", guard_qty=it.qty,
        ):
            raise ConflictError(f"SKU {it.sku_id}({it.channel}) 已售不足，无法返还")
        _write_log(session, row, "REFUND_RETURN", before, before + it.qty,
                   "REFUND", ref_id, remark="退款库存返还")


# ---------------------------------------------------------------------------
# 商家侧库存调整（PUT /api/mc/goods/{id}/stock，三模式 INCREASE/DECREASE/SET）
# ---------------------------------------------------------------------------
def adjust_stock(
    session: Session,
    goods_id: int,
    items: list[dict],
    operator_id: int | None = None,
    operator_name: str = "SYSTEM",
) -> list[dict]:
    """三模式调整 SKU×渠道库存，返回调整后的行。

    - INCREASE/DECREASE：value 为增量/减量；非法值 42007、余量不足 42008。
    - SET：value 为新的 total_stock；不得低于 locked+sold（42008）。
    恒等式 available = total - locked - sold 每次调整后重新核算。
    """
    results: list[dict] = []
    for raw in items or []:
        sku_id = int(raw.get("skuId") or raw.get("sku_id") or 0)
        channel = str(raw.get("channel") or "NORMAL")
        change_type = str(raw.get("changeType") or raw.get("change_type") or "SET").upper()
        value_raw = raw.get("value")
        if value_raw is None:
            value_raw = raw.get("totalStock", raw.get("stock", raw.get("quantity")))
        if sku_id <= 0 or channel not in CHANNELS or change_type not in ADJUST_TYPES:
            raise StockValueInvalidError("skuId/channel/changeType 非法")
        try:
            value = int(value_raw)
        except (TypeError, ValueError):
            raise StockValueInvalidError("库存变更值必须为整数")
        if change_type in ("INCREASE", "DECREASE") and value <= 0:
            raise StockValueInvalidError("增减量必须为正整数")
        if change_type == "SET" and value < 0:
            raise StockValueInvalidError("设置值不得为负")

        row = _get_row(session, sku_id, channel)
        if row is None:
            if change_type == "DECREASE":
                raise StockNotEnoughError("库存行不存在，无可减少")
            _require_sku_belongs(session, goods_id, sku_id)
            row = ensure_stock_row(session, goods_id, sku_id, channel)
        if row.goods_id != goods_id:
            raise ParamError(fields={"skuId": "SKU 不属于该商品"})

        before_total = row.total_stock or 0
        before_avail = row.available_stock or 0
        locked = row.locked_stock or 0
        sold = row.sold_stock or 0
        if change_type == "INCREASE":
            new_total = before_total + value
        elif change_type == "DECREASE":
            if value > before_avail:
                raise StockNotEnoughError(f"SKU {sku_id}({channel}) 可用库存不足")
            new_total = before_total - value
        else:  # SET
            if value < locked + sold:
                raise StockNotEnoughError("设置值不得低于已占用(锁定+已售)库存")
            new_total = value

        row.total_stock = new_total
        row.available_stock = new_total - locked - sold
        _write_log(
            session, row, change_type, before_avail, row.available_stock,
            "MANUAL", str(raw.get("refId") or ""),
            remark=raw.get("remark") or "商家库存调整",
            operator_id=operator_id, operator_name=operator_name,
        )
        results.append(
            {
                "skuId": sku_id,
                "channel": channel,
                "totalStock": row.total_stock,
                "lockedStock": locked,
                "soldStock": sold,
                "availableStock": row.available_stock,
            }
        )
    return results


def _require_sku_belongs(session: Session, goods_id: int, sku_id: int) -> None:
    """自动建库存行前校验 SKU 真实存在且归属本租户的该商品。

    否则跨租户/跨商品请求会撞 gd_sku_stock 的 (sku_id, channel) 唯一键，
    或给他人 SKU 凭空造行（越权写入面）。
    """
    from sqlalchemy import select

    from app.models.gd_goods import GdSku

    tid = require_tenant_id()
    sku = session.scalar(
        select(GdSku).where(
            GdSku.tenant_id == tid,
            GdSku.id == sku_id,
            GdSku.goods_id == goods_id,
            GdSku.deleted_at.is_(None),
        )
    )
    if sku is None:
        raise NotFoundError("SKU 不存在")


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------
def list_stocks(session: Session, goods_id: int) -> list[GdSkuStock]:
    tid = require_tenant_id()
    return list(
        session.scalars(
            select(GdSkuStock).where(
                GdSkuStock.tenant_id == tid, GdSkuStock.goods_id == goods_id
            )
        ).all()
    )


def sum_available_by_goods(session: Session, goods_ids: list[int]) -> dict[int, int]:
    """列表页库存列：两渠道 available 之和（03-API设计.md §3.3）。"""
    if not goods_ids:
        return {}
    tid = require_tenant_id()
    rows = session.execute(
        select(GdSkuStock.goods_id, func.coalesce(func.sum(GdSkuStock.available_stock), 0))
        .where(
            GdSkuStock.tenant_id == tid,
            GdSkuStock.goods_id.in_(goods_ids),
        )
        .group_by(GdSkuStock.goods_id)
    ).all()
    return {g: int(s) for g, s in rows}
