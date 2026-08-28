import time

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.core.errors import BizCode
from app.core.redis import blacklist_token, bump_token_version, get_kv, get_redis
from app.core.response import err, ok
from app.core.security import (
    SCOPE_MERCHANT,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    token_version_valid,
    verify_password,
)
from app.db.session import SessionLocal
from app.models.mc_staff import McStaff
from app.models.pf_tenant import PfTenant

router=APIRouter(prefix="/auth",tags=["商家认证"])
ACCESS_EXPIRES_IN = 7200

class Login(BaseModel): account:str; password:str

class RefreshIn(BaseModel):
    refreshToken: str

class PasswordIn(BaseModel):
    oldPassword: str
    newPassword: str
    confirmPassword: str | None = None


def _new_access_token(subject: str, tenant_id: int, impersonating: bool = False) -> str:
    """商家端 accessToken：typ=access，短期(JWT_ACCESS_EXPIRE=7200)。"""
    return create_access_token(
        subject=subject,
        scope=SCOPE_MERCHANT,
        tenant_id=tenant_id,
        perms=['MC_ALL'],
        extra={'typ': 'access', 'impersonating': impersonating},
    )


def _issue_tokens(u: McStaff, impersonating: bool = False) -> dict:
    """签发双 token：access 短期(typ=access)，refresh 长期(7天,typ=refresh,带 tid)。

    两者是独立签发结果（不同 jti/exp/typ），不再复用同一串。
    """
    with SessionLocal() as s:
        t=s.get(PfTenant, u.tenant_id)
        tenant={'id':u.tenant_id,'tenantNo':t.tenant_no if t else '', 'name':t.name if t else '', 'status':t.status if t else ''}
    return {
        'accessToken': _new_access_token(str(u.id), u.tenant_id, impersonating),
        'refreshToken': create_refresh_token(
            subject=str(u.id), scope=SCOPE_MERCHANT, tenant_id=u.tenant_id
        ),
        'expiresIn': ACCESS_EXPIRES_IN,
        'staff': {'id':u.id,'account':u.account,'name':u.name}, 'tenant': tenant, 'perms':['MC_ALL'], 'features':[], 'impersonating': impersonating,
    }
@router.post('/login')
def login(req:Login):
 with SessionLocal() as s:
  from app.core.tenant_context import set_tenant
  # 登录时尚不知租户，但 McStaff 是业务表会被 ORM 强制注入 tenant_id 过滤。
  # 用 skip_tenant_filter 跨租户按 account 唯一查到该员工，再以其真实 tenant_id
  # 设立租户上下文 —— 这样租户 2..N 的商家也能登录到自己的数据域。
  u = (
    s.query(McStaff)
    .filter_by(account=req.account)
    .execution_options(skip_tenant_filter=True)
    .first()
  )
  if not u or not verify_password(req.password,u.password_hash) or u.status!='ENABLED': return err(BizCode.LOGIN_FAILED,'账号或密码错误')
  set_tenant(u.tenant_id)
  return ok(_issue_tokens(u))

@router.post('/wecom-login')
def wecom_login(req: dict):
    """企微 code 登录适配器；fake code 仅在显式 WECOM_FAKE_LOGIN 开启时可用。"""
    code = str(req.get('code') or '').strip()
    if not code: return err(BizCode.PARAM_ERROR, 'code不能为空')
    if not settings.WECOM_FAKE_LOGIN:
        return err(BizCode.PARAM_ERROR, '企微登录服务未配置')
    userid = code.removeprefix('fake:')
    with SessionLocal() as s:
        u=s.query(McStaff).execution_options(skip_tenant_filter=True).filter_by(wecom_userid=userid, status='ENABLED').first()
        if not u: return err(BizCode.LOGIN_FAILED, '企微账号未绑定或已禁用')
        from app.core.tenant_context import set_tenant
        set_tenant(u.tenant_id)
        return ok(_issue_tokens(u))
@router.get('/me')
def me(request:Request):
 p=request.state.auth
 with SessionLocal() as s:
  u=s.get(McStaff,int(p['sub'])); t=s.get(PfTenant,int(p['tid']))
  return ok({'staff':{'id':int(p['sub']),'account':u.account if u else '','name':u.name if u else ''},'tenant':{'id':int(p['tid']),'tenantNo':t.tenant_no if t else '','name':t.name if t else '','status':t.status if t else ''},'perms':p.get('perms',[]),'features':[]})
@router.post('/logout')
def logout(request: Request):
    jti = request.state.auth.get('jti')
    if not jti:
        return err(BizCode.UNAUTHORIZED, 'Token 无效')
    blacklist_token(jti)
    return ok()
@router.post('/refresh')
def refresh(req: RefreshIn):
    """用 refreshToken 换新的 accessToken。

    契约（team-lead 锁定）：
    - refreshToken 从 **请求体** 读，不从 Authorization 头读；
    - 解码时忽略过期（access 过期是刷新的正常前提，refresh 自身的过期单独判）；
    - 失败一律 HTTP 200 + 业务码 40101，不抛 HTTPException（前端据此跳登录）。
    """
    try:
        p = decode_token(req.refreshToken, verify_exp=False)
    except Exception:
        return err(BizCode.TOKEN_EXPIRED, '刷新令牌无效')
    # 密码重置后旧 token 吊销：refresh 同样受版本约束
    if not token_version_valid(p):
        return err(BizCode.TOKEN_EXPIRED, '登录态已失效，请重新登录')
    # 必须是 refresh 型，杜绝拿 accessToken 来换新 token 的无限续期
    if p.get('typ') != 'refresh':
        return err(BizCode.TOKEN_EXPIRED, '刷新令牌无效')
    # 端隔离：商家端只认 merchant scope
    if p.get('scope') != SCOPE_MERCHANT:
        return err(BizCode.TOKEN_EXPIRED, '刷新令牌无效')
    # refresh 自身过期要拒（上面 verify_exp=False 关掉了库层校验，这里手工判）
    exp = p.get('exp')
    if not exp or int(exp) <= int(time.time()):
        return err(BizCode.TOKEN_EXPIRED, '刷新令牌已过期')
    tid = p.get('tid')
    sub = p.get('sub')
    if tid is None or sub is None:
        return err(BizCode.TOKEN_EXPIRED, '刷新令牌无效')
    # 回查账号状态：禁用/删除的员工不得续期
    with SessionLocal() as s:
        from app.core.tenant_context import set_tenant
        set_tenant(int(tid))
        u = s.get(McStaff, int(sub))
        if not u or u.status != 'ENABLED':
            return err(BizCode.TOKEN_EXPIRED, '账号不可用')
        return ok({'accessToken': _new_access_token(str(u.id), u.tenant_id),
                   'expiresIn': ACCESS_EXPIRES_IN})
@router.post('/sso')
def sso(req: dict):
    """消费平台代登录 ticket，一次性兑换商家管理员 JWT。"""
    ticket=str(req.get('ticket') or '')
    tid=get_kv(f'imp:{ticket}') if ticket else None
    if not tid: return err(BizCode.UNAUTHORIZED,'代登录链接已失效或已使用')
    try: get_redis().delete(f'imp:{ticket}')
    except Exception: pass
    from app.core.tenant_context import set_tenant
    set_tenant(int(tid))
    with SessionLocal() as s:
        u=s.query(McStaff).filter_by(tenant_id=int(tid),is_admin=1,status='ENABLED').first()
        if not u: return err(BizCode.UNAUTHORIZED,'租户没有可用管理员')
        return ok(_issue_tokens(u, impersonating=True))
@router.put('/password')
def password(req: PasswordIn, request:Request):
    """修改当前登录员工密码：校验旧密码 → 写入新 hash。"""
    p = request.state.auth
    if len(req.newPassword) < 6:
        return err(BizCode.PARAM_ERROR, '新密码至少 6 位')
    if req.confirmPassword is not None and req.confirmPassword != req.newPassword:
        return err(BizCode.PARAM_ERROR, '两次输入的新密码不一致')
    with SessionLocal() as s:
        from app.core.tenant_context import set_tenant
        set_tenant(int(p['tid']))
        u = s.get(McStaff, int(p['sub']))
        if not u:
            return err(BizCode.UNAUTHORIZED, '账号不存在')
        if not verify_password(req.oldPassword, u.password_hash):
            return err(BizCode.PARAM_ERROR, '原密码错误')
        u.password_hash = hash_password(req.newPassword)
        u.pwd_reset_required = 0
        s.commit()
    # 密码变更：自增 token 版本，使该员工的旧 access/refresh token 立即失效
    bump_token_version(SCOPE_MERCHANT, str(p['sub']))
    # 前端改密后应主动重新登录
    return ok()
