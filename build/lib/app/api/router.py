"""API 路由聚合。按端前缀分组：pf / mc / mp / c / common。"""
from __future__ import annotations

from fastapi import APIRouter

from app.api.c_points import router as c_points_router
from app.api.common import health, upload
from app.api.mall import customer_router, merchant_mp_router, merchant_router, shop_router
from app.api.mc.goods import router as mc_goods_router
from app.api.mc_auth import router as mc_auth_router
from app.api.mc_catalog import router as mc_catalog_router
from app.api.mc_p1 import router as mc_p1_router
from app.api.pf import pf_router

api_router = APIRouter()

api_router.include_router(health.router, prefix="/common", tags=["公共-健康检查"])
api_router.include_router(upload.router, prefix="/common")
api_router.include_router(pf_router, prefix="/pf", tags=["平台端"])

# ★端隔离（勿回退为"三前缀共挂同一个 router"）
# merchant_router 是商家管理面（改库存/发货/调积分/会员管理等），只允许出现在 /mc。
# shop_router 是用户侧共用面（商品浏览/购物车/下单/我的订单），三端都需要。
# 曾经三前缀共挂单一 router，导致 /c/goods/{id}/stock、/c/points/adjust
# 这类管理路径在用户端前缀下真实可路由，仅靠 handler 内 merchant_ctx() 兜底。
#
# T-032：mc 商品域已迁至 app/api/mc/goods.py（services/goods|sku|inventory 承载）。
# 必须排在 mall 的 merchant_router / shop_router 之前挂载 —— FastAPI 按注册顺序
# 匹配路由，mc_goods_router 的 /goods、/goods/{id} 在 /mc 前缀下优先生效；
# shop_router 的同名 GET 仅继续服务 /mp、/c 的用户侧商品浏览。
api_router.include_router(mc_goods_router, prefix="/mc")
api_router.include_router(mc_p1_router, prefix="/mc", tags=["商家端-P1"])
# mc_catalog_router（分类/运费模板 CRUD，商家专用）必须先于 shop_router(/mc) 挂载，
# 否则 /mc/category 被用户侧 shop_category 抢占返回 40301「仅用户端可访问」。
api_router.include_router(mc_catalog_router, prefix="/mc")
api_router.include_router(merchant_router, prefix="/mc", tags=["商家端"])
api_router.include_router(shop_router, prefix="/mc", tags=["商家端"])
api_router.include_router(mc_auth_router, prefix="/mc")
# ★商家小程序端：merchant_mp_router 必须先于 shop_router(/mp)
# 挂载，否则 /api/mp/order、/api/mp/order/{id} 等会被 shop_router 的用户侧
# 兼容端点（orders_compat/customer_order_detail，ctx 校验）抢先匹配。
# 商家小程序的管理能力（订单/会员/商品/积分）统一走 /api/mc/*，
# /mp 前缀仅暴露小程序专用端点（verify/workbench/me/notice 等）。
api_router.include_router(merchant_mp_router, prefix="/mp", tags=["商家小程序"])
api_router.include_router(shop_router, prefix="/mp", tags=["商家小程序"])
api_router.include_router(shop_router, prefix="/c", tags=["用户端"])
api_router.include_router(c_points_router, prefix="/c", tags=["用户端-积分商城/搜索"])
api_router.include_router(customer_router, prefix="/mp")
api_router.include_router(customer_router, prefix="/c")
