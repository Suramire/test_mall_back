from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from app.core.deps import require_perms
from app.core.response import ok
from app.db.session import SessionLocal
from app.models.pf_msg_template import PfMsgTemplate
from app.services.audit import write_audit
router=APIRouter(prefix="/msg-template",tags=["平台-消息模板"])
@router.get("")
def list_templates(_: None=Depends(require_perms("PF_MSG_TEMPLATE"))):
 with SessionLocal() as s:
  rows=s.scalars(select(PfMsgTemplate).where(PfMsgTemplate.deleted_at.is_(None)).order_by(PfMsgTemplate.id)).all()
  if not rows:
   with SessionLocal() as s:
    rows=[PfMsgTemplate(template_no="ORDER_PAID",name="订单支付通知",channel="WX",scene="ORDER_PAID",variables=["orderNo"],content="订单已支付",status="ENABLED"),PfMsgTemplate(template_no="ORDER_SHIPPED",name="发货提醒",channel="WX",scene="ORDER_SHIPPED",variables=["orderNo"],content="订单已发货",status="ENABLED")]
    s.add_all(rows); s.commit()
  return ok([{"id":x.id,"templateNo":x.template_no,"name":x.name,"channel":x.channel,"scene":x.scene,"variables":x.variables,"content":x.content,"status":x.status} for x in rows])

def _validate(payload: dict) -> None:
    for key in ("templateNo", "name", "channel", "content"):
        if not str(payload.get(key) or "").strip():
            raise HTTPException(400, f"{key}不能为空")
    if len(str(payload["templateNo"])) > 20:
        raise HTTPException(400, "templateNo长度不能超过20")

@router.post("")
def create_template(payload: dict, request: Request, _: None=Depends(require_perms("PF_MSG_TEMPLATE"))):
    _validate(payload)
    with SessionLocal() as s:
        if s.query(PfMsgTemplate).filter_by(template_no=payload["templateNo"], deleted_at=None).first():
            raise HTTPException(409, "模板编号已存在")
        x=PfMsgTemplate(template_no=payload["templateNo"], name=payload["name"], channel=payload["channel"], scene=payload.get("scene", ""), variables=payload.get("variables", []), content=payload["content"], status=payload.get("status", "ENABLED"))
        s.add(x); s.flush(); write_audit(s, action="MSG_TEMPLATE_CREATE", target_type="PF_MSG_TEMPLATE", target_id=str(x.id), detail={"templateNo":x.template_no}); s.commit()
        return ok({"id":x.id,"templateNo":x.template_no})

@router.put("/{template_id}")
def update_template(template_id: int, payload: dict, request: Request, _: None=Depends(require_perms("PF_MSG_TEMPLATE"))):
    with SessionLocal() as s:
        x=s.get(PfMsgTemplate, template_id)
        if not x or x.deleted_at: raise HTTPException(404, "模板不存在")
        if any(k in payload for k in ("templateNo", "name", "channel", "content")):
            _validate({"templateNo":payload.get("templateNo",x.template_no),"name":payload.get("name",x.name),"channel":payload.get("channel",x.channel),"content":payload.get("content",x.content)})
        for k,a in {"templateNo":"template_no","name":"name","channel":"channel","scene":"scene","variables":"variables","content":"content","status":"status"}.items():
            if k in payload: setattr(x,a,payload[k])
        write_audit(s, action="MSG_TEMPLATE_UPDATE", target_type="PF_MSG_TEMPLATE", target_id=str(x.id)); s.commit(); return ok({"id":x.id})

@router.post("/{template_id}/toggle-status")
def toggle_status(template_id: int, request: Request, _: None=Depends(require_perms("PF_MSG_TEMPLATE"))):
    with SessionLocal() as s:
        x=s.get(PfMsgTemplate, template_id)
        if not x or x.deleted_at: raise HTTPException(404, "模板不存在")
        x.status = "DISABLED" if x.status == "ENABLED" else "ENABLED"
        write_audit(s, action="MSG_TEMPLATE_STATUS", target_type="PF_MSG_TEMPLATE", target_id=str(x.id), detail={"status":x.status}); s.commit(); return ok({"id":x.id,"status":x.status})

@router.delete("/{template_id}")
def delete_template(template_id: int, request: Request, _: None=Depends(require_perms("PF_MSG_TEMPLATE"))):
    with SessionLocal() as s:
        x=s.get(PfMsgTemplate, template_id)
        if not x or x.deleted_at: raise HTTPException(404, "模板不存在")
        if x.template_no in {"ORDER_PAID", "ORDER_SHIPPED"}: raise HTTPException(403, "系统模板不可删除")
        from datetime import UTC, datetime
        x.deleted_at=datetime.now(UTC).replace(tzinfo=None); write_audit(s, action="MSG_TEMPLATE_DELETE", target_type="PF_MSG_TEMPLATE", target_id=str(x.id)); s.commit(); return ok()
