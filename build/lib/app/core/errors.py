"""业务错误码定义。对齐 PRD §公共.10 与 03-API设计.md。

统一响应体 {code, message, data, traceId}；业务错误一律 HTTP 200，靠 code 区分。
"""
from __future__ import annotations


class BizCode:
    # 成功
    OK = 0

    # 通用参数/校验
    PARAM_ERROR = 40001  # 参数校验失败
    NOT_FOUND = 40400  # 资源不存在
    METHOD_NOT_ALLOWED = 40500
    CONFLICT = 40900  # 状态冲突

    # 认证鉴权 (40100~40399)
    UNAUTHORIZED = 40100  # 未登录/Token 无效
    LOGIN_FAILED = 40102  # 账号或密码错误
    TOKEN_EXPIRED = 40101  # Token 过期(前端触发 refresh)
    FORBIDDEN = 40301  # 无权限码
    FEATURE_NOT_OPEN = 40302  # 租户未开通该功能点
    IMPERSONATION_FORBIDDEN = 40303  # 代客态禁止

    # 租户状态 (41000~)
    TENANT_EXPIRED = 41001  # 租户到期
    TENANT_DISABLED = 41002  # 租户禁用
    TENANT_QUOTA_GOODS = 41003  # 商品配额超限
    TENANT_QUOTA_MEMBER = 41004  # 会员配额超限
    TENANT_QUOTA_STORE = 41005  # 门店配额超限
    TENANT_QUOTA_STAFF = 41006  # 员工配额超限

    # 商品/库存 (42000~)
    GOODS_SHELF_INFO_INCOMPLETE = 42004  # 商品信息不全无法上架
    CATEGORY_HAS_GOODS = 42006  # 分类下有商品禁止删除
    STOCK_INVALID_VALUE = 42007  # 库存变更值非法
    STOCK_NOT_ENOUGH = 42008  # 库存不足

    # 订单 (43000~)
    VIRTUAL_REFUND_FORBIDDEN = 43004  # 虚拟商品不可退款
    REFUND_DUPLICATE = 43005  # 重复申请退款
    ORDER_STATUS_INVALID = 43006
    ORDER_SKU_INVALID = 43007
    ORDER_GOODS_OFF_SALE = 43008
    ORDER_EXCEED_LIMIT = 43009  # 超出购买限制

    # 核销 (44000~)
    VERIFY_CODE_INVALID = 44001  # 核销码不存在
    VERIFY_CODE_USED = 44002  # 已使用
    VERIFY_CODE_EXPIRED = 44003  # 已过期
    VERIFY_STORE_MISMATCH = 44004  # 门店不符
    VERIFY_ORDER_UNPAID = 44005  # 订单未支付

    # 积分 (45000~)
    POINTS_NOT_ENOUGH = 45001
    POINTS_AMOUNT_INVALID = 45002
    POINTS_DEBT_EXISTS = 45003
    POINTS_ORDER_NOT_ALLOWED = 45004
    POINTS_IMPORT_TOO_LARGE = 45006  # 单批>5000行

    # 支付 (46000~)
    PAY_NOT_CONFIGURED = 46001  # 未配置支付
    PAY_ORDER_UNPAYABLE = 46002  # 订单不可支付
    PAY_FAILED = 46003  # 支付发起失败

    # 服务端 (50000~)
    INTERNAL_ERROR = 50000  # 未捕获异常，见 handlers.py
    NOT_IMPLEMENTED = 50001  # 接口已占位但功能未实现
