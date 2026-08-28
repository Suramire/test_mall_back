"""商家 PC 端商品域 API：/api/mc/goods/**。

对应 docs/architecture/03-API设计.md §3.3（T-032 P0）：
  GET    /goods                 列表（keyword/type/channel/status/categoryId；库存列=两渠道总和）
  GET    /goods/{id}            详情（skus[] + 双渠道库存 + ticketConfig）
  POST   /goods                 新增（含 SKU 数组、渠道配置、核销券配置）；配额校验 41003
  PUT    /goods/{id}            编辑
  DELETE /goods/{id}            软删（P1，兼容既有前端）
  POST   /goods/{id}/shelf      按渠道独立上下架；信息不全 42004
  PUT    /goods/{id}/stock      库存三模式调整（INCREASE/DECREASE/SET）；42007/42008
  GET    /goods/{id}/stock-log  库存变更日志（P1）

约定（T-001）：service 层同步 def，本层 async + run_in_threadpool 桥接；
租户上下文由 TenantGuard 中间件写入 ContextVar（anyio 会复制到线程池）。
鉴权沿用现有 mc 员工链路：scope=merchant（中间件按前缀强制）+ JWT perms 守卫，
线上 mc 登录统一签发 MC_ALL（见 api/mc_auth.py），故每个权限码与 MC_ALL 并列。
"""
from __future__ import annotations

import csv
import io
from fastapi import APIRouter, Depends, Query, File, UploadFile
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.core.deps import require_merchant, require_perms
from app.core.response import ok
from app.core.response import page as page_response
from app.db.session import SessionLocal
from app.services import goods as goods_svc
from app.services import inventory as inventory_svc

router = APIRouter(tags=["商家端-商品库存"])

@router.post("/goods/batch-status")
async def goods_batch_status(body: dict, payload: dict = Depends(require_merchant), _: None = Depends(require_perms("GOODS_EDIT", "MC_ALL"))):
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids or body.get("status") not in ("ON_SALE", "OFF_SALE"):
        from app.core.exceptions import ParamError
        raise ParamError(fields={"ids/status": "ids不能为空且status仅支持ON_SALE/OFF_SALE"})
    channel = body.get("channel", "BOTH")
    if channel not in ("NORMAL", "POINTS", "BOTH"):
        from app.core.exceptions import ParamError
        raise ParamError(fields={"channel": "仅支持NORMAL/POINTS/BOTH"})
    def _work():
        with SessionLocal() as session:
            result = {"success": [], "failed": []}
            for gid in ids:
                try:
                    result["success"].append(goods_svc.shelf_goods(session, int(gid), channel, body["status"] == "ON_SALE"))
                except Exception as exc:
                    session.rollback(); result["failed"].append({"id": gid, "message": str(exc)})
            return result
    return ok(await run_in_threadpool(_work))

@router.get("/goods/export")
async def goods_export(payload: dict = Depends(require_merchant), _: None = Depends(require_perms("GOODS_LIST", "MC_ALL"))):
    def _work():
        with SessionLocal() as session:
            rows, _ = goods_svc.list_goods(session, page=1, size=10000)
            out = io.StringIO(); writer = csv.writer(out); writer.writerow(["id", "name", "type", "channel", "status", "sort", "virtualSold"])
            for row in rows: writer.writerow([row.get(k, "") for k in ("id", "name", "type", "channel", "status", "sort", "virtualSold")])
            return "\ufeff" + out.getvalue()
    return StreamingResponse(iter([await run_in_threadpool(_work)]), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=goods.csv"})

@router.post("/goods/import")
async def goods_import(file: UploadFile = File(...), payload: dict = Depends(require_merchant), _: None = Depends(require_perms("GOODS_CREATE", "MC_ALL"))):
    raw = await file.read()
    try: text = raw.decode("utf-8-sig")
    except UnicodeDecodeError: from app.core.exceptions import ParamError; raise ParamError(fields={"file": "CSV必须为UTF-8"})
    reader = csv.DictReader(io.StringIO(text)); rows = list(reader)
    if len(rows) > 5000: from app.core.exceptions import ParamError; raise ParamError(fields={"file": "单次最多5000行"})
    result = {"total": len(rows), "success": 0, "fail": 0, "failDetail": []}
    with SessionLocal() as session:
        for no, row in enumerate(rows, 2):
            try:
                name = str(row.get("name") or "").strip()
                if not name or name.startswith(("=", "+", "-", "@")): raise ValueError("商品名称非法")
                goods_svc.create_goods(session, {"name": name, "type": row.get("type") or "PHYSICAL", "channel": row.get("channel") or "NORMAL", "detail": row.get("detail") or ""})
                result["success"] += 1
            except Exception as exc:
                session.rollback(); result["fail"] += 1; result["failDetail"].append({"row": no, "message": str(exc)})
    return ok(result)


@router.get("/goods")
async def goods_list(
    keyword: str | None = None,
    type: str | None = Query(None, alias="type"),
    channel: str | None = None,
    status: str | None = None,
    categoryId: int | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    payload: dict = Depends(require_merchant),  # noqa: B008
    _: None = Depends(require_perms("GOODS_LIST", "MC_ALL")),
):
    def _work():
        with SessionLocal() as session:
            return goods_svc.list_goods(
                session,
                keyword=keyword,
                type_=type,
                channel=channel,
                status=status,
                category_id=categoryId,
                page=page,
                size=size,
            )

    rows, total = await run_in_threadpool(_work)
    return page_response(rows, total, page, size)


@router.get("/goods/{goods_id}")
async def goods_detail(
    goods_id: int,
    payload: dict = Depends(require_merchant),  # noqa: B008
    _: None = Depends(require_perms("GOODS_LIST", "MC_ALL")),
):
    def _work():
        with SessionLocal() as session:
            return goods_svc.get_goods(session, goods_id)

    return ok(await run_in_threadpool(_work))

@router.get("/goods/{goods_id}/detail")
async def goods_detail_compat(
    goods_id: int,
    payload: dict = Depends(require_merchant),
    _: None = Depends(require_perms("GOODS_LIST", "MC_ALL")),
):
    return await goods_detail(goods_id, payload, _)


@router.post("/goods")
async def goods_create(
    body: dict,
    payload: dict = Depends(require_merchant),  # noqa: B008
    _: None = Depends(require_perms("GOODS_CREATE", "MC_ALL")),
):
    def _work():
        with SessionLocal() as session:
            return goods_svc.create_goods(session, body)

    return ok(await run_in_threadpool(_work))


@router.put("/goods/{goods_id}")
async def goods_update(
    goods_id: int,
    body: dict,
    payload: dict = Depends(require_merchant),  # noqa: B008
    _: None = Depends(require_perms("GOODS_EDIT", "MC_ALL")),
):
    def _work():
        with SessionLocal() as session:
            return goods_svc.update_goods(session, goods_id, body)

    return ok(await run_in_threadpool(_work))


@router.delete("/goods/{goods_id}")
async def goods_delete(
    goods_id: int,
    payload: dict = Depends(require_merchant),  # noqa: B008
    _: None = Depends(require_perms("GOODS_DELETE", "MC_ALL")),
):
    def _work():
        with SessionLocal() as session:
            goods_svc.delete_goods(session, goods_id)
            return

    await run_in_threadpool(_work)
    return ok()


@router.post("/goods/{goods_id}/shelf")
async def goods_shelf(
    goods_id: int,
    body: dict,
    payload: dict = Depends(require_merchant),  # noqa: B008
    _: None = Depends(require_perms("GOODS_SHELF", "MC_ALL")),
):
    """★按渠道独立上下架（R-CH-04）：{channel, onSale}。"""
    channel = str(body.get("channel") or "NORMAL")
    on_sale = bool(body.get("onSale", body.get("on_sale", False)))

    def _work():
        with SessionLocal() as session:
            return goods_svc.shelf_goods(session, goods_id, channel, on_sale)

    return ok(await run_in_threadpool(_work))


@router.put("/goods/{goods_id}/stock")
async def goods_stock_adjust(
    goods_id: int,
    body: dict,
    payload: dict = Depends(require_merchant),  # noqa: B008
    _: None = Depends(require_perms("GOODS_STOCK", "MC_ALL")),
):
    """库存三模式调整：{items:[{skuId,channel,changeType,value}]}；42007/42008。

    兼容旧前端的扁平单体请求（无 items 时整个 body 视为一条明细）。
    """
    items = body.get("items") if isinstance(body.get("items"), list) else [body]

    def _work():
        with SessionLocal() as session:
            rows = inventory_svc.adjust_stock(
                session,
                goods_id,
                items,
                operator_id=int(payload.get("sub") or 0) or None,
            )
            session.commit()
            return rows

    return ok({"items": await run_in_threadpool(_work)})


@router.get("/goods/{goods_id}/stock-log")
async def goods_stock_log(
    goods_id: int,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    payload: dict = Depends(require_merchant),  # noqa: B008
    _: None = Depends(require_perms("GOODS_STOCK", "MC_ALL")),
):
    def _work():
        with SessionLocal() as session:
            return goods_svc.stock_log_of_goods(session, goods_id, page, size)

    rows, total = await run_in_threadpool(_work)
    return page_response(rows, total, page, size)
