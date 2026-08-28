"""健康检查接口。"""
from __future__ import annotations

from fastapi import APIRouter

from app.core.redis import ping as redis_ping
from app.core.response import ok

router = APIRouter()


@router.get("/health")
async def health():
    return ok({"service": "up", "redis": redis_ping()})


@router.get("/dict")
async def common_dict():
    """前端枚举字典的真实后端出口（不是前端 mock）。"""
    return ok({
        "tenantStatus": [{"code": x, "name": n} for x, n in (
            ("NORMAL", "正常"), ("TRIAL", "试用中"), ("EXPIRED", "已到期"), ("DISABLED", "已禁用"))],
        "goodsChannel": [{"code": x, "name": n} for x, n in (("NORMAL", "普通商城"), ("POINTS", "积分商城"), ("BOTH", "双渠道"))],
        "orderStatus": [{"code": x, "name": n} for x, n in (("PENDING_PAY", "待付款"), ("PAID", "已支付"), ("SHIPPED", "已发货"), ("COMPLETED", "已完成"), ("CLOSED", "已关闭"))],
    })


@router.get("/region")
async def common_region(parentCode: str | None = None):
    """开发/演示环境行政区数据；通过统一 API 返回，调用端不写死。"""
    rows = [
        {"code": "350000", "name": "福建省", "parentCode": "0", "level": 1},
        {"code": "350100", "name": "福州市", "parentCode": "350000", "level": 2},
        {"code": "350102", "name": "鼓楼区", "parentCode": "350100", "level": 3},
    ]
    return ok([row for row in rows if parentCode is None or row["parentCode"] == parentCode])
