# T-901 后端基座与多租户隔离 — 验收进度

维护者：tester | 依据：`docs/测试验收标准-P0.md` §2 基座横切 + §0.3 DoD

## 运行方式
```bash
cd backend
source .venv/bin/activate
python -m pytest tests/acceptance/base/ -v
```

## 测试环境策略（已落位，待 pm/be-dev 确认）
- DB：SQLite 内存库（`sqlite:///:memory:`），session 级事务回滚，不依赖本地 MySQL。
- Redis：fakeredis 内存模式，不依赖本地 Redis。
- 多租户钩子：正确挂载到 Session（`do_orm_execute` + `before_flush`）。
- 注：be-dev 基座面向 MySQL8 设计，conftest 已对 SQLite 兼容池参数做屏蔽（不影响生产代码）。

## DoD 覆盖映射
| 项 | 用例 | 状态 |
|---|---|---|
| B1 统一响应体 | TestUnifiedResponse | 部分（含 traceId 用例待路由挂载） |
| B2 错误码映射 | TestErrorMapping | 骨架（待业务路由触发） |
| B3 多租户隔离 | TestTenantIsolation.test_tenant_filter_injected_on_select | 已绿（钩子生效） |
| B4 tenant 缺失 Fail-Fast | TestTenantIsolation.test_require_tenant_id_failfast_when_missing | 已绿 |
| B5 JWT | TestJWT | 已绿（除密码哈希 bug） |
| B6 幂等键 / 黑名单 | TestIdempotency | 已绿 |
| B7 编号生成 | TestSequence | 已绿 |
| B8 权限守卫 | TestGuard | 骨架（待 TenantGuardMiddleware 挂入 main + 业务路由） |

## 已发现 BUG（均已修复并回归通过 ✅）
### T-901-BUG-001：多租户 ORM 钩子安装位置错误 ✅ 已修复
- 文件：`backend/app/db/orm_hooks.py` `install_tenant_hooks(engine)`
- 现象：`do_orm_execute` 是 Session 作用域事件，挂到 engine 上触发
  `AttributeError: do_orm_execute. Did you mean: 'before_execute'`
- 影响：多租户自动隔离**完全未生效**（生产安全隐患：跨租户数据可能泄漏）。
- 修复：be-dev-2 将 `install_tenant_hooks()` 改为无参、挂到 `Session` 并加幂等保护；
  `main.py` 启动时已调用 `install_tenant_hooks()`。验证：`test_tenant_filter_injected_on_select` /
  `test_cross_tenant_write_rejected` 均 PASSED。

### T-901-BUG-002：bcrypt 密码长度超限 ✅ 已修复
- 文件：`backend/app/core/security.py` `hash_password`
- 现象：密码 >72 字节直接抛 `ValueError: password cannot be longer than 72 bytes`
- 影响：超长密码注册/改密会 500。
- 修复：be-dev-2 改用 bcrypt 直连（passlib 与 bcrypt 5.x 不兼容），并采用
  **sha256 预摘要 + base64**（`_to_bcrypt_secret`）：口令先 sha256 得 32 字节，
  base64 后恒定 44 字节，稳定落在 bcrypt 72 字节限制内。
  验证：`test_password_hash_verify` 由 FAILED → PASSED（含 100 字节超长口令用例通过）。

  > ⚠️ 注意：**不是"截断到 72 字节"**（本文档旧版本如此描述，是错误的，已于
  > 2026-08-08 由 tester 更正）。截断方案存在凭证碰撞漏洞：`'A'*73` 与 `'A'*72`
  > 会产生同一 secret，超长口令的有效强度被压到前 72 字节。
  > tester 实测确认当前实现无此问题：`verify_password('A'*73, hash_password('A'*72))`
  > 返回 `False`（截断方案会返回 `True`）。

### T-901-BUG-003：main.py 未装配基座中间件与路由 ✅ 已修复（中间件部分）
- 文件：`backend/app/main.py`
- 现象：仅挂了 TraceIdMiddleware 与 `/api/common/health`；`TenantGuardMiddleware`
  虽 import 但未 `add_middleware`；`install_tenant_hooks` 未调用；
  `/api/pf` 等业务路由未挂载（api_router 仅含 health）。
- 影响：权限守卫、多租户注入、统一响应全链路在运行中不生效。
- 修复：`TenantGuardMiddleware` 已 `add_middleware`，`install_tenant_hooks()` 已调用。
  注：业务路由（`/api/pf` 等）仍未挂载 —— 属 T-030/T-031 交付范围（T-902 验收），
  非 T-901 阻塞项。对应骨架用例 `TestGuard.*` 仍 skip，待 T-902 转绿。

## 最新结果（2026-08-07 回归）
12 passed, 5 skipped, 0 failed
- passed：B1(部分)/B2(部分)/B3/B4/B5/B6/B7 全部绿；跨租户隔离钩子与 Fail-Fast 验证通过
- 5 skipped：依赖 be-dev 尚未挂载的业务路由（TestGuard / 统一响应体链路用例），
  属 T-902 范围，合理
- 0 failed：3 个基座 bug 全部修复验证通过
