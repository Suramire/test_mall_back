from fastapi import APIRouter,HTTPException,Request
from pydantic import BaseModel
from app.core.deps import get_auth_payload
from app.core.response import ok
from app.core.security import SCOPE_MERCHANT
from app.db.session import SessionLocal
from app.models.gd_goods import GdCategory,GdFreightTemplate
router=APIRouter(tags=['商家目录'])
class Name(BaseModel): name:str; channel:str='NORMAL'; mode:str='BY_PIECE'; firstFee:float=0; type:str|None=None; amount:float|None=None
def tid(r):
 """取租户 ID，并校验必须是商家端 token。

 本模块全部端点（分类/运费模板的增删改查）均为商家管理操作，
 此前缺少 scope 校验，任意有效 token 均可调用（含 DELETE）。
 返回签名保持为单个 int，避免改动 8 处调用点。
 """
 p=get_auth_payload(r)
 if p.get('scope')!=SCOPE_MERCHANT: raise HTTPException(403,'仅商家端可访问')
 return int(p['tid'])
@router.get('/category')
def cats(r:Request,channel:str='NORMAL'):
 with SessionLocal() as s:return ok([{'id':x.id,'name':x.name,'channel':x.channel,'parentId':x.parent_id} for x in s.query(GdCategory).filter_by(tenant_id=tid(r),channel=channel).all()])
@router.post('/category')
def cat_add(x:Name,r:Request):
 with SessionLocal() as s:
  o=GdCategory(tenant_id=tid(r),channel=x.channel,parent_id=0,name=x.name);s.add(o);s.commit();return ok({'id':o.id,'name':o.name,'channel':o.channel})
@router.delete('/category/{id}')
def cat_del(id:int,r:Request):
 with SessionLocal() as s:o=s.query(GdCategory).filter_by(id=id,tenant_id=tid(r)).first();s.delete(o);s.commit();return ok()
@router.put('/category/{id}')
def cat_put(id:int,x:Name,r:Request):
 with SessionLocal() as s:o=s.query(GdCategory).filter_by(id=id,tenant_id=tid(r)).first();o.name=x.name;s.commit();return ok({'id':id,'name':x.name})
@router.get('/freight-template')
def freights(r:Request):
 with SessionLocal() as s:return ok([{'id':x.id,'name':x.name,'mode':x.mode,'type':x.mode,'firstFee':str(x.first_fee),'amount':str(x.first_fee),'createdAt':x.created_at.isoformat()} for x in s.query(GdFreightTemplate).filter_by(tenant_id=tid(r)).all()])
@router.post('/freight-template')
def freight_add(x:Name,r:Request):
 with SessionLocal() as s:o=GdFreightTemplate(tenant_id=tid(r),name=x.name,mode=x.type or x.mode,first_fee=x.amount if x.amount is not None else x.firstFee);s.add(o);s.commit();return ok({'id':o.id,'name':o.name,'mode':o.mode,'type':o.mode,'firstFee':str(o.first_fee),'amount':str(o.first_fee)})
@router.put('/freight-template/{id}')
def freight_put(id:int,x:Name,r:Request):
 with SessionLocal() as s:o=s.query(GdFreightTemplate).filter_by(id=id,tenant_id=tid(r)).first();o.name=x.name;o.mode=x.type or x.mode;o.first_fee=x.amount if x.amount is not None else x.firstFee;s.commit();return ok()
@router.delete('/freight-template/{id}')
def freight_del(id:int,r:Request):
 with SessionLocal() as s:o=s.query(GdFreightTemplate).filter_by(id=id,tenant_id=tid(r)).first();s.delete(o);s.commit();return ok()
