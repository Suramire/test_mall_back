"""平台功能点字典（68 项）。

来源：prd/00-公共基础与数据字典.md §附录 A。
每行：(code, end_code, l1_name, l2_name, l3_name, description, default_on)

default_on：开户时默认勾选。user 端基础浏览/交易/会员默认开；
merchant_pc 核心经营（商品/订单/核销/会员）默认开；高级（积分商城/导入/运费/门店/消息）默认关。
merchant_mp 核心（核销/订单/商品/会员查询）默认开。
"""

# (code, end, l1, l2, l3, desc, default_on)
FEATURES: list[tuple] = [
    # ---- user 端（21）----
    ("user.browse.goods.home", "user", "商城浏览", "商品", "首页推荐", "首页商品推荐、活动横幅与金刚区入口", 1),
    ("user.browse.goods.list", "user", "商城浏览", "商品", "商品列表", "按分类/销量/价格筛选排序", 1),
    ("user.browse.goods.category", "user", "商城浏览", "商品", "商品分类", "多级分类导航与筛选", 1),
    ("user.browse.goods.search", "user", "商城浏览", "商品", "商品搜索", "关键词搜索，结果与联想", 1),
    ("user.browse.goods.detail", "user", "商城浏览", "商品", "商品详情", "图文详情、规格选择、收藏/分享", 1),
    ("user.trade.cart.add", "user", "交易下单", "购物车", "加入购物车", "角标实时计数", 1),
    ("user.trade.cart.manage", "user", "交易下单", "购物车", "购物车管理", "改数量/删除/全选/批量结算；下单锁库存", 1),
    ("user.trade.confirm.order", "user", "交易下单", "确认订单", "订单确认", "收货地址、运费、优惠券/积分抵扣", 1),
    ("user.trade.confirm.address", "user", "交易下单", "确认订单", "收货地址", "增删改、设为默认", 1),
    ("user.trade.pay.wechat", "user", "交易下单", "支付", "微信支付", "调起微信支付，失败可重试", 1),
    ("user.order.my.list", "user", "订单中心", "我的订单", "订单列表", "10 状态筛选", 1),
    ("user.order.my.detail", "user", "订单中心", "我的订单", "订单详情", "商品、金额、状态与时间节点", 1),
    ("user.order.my.logistics", "user", "订单中心", "我的订单", "物流跟踪", "实物订单配送轨迹", 1),
    ("user.order.my.refund", "user", "订单中心", "我的订单", "申请退款", "退款申请、原因、进度", 1),
    ("user.points.center.balance", "user", "积分中心", "积分", "积分余额与明细", "可用积分、获取/消耗流水", 1),
    ("user.points.center.mall", "user", "积分中心", "积分", "积分商城", "兑换商品列表与详情", 0),
    ("user.points.center.exchange", "user", "积分中心", "积分", "积分兑换", "生成兑换记录", 0),
    ("user.member.center.info", "user", "会员中心", "会员", "会员信息", "资料、等级与专属权益", 1),
    ("user.member.center.level", "user", "会员中心", "会员", "会员等级权益", "成长进度、等级折扣、积分倍率", 1),
    ("user.help.service.contact", "user", "帮助服务", "客服", "联系客服", "在线客服/留言", 1),
    # ---- merchant_pc 端（39）----
    ("merchant_pc.dashboard.home.board", "merchant_pc", "数据概览", "首页", "经营数据看板", "", 1),
    ("merchant_pc.goods.list.list", "merchant_pc", "商品管理", "商品列表", "商品列表", "", 1),
    ("merchant_pc.goods.list.create", "merchant_pc", "商品管理", "商品列表", "新增商品（实物/虚拟/核销券）", "", 1),
    ("merchant_pc.goods.list.import", "merchant_pc", "商品管理", "商品列表", "商品导入（Excel）", "", 0),
    ("merchant_pc.goods.list.shelf", "merchant_pc", "商品管理", "商品列表", "上下架管理", "", 1),
    ("merchant_pc.goods.category.manage", "merchant_pc", "商品管理", "分类管理", "商品分类", "", 1),
    ("merchant_pc.goods.freight.template", "merchant_pc", "商品管理", "运费模板", "运费模板", "", 0),
    ("merchant_pc.member.list.list", "merchant_pc", "会员管理", "会员列表", "会员列表", "", 1),
    ("merchant_pc.member.list.detail", "merchant_pc", "会员管理", "会员列表", "会员详情", "", 1),
    ("merchant_pc.member.level.config", "merchant_pc", "会员管理", "等级设置", "会员等级", "", 1),
    ("merchant_pc.points.rule.config", "merchant_pc", "积分管理", "积分规则", "积分规则", "", 1),
    ("merchant_pc.points.log.query", "merchant_pc", "积分管理", "积分流水", "积分流水", "", 1),
    ("merchant_pc.points.adjust.manual", "merchant_pc", "积分管理", "积分调整", "手动调整积分", "", 1),
    ("merchant_pc.points.adjust.import", "merchant_pc", "积分管理", "积分调整", "批量导入积分", "", 0),
    ("merchant_pc.points.mall.goods", "merchant_pc", "积分管理", "积分商城", "积分商品", "", 0),
    ("merchant_pc.points.mall.category", "merchant_pc", "积分管理", "积分商城", "积分分类", "", 0),
    ("merchant_pc.points.mall.order", "merchant_pc", "积分管理", "积分商城", "积分订单", "", 0),
    ("merchant_pc.order.list.list", "merchant_pc", "订单管理", "订单列表", "订单列表", "", 1),
    ("merchant_pc.order.list.detail", "merchant_pc", "订单管理", "订单列表", "订单详情", "", 1),
    ("merchant_pc.order.list.ship", "merchant_pc", "订单管理", "订单列表", "实物发货（支持扫码录单）", "", 1),
    ("merchant_pc.order.list.pickup", "merchant_pc", "订单管理", "订单列表", "自提订单", "", 1),
    ("merchant_pc.order.list.refund", "merchant_pc", "订单管理", "订单列表", "退款审核（积分回滚预览）", "", 1),
    ("merchant_pc.verify.do.manual", "merchant_pc", "核销管理", "核销", "手动核销", "", 1),
    ("merchant_pc.verify.do.log", "merchant_pc", "核销管理", "核销", "核销记录", "", 1),
    ("merchant_pc.setting.shop.info", "merchant_pc", "系统设置", "店铺信息", "店铺信息", "", 1),
    ("merchant_pc.setting.store.manage", "merchant_pc", "系统设置", "门店管理", "门店管理", "", 0),
    ("merchant_pc.setting.pay.manage", "merchant_pc", "系统设置", "支付管理", "支付管理", "", 1),
    ("merchant_pc.setting.perm.role", "merchant_pc", "系统设置", "权限", "角色权限", "", 1),
    ("merchant_pc.setting.perm.user", "merchant_pc", "系统设置", "权限", "用户管理", "", 1),
    ("merchant_pc.setting.msg.config", "merchant_pc", "系统设置", "消息配置", "消息配置", "", 0),
    # ---- merchant_mp 端（21）----
    ("merchant_mp.workbench.overview.board", "merchant_mp", "工作台", "经营概览", "数据概览", "", 1),
    ("merchant_mp.workbench.overview.todo", "merchant_mp", "工作台", "经营概览", "待办处理", "", 1),
    ("merchant_mp.workbench.overview.quick", "merchant_mp", "工作台", "经营概览", "快捷操作", "", 1),
    ("merchant_mp.workbench.overview.notice", "merchant_mp", "工作台", "经营概览", "运营通知", "", 1),
    ("merchant_mp.verify.scan.scan", "merchant_mp", "核销", "扫码核销", "扫码核销", "", 1),
    ("merchant_mp.verify.scan.manual", "merchant_mp", "核销", "扫码核销", "手动输入券码", "", 1),
    ("merchant_mp.verify.scan.confirm", "merchant_mp", "核销", "扫码核销", "核销确认", "", 1),
    ("merchant_mp.order.list.list", "merchant_mp", "订单处理", "订单", "订单列表", "", 1),
    ("merchant_mp.order.list.detail", "merchant_mp", "订单处理", "订单", "订单详情", "", 1),
    ("merchant_mp.order.list.ship", "merchant_mp", "订单处理", "订单", "实物发货（支持扫码录入）", "", 1),
    ("merchant_mp.order.list.pickup", "merchant_mp", "订单处理", "订单", "待自提处理", "", 1),
    ("merchant_mp.order.list.refund", "merchant_mp", "订单处理", "订单", "退款审核", "", 1),
    ("merchant_mp.goods.list.list", "merchant_mp", "商品管理", "商品", "商品列表", "", 1),
    ("merchant_mp.goods.list.stock", "merchant_mp", "商品管理", "商品", "库存查看（含预警）", "", 1),
    ("merchant_mp.member.query.list", "merchant_mp", "会员查询", "会员", "会员列表", "", 1),
    ("merchant_mp.member.query.detail", "merchant_mp", "会员查询", "会员", "会员详情", "", 1),
    ("merchant_mp.member.query.points", "merchant_mp", "会员查询", "会员", "手动调整积分", "", 1),
    ("merchant_mp.my.account.profile", "merchant_mp", "我的", "账号", "个人信息", "", 1),
]

# 平台系统角色模板（is_system=1，开店时默认开通 PF_DASHBOARD 等平台权限）
PLATFORM_ROLE_TEMPLATES: list[dict] = [
    {
        "name": "平台超级管理员",
        "remark": "系统预置，全部平台权限",
        "perms": [
            "PF_DASHBOARD", "PF_MERCHANT_LIST", "PF_MERCHANT_EDIT", "PF_MERCHANT_STATUS",
            "PF_MERCHANT_IMPERSONATE", "PF_MERCHANT_RESET_PWD", "PF_FEATURE_EDIT",
            "PF_ROLE", "PF_STAFF", "PF_STAFF_RESET_PWD", "PF_MSG_TEMPLATE", "PF_AUDIT_LOG",
        ],
        "is_system": 1,
    },
    {
        "name": "平台运营",
        "remark": "商家管理与看板，无代客/角色管理",
        "perms": ["PF_DASHBOARD", "PF_MERCHANT_LIST", "PF_MERCHANT_EDIT", "PF_MERCHANT_STATUS", "PF_FEATURE_EDIT"],
        "is_system": 1,
    },
]

# 平台超级管理员初始账号（首次部署写入）
PLATFORM_SUPER_ADMIN: dict = {
    "account": "admin",
    "name": "平台超级管理员",
    "password": "Admin@123456",
    "role_name": "平台超级管理员",
}

# 本地开发演示租户（幂等写入，生产可由平台开户接口创建）
DEMO_TENANTS = [
    {"tenant_no": "M10001", "name": "优鲜生活", "contact_name": "张伟", "contact_phone": "13810001024", "status": "NORMAL"},
    {"tenant_no": "M10002", "name": "潮玩星球", "contact_name": "李娜", "contact_phone": "13810011024", "status": "TRIAL"},
]
