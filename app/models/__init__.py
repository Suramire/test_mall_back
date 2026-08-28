"""模型注册聚合。import 本模块即触发所有 ORM 模型注册到 Base.metadata。"""
from __future__ import annotations

from app.models.pf_tenant import PfTenant
from app.models.pf_feature import PfFeature
from app.models.pf_tenant_feature import PfTenantFeature
from app.models.pf_role import PfRole
from app.models.pf_staff import PfStaff
from app.models.pf_audit_log import PfAuditLog
from app.models.pf_msg_template import PfMsgTemplate, PfSequence
from app.models.mc_staff import McStaff

# --- 业务域模型 (T-021, owner=be-dev-2) ---
from app.models.mc_config import (
    McMsgConfig,
    McNotice,
    McPayConfig,
    McRole,
    McShop,
    McStore,
)
from app.models.gd_goods import (
    GdCategory,
    GdFreightTemplate,
    GdGoods,
    GdSku,
    GdSkuStock,
    GdStockLog,
)
from app.models.mb_member import (
    MbLevel,
    MbMember,
    MbPointsGrant,
    MbPointsImport,
    MbPointsLog,
    MbPointsRule,
)
from app.models.od_order import (
    OdAddress,
    OdCart,
    OdOrder,
    OdOrderItem,
    OdPayment,
    OdRefund,
    OdVerifyCode,
)
from app.models.sys_common import SysExportTask, SysFile

__all__ = [
    "PfTenant",
    "PfFeature",
    "PfTenantFeature",
    "PfRole",
    "PfStaff",
    "PfAuditLog",
    "PfMsgTemplate",
    "PfSequence",
    "McStaff",
    # 商家配置域
    "McRole",
    "McShop",
    "McStore",
    "McPayConfig",
    "McMsgConfig",
    "McNotice",
    # 商品域
    "GdCategory",
    "GdGoods",
    "GdSku",
    "GdSkuStock",
    "GdStockLog",
    "GdFreightTemplate",
    # 会员积分域
    "MbLevel",
    "MbMember",
    "MbPointsRule",
    "MbPointsGrant",
    "MbPointsLog",
    "MbPointsImport",
    # 交易域
    "OdOrder",
    "OdOrderItem",
    "OdPayment",
    "OdRefund",
    "OdVerifyCode",
    "OdCart",
    "OdAddress",
    # 系统域
    "SysExportTask",
    "SysFile",
]
