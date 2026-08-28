"""多租户 ORM 强制注入（事件监听实现，可靠且无侵入）。

机制：
- do_orm_execute: 对每个 SELECT 查询，若目标模型注册为业务表(有 tenant_id)，
  则自动注入 WHERE tenant_id = 当前上下文值；缺失租户上下文则 Fail-Fast。
- before_flush: INSERT/UPDATE 时自动填充 tenant_id / 拒绝跨租户写入。

注意：do_orm_execute / before_flush 均为 Session 作用域事件，必须挂到
Session（或 sessionmaker）而非 Engine，挂到 Engine 会 AttributeError。

用法：
  from app.db.orm_hooks import install_tenant_hooks
  install_tenant_hooks()   # 在 app 启动时安装一次（幂等）

业务模型定义后需调用 register_tenant_model(Model)。
"""
from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.tenant_context import TenantContextMissingError, get_tenant_id

# 注册为"业务表"（含 tenant_id 需隔离）的模型
_TENANT_MODELS: list[type] = []

# 明确豁免 tenant 过滤的场景（跨租户扫描，如订单关单定时任务）
# 通过 query.execution_options(skip_tenant_filter=True) 标记
SKIP_OPTION = "skip_tenant_filter"


def register_tenant_model(model: type) -> None:
    """注册业务模型（其 tenant_id 列将被强制注入）。"""
    if model not in _TENANT_MODELS:
        _TENANT_MODELS.append(model)


def _collect_entities(statement):
    """从 statement 中收集将被查询/写入的实体类型。"""
    from sqlalchemy.orm import Query, selectinload
    from sqlalchemy.sql.selectable import Select

    entities = []
    if isinstance(statement, Select):
        for col in statement.columns_clause_froms:
            pass
        # 遍历 column_descriptions 不可靠，改用核心字段
    return entities


def _inject_tenant_filter(execute_state) -> None:
    """SELECT 注入 tenant_id 过滤。

    注意：不要为了让某个查询跨租户而摘掉这个装饰器 —— 那会让全局租户隔离失效。
    需要跨租户时用 `.execution_options(skip_tenant_filter=True)`（见下方 SKIP_OPTION 分支）。
    """
    if not _TENANT_MODELS:
        return
    if execute_state.execution_options.get(SKIP_OPTION):
        return

    statement = execute_state.statement
    tenant_models = getattr(statement, "_tenant_filter_models", None)
    # 从 statement 中解析涉及的租户模型
    involved = _involved_tenant_models(statement)
    if not involved:
        return

    tid = get_tenant_id()
    if tid is None:
        raise TenantContextMissingError(
            "查询涉及业务表但缺少租户上下文(tenant_id)"
        )
    from sqlalchemy import and_

    criteria = and_(*[m.tenant_id == tid for m in involved])
    execute_state.statement = statement.where(criteria)


def _involved_tenant_models(statement):
    """递归找出 SELECT 中引用的已注册业务模型。"""
    models = set()
    for ent in getattr(statement, "entities", []) or []:
        cls = getattr(ent, "entity", None) or getattr(ent, "class_", None)
        if cls and any(cls is m for m in _TENANT_MODELS):
            models.add(cls)
    # 处理 select(Model) 与 select(*columns) 两种形态
    for col in getattr(statement, "column_descriptions", []) or []:
        ent = col.get("entity")
        if ent and any(ent is m for m in _TENANT_MODELS):
            models.add(ent)
    return list(models)


def _inject_tenant_on_flush(session, flush_context, instances) -> None:
    """INSERT/UPDATE 自动填充 tenant_id / 拒绝跨租户。"""
    from app.core.exceptions import ForbiddenError

    tid = get_tenant_id()
    for target in list(session.new):
        if not any(target.__class__ is m for m in _TENANT_MODELS):
            continue
        if tid is None:
            raise TenantContextMissingError("写入业务表缺少租户上下文(tenant_id)")
        existing = target.tenant_id
        if existing is not None and existing != tid:
            raise ForbiddenError("禁止跨租户写入")
        target.tenant_id = tid

    for target in list(session.dirty):
        if not any(target.__class__ is m for m in _TENANT_MODELS):
            continue
        if tid is None:
            raise TenantContextMissingError("更新业务表缺少租户上下文(tenant_id)")
        if target.tenant_id != tid:
            raise ForbiddenError("禁止跨租户写入")


_INSTALLED = False


def install_tenant_hooks() -> None:
    """安装多租户 Session 事件监听（幂等，全局仅挂一次）。

    do_orm_execute / before_flush 是 Session 级事件，挂到 Engine 会 AttributeError。
    """
    global _INSTALLED
    if _INSTALLED:
        return
    event.listen(Session, "do_orm_execute", _inject_tenant_filter)
    event.listen(Session, "before_flush", _inject_tenant_on_flush)
    _INSTALLED = True
