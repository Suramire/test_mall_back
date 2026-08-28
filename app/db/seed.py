"""数据库 Seed：pf_feature(68项) / 平台角色模板 / 平台超级管理员。

幂等：已存在则跳过。可由 `python -m app.db.seed` 或 alembic 后钩子调用。
注意：依赖已建表（先跑 alembic upgrade head）。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.security import SCOPE_PLATFORM, create_access_token, hash_password
from app.db.seed_data import (
    FEATURES,
    PLATFORM_ROLE_TEMPLATES,
    PLATFORM_SUPER_ADMIN,
    DEMO_TENANTS,
)
from app.db.session import SessionLocal
from app.models.pf_feature import PfFeature
from app.models.pf_role import PfRole
from app.models.pf_staff import PfStaff
from app.models.pf_tenant import PfTenant
from app.models.pf_msg_template import PfMsgTemplate
from app.models.mc_staff import McStaff
from app.models.gd_goods import GdCategory, GdFreightTemplate
from app.models.gd_goods import GdGoods, GdSku
from app.models.mb_member import MbMember
from app.models.od_order import OdOrder, OdOrderItem, OdVerifyCode


def _seed_features(session: Session) -> int:
    count = 0
    for code, end, l1, l2, l3, desc, default_on in FEATURES:
        exists = session.query(PfFeature).filter_by(code=code).first()
        if exists:
            continue
        session.add(PfFeature(
            code=code, end_code=end, l1_name=l1, l2_name=l2, l3_name=l3,
            description=desc, default_on=default_on,
        ))
        count += 1
    return count


def _seed_roles(session: Session) -> int:
    count = 0
    for tpl in PLATFORM_ROLE_TEMPLATES:
        exists = session.query(PfRole).filter_by(name=tpl["name"]).first()
        if exists:
            continue
        session.add(PfRole(
            name=tpl["name"], remark=tpl["remark"],
            perms=tpl["perms"], is_system=tpl["is_system"],
        ))
        count += 1
    return count


def _seed_super_admin(session: Session) -> int:
    exists = session.query(PfStaff).filter_by(account=PLATFORM_SUPER_ADMIN["account"]).first()
    if exists:
        return 0
    role = session.query(PfRole).filter_by(name=PLATFORM_SUPER_ADMIN["role_name"]).first()
    if not role:
        return 0
    session.add(PfStaff(
        account=PLATFORM_SUPER_ADMIN["account"],
        name=PLATFORM_SUPER_ADMIN["name"],
        password_hash=hash_password(PLATFORM_SUPER_ADMIN["password"]),
        phone="",
        role_id=role.id,
        status="ENABLED",
    ))
    return 1

def _seed_demo_tenants(session: Session) -> int:
    n=0
    for data in DEMO_TENANTS:
        existing=session.query(PfTenant).filter_by(tenant_no=data["tenant_no"]).first()
        if existing:
            existing.qualification = "91350100DEMO"; existing.remark="演示租户"; existing.wx_appid="wx_demo_"+data["tenant_no"]
            from datetime import date, datetime
            existing.expire_at=date(2026,12,31); existing.opened_at=datetime(2025,1,1)
            if existing.goods_limit <= 0: existing.goods_limit=1000
            if existing.member_limit <= 0: existing.member_limit=10000
            if existing.store_limit <= 0: existing.store_limit=20
            if existing.staff_limit <= 0: existing.staff_limit=50
            continue
        session.add(PfTenant(**data)); n += 1
    return n
def _seed_templates(session: Session) -> int:
    rows=[("TM0001","订单支付通知","SMS","ORDER_PAID","您的订单已支付成功"),("TM0002","发货提醒","WX","ORDER_SHIPPED","您的包裹已发出")]
    n=0
    for no,name,ch,scene,content in rows:
        if session.query(PfMsgTemplate).filter_by(template_no=no).first(): continue
        session.add(PfMsgTemplate(template_no=no,name=name,channel=ch,scene=scene,variables=["orderNo"],content=content,status="ENABLED")); n+=1
    return n
def _seed_mc_admin(session):
    from app.core.tenant_context import set_tenant
    set_tenant(1)
    if session.query(McStaff).filter_by(tenant_id=1,account='merchant_admin').first(): return 0
    from app.core.security import hash_password
    session.add(McStaff(tenant_id=1,account='merchant_admin',name='商家管理员',password_hash=hash_password('123456'),is_admin=1,status='ENABLED')); return 1
def _seed_catalog(session):
    from app.core.tenant_context import set_tenant
    set_tenant(1)
    if not session.query(GdCategory).filter_by(tenant_id=1).first():
        session.add_all([GdCategory(tenant_id=1,channel='NORMAL',parent_id=0,name='演示分类A'),GdCategory(tenant_id=1,channel='NORMAL',parent_id=0,name='演示分类B')])
    if not session.query(GdFreightTemplate).filter_by(tenant_id=1).first():
        session.add_all([GdFreightTemplate(tenant_id=1,name='默认包邮'),GdFreightTemplate(tenant_id=1,name='按件计费')])
    for f in session.query(GdFreightTemplate).filter_by(tenant_id=1).all():
        if f.name=='默认包邮': f.mode='FREE'; f.first_fee=0
        if f.name=='按件计费': f.mode='COUNT'; f.first_fee=5
    for f in session.query(GdFreightTemplate).filter(GdFreightTemplate.tenant_id==1, GdFreightTemplate.name.like('QA%')).all(): session.delete(f)
    return 1
def _seed_goods_members(session):
    from app.core.tenant_context import set_tenant
    from decimal import Decimal
    from datetime import datetime, timezone
    set_tenant(1)
    if not session.query(GdGoods).filter_by(tenant_id=1).first():
        g=GdGoods(tenant_id=1,name='演示商品',type='PHYSICAL',channel='NORMAL',status='ON_SALE',normal_on_sale=1); session.add(g); session.flush(); session.add(GdSku(tenant_id=1,goods_id=g.id,sku_code='DEMO-001',price=Decimal('9.90')))
    if not session.query(MbMember).filter_by(tenant_id=1).first():
        for i in range(2): session.add(MbMember(tenant_id=1,member_no=f'MBDEMO{i+1}',openid=f'openid{i}',unionid='',nickname=f'演示会员{i+1}',avatar='',phone_enc='',phone_mask='',phone_hash='',gender=0,points_balance=0,points_total_earn=0,points_total_used=0,points_debt=0,total_amount=Decimal('0'),total_order_count=0,joined_at=datetime.now(timezone.utc)))
def _seed_order(session):
    from app.core.tenant_context import set_tenant
    from decimal import Decimal
    from datetime import datetime, timezone, timedelta
    set_tenant(1)
    if session.query(OdOrder).filter_by(tenant_id=1,order_no='ORDDEMO0001').first(): return
    m=session.query(MbMember).filter_by(tenant_id=1).first(); g=session.query(GdGoods).filter_by(tenant_id=1).first(); sku=session.query(GdSku).filter_by(tenant_id=1).first()
    o=OdOrder(tenant_id=1,order_no='ORDDEMO0001',channel='NORMAL',member_id=m.id,member_no=m.member_no,status='PAID',delivery_type='VERIFY',goods_amount=Decimal('9.90'),freight_amount=Decimal('0'),discount_amount=Decimal('0'),pay_amount=Decimal('9.90'),pay_method='WECHAT',receiver_name='',receiver_phone='',receiver_address='')
    session.add(o); session.flush(); it=OdOrderItem(tenant_id=1,order_id=o.id,goods_id=g.id,sku_id=sku.id,channel='NORMAL',goods_name=g.name,goods_type=g.type,spec_text='',image='',price=Decimal('9.90'),quantity=1,subtotal_amount=Decimal('9.90'),subtotal_points=0); session.add(it); session.flush(); session.add(OdVerifyCode(tenant_id=1,order_id=o.id,order_item_id=it.id,member_id=m.id,code='HXDEMO000001',code_type='VERIFY',goods_name=g.name,valid_start=datetime.now(timezone.utc),valid_end=datetime.now(timezone.utc)+timedelta(days=30),status='UNUSED'))


def run_seed() -> dict:
    """执行全部 seed，返回各模块新增数。"""
    with SessionLocal() as session:
        features = _seed_features(session)
        roles = _seed_roles(session)
        admin = _seed_super_admin(session)
        tenants = _seed_demo_tenants(session)
        templates = _seed_templates(session)
        mc_admin = _seed_mc_admin(session)
        catalog = _seed_catalog(session)
        _seed_goods_members(session)
        _seed_order(session)
        session.commit()
    return {"features": features, "roles": roles, "super_admin": admin, "tenants": tenants, "templates": templates, "mc_admin": mc_admin}


if __name__ == "__main__":
    result = run_seed()
    print("Seed 完成:", result)
