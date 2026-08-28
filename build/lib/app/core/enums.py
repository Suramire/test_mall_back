"""全局枚举/状态码常量（存 code 字符串）。值域对齐 PRD §公共.6 与数据库 DDL。"""
from __future__ import annotations


class TenantStatus:
    NORMAL = "NORMAL"
    TRIAL = "TRIAL"
    EXPIRED = "EXPIRED"
    DISABLED = "DISABLED"

    @classmethod
    def valid_values(cls) -> tuple[str, ...]:
        return (cls.NORMAL, cls.TRIAL, cls.EXPIRED, cls.DISABLED)


class OrderStatus:
    PENDING_PAYMENT = "PENDING_PAYMENT"
    PAID = "PAID"
    PENDING_SHIP = "PENDING_SHIP"
    PENDING_RECEIVE = "PENDING_RECEIVE"
    PENDING_PICKUP = "PENDING_PICKUP"
    PICKED_UP = "PICKED_UP"
    COMPLETED = "COMPLETED"
    CLOSED = "CLOSED"
    REFUNDING = "REFUNDING"
    REFUNDED = "REFUNDED"


class GoodsType:
    PHYSICAL = "PHYSICAL"
    VIRTUAL = "VIRTUAL"
    TICKET = "TICKET"


class GoodsChannel:
    NORMAL = "NORMAL"
    POINTS = "POINTS"
    BOTH = "BOTH"


class GoodsStatus:
    DRAFT = "DRAFT"
    ON_SALE = "ON_SALE"
    OFF_SALE = "OFF_SALE"


class OrderChannel:
    NORMAL = "NORMAL"
    POINTS = "POINTS"


class DeliveryType:
    EXPRESS = "EXPRESS"
    PICKUP = "PICKUP"
    VERIFY = "VERIFY"
    INSTANT = "INSTANT"
