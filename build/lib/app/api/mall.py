"""商城 P0 垂直切片：商品、购物车、订单及核销。"""
import csv
import hashlib
import io
import secrets
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from app.core.config import settings
from app.core.crypto_secret import decrypt_secret
from app.core.deps import get_auth_payload
from app.core.errors import BizCode
from app.core.exceptions import BizError, NotFoundError, ParamError
from app.core.id_generator import next_member_no
from app.core.response import err, ok, page
from app.core.security import SCOPE_CUSTOMER, SCOPE_MERCHANT, create_access_token, hash_password
from app.db.session import SessionLocal
from app.models.gd_goods import GdGoods, GdSku, GdSkuStock
from app.models.mb_member import MbLevel, MbMember, MbPointsImport, MbPointsLog, MbPointsRule
from app.models.mc_config import McMsgConfig, McRole, McShop, McStore
from app.models.mc_staff import McStaff
from app.models.od_order import (
    OdAddress,
    OdCart,
    OdOrder,
    OdOrderItem,
    OdPayment,
    OdRefund,
    OdVerifyCode,
)
from app.models.sys_common import SysFile
from app.services import inventory
from app.integrations.wechat import WechatCode2SessionError, code2session

# ★端隔离：拆两个 router，避免商家管理端点被挂到用户端前缀下。
#   merchant_router —— 商家管理专属，仅挂 /mc（走 merchant_ctx，校验 SCOPE_MERCHANT）
#   shop_router     —— 用户侧共用，挂 /mc + /mp + /c（走 ctx，不校验 scope）
# 归类依据：端点用的是 merchant_ctx() 还是 ctx()。
# 历史问题：此前单一 router 同时挂三前缀，导致 /c/goods/{id}/stock、
#   /c/points/adjust 等商家管理路径在用户端前缀下真实可路由。
merchant_router = APIRouter(tags=["商城-P0-商家管理"])
shop_router = APIRouter(tags=["商城-P0-用户侧"])
customer_router = APIRouter(tags=["商城-P0-用户认证"])
merchant_mp_router = APIRouter(tags=["商家小程序"])

@customer_router.post('/auth/login')
def customer_login(payload: dict):
    """用户小程序开发登录：手机号在所属租户内建/取真实会员，禁止信任 memberId。"""
    phone = str(payload.get("phone") or "").strip()
    if not phone or len(phone) > 20:
        return err(BizCode.PARAM_ERROR, "请输入有效手机号")
    # 租户归属只能由受控 AppID 反查。当前本地小程序未注入 AppID 时，
    # 仅允许固定开发租户 1；绝不接受客户端传来的 tenantId 作为授权依据。
    appid = str(payload.get("appid") or payload.get("appId") or "").strip()
    from app.core.tenant_context import reset, set_tenant
    try:
        with SessionLocal() as s:
            from app.models.pf_tenant import PfTenant
            if appid:
                tenant = s.query(PfTenant).filter_by(wx_appid=appid).first()
            else:
                tenant = s.get(PfTenant, 1)
            tenant_id = int(tenant.id) if tenant else 0
            if not tenant or tenant.status in ("DISABLED", "EXPIRED"):
                return err(BizCode.UNAUTHORIZED, "租户不可用")
            set_tenant(tenant_id)
            phone_hash = hashlib.sha256(phone.encode()).hexdigest()
            member = s.query(MbMember).filter_by(tenant_id=tenant_id, phone_hash=phone_hash).first()
            if not member:
                used = s.query(MbMember).filter_by(tenant_id=tenant_id).count()
                if tenant.member_limit and used >= tenant.member_limit:
                    return err(BizCode.TENANT_QUOTA_MEMBER, "会员配额已满")
                member = MbMember(tenant_id=tenant_id, member_no=next_member_no(s, tenant_id),
                    nickname=f"用户{phone[-4:]}", phone_enc=phone,
                    phone_mask=f"{phone[:3]}****{phone[-4:]}", phone_hash=phone_hash,
                    joined_at=datetime.now(UTC).replace(tzinfo=None))
                s.add(member); s.flush(); s.commit()
            token=create_access_token(subject=str(member.id),scope=SCOPE_CUSTOMER,tenant_id=tenant_id,perms=[])
            return ok({'accessToken':token,'refreshToken':token,'expiresIn':7200,
                       'member':{'id':member.id,'memberNo':member.member_no},
                       'tenant':{'id':tenant_id,'name':tenant.name,'tenantNo':tenant.tenant_no}})
    finally:
        reset()


@customer_router.post('/auth/wx-login')
def customer_wx_login(payload: dict):
    """用户小程序微信登录：AppID 决定租户，code2session 决定会员身份。

    不接受 tenantId、openid 等客户端声称的归属或身份字段，避免串租户登录。
    """
    appid = str(payload.get("appid") or payload.get("appId") or "").strip()
    code = str(payload.get("code") or "").strip()
    if not appid or not code:
        return err(BizCode.PARAM_ERROR, "参数校验失败", {"fields": {
            key: "Field required" for key, value in {"appid": appid, "code": code}.items() if not value
        }})

    from app.core.tenant_context import reset, set_tenant
    from app.models.pf_tenant import PfTenant

    try:
        with SessionLocal() as s:
            tenant = s.query(PfTenant).filter_by(wx_appid=appid).first()
            if not tenant:
                return err(BizCode.UNAUTHORIZED, "小程序 AppID 未绑定租户")
            if tenant.status == "DISABLED":
                return err(BizCode.TENANT_DISABLED, "租户已被禁用")
            if tenant.status == "EXPIRED" or (tenant.expire_at and tenant.expire_at < date.today()):
                return err(BizCode.TENANT_EXPIRED, "租户服务已到期")

            try:
                identity = code2session(appid=tenant.wx_appid, secret=decrypt_secret(tenant.wx_secret_enc), code=code)
            except WechatCode2SessionError as exc:
                return err(BizCode.UNAUTHORIZED, str(exc))

            tenant_id = int(tenant.id)
            set_tenant(tenant_id)
            openid = identity["openid"]
            member = s.query(MbMember).filter_by(tenant_id=tenant_id, openid=openid).first()
            if not member:
                used = s.query(MbMember).filter_by(tenant_id=tenant_id).count()
                if tenant.member_limit and used >= tenant.member_limit:
                    return err(BizCode.TENANT_QUOTA_MEMBER, "会员配额已满")
                member = MbMember(
                    tenant_id=tenant_id,
                    member_no=next_member_no(s, tenant_id),
                    openid=openid,
                    unionid=identity["unionid"],
                    nickname="微信用户",
                    source="ONLINE",
                    joined_at=datetime.now(UTC).replace(tzinfo=None),
                )
                s.add(member)
                s.flush()
                s.commit()
            token = create_access_token(subject=str(member.id), scope=SCOPE_CUSTOMER, tenant_id=tenant_id, perms=[])
            return ok({"accessToken": token, "refreshToken": token, "expiresIn": 7200,
                       "member": {"id": member.id, "memberNo": member.member_no},
                       "tenant": {"id": tenant_id, "name": tenant.name, "tenantNo": tenant.tenant_no}})
    finally:
        reset()

@merchant_mp_router.get('/verify/query')
def mp_verify_query(code: str, request: Request):
    return verify_query(code, request)

@merchant_mp_router.post('/verify')
def mp_verify(req: "VerifyIn", request: Request):
    # 商家小程序的门店归属后续由员工门店字段解析；当前模型未有 store_id，
    # 仍由同一商家核销服务执行，且强制 merchant scope。
    return verify(req, request)

@merchant_mp_router.get('/verify/log')
def mp_verify_log(request: Request):
    result = verify_log(request, pageNo=1, size=20)
    return ok(result.get('data', {}).get('list', []))

@merchant_mp_router.get('/verify/records')
def mp_verify_records(request: Request):
    result = verify_log(request, pageNo=1, size=20)
    return ok(result.get('data', {}).get('list', []))

@merchant_mp_router.post('/auth/merchant-login')
def mp_merchant_login(payload: dict):
    """商家小程序登录。路径避开 /api/mp/auth/login（该路径属用户端契约），
    复用 mc_auth 的账密校验与 token 签发，token 体系与 /api/mc 完全一致。"""
    from app.api.mc_auth import Login as McAuthLogin
    from app.api.mc_auth import login as mc_login
    account = str(payload.get('account') or '').strip()
    password = str(payload.get('password') or '')
    if not account or not password:
        return err(BizCode.PARAM_ERROR, '账号或密码不能为空')
    return mc_login(McAuthLogin(account=account, password=password))

@merchant_mp_router.get('/me/profile')
def mp_me_profile(request: Request):
    _tid, _ = merchant_ctx(request)
    from app.api.mc_auth import me as mc_me
    return mc_me(request)

@merchant_mp_router.get('/workbench/kpi')
def mp_workbench_kpi(request: Request):
    return mc_kpi(request)

@merchant_mp_router.get('/workbench/todo')
def mp_workbench_todo(request: Request):
    return mc_todo(request)

@merchant_mp_router.get('/points/records')
def mp_points_records(request: Request, memberId: int | None = None, pageNo: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100)):
    return points_log(request, memberId=memberId, pageNo=pageNo, size=size)


@merchant_mp_router.get('/notice')
def mp_notice_list(request: Request, pageNo: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100)):
    """运营通知列表（STAFF 定向或全员广播），未读数优先。"""
    from app.models.mc_config import McNotice
    tid, operator = merchant_ctx(request)
    with SessionLocal() as s:
        base = s.query(McNotice).filter(McNotice.tenant_id == tid, McNotice.receiver_type == "STAFF")
        q = base.filter((McNotice.receiver_id == operator) | (McNotice.receiver_id.is_(None)))
        total = q.count()
        rows = q.order_by(McNotice.is_read.asc(), McNotice.id.desc()).offset((pageNo - 1) * size).limit(size).all()
        unread = base.filter(McNotice.is_read == 0).filter(
            (McNotice.receiver_id == operator) | (McNotice.receiver_id.is_(None))).count()
        data = page([{
            "id": n.id, "type": n.type, "title": n.title, "content": n.content,
            "link": n.link, "isRead": n.is_read,
            "readAt": n.read_at.isoformat() if n.read_at else None,
            "createdAt": n.created_at.isoformat(),
        } for n in rows], total, pageNo, size)
        data["data"]["unread"] = unread
        return data


@merchant_mp_router.post('/notice/read-all')
def mp_notice_read_all(request: Request):
    from datetime import UTC, datetime

    from app.models.mc_config import McNotice
    tid, operator = merchant_ctx(request)
    with SessionLocal() as s:
        q = s.query(McNotice).filter(McNotice.tenant_id == tid, McNotice.receiver_type == "STAFF",
                                     McNotice.is_read == 0)
        q = q.filter((McNotice.receiver_id == operator) | (McNotice.receiver_id.is_(None)))
        now = datetime.now(UTC).replace(tzinfo=None)
        rows = q.all()
        for n in rows:
            n.is_read = 1
            n.read_at = now
        s.commit()
        return ok({"updated": len(rows)})

# 兼容别名：本模块内既有装饰器大量使用 @router，默认指向商家管理 router。
router = merchant_router

def _day_range(offset_days: int = 0):
    """返回 [当日00:00, 次日00:00) 的半开区间。offset_days=-1 即昨天。"""
    base = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=offset_days)
    start = base.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def _delta_pct(today_val, yest_val) -> int:
    """环比百分比（整数）。昨日为 0 时：今日有量记 100%，否则 0%。"""
    t, y = float(today_val or 0), float(yest_val or 0)
    if y == 0: return 100 if t > 0 else 0
    return round((t - y) / y * 100)


# 计入销售额的订单状态。★注意与 app/core/enums.py OrderStatus 存在口径分歧：
# 枚举定义为 PENDING_PAYMENT/PENDING_SHIP/PENDING_RECEIVE/PICKED_UP，
# 而本文件历史写入的是字面量 PAID/SHIPPED/STOCKED/COMPLETED/PENDING_PAY。
# 此处按"库中实际存在的值"取并集，避免统计漏算；待状态机统一后收敛。
_SALES_STATUSES = ("PAID","SHIPPED","STOCKED","COMPLETED","PENDING_SHIP","PENDING_RECEIVE","PICKED_UP")


@router.get("/dashboard/kpi")
def mc_kpi(request: Request):
    tid,_=merchant_ctx(request)
    from sqlalchemy import func
    t_start,t_end=_day_range(0); y_start,y_end=_day_range(-1)
    with SessionLocal() as s:
        def orders_cnt(a,b):
            return s.query(func.count(OdOrder.id)).filter(OdOrder.tenant_id==tid,OdOrder.created_at>=a,OdOrder.created_at<b).scalar() or 0
        def sales_sum(a,b):
            return s.query(func.coalesce(func.sum(OdOrder.pay_amount),0)).filter(OdOrder.tenant_id==tid,OdOrder.status.in_(_SALES_STATUSES),OdOrder.created_at>=a,OdOrder.created_at<b).scalar() or 0
        def members_cnt(a,b):
            return s.query(func.count(MbMember.id)).filter(MbMember.tenant_id==tid,MbMember.joined_at>=a,MbMember.joined_at<b).scalar() or 0
        def verify_cnt(a,b):
            return s.query(func.count(OdVerifyCode.id)).filter(OdVerifyCode.tenant_id==tid,OdVerifyCode.status=="USED",OdVerifyCode.verified_at>=a,OdVerifyCode.verified_at<b).scalar() or 0

        to,yo=orders_cnt(t_start,t_end),orders_cnt(y_start,y_end)
        ts,ys=sales_sum(t_start,t_end),sales_sum(y_start,y_end)
        tm,ym=members_cnt(t_start,t_end),members_cnt(y_start,y_end)
        tv,yv=verify_cnt(t_start,t_end),verify_cnt(y_start,y_end)
        return ok({
            "todayOrders":{"value":to,"delta":_delta_pct(to,yo)},
            "todaySales":{"value":str(ts),"delta":_delta_pct(ts,ys)},
            "newMembers":{"value":tm,"delta":_delta_pct(tm,ym)},
            "todayVerify":{"value":tv,"delta":_delta_pct(tv,yv)},
        })

@router.get("/dashboard/recent-orders")
def mc_recent(request: Request):
    tid,_=merchant_ctx(request)
    with SessionLocal() as s:
        rows=s.query(OdOrder).filter_by(tenant_id=tid).order_by(OdOrder.id.desc()).limit(10).all(); return ok([{"orderNo":o.order_no,"status":o.status,"buyerName":o.member_no,"amount":str(o.pay_amount),"payAmount":str(o.pay_amount),"createdAt":o.created_at.isoformat()} for o in rows])

@router.get("/dashboard/todo")
def mc_todo(request: Request):
    """待办计数。字段对齐 shared-types McTodo：pendingShip/pendingRefund/pendingVerify/pendingPickup。

    此前返回的是 {pendingPay, pendingShip}，与前端类型不符（pendingPay 前端根本不读，
    而 pendingRefund/pendingVerify/pendingPickup 三项永远 undefined）。
    """
    tid,_=merchant_ctx(request)
    from sqlalchemy import func
    with SessionLocal() as s:
        def cnt(*statuses):
            return s.query(func.count(OdOrder.id)).filter(OdOrder.tenant_id==tid,OdOrder.status.in_(statuses)).scalar() or 0
        pending_verify=s.query(func.count(OdVerifyCode.id)).filter(OdVerifyCode.tenant_id==tid,OdVerifyCode.status=="UNUSED").scalar() or 0
        return ok({
            "pendingShip":cnt("PAID","PENDING_SHIP"),
            "pendingRefund":cnt("REFUNDING"),
            "pendingVerify":pending_verify,
            "pendingPickup":cnt("STOCKED","PENDING_PICKUP"),
        })

@router.get("/dashboard/trend")
def mc_trend(request: Request, days: int = 30):
    if days not in (7, 30, 90):
        raise ParamError(fields={'days': '仅支持7/30/90'})
    """销售趋势：按天聚合近 N 天的销售额与单量，返回 [{date,sales,orders}]。

    无数据的日期补 0，保证前端折线图 X 轴连续。
    """
    tid,_=merchant_ctx(request)
    from sqlalchemy import func
    days=max(1,min(days,90))
    start,_end=_day_range(-(days-1)); _s,end=_day_range(0)
    with SessionLocal() as s:
        day=func.date(OdOrder.created_at)
        rows=s.query(day.label("d"),func.coalesce(func.sum(OdOrder.pay_amount),0),func.count(OdOrder.id)).filter(
            OdOrder.tenant_id==tid,OdOrder.status.in_(_SALES_STATUSES),
            OdOrder.created_at>=start,OdOrder.created_at<end,
        ).group_by(day).all()
    bucket={str(d):(sales,cnt) for d,sales,cnt in rows}
    out=[]
    for i in range(days):
        d=(start+timedelta(days=i)).date()
        sales,cnt=bucket.get(str(d),(0,0))
        out.append({"date":d.isoformat(),"sales":float(sales or 0),"orders":int(cnt or 0)})
    return ok(out)

@router.get('/dashboard/goods-rank')
def goods_rank(request: Request, limit: int = Query(10, ge=1, le=100)):
    tid, _ = merchant_ctx(request)
    with SessionLocal() as s:
        rows = s.query(OdOrderItem.goods_id, OdOrderItem.goods_name, OdOrderItem.quantity).filter_by(tenant_id=tid).all()
        totals = {}
        for gid, name, qty in rows:
            key = gid; totals.setdefault(key, {'goodsId': gid, 'goodsName': name, 'sales': 0}); totals[key]['sales'] += qty or 0
        return ok(sorted(totals.values(), key=lambda x: x['sales'], reverse=True)[:limit])

@router.get('/dashboard/member-rank')
def member_rank(request: Request, limit: int = Query(10, ge=1, le=100)):
    tid, _ = merchant_ctx(request)
    with SessionLocal() as s:
        rows = s.query(MbMember).filter_by(tenant_id=tid).order_by(MbMember.total_amount.desc()).limit(limit).all()
        return ok([{'memberId': x.id, 'memberNo': x.member_no, 'nickname': x.nickname, 'totalAmount': str(x.total_amount or 0), 'orderCount': x.total_order_count} for x in rows])

def ctx(request: Request):
    """用户小程序资源上下文。用户商品/购物车/订单不得接受商家 Token。"""
    p = get_auth_payload(request)
    if p.get("scope") != SCOPE_CUSTOMER:
        raise HTTPException(403, "仅用户端可访问")
    return int(p.get("tid") or 0), int(p.get("sub") or 0)
def merchant_ctx(request: Request):
    p=get_auth_payload(request)
    if p.get("scope") != SCOPE_MERCHANT: raise HTTPException(403,"仅商家端可访问")
    # 角色权限在请求时回查，避免角色变更后旧 JWT 继续持有旧权限。
    if "MC_ALL" not in (p.get("perms") or []):
        tid, staff_id = int(p.get("tid") or 0), int(p.get("sub") or 0)
        path = request.url.path
        required = None
        if "/goods" in path and request.method == "POST": required = "GOODS_CREATE"
        elif "/goods" in path and request.method == "PUT": required = "GOODS_EDIT"
        elif "/goods" in path and request.method == "DELETE": required = "GOODS_DELETE"
        elif "/staff" in path: required = "SET_USER"
        elif "/role" in path: required = "SET_ROLE"
        elif "/points/adjust" in path: required = "POINTS_ADJUST"
        elif "/verify" in path and request.method == "POST": required = "VERIFY_DO"
        elif "/store" in path: required = "SET_STORE"
        if required:
            with SessionLocal() as check:
                staff = check.query(McStaff).filter_by(id=staff_id, tenant_id=tid).first()
                role = check.query(McRole).filter_by(id=staff.role_id, tenant_id=tid).first() if staff and staff.role_id else None
                if not role or required not in (role.perms or []):
                    raise HTTPException(403, "无权限执行此操作")
    return int(p.get("tid") or 0), int(p.get("sub") or 0)


def _staff_store_scope(s, tid: int, staff_id: int) -> int | None:
    """员工门店数据范围：NULL=总部看全量；非空=仅本门店（设计 U9）。

    返回员工 store_id（或 None 表示总部全量）。员工不存在/已禁用时返回 None，
    调用方据此视为无门店归属（不自动放行）。
    """
    staff = s.query(McStaff).filter_by(id=staff_id, tenant_id=tid).first()
    if not staff or staff.status != "ENABLED":
        return None
    return staff.store_id


def _ensure_order_store_scope(s, o, staff_store_id: int | None) -> None:
    """订单门店归属校验：非总部员工仅可操作本门店订单（设计 U9）。

    判定依据：PICKUP/VERIFY 订单按其核销门店（od_order.store_id）归属；
    订单未绑定门店则仅总店可操作。总部（staff_store_id=None）不受限。
    """
    if staff_store_id is None:
        return
    order_store = getattr(o, "store_id", None)
    if order_store is None:
        raise HTTPException(403, "非总部员工仅可操作本门店订单")
    if order_store != staff_store_id:
        raise HTTPException(403, "员工不属于该门店，无权操作此订单")

class CartIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    goods_id: int = Field(validation_alias=AliasChoices("goods_id", "goodsId")); sku_id: int = Field(validation_alias=AliasChoices("sku_id", "skuId")); quantity: int = Field(ge=1, le=999)
class CartUpdateIn(BaseModel):
    quantity: int = Field(ge=1, le=999)
class OrderIn(BaseModel):
    sku_id: int; quantity: int = Field(ge=1, le=999); delivery_type: str = "EXPRESS"
    receiver_name: str = ""; receiver_phone: str = ""; receiver_address: str = ""

def _validate_order_item(session, tenant_id: int, sku_id: int, quantity: int):
    """订单预览/创建共享商品、租户、上架及库存校验，并返回真实定价快照。"""
    if quantity < 1:
        raise ParamError({'quantity': '数量必须大于0'})
    sku=session.query(GdSku).filter_by(id=sku_id, tenant_id=tenant_id, deleted_at=None).first()
    if not sku: raise BizError(BizCode.ORDER_SKU_INVALID, 'SKU不存在')
    goods=session.query(GdGoods).filter_by(id=sku.goods_id, tenant_id=tenant_id, deleted_at=None).first()
    if not goods or goods.status != 'ON_SALE' or not goods.normal_on_sale:
        raise BizError(BizCode.ORDER_GOODS_OFF_SALE, '商品已失效')
    stock=session.query(GdSkuStock).filter_by(tenant_id=tenant_id, sku_id=sku.id, channel='NORMAL').first()
    if not stock or stock.available_stock < quantity:
        raise BizError(BizCode.STOCK_NOT_ENOUGH, '库存不足')
    return sku, goods, stock
class VerifyIn(BaseModel):
    code: str
    # 管理后台/小程序统一使用 camelCase；兼容历史 snake_case 调用方。
    store_id: int | None = Field(default=None, validation_alias=AliasChoices("storeId", "store_id"))
class PayIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    pay_method: str = "WECHAT"
    points_deduct: int | None = Field(default=None, alias="pointsDeduct")  # 本次支付使用的积分数量（抵扣），不传则不抵扣
class ShipIn(BaseModel): express_company: str = ""; express_no: str = ""
class PointsIn(BaseModel): points: int; remark: str = ""; idempotency_key: str

def _address_out(x: OdAddress) -> dict:
    return {'id':x.id,'receiverName':x.receiver_name,'phone':x.phone,'province':x.province,
            'city':x.city,'district':x.district,'detail':x.detail,'isDefault':bool(x.is_default)}

@router.get('/role')
def merchant_roles(request:Request):
    tid,_=merchant_ctx(request)
    with SessionLocal() as s:
        rows=s.query(McRole).filter_by(tenant_id=tid).all()
        if not rows:
            rows=[McRole(tenant_id=tid,name='店长',remark='演示角色',perms=['MC_ALL'],is_system=1),McRole(tenant_id=tid,name='收银员',remark='演示角色',perms=['ORDER_LIST','VERIFY_DO'],is_system=1)]
            s.add_all(rows); s.commit()
        return ok([{'id':x.id,'name':x.name,'remark':x.remark,'perms':x.perms or [],'isSystem':x.is_system} for x in rows])

@router.post('/role')
def merchant_role_create(payload:dict,request:Request):
    tid,_=merchant_ctx(request)
    with SessionLocal() as s:
        if s.query(McRole).filter_by(tenant_id=tid,name=payload.get('name','')).first(): raise HTTPException(409,'角色名称已存在')
        x=McRole(tenant_id=tid,name=payload.get('name',''),remark=payload.get('remark',''),perms=payload.get('perms',[]),is_system=0); s.add(x); s.commit(); return ok({'id':x.id})

@router.put('/role/{role_id}')
def merchant_role_update(role_id:int,payload:dict,request:Request):
    tid,_=merchant_ctx(request)
    with SessionLocal() as s:
        x=s.query(McRole).filter_by(id=role_id,tenant_id=tid).first()
        if not x: raise HTTPException(404,'角色不存在')
        for k,a in {'name':'name','remark':'remark','perms':'perms'}.items():
            if k in payload:setattr(x,a,payload[k])
        s.commit(); return ok()

@router.delete('/role/{role_id}')
def merchant_role_delete(role_id: int, request: Request):
    tid, _ = merchant_ctx(request)
    with SessionLocal() as s:
        x = s.query(McRole).filter_by(id=role_id, tenant_id=tid).first()
        if not x: raise HTTPException(404, '角色不存在')
        if x.is_system: raise HTTPException(403, '系统预置角色不可删除')
        # 有员工仍绑定该角色时拒绝，避免留下无权限归属的员工。
        if s.query(McStaff).filter_by(tenant_id=tid, role_id=role_id).count():
            raise HTTPException(409, '角色仍有员工使用，不能删除')
        s.delete(x); s.commit(); return ok()

@router.get('/staff')
def merchant_staff_list(request:Request):
    tid,_=merchant_ctx(request)
    with SessionLocal() as s:return ok([{'id':x.id,'account':x.account,'name':x.name,'phone':x.phone,'roleId':x.role_id,'storeId':x.store_id,'status':x.status,'isAdmin':x.is_admin} for x in s.query(McStaff).filter_by(tenant_id=tid).all()])

@router.post('/staff')
def merchant_staff_create(payload:dict,request:Request):
    tid,_=merchant_ctx(request); account=payload.get('account','').strip()
    if not account: raise HTTPException(400,'员工账号不能为空')
    with SessionLocal() as s:
        if s.query(McStaff).filter_by(tenant_id=tid,account=account).first(): raise HTTPException(409,'员工账号已存在')
        x=McStaff(tenant_id=tid,account=account,name=payload.get('name',account),phone=payload.get('phone',''),role_id=int(payload.get('roleId') or 0),store_id=payload.get('storeId'),password_hash=hash_password(payload.get('password') or '123456'),status='ENABLED',is_admin=0)
        s.add(x); s.commit(); return ok({'id':x.id,'account':x.account})

@router.put('/staff/{staff_id}')
def merchant_staff_update(staff_id:int,payload:dict,request:Request):
    tid,_=merchant_ctx(request)
    with SessionLocal() as s:
        x=s.query(McStaff).filter_by(id=staff_id,tenant_id=tid).first()
        if not x: raise HTTPException(404,'员工不存在')
        for k,a in {'name':'name','phone':'phone','roleId':'role_id','storeId':'store_id','status':'status'}.items():
            if k in payload:setattr(x,a,payload[k])
        if payload.get('password'):x.password_hash=hash_password(payload['password'])
        s.commit(); return ok()

@router.post('/staff/{staff_id}/bind-wecom')
def staff_bind_wecom(staff_id: int, payload: dict, request: Request):
    tid, _ = merchant_ctx(request); userid = str(payload.get('wecomUserId') or payload.get('wecom_userid') or '').strip()
    if not userid: raise ParamError(fields={'wecomUserId':'不能为空'})
    with SessionLocal() as s:
        staff=s.query(McStaff).filter_by(id=staff_id,tenant_id=tid).first()
        if not staff: raise NotFoundError('员工不存在')
        if staff.status != 'ENABLED': raise BizError(BizCode.UNAUTHORIZED,'禁用员工不可绑定企微')
        if s.query(McStaff).filter(McStaff.tenant_id==tid,McStaff.wecom_userid==userid,McStaff.id!=staff_id).first(): raise HTTPException(409,'企微账号已绑定')
        staff.wecom_userid=userid; s.commit(); return ok({'id':staff.id,'wecomUserId':userid})

@router.post('/staff/{staff_id}/unbind-wecom')
def staff_unbind_wecom(staff_id: int, request: Request):
    tid, _ = merchant_ctx(request)
    with SessionLocal() as s:
        staff=s.query(McStaff).filter_by(id=staff_id,tenant_id=tid).first()
        if not staff: raise NotFoundError('员工不存在')
        staff.wecom_userid=None; s.commit(); return ok({'id':staff.id,'wecomUserId':None})

@router.get('/msg-config')
def merchant_msg_config(request:Request):
    tid,_=merchant_ctx(request)
    with SessionLocal() as s:
        rows=s.query(McMsgConfig).filter_by(tenant_id=tid).all()
        if not rows:
            rows=[McMsgConfig(tenant_id=tid,template_no='ORDER_PAID',enabled=1,channels=['INTERNAL']),McMsgConfig(tenant_id=tid,template_no='ORDER_SHIPPED',enabled=1,channels=['WX_SUBSCRIBE'])]
            s.add_all(rows); s.commit()
        return ok([{'id':x.id,'templateNo':x.template_no,'enabled':x.enabled,'channels':x.channels or []} for x in rows])

@router.put('/msg-config/{config_id}')
def merchant_msg_config_update(config_id:int,payload:dict,request:Request):
    tid,_=merchant_ctx(request)
    allowed={'WX_SUBSCRIBE','INTERNAL'}
    channels=payload.get('channels')
    if channels is not None and (not isinstance(channels,list) or any(c not in allowed for c in channels)):
        raise BizError(BizCode.PARAM_ERROR,'channels 仅支持 WX_SUBSCRIBE/INTERNAL')
    if 'enabled' in payload and payload['enabled'] not in (0,1,True,False):
        raise BizError(BizCode.PARAM_ERROR,'enabled 必须为 0/1')
    with SessionLocal() as s:
        x=s.query(McMsgConfig).filter_by(id=config_id,tenant_id=tid).first()
        if not x: raise HTTPException(404,'消息配置不存在')
        if 'enabled' in payload:x.enabled=payload['enabled']
        if 'channels' in payload:x.channels=payload['channels']
        s.commit(); return ok()

@router.get('/message')
def merchant_message_compat(request:Request):
    return merchant_msg_config(request)

@router.put('/message/{config_id}')
def merchant_message_update_compat(config_id:int,payload:dict,request:Request):
    return merchant_msg_config_update(config_id,payload,request)

@shop_router.get("/category")
def shop_category(request: Request, channel: str = 'NORMAL'):
    tid, _ = ctx(request)
    from app.models.gd_goods import GdCategory
    with SessionLocal() as s:
        rows=s.query(GdCategory).filter_by(tenant_id=tid,channel=channel,deleted_at=None).order_by(GdCategory.sort.desc(),GdCategory.id.desc()).all()
        return ok([{'id':x.id,'name':x.name,'parentId':x.parent_id,'channel':x.channel} for x in rows])

@shop_router.get("/goods")
def shop_goods_list(request: Request, page_no: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100), keyword: str|None=None, categoryId:int|None=None):
    # 用户侧商品浏览（/mp、/c）。商家端列表已由 app/api/mc/goods.py 承载
    # （/mc 前缀下本端点被 mc_goods_router 优先匹配，仅 mp/c 生效）。
    tid, _ = ctx(request)
    from sqlalchemy import func
    with SessionLocal() as s:
        q = s.query(GdGoods).filter(GdGoods.tenant_id == tid, GdGoods.deleted_at.is_(None), GdGoods.status == "ON_SALE", GdGoods.normal_on_sale == 1)
        if keyword: q=q.filter(GdGoods.name.like(f'%{keyword}%'))
        if categoryId: q=q.filter(GdGoods.normal_category_id==categoryId)
        total = q.count(); rows = q.order_by(GdGoods.sort.desc(), GdGoods.id.desc()).offset((page_no-1)*size).limit(size).all()
        ids=[x.id for x in rows]
        # 批量聚合价格与库存，避免逐条查询（N+1）
        price_map,stock_map={},{}
        if ids:
            price_map={g:p for g,p in s.query(GdSku.goods_id,func.min(GdSku.price)).filter(
                GdSku.tenant_id==tid,GdSku.goods_id.in_(ids),GdSku.deleted_at.is_(None)).group_by(GdSku.goods_id).all()}
            stock_map={g:c for g,c in s.query(GdSkuStock.goods_id,func.coalesce(func.sum(GdSkuStock.available_stock),0)).filter(
                GdSkuStock.tenant_id==tid,GdSkuStock.goods_id.in_(ids),GdSkuStock.channel=="NORMAL").group_by(GdSkuStock.goods_id).all()}
        return page([{"id":x.id,"name":x.name,"subtitle":x.subtitle,"mainImage":x.main_image,"type":x.type,"channel":x.channel,"status":x.status,"price":str(price_map.get(x.id) or "0.00"),"stock":int(stock_map.get(x.id) or 0),"soldCount":x.sold_count} for x in rows], total, page_no, size)

# T-032：商品 SPU/SKU/上下架管理已迁至 app/api/mc/goods.py
# （services/goods.py + services/sku.py + services/inventory.py）。

@shop_router.get("/goods/{goods_id}")
def shop_goods_detail(goods_id: int, request: Request):
    # 用户侧商品详情（/mp、/c）。商家端详情见 app/api/mc/goods.py。
    tid, _ = ctx(request)
    with SessionLocal() as s:
        g = s.query(GdGoods).filter(GdGoods.id==goods_id,GdGoods.tenant_id==tid,GdGoods.deleted_at.is_(None)).first()
        if not g: return err(BizCode.NOT_FOUND, "商品不存在")
        skus = s.query(GdSku).filter(GdSku.goods_id==goods_id,GdSku.tenant_id==tid,GdSku.deleted_at.is_(None)).all()
        stocks=s.query(GdSkuStock).filter_by(tenant_id=tid).filter(GdSkuStock.goods_id==goods_id).all(); total=sum((x.available_stock or 0) for x in stocks)
        return ok({"id":g.id,"name":g.name,"detail":g.detail,"mainImage":g.main_image,"type":g.type,"channel":g.channel,"status":g.status,"price":str(skus[0].price if skus else 0),"totalStock":total,"stocks":[{"skuId":x.sku_id,"channel":x.channel,"totalStock":x.total_stock,"availableStock":x.available_stock} for x in stocks],"ticketConfig":None,"skus":[{"id":x.id,"skuCode":x.sku_code,"specJson":x.spec_json or {},"specText":x.spec_text,"price":str(x.price),"originalPrice":str(x.original_price),"points":x.points,"stocks":[{"channel":z.channel,"totalStock":z.total_stock,"availableStock":z.available_stock} for z in stocks if z.sku_id==x.id]} for x in skus]})

@shop_router.post("/cart")
def add_cart(req: CartIn, request: Request):
    tid, mid = ctx(request)
    with SessionLocal() as s:
        sku=s.query(GdSku).filter_by(id=req.sku_id,tenant_id=tid,deleted_at=None).first()
        if not sku or sku.goods_id != req.goods_id: raise HTTPException(400,"商品或SKU不存在")
        row=s.query(OdCart).filter_by(tenant_id=tid,member_id=mid,sku_id=req.sku_id,channel="NORMAL").first()
        if row: row.quantity += req.quantity
        else:
            row=OdCart(tenant_id=tid,member_id=mid,goods_id=req.goods_id,sku_id=req.sku_id,quantity=req.quantity,channel="NORMAL",selected=1)
            s.add(row); s.flush()
        s.commit(); return ok({"id":row.id,"goodsId":row.goods_id,"skuId":row.sku_id,"quantity":row.quantity,"selected":row.selected})

@shop_router.get("/cart")
def cart(request: Request):
    tid, mid=ctx(request)
    with SessionLocal() as s:
        rows=s.query(OdCart).filter_by(tenant_id=tid,member_id=mid,channel="NORMAL").all()
        sku_ids=[x.sku_id for x in rows]; goods_ids=[x.goods_id for x in rows]
        skus={x.id:x for x in s.query(GdSku).filter(GdSku.tenant_id==tid,GdSku.id.in_(sku_ids)).all()} if sku_ids else {}
        goods={x.id:x for x in s.query(GdGoods).filter(GdGoods.tenant_id==tid,GdGoods.id.in_(goods_ids)).all()} if goods_ids else {}
        stocks={x.sku_id:x.available_stock for x in s.query(GdSkuStock).filter(GdSkuStock.tenant_id==tid,GdSkuStock.sku_id.in_(sku_ids),GdSkuStock.channel=='NORMAL').all()} if sku_ids else {}
        return ok([{"id":x.id,"goodsId":x.goods_id,"skuId":x.sku_id,"quantity":x.quantity,"selected":x.selected,
            "goodsName":goods.get(x.goods_id).name if goods.get(x.goods_id) else '',
            "mainImage":goods.get(x.goods_id).main_image if goods.get(x.goods_id) else '',
            "skuCode":skus.get(x.sku_id).sku_code if skus.get(x.sku_id) else '',
            "specText":skus.get(x.sku_id).spec_text if skus.get(x.sku_id) else '',
            "price":str(skus.get(x.sku_id).price) if skus.get(x.sku_id) else '0.00',
            "stock":int(stocks.get(x.sku_id) or 0), "invalid":not bool(goods.get(x.goods_id) and skus.get(x.sku_id))} for x in rows])

@shop_router.get('/cart/count')
def cart_count(request: Request):
    tid, mid = ctx(request)
    with SessionLocal() as s:
        rows=s.query(OdCart).filter_by(tenant_id=tid,member_id=mid,channel='NORMAL').all()
        return ok({'count': sum(x.quantity for x in rows), 'itemCount': len(rows)})

@shop_router.delete('/cart')
def delete_cart_batch(payload: dict, request: Request):
    tid, mid = ctx(request); ids = payload.get('ids') or []
    if not isinstance(ids, list) or not ids: raise HTTPException(400, 'ids不能为空')
    with SessionLocal() as s:
        deleted=s.query(OdCart).filter(OdCart.tenant_id==tid,OdCart.member_id==mid,OdCart.id.in_(ids)).delete(synchronize_session=False)
        s.commit(); return ok({'deleted': deleted})

@shop_router.get('/address')
def address_list(request: Request):
    tid, mid = ctx(request)
    with SessionLocal() as s:
        rows=s.query(OdAddress).filter_by(tenant_id=tid,member_id=mid,deleted_at=None).order_by(OdAddress.is_default.desc(),OdAddress.id.desc()).all()
        return ok([_address_out(x) for x in rows])

@shop_router.get('/address/{address_id}')
def address_detail(address_id: int, request: Request):
    tid, mid = ctx(request)
    with SessionLocal() as s:
        x=s.query(OdAddress).filter_by(id=address_id,tenant_id=tid,member_id=mid,deleted_at=None).first()
        if not x: raise HTTPException(404,'地址不存在')
        return ok(_address_out(x))

@shop_router.post('/address')
def address_create(payload: dict, request: Request):
    tid, mid=ctx(request)
    if not str(payload.get('receiverName') or payload.get('receiver_name') or '').strip(): raise HTTPException(400,'收货人不能为空')
    with SessionLocal() as s:
        existing=s.query(OdAddress).filter_by(tenant_id=tid,member_id=mid,deleted_at=None).count()
        default=bool(payload.get('isDefault',payload.get('is_default',False))) or existing==0
        if default: s.query(OdAddress).filter_by(tenant_id=tid,member_id=mid).update({'is_default':0})
        x=OdAddress(tenant_id=tid,member_id=mid,receiver_name=payload.get('receiverName',payload.get('receiver_name')),
            phone=payload.get('phone',''),province=payload.get('province',''),city=payload.get('city',''),district=payload.get('district',''),detail=payload.get('detail',''),is_default=int(default))
        s.add(x);s.commit();return ok(_address_out(x))

@shop_router.put('/address/{address_id}')
def address_update(address_id:int,payload:dict,request:Request):
    tid,mid=ctx(request)
    with SessionLocal() as s:
        x=s.query(OdAddress).filter_by(id=address_id,tenant_id=tid,member_id=mid,deleted_at=None).first()
        if not x: raise HTTPException(404,'地址不存在')
        for key,attr in {'receiverName':'receiver_name','phone':'phone','province':'province','city':'city','district':'district','detail':'detail'}.items():
            if key in payload:setattr(x,attr,payload[key])
        if payload.get('isDefault'):
            s.query(OdAddress).filter_by(tenant_id=tid,member_id=mid).update({'is_default':0});x.is_default=1
        s.commit();return ok(_address_out(x))

@shop_router.delete('/address/{address_id}')
def address_delete(address_id:int,request:Request):
    tid,mid=ctx(request)
    with SessionLocal() as s:
        x=s.query(OdAddress).filter_by(id=address_id,tenant_id=tid,member_id=mid,deleted_at=None).first()
        if not x: raise HTTPException(404,'地址不存在')
        was_default=bool(x.is_default)
        x.deleted_at=datetime.now(UTC).replace(tzinfo=None);x.is_default=0
        if was_default:
            # 稳定选择最新有效地址，确保一个会员始终至多且至少一个默认地址。
            replacement=s.query(OdAddress).filter(OdAddress.tenant_id==tid,OdAddress.member_id==mid,OdAddress.deleted_at.is_(None),OdAddress.id!=address_id).order_by(OdAddress.id.desc()).first()
            if replacement: replacement.is_default=1
        s.commit();return ok()

@shop_router.put('/address/{address_id}/default')
def address_default(address_id:int,request:Request):
    return address_update(address_id,{'isDefault':True},request)

@shop_router.put("/cart/{cart_id}")
def update_cart(cart_id: int, req: CartUpdateIn, request: Request):
    tid, mid = ctx(request)
    with SessionLocal() as s:
        row=s.query(OdCart).filter_by(id=cart_id, tenant_id=tid, member_id=mid).first()
        if not row: raise HTTPException(404, "购物车项不存在")
        row.quantity=req.quantity; s.commit(); return ok({"id":row.id,"quantity":row.quantity})

@shop_router.delete("/cart/{cart_id}")
def delete_cart(cart_id: int, request: Request):
    tid, mid = ctx(request)
    with SessionLocal() as s:
        row=s.query(OdCart).filter_by(id=cart_id, tenant_id=tid, member_id=mid).first()
        if not row: raise HTTPException(404, "购物车项不存在")
        s.delete(row); s.commit(); return ok()

@shop_router.post("/orders")
def create_order(req: OrderIn, request: Request):
    tid, mid=ctx(request)
    idem=(request.headers.get('Idempotency-Key') or '').strip()
    if len(idem)>80: raise ParamError({'Idempotency-Key':'长度不能超过80'})
    with SessionLocal() as s:
        if idem:
            old=s.query(OdOrder).filter_by(tenant_id=tid, member_id=mid, idempotency_key=idem).first()
            if old: return ok({'id':old.id,'orderNo':old.order_no,'payAmount':str(old.pay_amount),'status':old.status})
        sku,g,_stock=_validate_order_item(s, tid, req.sku_id, req.quantity)
        no="ORD"+datetime.now(UTC).strftime("%Y%m%d%H%M%S")+secrets.token_hex(2).upper()
        amount=Decimal(sku.price)*req.quantity
        o=OdOrder(tenant_id=tid,order_no=no,channel="NORMAL",member_id=mid,idempotency_key=idem or None,status="PENDING_PAY",delivery_type=req.delivery_type,goods_amount=amount,pay_amount=amount,receiver_name=req.receiver_name,receiver_phone=req.receiver_phone,receiver_address=req.receiver_address,pay_deadline=datetime.now(UTC)+timedelta(minutes=30))
        s.add(o); s.flush()
        inventory.lock_stock(s, [{'skuId': sku.id, 'channel': 'NORMAL', 'qty': req.quantity}], no)
        s.add(OdOrderItem(tenant_id=tid,order_id=o.id,goods_id=g.id,sku_id=sku.id,channel="NORMAL",goods_name=g.name,goods_type=g.type,spec_text=sku.spec_text,image=sku.image,price=sku.price,quantity=req.quantity,subtotal_amount=amount)); s.commit(); return ok({"id":o.id,"orderNo":no,"payAmount":str(amount),"status":o.status})

@shop_router.post('/order/preview')
def order_preview(payload:dict,request:Request):
    tid,_mid=ctx(request); items=payload.get('items') or []
    if not isinstance(items, list) or not items:
        raise ParamError({'items': '至少选择一件商品'}, 'items不能为空')
    with SessionLocal() as s:
        out=[]; total=Decimal(0)
        for item in items:
            if not isinstance(item, dict):
                raise ParamError({'items': '商品项格式错误'})
            try:
                sku_id=int(item.get('skuId') or item.get('sku_id') or 0)
                qty=int(item.get('quantity') or 0)
            except (TypeError, ValueError):
                raise ParamError({'items': 'SKU或数量格式错误'})
            if qty<1: raise ParamError({'quantity': '数量必须大于0'})
            sku=s.query(GdSku).filter_by(id=sku_id,tenant_id=tid,deleted_at=None).first()
            if not sku: raise BizError(BizCode.ORDER_SKU_INVALID,'SKU不存在')
            g=s.query(GdGoods).filter_by(id=sku.goods_id,tenant_id=tid,deleted_at=None).first()
            if not g or g.status!='ON_SALE' or not g.normal_on_sale:
                raise BizError(BizCode.ORDER_GOODS_OFF_SALE,'商品已失效')
            stock=s.query(GdSkuStock).filter_by(
                tenant_id=tid, sku_id=sku.id, channel='NORMAL'
            ).first()
            if not stock or stock.available_stock < qty:
                raise BizError(BizCode.STOCK_NOT_ENOUGH,'库存不足')
            amount=Decimal(sku.price)*qty;total+=amount
            out.append({'goodsId':g.id,'skuId':sku.id,'goodsName':g.name,'quantity':qty,'price':str(sku.price),'subtotalAmount':str(amount)})
        return ok({'items':out,'goodsAmount':str(total),'discountAmount':'0.00','freightAmount':'0.00','payAmount':str(total),'payPoints':0,'priceMode':'CASH','availableDelivery':['EXPRESS','PICKUP']})

@shop_router.post("/order/create")
def create_order_compat_payload(payload: dict, request: Request):
    """兼容用户小程序旧版创建订单字段，仍落入同一真实订单表。"""
    tid, _ = ctx(request)
    sku_id = payload.get("sku_id") or payload.get("skuId")
    with SessionLocal() as s:
        if not sku_id:
            sku = s.query(GdSku).filter_by(tenant_id=tid, deleted_at=None).order_by(GdSku.id).first()
            if not sku: raise HTTPException(400, "暂无可购买商品")
            sku_id = sku.id
    req=OrderIn(sku_id=int(sku_id), quantity=int(payload.get("quantity") or 1), receiver_name=payload.get("name") or payload.get("receiverName") or "", receiver_phone=payload.get("phone") or payload.get("receiverPhone") or "")
    return create_order(req, request)

@shop_router.get("/member/me")
def customer_member_me(request: Request):
    tid, mid = ctx(request)
    with SessionLocal() as s:
        m=s.query(MbMember).filter_by(id=mid, tenant_id=tid).first()
        if not m: raise HTTPException(404, "会员不存在")
        return ok({"id":m.id,"memberNo":m.member_no,"nickname":m.nickname,"phoneMask":m.phone_mask,"pointsBalance":m.points_balance,"totalAmount":str(m.total_amount),"totalOrderCount":m.total_order_count})

@shop_router.get('/points/summary')
def customer_points_summary(request: Request):
    tid, mid = ctx(request)
    with SessionLocal() as s:
        m=s.query(MbMember).filter_by(id=mid,tenant_id=tid,deleted_at=None).first()
        if not m: raise HTTPException(404,'会员不存在')
        return ok({'pointsBalance':m.points_balance,'totalEarn':m.points_total_earn,
                   'totalUsed':m.points_total_used,'pointsDebt':m.points_debt,'expiringPoints':0,'expireDays':0})

@shop_router.get('/points/log')
def customer_points_log(request: Request, page_no:int=Query(1,ge=1,alias='page'), size:int=Query(20,ge=1,le=100), type:str|None=None):
    tid, mid=ctx(request)
    with SessionLocal() as s:
        q=s.query(MbPointsLog).filter_by(tenant_id=tid,member_id=mid)
        if type: q=q.filter_by(change_type=type)
        total=q.count();rows=q.order_by(MbPointsLog.id.desc()).offset((page_no-1)*size).limit(size).all()
        return page([{'id':x.id,'amount':x.amount,'balanceAfter':x.balance_after,
            'changeType':x.change_type,'remark':x.remark,
            'createdAt':x.created_at.replace(tzinfo=UTC).isoformat() if x.created_at else None} for x in rows],total,page_no,size)

@shop_router.get("/orders")
def orders(request: Request):
    tid,mid=ctx(request)
    with SessionLocal() as s:
        rows=s.query(OdOrder).filter_by(tenant_id=tid,member_id=mid).order_by(OdOrder.id.desc()).all(); return ok([{"id":x.id,"orderNo":x.order_no,"status":x.status,"payAmount":str(x.pay_amount)} for x in rows])

@shop_router.get("/order/{order_id}")
def customer_order_detail(order_id: int, request: Request):
    tid, mid = ctx(request)
    with SessionLocal() as s:
        o=s.query(OdOrder).filter_by(id=order_id, tenant_id=tid, member_id=mid).first()
        if not o: raise HTTPException(404, "订单不存在")
        items=s.query(OdOrderItem).filter_by(order_id=o.id, tenant_id=tid).all()
        verify_codes = [{"code": v.code, "status": v.status, "goodsName": v.goods_name,
                          "validEnd": v.valid_end.isoformat() if v.valid_end else None}
                         for v in s.query(OdVerifyCode).filter_by(tenant_id=tid, order_id=o.id).all()]
        return ok({"id":o.id,"orderNo":o.order_no,"status":o.status,"deliveryType":o.delivery_type,"payAmount":str(o.pay_amount),"createdAt":o.created_at.isoformat(),"verifyCodes":verify_codes,"items":[{"goodsId":i.goods_id,"skuId":i.sku_id,"goodsName":i.goods_name,"quantity":i.quantity,"price":str(i.price),"subtotalAmount":str(i.subtotal_amount)} for i in items]})

def _gen_verify_codes(s, o, items):
    """支付成功后为 TICKET/VIRTUAL 商品按购买数量生成核销码/券码（幂等）。

    设计文档（02-数据库设计 §od_verify_code）：购买 N 张生成 N 条，
    code 格式：HX+12位（核销码）/ VC+日期+6位（券码）。
    幂等依据：同一订单已存在 od_verify_code 则跳过，避免支付重放/重复回调双写。
    有效期：FIXED_DATE 用商品配置的 valid_end_date；DAYS_AFTER_PAY 按支付后 N 天；
    未配置则默认支付后 30 天。
    """
    existing = s.query(OdVerifyCode).filter_by(tenant_id=o.tenant_id, order_id=o.id).first()
    if existing:
        return
    now = datetime.now(UTC)
    for it in items:
        if it.goods_type not in ("TICKET", "VIRTUAL"):
            continue
        g = s.query(GdGoods).filter_by(id=it.goods_id, tenant_id=o.tenant_id).first()
        code_type = "VERIFY" if it.goods_type == "TICKET" else "VIRTUAL"
        valid_start = now.replace(tzinfo=None)
        if g and g.valid_type == "FIXED_DATE" and g.valid_end_date:
            valid_end = datetime.combine(g.valid_end_date, datetime.min.time())
        else:
            days = (g.valid_days if g and g.valid_days else 30)
            valid_end = (now + timedelta(days=days)).replace(tzinfo=None)
        for _ in range(it.quantity):
            code = _new_verify_code(code_type)
            s.add(OdVerifyCode(
                tenant_id=o.tenant_id, order_id=o.id, order_item_id=it.id,
                member_id=o.member_id, code=code, code_type=code_type,
                goods_name=it.goods_name, spec_text=it.spec_text,
                valid_start=valid_start, valid_end=valid_end,
                applicable_store_ids=(g.verify_store_ids if g else None),
                status="UNUSED",
                expire_refund_policy=(g.expire_refund_policy if g else "FULL_CASH"),
            ))


def _new_verify_code(code_type: str) -> str:
    """HX+12位（核销码）/ VC+YYYYMMDD+6位（券码）。"""
    if code_type == "VERIFY":
        return "HX" + secrets.token_hex(6).upper()
    return f"VC{datetime.now(UTC).strftime('%Y%m%d')}{secrets.randbelow(10**6):06d}"


def grant_order_points(s, o) -> int:
    """订单完成后按实付金额发放积分，幂等：同一订单仅发放一次。

    幂等依据：若该订单已存在 change_type='ORDER_EARN' 的流水则直接跳过。
    发放积分 = int(实付金额 * POINTS_EARN_RATE)，向下取整。
    """
    existing = s.query(MbPointsLog).filter_by(
        tenant_id=o.tenant_id, member_id=o.member_id,
        ref_type="ORDER", ref_id=str(o.id), change_type="ORDER_EARN",
    ).first()
    if existing:
        o.earned_points = existing.amount
        return existing.amount
    earned = int((o.pay_amount * Decimal(settings.POINTS_EARN_RATE)).to_integral_value(rounding=ROUND_FLOOR))
    if earned <= 0:
        o.earned_points = 0
        return 0
    member = s.query(MbMember).filter_by(id=o.member_id, tenant_id=o.tenant_id).first()
    if not member:
        o.earned_points = 0
        return 0
    member.points_balance += earned
    member.points_total_earn += earned
    o.earned_points = earned
    s.add(MbPointsLog(tenant_id=o.tenant_id, member_id=o.member_id, change_type="ORDER_EARN",
        amount=earned, balance_after=member.points_balance, ref_type="ORDER", ref_id=str(o.id),
        remark="订单完成发放积分"))
    return earned


@shop_router.post("/order/{order_id}/pay")
def customer_order_pay(order_id: int, req: PayIn, request: Request):
    tid, mid = ctx(request)
    with SessionLocal() as s:
        o=s.query(OdOrder).filter_by(id=order_id, tenant_id=tid, member_id=mid).first()
        if not o: raise HTTPException(404, "订单不存在")
        used_points = o.pay_points
        if o.status == "PENDING_PAY":
            now=datetime.now(UTC)
            items=s.query(OdOrderItem).filter_by(tenant_id=tid,order_id=o.id).all()
            inventory.confirm_lock(s,[{'skuId':i.sku_id,'channel':i.channel,'qty':i.quantity} for i in items],o.order_no)
            used_points = 0
            if req.points_deduct:
                try:
                    want = int(req.points_deduct)
                except (TypeError, ValueError):
                    raise BizError(BizCode.POINTS_AMOUNT_INVALID, "pointsDeduct 必须为整数")
                if want < 0:
                    raise BizError(BizCode.POINTS_AMOUNT_INVALID, "pointsDeduct 不能为负数")
                if want > 0:
                    member = s.query(MbMember).filter_by(id=mid, tenant_id=tid).first()
                    if not member: raise HTTPException(404, "会员不存在")
                    if member.points_balance < want:
                        raise BizError(BizCode.POINTS_NOT_ENOUGH, "积分余额不足以抵扣")
                    max_usable = int((o.pay_amount * Decimal(settings.POINTS_REDUCE_RATIO)).to_integral_value(rounding=ROUND_FLOOR))
                    used_points = min(want, max_usable)
                    if used_points > 0:
                        discount = Decimal(used_points) / Decimal(settings.POINTS_REDUCE_RATIO)
                        member.points_balance -= used_points
                        member.points_total_used += used_points
                        o.pay_points = used_points
                        o.pay_amount = o.pay_amount - discount
                        s.add(MbPointsLog(tenant_id=tid, member_id=mid, change_type="SPEND",
                            amount=-used_points, balance_after=member.points_balance,
                            ref_type="ORDER", ref_id=str(o.id),
                            remark=f"支付抵扣 {discount} 元", operator_id=mid))
            o.status="PAID"; o.pay_method=req.pay_method; o.paid_at=now
            s.add(OdPayment(tenant_id=tid,order_id=o.id,out_trade_no=o.order_no,transaction_id="DEMO-"+o.order_no,pay_method=req.pay_method,channel=o.channel,amount=o.pay_amount,points=used_points,status="SUCCESS",paid_at=now))
            grant_order_points(s, o)
            _gen_verify_codes(s, o, items)
            s.commit()
        return ok({"orderId":o.id,"status":o.status,"payAmount":str(o.pay_amount),"pointsDeducted":used_points})

@shop_router.post('/order/{order_id}/cancel')
def customer_order_cancel(order_id:int,request:Request):
    tid,mid=ctx(request)
    with SessionLocal() as s:
        o=s.query(OdOrder).filter_by(id=order_id,tenant_id=tid,member_id=mid).first()
        if not o: raise HTTPException(404,'订单不存在')
        if o.status=='CLOSED': return ok({'id':o.id,'status':o.status})
        if o.status not in ('PENDING_PAY','PENDING_PAYMENT'): raise HTTPException(409,'订单当前状态不可取消')
        items=s.query(OdOrderItem).filter_by(tenant_id=tid,order_id=o.id).all()
        inventory.release_lock(s,[{'skuId':i.sku_id,'channel':i.channel,'qty':i.quantity} for i in items],o.order_no)
        o.status='CLOSED';o.cancelled_at=datetime.now(UTC).replace(tzinfo=None);s.commit();return ok({'id':o.id,'status':o.status})

@shop_router.post('/order/{order_id}/confirm-receive')
def customer_confirm_receive(order_id:int,request:Request):
    tid,mid=ctx(request)
    with SessionLocal() as s:
        o=s.query(OdOrder).filter_by(id=order_id,tenant_id=tid,member_id=mid).first()
        if not o: raise HTTPException(404,'订单不存在')
        if o.status=='COMPLETED': return ok({'id':o.id,'status':o.status})
        if o.status not in ('SHIPPED','PENDING_RECEIVE'): raise HTTPException(409,'订单当前状态不可确认收货')
        now=datetime.now(UTC).replace(tzinfo=None);o.status='COMPLETED';o.received_at=now;o.completed_at=now
        grant_order_points(s, o); s.commit();return ok({'id':o.id,'status':o.status})

@shop_router.post("/order/{order_id}/refund")
def customer_refund_request(order_id:int, payload:dict, request:Request):
    tid,mid=ctx(request)
    if order_id <= 0: return err(BizCode.PARAM_ERROR,'orderId不能为空')
    with SessionLocal() as s:
        o=s.query(OdOrder).filter_by(id=order_id,tenant_id=tid,member_id=mid).first()
        if not o: return err(BizCode.NOT_FOUND,"订单不存在")
        if o.status not in ('PAID','SHIPPED','PENDING_RECEIVE','REFUNDING'):
            return err(BizCode.ORDER_STATUS_INVALID,'订单当前状态不可退款')
        items=s.query(OdOrderItem).filter_by(tenant_id=tid,order_id=o.id).all()
        if any(x.goods_type == 'VIRTUAL' for x in items): return err(BizCode.VIRTUAL_REFUND_FORBIDDEN,'虚拟商品不可退款')
        if s.query(OdRefund).filter(OdRefund.tenant_id==tid,OdRefund.order_id==o.id,OdRefund.status.in_(('PENDING','PENDING_AUDIT','APPROVED'))).first():
            return err(BizCode.REFUND_DUPLICATE,'已存在退款申请')
        no="RF"+datetime.now(UTC).strftime("%Y%m%d%H%M%S")+secrets.token_hex(2).upper()
        r=OdRefund(tenant_id=tid,order_id=o.id,refund_no=no,refund_amount=o.pay_amount,reason_code=payload.get("reasonCode","USER"),reason_desc=payload.get("reasonDesc",{} if False else ""),source="USER")
        s.add(r)
        r.order_status_before=o.status
        o.status='REFUNDING'
        s.commit(); return ok({"id":r.id,"refundNo":no,"status":r.status})

@shop_router.post('/refund')
def customer_refund(payload: dict, request: Request):
    try: order_id = int(payload.get('orderId') or 0)
    except (TypeError,ValueError): return err(BizCode.PARAM_ERROR,'orderId不能为空')
    return customer_refund_request(order_id, {'reasonCode':payload.get('reasonCode','USER'), 'reasonDesc':payload.get('reasonDesc',payload.get('reason',''))}, request)

@shop_router.get('/refund/{refund_id}')
def customer_refund_detail(refund_id:int, request:Request):
    tid,mid=ctx(request)
    with SessionLocal() as s:
        r=s.query(OdRefund).filter_by(id=refund_id,tenant_id=tid).first()
        if not r: raise HTTPException(404,'退款单不存在')
        o=s.query(OdOrder).filter_by(id=r.order_id,tenant_id=tid,member_id=mid).first()
        if not o: raise HTTPException(404,'退款单不存在')
        return ok({'id':r.id,'refundNo':r.refund_no,'orderId':r.order_id,'status':r.status,'refundAmount':str(r.refund_amount),'reasonCode':r.reason_code,'reasonDesc':r.reason_desc,'createdAt':r.created_at.replace(tzinfo=UTC).isoformat()})

# 小程序旧版契约使用单数 /order，保留兼容别名，实际复用同一租户/会员逻辑。
@shop_router.post("/order")
def create_order_compat(req: OrderIn, request: Request):
    return create_order(req, request)

@shop_router.get("/order")
def orders_compat(request: Request):
    return orders(request)

@router.get("/orders")
def merchant_orders_plural(request: Request, status: str|None=None, pageNo:int=Query(1,ge=1), size:int=Query(20,ge=1,le=100)):
    tid,operator=merchant_ctx(request)
    with SessionLocal() as s:
        staff_store = _staff_store_scope(s, tid, operator)
        q=s.query(OdOrder).filter(OdOrder.tenant_id==tid)
        if staff_store is not None:
            q=q.filter(OdOrder.store_id==staff_store)
        if status:q=q.filter(OdOrder.status==status)
        total=q.count(); rows=q.order_by(OdOrder.id.desc()).offset((pageNo-1)*size).limit(size).all()
        out=[]
        for o in rows:
            it=s.query(OdOrderItem).filter_by(order_id=o.id,tenant_id=tid).first(); m=s.query(MbMember).filter_by(id=o.member_id,tenant_id=tid).first()
            out.append({"id":o.id,"orderNo":o.order_no,"status":o.status,"amount":str(o.pay_amount),"payAmount":str(o.pay_amount),"firstGoodsName":it.goods_name if it else '',"goodsName":it.goods_name if it else '',"type":it.goods_type if it else '',"buyerName":m.nickname if m else o.receiver_name,"nickname":m.nickname if m else '',"createdAt":o.created_at.isoformat()})
        return page(out,total,pageNo,size)

@router.get("/order")
def merchant_order_alias(request: Request, status: str|None=None):
    tid,operator=merchant_ctx(request)
    with SessionLocal() as s:
        staff_store = _staff_store_scope(s, tid, operator)
        q=s.query(OdOrder).filter(OdOrder.tenant_id==tid)
        if staff_store is not None:
            q=q.filter(OdOrder.store_id==staff_store)
        if status:q=q.filter(OdOrder.status==status)
        out=[]
        for o in q.order_by(OdOrder.id.desc()).all():
            it=s.query(OdOrderItem).filter_by(order_id=o.id,tenant_id=tid).first(); m=s.query(MbMember).filter_by(id=o.member_id,tenant_id=tid).first()
            out.append({"id":o.id,"orderNo":o.order_no,"status":o.status,"amount":str(o.pay_amount),"payAmount":str(o.pay_amount),"firstGoodsName":it.goods_name if it else '',"goodsName":it.goods_name if it else '',"type":it.goods_type if it else '',"buyerName":m.nickname if m else o.receiver_name,"nickname":m.nickname if m else '',"createdAt":o.created_at.isoformat()})
        return ok(out)

@router.get('/order/status-counts')
def order_status_counts(request:Request):
    tid,_=merchant_ctx(request)
    with SessionLocal() as s:
        from sqlalchemy import func
        rows=s.query(OdOrder.status,func.count(OdOrder.id)).filter_by(tenant_id=tid).group_by(OdOrder.status).all()
        return ok({k:v for k,v in rows})

@router.get('/order/{order_id}')
def order_detail(order_id:int,request:Request):
    tid,operator=merchant_ctx(request)
    with SessionLocal() as s:
        staff_store = _staff_store_scope(s, tid, operator)
        o=s.query(OdOrder).filter_by(id=order_id,tenant_id=tid).first()
        if not o: raise HTTPException(404,'订单不存在')
        _ensure_order_store_scope(s, o, staff_store)
        items=s.query(OdOrderItem).filter_by(order_id=o.id,tenant_id=tid).all()
        verify_codes = [{"code": v.code, "status": v.status, "goodsName": v.goods_name,
                          "validEnd": v.valid_end.isoformat() if v.valid_end else None}
                         for v in s.query(OdVerifyCode).filter_by(tenant_id=tid, order_id=o.id).all()]
        return ok({'id':o.id,'orderNo':o.order_no,'status':o.status,'deliveryType':o.delivery_type,'payAmount':str(o.pay_amount),'goodsAmount':str(o.goods_amount),'receiverName':o.receiver_name,'receiverPhone':o.receiver_phone,'receiverAddress':o.receiver_address,'expressCompany':o.express_company,'expressNo':o.express_no,'createdAt':o.created_at.isoformat(),'paidAt':o.paid_at.isoformat() if o.paid_at else None,'shippedAt':o.shipped_at.isoformat() if o.shipped_at else None,'verifyCodes':verify_codes,'items':[{'id':i.id,'goodsId':i.goods_id,'skuId':i.sku_id,'goodsName':i.goods_name,'specText':i.spec_text,'quantity':i.quantity,'price':str(i.price),'subtotalAmount':str(i.subtotal_amount)} for i in items]})

@router.post('/order/{order_id}/stocking')
def order_stocking(order_id:int,request:Request):
    tid,operator=merchant_ctx(request)
    with SessionLocal() as s:
        staff_store = _staff_store_scope(s, tid, operator)
        o=s.query(OdOrder).filter_by(id=order_id,tenant_id=tid).first()
        if not o: raise HTTPException(404,'订单不存在')
        _ensure_order_store_scope(s, o, staff_store)
        if o.status not in ('PAID','PENDING_STOCK','STOCKED'):
            raise HTTPException(400,'订单当前状态不可备货')
        o.stocked_at=datetime.now(UTC)
        o.status='PENDING_PICKUP' if o.delivery_type=='PICKUP' else 'PENDING_SHIP'
        s.commit(); return ok({'id':o.id,'status':o.status})

@router.post('/order/{order_id}/pickup-confirm')
def pickup_confirm(order_id:int,request:Request):
    tid,operator=merchant_ctx(request)
    with SessionLocal() as s:
        staff_store = _staff_store_scope(s, tid, operator)
        o=s.query(OdOrder).filter_by(id=order_id,tenant_id=tid).first()
        if not o: raise HTTPException(404,'订单不存在')
        _ensure_order_store_scope(s, o, staff_store)
        if o.delivery_type!='PICKUP' or o.status not in ('STOCKED','PENDING_PICKUP'):
            raise HTTPException(400,'订单当前状态不可确认提货')
        o.status='COMPLETED'; o.received_at=datetime.now(UTC); o.completed_at=datetime.now(UTC); o.operator_pickup=str(operator); s.commit(); return ok({'id':o.id,'status':o.status})

@router.post("/verify")
def verify(req: VerifyIn, request: Request):
    # 核销是写操作（置 USED），此前用 ctx() 无 scope 校验，任意有效 token 可烧掉商家核销码。
    tid,mid=merchant_ctx(request)
    with SessionLocal() as s:
        v=s.query(OdVerifyCode).filter_by(tenant_id=tid,code=req.code).first()
        if not v: return err(BizCode.VERIFY_CODE_INVALID,"核销码不存在")
        if v.status == "USED": return err(BizCode.VERIFY_CODE_USED,"核销码已使用")
        # MySQL DATETIME 不保存 tzinfo，统一按 UTC naive 存储/比较；不能混用
        # datetime.now() 本地时间，否则 UTC 写入的有效码会被误判为过期。
        now_utc = datetime.now(UTC).replace(tzinfo=None)
        if v.status == "EXPIRED" or (v.valid_start and v.valid_start > now_utc) or (v.valid_end and v.valid_end < now_utc):
            return err(BizCode.VERIFY_CODE_EXPIRED,"核销码已过期")
        if v.status != "UNUSED": return err(BizCode.VERIFY_CODE_INVALID,"核销码无效")
        staff = s.query(McStaff).filter_by(id=mid, tenant_id=tid).first()
        # 设计 03-API：核销门店由后端从员工 store_id 解析；门店员工强制本门店，
        # 不接受前端传参指定其他门店；总部（store_id=None）才允许前端指定门店。
        if staff and staff.store_id is not None:
            if req.store_id not in (None, staff.store_id):
                return err(BizCode.VERIFY_STORE_MISMATCH, "员工不属于该门店")
            req.store_id = staff.store_id
        v.status="USED"; v.verified_at=datetime.now(UTC); v.verify_store_id=req.store_id; v.verify_staff_id=mid; s.commit(); return ok({"code":v.code,"status":v.status})

@router.post("/verify/confirm")
def verify_confirm(req: VerifyIn, request: Request):
    return verify(req, request)

@router.get('/verify/query')
def verify_query(code:str,request:Request):
    tid,_=merchant_ctx(request)
    with SessionLocal() as s:
        v=s.query(OdVerifyCode).filter_by(tenant_id=tid,code=code).first()
        if not v: return err(BizCode.VERIFY_CODE_INVALID,'核销码不存在')
        now_utc = datetime.now(UTC).replace(tzinfo=None)
        if v.status == "EXPIRED" or (v.valid_start and v.valid_start > now_utc) or (v.valid_end and v.valid_end < now_utc):
            return err(BizCode.VERIFY_CODE_EXPIRED,'核销码已过期')
        return ok({'code':v.code,'status':v.status,'goodsName':v.goods_name,'memberId':v.member_id,'orderId':v.order_id,'validEnd':v.valid_end.isoformat()})

@router.get("/verify/log")
def verify_log(request: Request, code: str | None = None, storeId: int | None = None, pageNo: int = Query(1, ge=1, alias='page'), size: int = Query(20, ge=1, le=100)):
    tid,_=merchant_ctx(request)
    with SessionLocal() as s:
        q=s.query(OdVerifyCode).filter_by(tenant_id=tid)
        if code: q=q.filter(OdVerifyCode.code==code)
        if storeId is not None: q=q.filter(OdVerifyCode.verify_store_id==storeId)
        total=q.count(); rows=q.order_by(OdVerifyCode.id.desc()).offset((pageNo-1)*size).limit(size).all()
        return page([{"code":v.code,"status":v.status,"verifiedAt":v.verified_at.isoformat() if v.verified_at else None} for v in rows],total,pageNo,size)

@router.get("/verify/log/export")
@router.post("/verify/log/export")
def verify_log_export(request: Request, code: str | None = None, storeId: int | None = None):
    tid,_=merchant_ctx(request)
    with SessionLocal() as s:
        q=s.query(OdVerifyCode).filter_by(tenant_id=tid)
        if code: q=q.filter(OdVerifyCode.code==code)
        if storeId is not None: q=q.filter(OdVerifyCode.verify_store_id==storeId)
        out=io.StringIO(); w=csv.writer(out); w.writerow(['code','status','verifiedAt'])
        for v in q.order_by(OdVerifyCode.id.desc()).all(): w.writerow([v.code,v.status,v.verified_at.isoformat() if v.verified_at else ''])
        return StreamingResponse(iter(['\ufeff'+out.getvalue()]), media_type='text/csv; charset=utf-8')

# 商家小程序/旧版页面契约别名；正式后台同时保留 /verify/log。
@router.get("/verify/records")
def verify_records_compat(request: Request):
    return verify_log(request)

@router.post("/order/{order_id}/ship")
def ship_order(order_id: int, req: ShipIn, request: Request):
    tid,operator=merchant_ctx(request)
    with SessionLocal() as s:
        staff_store = _staff_store_scope(s, tid, operator)
        o=s.query(OdOrder).filter_by(id=order_id,tenant_id=tid).first()
        if not o: raise HTTPException(404,"订单不存在")
        _ensure_order_store_scope(s, o, staff_store)
        # STOCKED 为备货中中间态：备货完成应走 PENDING_SHIP/PENDING_PICKUP，不允许直接发货
        if o.status not in ("PAID","PENDING_SHIP"): raise HTTPException(400,"订单当前状态不可发货")
        o.status="SHIPPED"; o.express_company=req.express_company; o.express_no=req.express_no; o.shipped_at=datetime.now(UTC); s.commit(); return ok({'id':o.id,'status':o.status})

@router.post('/order/batch-ship')
def batch_ship(payload: dict, request: Request):
    tid, operator = merchant_ctx(request)
    ids = payload.get('ids')
    if not isinstance(ids, list) or not ids:
        raise ParamError(fields={'ids': '至少选择一笔订单'})
    result = {'success': [], 'failed': []}
    with SessionLocal() as s:
        staff_store = _staff_store_scope(s, tid, operator)
        for oid in ids:
            o = s.query(OdOrder).filter_by(id=int(oid), tenant_id=tid).first()
            if not o:
                result['failed'].append({'id': oid, 'message': '订单不存在'}); continue
            try:
                _ensure_order_store_scope(s, o, staff_store)
            except HTTPException as e:
                result['failed'].append({'id': oid, 'message': e.detail}); continue
            if o.status not in ('PAID', 'PENDING_SHIP'):
                result['failed'].append({'id': oid, 'message': '订单当前状态不可发货'}); continue
            o.status = 'SHIPPED'; o.express_company = str(payload.get('expressCompany') or '')
            o.express_no = str(payload.get('expressNo') or ''); o.shipped_at = datetime.now(UTC)
            result['success'].append({'id': o.id, 'status': o.status})
        s.commit()
    return ok(result)

@router.get("/member")
def member_list(request:Request):
    tid,_=merchant_ctx(request)
    with SessionLocal() as s:return ok([{"id":m.id,"memberNo":m.member_no,"nickname":m.nickname,"phoneMask":m.phone_mask,"pointsBalance":m.points_balance,"totalAmount":str(m.total_amount),"totalOrderCount":m.total_order_count,"levelId":m.level_id,"tags":m.tags or []} for m in s.query(MbMember).filter_by(tenant_id=tid).all()])

@router.get('/member/level')
def member_levels(request:Request):
    tid,_=merchant_ctx(request)
    with SessionLocal() as s:
        rows=s.query(MbLevel).filter_by(tenant_id=tid).order_by(MbLevel.level).all()
        if not rows:
            rows=[MbLevel(tenant_id=tid,level=1,name='普通会员',up_condition=None,up_value=0,discount_rate=100,points_rate=1,free_freight=0,benefits_desc=['注册即享']),MbLevel(tenant_id=tid,level=2,name='银卡会员',up_condition='TOTAL_AMOUNT',up_value=500,discount_rate=98,points_rate=1.1,free_freight=0,benefits_desc=['消费返积分']),MbLevel(tenant_id=tid,level=3,name='金卡会员',up_condition='TOTAL_AMOUNT',up_value=2000,discount_rate=95,points_rate=1.2,free_freight=1,benefits_desc=['免基础运费'])]
            s.add_all(rows); s.commit()
        return ok([{'id':x.id,'level':x.level,'name':x.name,'icon':x.icon,'upCondition':x.up_condition,'upValue':str(x.up_value),'discountRate':x.discount_rate,'pointsRate':str(x.points_rate),'freeFreight':x.free_freight,'benefitsDesc':x.benefits_desc or []} for x in rows])

@router.put('/member/level/{level_id}')
def member_level_update(level_id:int,payload:dict,request:Request):
    tid,_=merchant_ctx(request)
    with SessionLocal() as s:
        x=s.query(MbLevel).filter_by(id=level_id,tenant_id=tid).first()
        if not x: raise HTTPException(404,'等级不存在')
        for k,a in {'name':'name','icon':'icon','upCondition':'up_condition','upValue':'up_value','discountRate':'discount_rate','pointsRate':'points_rate','freeFreight':'free_freight','benefitsDesc':'benefits_desc'}.items():
            if k in payload:setattr(x,a,payload[k])
        s.commit(); return ok({'id':x.id,'name':x.name,'discountRate':x.discount_rate})

# 兼容 PRD 简写路径 /mc/level。
@router.get("/member/{member_id}")
def member_detail(member_id:int,request:Request):
    tid,_=merchant_ctx(request)
    with SessionLocal() as s:
        m=s.query(MbMember).filter_by(id=member_id,tenant_id=tid).first()
        if not m: raise NotFoundError("会员不存在")
        return ok({"id":m.id,"memberNo":m.member_no,"nickname":m.nickname,"phoneMask":m.phone_mask,"pointsBalance":m.points_balance,"totalAmount":str(m.total_amount),"totalOrderCount":m.total_order_count,"levelId":m.level_id,"tags":m.tags or []})

@router.get('/member/{member_id}/phone')
def member_phone(member_id: int, request: Request):
    """按 API 约定单独返回会员明文手机号，并强制记录审计。"""
    payload = get_auth_payload(request)
    if 'MEMBER_PHONE_FULL' not in (payload.get('perms') or []):
        raise HTTPException(403, '无权查看会员手机号')
    tid, _operator = merchant_ctx(request)
    with SessionLocal() as s:
        member = s.query(MbMember).filter_by(id=member_id, tenant_id=tid).first()
        if not member:
            raise NotFoundError('会员不存在')
        from app.services.audit import write_audit
        write_audit(s, action='MEMBER_PHONE_VIEW', target_type='MEMBER', target_id=str(member_id),
                    detail={'memberNo': member.member_no}, scope='merchant', tenant_id=tid)
        s.commit()
        return ok({'id': member.id, 'memberNo': member.member_no, 'phone': member.phone_enc})

@router.get('/member/{member_id}/orders')
def member_orders(member_id:int,request:Request):
    tid,_=merchant_ctx(request)
    with SessionLocal() as s:
        rows=s.query(OdOrder).filter_by(tenant_id=tid,member_id=member_id).order_by(OdOrder.id.desc()).all()
        return ok([{'id':o.id,'orderNo':o.order_no,'status':o.status,'amount':str(o.pay_amount),'createdAt':o.created_at.isoformat()} for o in rows])

@router.put('/member/{member_id}/tags')
def member_tags_update(member_id: int, payload: dict, request: Request):
    tid, _ = merchant_ctx(request)
    tags = payload.get('tags')
    if not isinstance(tags, list) or any(not isinstance(x, str) or len(x) > 30 for x in tags):
        raise ParamError(fields={'tags': '必须是字符串标签数组'})
    with SessionLocal() as s:
        m = s.query(MbMember).filter_by(id=member_id, tenant_id=tid).first()
        if not m: raise NotFoundError('会员不存在')
        m.tags = list(dict.fromkeys(tags))[:20]; s.commit()
        return ok({'id': m.id, 'tags': m.tags})

@router.get('/points/rule')
def points_rule(request:Request):
    tid,_=merchant_ctx(request)
    with SessionLocal() as s:
        r=s.query(MbPointsRule).filter_by(tenant_id=tid).first()
        if not r:r=MbPointsRule(tenant_id=tid);s.add(r);s.commit()
        return ok({'earnAmount':str(r.earn_amount),'earnPoints':r.earn_points,'expireMode':r.expire_mode,'expireMonths':r.expire_months})

@router.get('/level')
def levels_compat(request:Request):
    return member_levels(request)

@router.put('/level/{level_id}')
def level_update_compat(level_id:int,payload:dict,request:Request):
    return member_level_update(level_id,payload,request)

@router.put('/points/rule')
def points_rule_update(payload:dict,request:Request):
    tid,_=merchant_ctx(request)
    with SessionLocal() as s:
        r=s.query(MbPointsRule).filter_by(tenant_id=tid).first() or MbPointsRule(tenant_id=tid)
        r.earn_amount=payload.get('earnAmount',r.earn_amount);r.earn_points=payload.get('earnPoints',r.earn_points);r.expire_mode=payload.get('expireMode',r.expire_mode);r.expire_months=payload.get('expireMonths',r.expire_months);s.add(r);s.commit();return ok()

@router.get('/shop')
def shop_get(request:Request):
    tid,_=merchant_ctx(request)
    with SessionLocal() as s:
        x=s.query(McShop).filter_by(tenant_id=tid).first()
        if not x:x=McShop(tenant_id=tid,name='演示店铺');s.add(x);s.commit()
        return ok({'name':x.name,'phone':x.phone,'logo':x.logo,'notice':x.notice,'intro':x.intro,'banners':x.banners})

@router.put('/shop')
def shop_put(payload:dict,request:Request):
    tid,_=merchant_ctx(request)
    with SessionLocal() as s:
        for key in ('logo',):
            ref=payload.get(key)
            if isinstance(ref,str) and ref.startswith('/api/common/upload/file/'):
                if not s.query(SysFile).filter_by(tenant_id=tid,url=ref).first():
                    raise ParamError(fields={key:'上传文件不存在或不属于当前租户'})
        banners=payload.get('banners')
        if isinstance(banners,list):
            for item in banners:
                ref=item.get('img') if isinstance(item,dict) else item
                if isinstance(ref,str) and ref.startswith('/api/common/upload/file/') and not s.query(SysFile).filter_by(tenant_id=tid,url=ref).first():
                    raise ParamError(fields={'banners':'存在不属于当前租户的上传文件'})
        x=s.query(McShop).filter_by(tenant_id=tid).first() or McShop(tenant_id=tid,name=payload.get('name',''))
        for k in ('name','phone','logo','notice','intro','banners'):
            if k in payload:setattr(x,k,payload[k])
        s.add(x);s.commit();return ok()

@router.get('/member/{member_id}/points-log')
def member_points_log(member_id:int,request:Request):
    tid,_=merchant_ctx(request)
    with SessionLocal() as s:
        rows=s.query(MbPointsLog).filter_by(tenant_id=tid,member_id=member_id).order_by(MbPointsLog.id.desc()).all()
        return ok([{'id':x.id,'amount':x.amount,'balanceAfter':x.balance_after,'changeType':x.change_type,'remark':x.remark,'createdAt':x.created_at.isoformat()} for x in rows])

@router.get('/points/log')
def points_log(request:Request,memberId:int|None=None,pageNo:int=Query(1, ge=1),size:int=Query(20, ge=1, le=100)):
    tid,_=merchant_ctx(request)
    with SessionLocal() as s:
        q=s.query(MbPointsLog).filter_by(tenant_id=tid)
        if memberId:q=q.filter_by(member_id=memberId)
        total=q.count(); rows=q.order_by(MbPointsLog.id.desc()).offset((pageNo-1)*size).limit(size).all()
        return page([{'id':x.id,'memberId':x.member_id,'amount':x.amount,'balanceAfter':x.balance_after,'changeType':x.change_type,'remark':x.remark,'createdAt':x.created_at.isoformat()} for x in rows],total,pageNo,size)

@router.get('/refund')
def refund_list(request:Request,status:str|None=None):
    tid,_=merchant_ctx(request)
    with SessionLocal() as s:
        q=s.query(OdRefund).filter_by(tenant_id=tid)
        if status:q=q.filter_by(status=status)
        out=[]
        for r in q.order_by(OdRefund.id.desc()).all():
            o=s.query(OdOrder).filter_by(id=r.order_id,tenant_id=tid).first(); m=s.query(MbMember).filter_by(id=o.member_id,tenant_id=tid).first() if o else None
            out.append({'id':r.id,'refundNo':r.refund_no,'orderId':r.order_id,'orderNo':o.order_no if o else '','amount':str(r.refund_amount),'refundAmount':str(r.refund_amount),'status':r.status,'reason':r.reason_desc,'reasonDesc':r.reason_desc,'memberName':m.nickname if m else '','createdAt':r.created_at.isoformat()})
        return ok(out)

@router.get('/refund/{refund_id}')
def refund_detail(refund_id: int, request: Request):
    tid, _ = merchant_ctx(request)
    with SessionLocal() as s:
        r = s.query(OdRefund).filter_by(id=refund_id, tenant_id=tid).first()
        if not r: raise HTTPException(404, '退款单不存在')
        o = s.query(OdOrder).filter_by(id=r.order_id, tenant_id=tid).first()
        return ok({'id': r.id, 'refundNo': r.refund_no, 'orderId': r.order_id,
            'orderNo': o.order_no if o else '', 'refundAmount': str(r.refund_amount),
            'status': r.status, 'reasonCode': r.reason_code, 'reasonDesc': r.reason_desc,
            'rejectReason': r.reject_reason, 'createdAt': r.created_at.isoformat(),
            'auditAt': r.audit_at.isoformat() if r.audit_at else None})

@router.get('/refund/{refund_id}/rollback-preview')
def refund_rollback_preview(refund_id: int, request: Request):
    tid, _ = merchant_ctx(request)
    with SessionLocal() as s:
        r = s.query(OdRefund).filter_by(id=refund_id, tenant_id=tid).first()
        if not r: raise HTTPException(404, '退款单不存在')
        o = s.query(OdOrder).filter_by(id=r.order_id, tenant_id=tid).first()
        member = s.query(MbMember).filter_by(id=o.member_id, tenant_id=tid).first() if o else None
        earned = sum(x.amount for x in s.query(MbPointsLog).filter_by(
            tenant_id=tid, member_id=o.member_id if o else 0, ref_type='ORDER',
            ref_id=str(o.id if o else ''), change_type='ORDER_EARN').all())
        rollback = max(0, int(earned))
        balance = int(member.points_balance) if member else 0
        debt = max(0, rollback - balance)
        return ok({'orderEarnedPoints': rollback, 'currentBalance': balance,
            'rollbackPoints': rollback, 'debt': debt,
            'resultText': '积分余额足够，可全额回滚' if not debt else f'积分不足，将产生 {debt} 积分欠账'})

@router.post('/refund/{refund_id}/audit')
def refund_audit(refund_id:int,payload:dict,request:Request):
    tid,operator=merchant_ctx(request)
    with SessionLocal() as s:
        r=s.query(OdRefund).filter_by(id=refund_id,tenant_id=tid).first()
        if not r: raise HTTPException(404,'退款单不存在')
        approved=bool(payload.get('approved',False)); target='APPROVED' if approved else 'REJECTED'
        if r.status in ('APPROVED','REJECTED') or r.status==target:
            raise HTTPException(409,'退款已审核，不能重复操作')
        if r.status!='PENDING_AUDIT': raise HTTPException(409,'退款当前状态不可审核')
        o=s.query(OdOrder).filter_by(id=r.order_id,tenant_id=tid).first()
        now=datetime.now(UTC).replace(tzinfo=None);r.audit_staff_id=operator;r.audit_at=now
        if approved:
            items=s.query(OdOrderItem).filter_by(tenant_id=tid,order_id=r.order_id).all() if o else []
            if items:
                inventory.refund_return(s,[{'skuId':i.sku_id,'channel':i.channel,'qty':i.quantity} for i in items],o.order_no)
            member=s.query(MbMember).filter_by(id=o.member_id,tenant_id=tid).first() if o else None
            earned=sum(x.amount for x in s.query(MbPointsLog).filter_by(tenant_id=tid,member_id=o.member_id if o else 0,ref_type='ORDER',ref_id=str(o.id if o else ''),change_type='ORDER_EARN').all())
            rollback=max(0,int(earned));balance=int(member.points_balance) if member else 0;actual=min(balance,rollback);debt=rollback-actual
            if member:
                member.points_balance-=actual;member.points_debt+=debt
                s.add(MbPointsLog(tenant_id=tid,member_id=member.id,change_type='REFUND_ROLLBACK',amount=-actual,balance_after=member.points_balance,ref_type='REFUND',ref_id=str(r.id),remark='退款积分回滚',operator_id=operator))
            r.status='APPROVED';r.rollback_points=actual;r.rollback_debt=debt;r.wx_refund_id='FAKE-'+r.refund_no;r.finished_at=now
            if o:o.status='REFUNDED'
            s.query(OdVerifyCode).filter_by(tenant_id=tid,order_id=r.order_id,status='UNUSED').update({'status':'REFUNDED'})
        else:
            r.status='REJECTED';r.reject_reason=str(payload.get('rejectReason',''))
            if o and o.status=='REFUNDING':
                o.status=r.order_status_before if r.order_status_before else 'PAID'
        s.commit();return ok({'id':r.id,'status':r.status})

@router.post('/refund/{refund_id}/approve')
def refund_approve(refund_id:int, request:Request, payload:dict|None=None):
    return refund_audit(refund_id, {**(payload or {}), 'approved': True}, request)

@router.post('/refund/{refund_id}/reject')
def refund_reject(refund_id:int, request:Request, payload:dict|None=None):
    return refund_audit(refund_id, {**(payload or {}), 'approved': False}, request)

@router.post('/points/adjust')
def points_adjust(payload:dict,request:Request):
    try:
        member_id = int(payload.get('memberId'))
        points = int(payload.get('points', 0))
    except (TypeError, ValueError):
        return err(BizCode.PARAM_ERROR, "参数校验失败", {"fields": {"memberId": "必须是整数", "points": "必须是整数"}})
    # mb_points_log.ref_id 为 VARCHAR(40)。幂等键来自请求，必须在写库前
    # 校验，不能把 DB DataError 直接暴露成 500。
    idempotency_key = str(payload.get('idempotencyKey', payload.get('idempotency_key', ''))).strip()
    if not idempotency_key or len(idempotency_key) > 40:
        return err(BizCode.PARAM_ERROR, "参数校验失败", {
            "fields": {"idempotencyKey": "长度须为 1-40 个字符"}
        })
    return adjust_points(member_id, PointsIn(points=points,remark=payload.get('remark',''),idempotency_key=idempotency_key),request)

@router.post('/points/import')
async def points_import(request: Request, file: UploadFile = File(...)):
    raw = await file.read()
    try: rows = list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))
    except UnicodeDecodeError: raise ParamError(fields={'file': 'CSV必须为UTF-8'})
    if len(rows) > 5000: raise ParamError(fields={'file': '单次最多5000行'})
    result = {'total': len(rows), 'success': 0, 'fail': 0, 'failDetail': []}
    tid, operator = merchant_ctx(request)
    batch_key = (request.headers.get('Idempotency-Key') or '').strip()
    if not batch_key or len(batch_key) > 80: raise ParamError(fields={'Idempotency-Key': '必填且不超过80字符'})
    with SessionLocal() as s:
        old=s.query(MbPointsImport).filter_by(tenant_id=tid, idempotency_key=batch_key).first()
        if old: return ok({'batchId': old.id, 'status': old.status, 'total': old.total_rows, 'success': old.success_rows, 'fail': old.fail_rows, 'failDetail': old.fail_detail or []})
        batch = MbPointsImport(tenant_id=tid, idempotency_key=batch_key, file_name=file.filename or 'points.csv', total_rows=len(rows), operator_id=operator)
        s.add(batch); s.commit(); batch_id = batch.id
    for no, row in enumerate(rows, 2):
        try:
            key = str(row.get('idempotencyKey') or row.get('idempotency_key') or '').strip()
            if not key: raise ValueError('缺少幂等键')
            result_code = points_adjust({'memberId': row.get('memberId'), 'points': row.get('points'), 'remark': row.get('remark', ''), 'idempotencyKey': key}, request)
            if isinstance(result_code, dict) and result_code.get('code') not in (0, None): raise ValueError(result_code.get('message', '调整失败'))
            result['success'] += 1
        except Exception as exc:
            result['fail'] += 1; result['failDetail'].append({'row': no, 'message': str(exc)})
    with SessionLocal() as s:
        batch = s.get(MbPointsImport, batch_id)
        batch.success_rows=result['success']; batch.fail_rows=result['fail']; batch.fail_detail=result['failDetail']; batch.status='COMPLETED'; s.commit()
    return ok({**result, 'batchId': batch_id, 'status': 'COMPLETED'})

@router.get('/points/import/{batch_id}')
def points_import_result(batch_id: str, request: Request):
    tid, _ = merchant_ctx(request)
    with SessionLocal() as s:
        batch=s.query(MbPointsImport).filter_by(id=int(batch_id), tenant_id=tid).first()
        if not batch: raise NotFoundError('导入批次不存在')
        return ok({'batchId': batch.id, 'status': batch.status, 'total': batch.total_rows, 'success': batch.success_rows, 'fail': batch.fail_rows, 'failDetail': batch.fail_detail or []})

@router.get('/points/export')
def points_export(request: Request):
    tid, _ = merchant_ctx(request)
    with SessionLocal() as s:
        rows=s.query(MbPointsLog).filter_by(tenant_id=tid).order_by(MbPointsLog.id.desc()).all()
        out=io.StringIO(); w=csv.writer(out); w.writerow(['memberId','amount','balanceAfter','changeType','remark'])
        for x in rows: w.writerow([x.member_id,x.amount,x.balance_after,x.change_type,x.remark])
        return StreamingResponse(iter(['\ufeff'+out.getvalue()]), media_type='text/csv; charset=utf-8')

@router.get('/store')
def store_list(request:Request):
    tid,_=merchant_ctx(request)
    with SessionLocal() as s:return ok([{'id':x.id,'name':x.name,'address':x.address,'phone':x.phone,'status':x.status,'isPickup':x.is_pickup,'isVerify':x.is_verify} for x in s.query(McStore).filter(McStore.tenant_id==tid, McStore.deleted_at.is_(None)).all()])

@router.post('/store')
def store_create(payload:dict,request:Request):
    tid,_=merchant_ctx(request)
    with SessionLocal() as s:
        x=McStore(tenant_id=tid,name=payload.get('name',''),address=payload.get('address',''),phone=payload.get('phone',''),is_pickup=payload.get('isPickup',1),is_verify=payload.get('isVerify',1));s.add(x);s.commit();return ok({'id':x.id})

@router.put('/store/{store_id}')
def store_update(store_id:int,payload:dict,request:Request):
    tid,_=merchant_ctx(request)
    with SessionLocal() as s:
        x=s.query(McStore).filter(McStore.id==store_id, McStore.tenant_id==tid, McStore.deleted_at.is_(None)).first()
        if not x:raise HTTPException(404,'门店不存在')
        for k,a in {'name':'name','address':'address','phone':'phone','status':'status','isPickup':'is_pickup','isVerify':'is_verify'}.items():
            if k in payload:setattr(x,a,payload[k])
        s.commit();return ok()

@router.delete('/store/{store_id}')
def store_delete(store_id:int,request:Request):
    tid,_=merchant_ctx(request)
    with SessionLocal() as s:
        x=s.query(McStore).filter(McStore.id==store_id, McStore.tenant_id==tid, McStore.deleted_at.is_(None)).first()
        if not x:raise HTTPException(404,'门店不存在')
        x.deleted_at=datetime.now(UTC).replace(tzinfo=None);s.commit();return ok()

def adjust_points(member_id: int, req: PointsIn, request: Request):
    """积分调整核心逻辑。由 POST /points/adjust 调用，ref_id 存幂等号做去重。"""
    tid,operator=merchant_ctx(request)
    with SessionLocal() as s:
        m=s.query(MbMember).filter_by(id=member_id,tenant_id=tid).first()
        if not m: raise HTTPException(404,"会员不存在")
        if m.points_balance + req.points < 0: raise HTTPException(400,"积分余额不足")
        previous = s.query(MbPointsLog).filter_by(
            tenant_id=tid, ref_type="MANUAL", ref_id=req.idempotency_key
        ).first()
        if previous:
            if (previous.member_id, previous.amount, previous.remark) == (
                m.id, req.points, req.remark
            ):
                return ok({"memberId":previous.member_id, "pointsBalance":previous.balance_after})
            return err(BizCode.CONFLICT, "幂等号已用于其他请求")
        m.points_balance += req.points
        if req.points > 0: m.points_total_earn += req.points
        else: m.points_total_used += -req.points
        s.add(MbPointsLog(tenant_id=tid,member_id=m.id,change_type="MANUAL_ADJUST",amount=req.points,balance_after=m.points_balance,ref_type="MANUAL",ref_id=req.idempotency_key,remark=req.remark,operator_id=operator))
        s.commit(); return ok({"memberId":m.id,"pointsBalance":m.points_balance})
