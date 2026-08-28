"""商城/商家管理 P0 路由契约测试（不依赖外部数据库）。"""
from app.main import app


def test_mall_and_merchant_routes_registered():
    """商家管理端点走契约路径（03-API设计.md §订单/会员）。

    /api/mc/merchant/** 一批别名已下线（前端 merchantOpsApi 零引用），
    统一收敛到 /api/mc/order、/member、/verify。
    """
    paths = set(app.openapi()["paths"])
    expected = {
        "/api/mc/goods", "/api/mc/orders",
        "/api/mc/order", "/api/mc/order/status-counts",
        "/api/mc/order/{order_id}", "/api/mc/order/{order_id}/ship",
        "/api/mc/member", "/api/mc/member/{member_id}",
        "/api/mc/points/adjust", "/api/mc/verify/log",
    }
    assert expected <= paths


def test_merchant_alias_routes_removed():
    """已下线的 /api/mc/merchant/** 别名不得回归。"""
    paths = set(app.openapi()["paths"])
    removed = {
        "/api/mc/merchant/orders",
        "/api/mc/merchant/orders/{order_id}/ship",
        "/api/mc/merchant/verify-records",
        "/api/mc/merchant/members",
        "/api/mc/merchant/members/{member_id}/points",
        "/api/mc/order/{order_id}/detail",
        "/api/mc/member/{member_id}/detail",
    }
    assert not (removed & paths)


def test_merchant_admin_endpoints_not_exposed_on_customer_prefixes():
    """★端隔离回归：商家管理端点不得出现在 /api/c 与 /api/mp 前缀下。

    历史问题：mall_router 同时挂 /mc、/mp、/c，使 /api/c/points/adjust、
    /api/c/goods/{id}/stock 等管理路径真实可路由。
    """
    paths = set(app.openapi()["paths"])
    # /mp 是商家小程序端，按 API 设计允许其暴露核销记录；其余管理能力
    # 仍不得因为 router 复用而泄漏给用户端。
    admin_suffixes = [
        "/points/adjust", "/goods/{goods_id}/stock", "/order/{order_id}/ship",
        "/member", "/verify/log", "/store", "/dashboard/kpi",
    ]
    leaked = [
        f"/api/{p}{s}"
        for p in ("c", "mp")
        for s in admin_suffixes
        if f"/api/{p}{s}" in paths and not (p == "mp" and s == "/verify/log")
    ]
    assert not leaked, f"商家管理端点泄漏到用户端前缀: {leaked}"


def test_shop_endpoints_available_on_all_prefixes():
    """用户侧端点（商品浏览/购物车/下单）三端都要在。"""
    paths = set(app.openapi()["paths"])
    for p in ("mc", "c", "mp"):
        assert f"/api/{p}/goods" in paths
        assert f"/api/{p}/cart" in paths
        assert f"/api/{p}/orders" in paths


def test_platform_tenant_crud_routes_registered():
    paths = set(app.openapi()["paths"])
    assert {"/api/pf/merchant", "/api/pf/merchant/{tenant_id}",
            "/api/pf/merchant/{tenant_id}/enable",
            "/api/pf/merchant/{tenant_id}/disable"} <= paths

def test_platform_role_permission_routes_registered():
    paths = set(app.openapi()["paths"])
    assert "/api/pf/role/{role_id}/perms" in paths
    assert "/api/pf/staff/{staff_id}/toggle-status" in paths
