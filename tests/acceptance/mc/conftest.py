"""商家管理后台 `/api/mc` 验收夹具。

依据 docs/测试验收标准-P0.md 与 docs/architecture/03-API设计.md。

设计要点：
- 复用根 conftest 的 SQLite 内存 engine / fakeredis / app / client。
- 额外提供两个租户（1001 / 2002）的种子数据，用于跨租户越权验收。
- 所有 token 均由产品代码 create_access_token 真实签发，不手工拼装，
  确保验的是产品的鉴权链路而非测试自造的凭证。
"""
from __future__ import annotations

import uuid

import pytest

from app.core.security import (
    SCOPE_MERCHANT,
    create_access_token,
    hash_password,
)

# 租户 A 固定用 1，因为 mc_auth.login 目前写死 set_tenant(1)（BUG-103），
# 其他租户根本登录不进来。待 BUG-103 修复后可改回任意值。
TENANT_A = 1
TENANT_B = 2002

STAFF_A_ACCOUNT = "shopA"
STAFF_A_PASSWORD = "Secret123"
STAFF_B_ACCOUNT = "shopB"
STAFF_B_PASSWORD = "Secret456"


#: 用例会写入/修改的表，需在每个用例前后清空
_DIRTY_TABLES = (
    "mc_staff", "gd_goods", "gd_sku", "gd_sku_stock", "gd_stock_log",
    "mb_member", "mb_points_log",
    "od_order", "od_order_item", "od_verify_code", "od_cart",
    "mc_store", "mc_shop", "mb_points_rule",
)


def _enable_sqlite_autoincrement() -> None:
    """让 BigInteger 主键在 SQLite 下自增。

    产品用 MySQL：`id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT`。
    SQLite 只对 `INTEGER PRIMARY KEY` 自增，BIGINT 会编译成
    `id BIGINT NOT NULL`，导致所有 INSERT 报 NOT NULL constraint failed。
    这是测试环境方言差异，不是产品缺陷 —— 用 variant 让 SQLite 渲染为
    INTEGER 即可，MySQL 侧 DDL 完全不受影响。
    """
    from sqlalchemy import BigInteger, Integer

    from app.db.base import Base

    for table in Base.metadata.tables.values():
        for col in table.primary_key.columns:
            if not isinstance(col.type, BigInteger):
                continue
            # 部分模型已声明 sqlite variant，但仍映射到 BigInteger（照样不自增），
            # 需要整体换成干净的 BigInteger + Integer variant，不能直接 with_variant
            # （同一方言重复声明会 ArgumentError）。
            col.type = BigInteger().with_variant(Integer(), "sqlite")


# 必须在 import 期执行：根 conftest 的 engine fixture 一旦 create_all 建过表，
# 再改 metadata 就来不及了（全量跑 tests/ 时基座用例会先触发建表）。
_enable_sqlite_autoincrement()


def _truncate(engine) -> None:
    """用裸 SQL 清表（绕过 ORM 租户钩子与软删除，保证真正清空）。"""
    from sqlalchemy import text

    with engine.begin() as conn:
        for table in _DIRTY_TABLES:
            try:
                conn.execute(text(f"DELETE FROM {table}"))
            except Exception:
                # 表不存在（模型尚未交付）时跳过
                pass


from datetime import UTC, datetime, timedelta


def _now_iso(offset_days: int = 0) -> datetime:
    """含当日聚合基线用的 UTC naive now（与 mall.py 的 _day_range 同口径）。

    MySQL DATETIME 按约定存 UTC naive；生产代码通过 UTC 时间窗口聚合，
    测试种子也必须使用 UTC 基准，否则在 Asia/Shanghai 机器上会被算入昨天。
    """
    return datetime.now(UTC).replace(tzinfo=None) + timedelta(days=offset_days)


def _mk_staff(tenant_id: int, sid: int, account: str, password: str):
    from app.models.mc_staff import McStaff

    return McStaff(
        id=sid,
        tenant_id=tenant_id,
        account=account,
        name=f"店主{tenant_id}",
        password_hash=hash_password(password),
        phone="13800001024",
        is_admin=1,
        status="ENABLED",
        pwd_reset_required=0,
    )


@pytest.fixture
def seed_tenants(app, engine):
    """建立两个租户各自的员工/商品/会员/订单，用于跨租户隔离验收。

    直接用 engine 级 session 落库（绕过请求上下文），并按租户切换
    TenantContext，确保 ORM 钩子填充正确的 tenant_id。

    依赖 `app` fixture：业务代码内部用的是 app.db.session.SessionLocal，
    必须先由 app fixture 把它重绑到测试 engine，种子数据才对接口可见。
    """
    from sqlalchemy.orm import sessionmaker

    from app.core.tenant_context import reset, set_tenant
    from app.models.gd_goods import GdGoods, GdSku, GdSkuStock
    from app.models.mb_member import MbMember, MbPointsLog
    from app.models.mc_config import McShop
    from app.models.od_order import (
        OdVerifyCode,
    )

    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    # 每个用例开始前清空相关表：engine 是 session 作用域且用例间会写库
    # （建商品/删商品/调积分），不清空会造成用例互相污染、结果随执行顺序变化。
    _truncate(engine)

    s = Session()
    created: dict[str, object] = {}
    try:
        set_tenant(TENANT_A)
        s.add(_mk_staff(TENANT_A, 10, STAFF_A_ACCOUNT, STAFF_A_PASSWORD))
        s.add(GdGoods(id=101, tenant_id=TENANT_A, name="A租户商品",
                      type="NORMAL", channel="NORMAL", status="ON_SALE",
                      normal_on_sale=1))
        s.add(GdSku(tenant_id=TENANT_A, goods_id=101, sku_code="G101-1",
                    price="12.50", original_price="20.00"))
        s.add(GdSkuStock(tenant_id=TENANT_A, goods_id=101, sku_id=1,
                         channel="NORMAL", total_stock=30, available_stock=30))
        s.add(MbMember(id=201, tenant_id=TENANT_A, member_no="MA0001",
                       nickname="A会员", phone_mask="138****1111",
                       points_balance=100, joined_at=_now_iso(0)))
        s.add(MbPointsLog(tenant_id=TENANT_A, member_id=201, amount=10,
                          balance_after=100, change_type="MANUAL_ADJUST",
                          ref_type="MANUAL", ref_id="seed-log-1",
                          remark="seed", operator_id=10))
        # 订单：2 历史（均为更早，避开"昨日"区间）+ 1 当日（今日支付）
        # 注意：要让 kpi delta 边界"昨日0今日非0 -> 100%"成立，必须保证
        # "昨日"(offset -1 天)无订单，故历史单落在 -3 / -2 天。
        _seed_order(s, TENANT_A, 1001, 201, "PAID", "12.50", _now_iso(-3))
        _seed_order(s, TENANT_A, 1002, 201, "PAID", "12.50", _now_iso(-2))
        _seed_order(s, TENANT_A, 1003, 201, "PAID", "12.50", _now_iso(0))
        # 核销码：1 当日已核销 + 1 未核销
        s.add(OdVerifyCode(tenant_id=TENANT_A, order_id=1001, order_item_id=1,
                           member_id=201, code="HX-USED-A", code_type="VERIFY",
                           goods_name="A租户商品",
                           valid_start=_now_iso(-5), valid_end=_now_iso(5),
                           status="USED", verified_at=_now_iso(0)))
        s.add(OdVerifyCode(tenant_id=TENANT_A, order_id=1003, order_item_id=2,
                           member_id=201, code="HX-UNUSED-A", code_type="VERIFY",
                           goods_name="A租户商品",
                           valid_start=_now_iso(-5), valid_end=_now_iso(5),
                           status="UNUSED"))
        s.add(OdVerifyCode(tenant_id=TENANT_A, order_id=1003, order_item_id=4,
                           member_id=201, code="HX-EXPIRED-A", code_type="VERIFY",
                           goods_name="A租户商品",
                           valid_start=_now_iso(-10), valid_end=_now_iso(-1),
                           status="EXPIRED"))
        s.add(McShop(tenant_id=TENANT_A, name="A演示店铺"))
        s.commit()

        set_tenant(TENANT_B)
        s.add(_mk_staff(TENANT_B, 20, STAFF_B_ACCOUNT, STAFF_B_PASSWORD))
        s.add(GdGoods(id=102, tenant_id=TENANT_B, name="B租户商品",
                      type="NORMAL", channel="NORMAL", status="ON_SALE",
                      normal_on_sale=1))
        s.add(GdSku(tenant_id=TENANT_B, goods_id=102, sku_code="G102-1",
                    price="9.90", original_price="15.00"))
        s.add(GdSkuStock(tenant_id=TENANT_B, goods_id=102, sku_id=2,
                         channel="NORMAL", total_stock=20, available_stock=20))
        s.add(MbMember(id=202, tenant_id=TENANT_B, member_no="MB0001",
                       nickname="B会员", phone_mask="138****2222",
                       points_balance=100, joined_at=_now_iso(-3)))
        s.add(MbPointsLog(tenant_id=TENANT_B, member_id=202, amount=5,
                          balance_after=100, change_type="MANUAL_ADJUST",
                          ref_type="MANUAL", ref_id="seed-log-b",
                          remark="seed", operator_id=20))
        s.add(OdVerifyCode(tenant_id=TENANT_B, order_id=2001, order_item_id=3,
                           member_id=202, code="HX-UNUSED-B", code_type="VERIFY",
                           goods_name="B租户商品",
                           valid_start=_now_iso(-5), valid_end=_now_iso(5),
                           status="UNUSED"))
        s.commit()

        created = {
            "goods_a": 101, "goods_b": 102,
            "member_a": 201, "member_b": 202,
            "staff_a": 10, "staff_b": 20,
            "order_today": 1003, "order_hist1": 1001, "order_hist2": 1002,
        }
        yield created
    finally:
        s.close()
        _truncate(engine)
        reset()


def _seed_order(s, tid: int, oid: int, mid: int, status: str,
                pay_amount: str, created_at):
    """落一条订单 + 明细（供 dashboard 数字真实性断言）。

    注意：订单状态用业务代码实际落库的字面量（PAID/SHIPPED/STOCKED/
    COMPLETED/PENDING_PAY...），不依赖 app/core/enums.py 的枚举名
    —— 两者存在口径分歧（T-P11），按"库里真实存在的值"取。
    """
    from app.models.od_order import OdOrder, OdOrderItem

    o = OdOrder(tenant_id=tid, id=oid, order_no=f"ORDSEED{oid}",
                channel="NORMAL", member_id=mid, member_no=f"M{mid}",
                status=status, delivery_type="EXPRESS",
                goods_amount=pay_amount, pay_amount=pay_amount,
                created_at=created_at, paid_at=created_at)
    s.add(o)
    s.add(OdOrderItem(tenant_id=tid, order_id=oid, goods_id=101, sku_id=1,
                      channel="NORMAL", goods_name="A租户商品",
                      goods_type="NORMAL", spec_text="", price=pay_amount,
                      quantity=1, subtotal_amount=pay_amount))


@pytest.fixture
def created(seed_tenants):
    """种子产物字典（order_today/order_hist1/order_hist2/goods_a 等）。

    业务用例需要具体种子 id 时直接依赖此 fixture，而非每次重新落库。
    """
    return seed_tenants


@pytest.fixture
def token_a() -> str:
    """租户 A 的商家端 token。"""
    return create_access_token(
        subject="10", scope=SCOPE_MERCHANT, tenant_id=TENANT_A,
        perms=["MC_ALL"], features=["goods.manage", "order.view"],
    )


@pytest.fixture
def token_b() -> str:
    """租户 B 的商家端 token。"""
    return create_access_token(
        subject="20", scope=SCOPE_MERCHANT, tenant_id=TENANT_B,
        perms=["MC_ALL"], features=["goods.manage", "order.view"],
    )


@pytest.fixture
def headers_a(token_a) -> dict[str, str]:
    return {"Authorization": f"Bearer {token_a}"}


@pytest.fixture
def headers_b(token_b) -> dict[str, str]:
    return {"Authorization": f"Bearer {token_b}"}


@pytest.fixture
def expired_token() -> str:
    """已过期的商家 token（用于 40101 验收）。

    create_access_token 不支持自定义有效期，故用同一套 JWT_SECRET/算法
    直接签一枚 exp 已过去的 token —— 与产品签发格式完全一致。
    """
    from datetime import datetime, timedelta

    import jwt

    from app.core.config import settings

    past = datetime.now(UTC) - timedelta(hours=1)
    return jwt.encode(
        {
            "sub": "10",
            "scope": SCOPE_MERCHANT,
            "tid": TENANT_A,
            "jti": uuid.uuid4().hex,
            "iat": int((past - timedelta(hours=1)).timestamp()),
            "exp": int(past.timestamp()),
        },
        settings.JWT_SECRET,
        algorithm=settings.JWT_ALGORITHM,
    )


@pytest.fixture
def platform_headers(platform_token) -> dict[str, str]:
    """平台端 token 访问商家端（应 40301）。"""
    return {"Authorization": f"Bearer {platform_token}"}


@pytest.fixture
def idem_key() -> str:
    return f"idem-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# 契约断言助手
# ---------------------------------------------------------------------------
def assert_envelope(resp):
    """取出响应体并校验 code/message/data 三件套。

    说明：`traceId` 的完整校验单独由 TestEnvelope 里的专项用例负责
    （见 BUG-101/BUG-102）。此处不把 traceId 当作前置门槛，否则一个
    横切缺陷会把所有业务用例一起染红，掩盖真正的业务问题。
    """
    body = resp.json()
    for field in ("code", "message", "data"):
        assert field in body, f"响应体缺少 {field}：{body}"
    return body


def assert_http200(resp):
    """契约：HTTP 恒 200，业务错误靠 code 区分（03-API设计.md §9.2）。"""
    assert resp.status_code == 200, (
        f"契约要求 HTTP 恒 200（业务错误靠 code 区分），实际 {resp.status_code}: {resp.text[:300]}"
    )
    return resp


def assert_trace_id(resp, expected: str | None = None):
    """契约：响应体必须透出非空 traceId，传入 X-Trace-Id 时须一致。"""
    body = resp.json()
    assert "traceId" in body, f"响应体缺少 traceId：{body}"
    assert body["traceId"], f"traceId 必须非空，实际 {body['traceId']!r}"
    if expected is not None:
        assert body["traceId"] == expected, (
            f"traceId 未透传：请求 {expected}，响应 {body['traceId']}"
        )
    return body


def assert_code(resp, expected: int):
    body = assert_envelope(resp)
    assert body["code"] == expected, (
        f"期望 code={expected}，实际 code={body['code']} message={body.get('message')}"
    )
    return body
