# UTC 迁移操作手册（历史本地时区 DATETIME → UTC）

工具：`scripts/utc_migrate.py`（scan 只读预检 / apply 受控偏移）。
适用前提：库内历史时间以本地墙钟(UTC+8)写入，目标口径为 UTC naive（DATETIME 不存 tzinfo）。

## 前置条件

1. 已执行 `scan` 并逐表核对预检报告；样本全部"过旧/未来不可判"的列不得仅凭启发式决策。
2. 确认应用层已统一 UTC 写入（`datetime.now(timezone.utc)`），否则迁移后新数据将再次混合口径。
3. 明确本次偏移方向：本地→UTC 为 `-8`（东八区）；任何其他偏移须单独评审。
4. 低峰期执行；执行期间暂停相关表的写入作业（Celery 定时任务、导入任务）。
5. 每列只允许迁移一次。已迁移列记录在 `schema_migrate_flags` 表，重复执行会二次偏移 8 小时。

## 备份要求

- 全量备份（必做）：
  `mysqldump -h127.0.0.1 -uroot -p --single-transaction mall > mall_pre_utc_$(date +%F).sql`
- 工具自动备份（apply 时强制先行）：备份 JSON 落在 `--backup-dir`（默认 `backups/utc_migrate/`），
  含每行 `id / old_value / new_value`，文件名含 run_id 与 UTC 时间戳。
- 两份备份齐备并验证可读后才允许进入 apply。

## 执行步骤

```bash
cd backend
# 1) 只读预检（不写库）
.venv/bin/python scripts/utc_migrate.py scan --tables od_order,od_payment,mb_member

# 2) dry-run（默认；只打印计划与抽样对照，不写库、不写备份）
.venv/bin/python scripts/utc_migrate.py apply \
  --tables od_order.paid_at,od_order.pay_deadline \
  --hours-offset -8 --i-know-offset

# 3) 真实执行（先写备份 JSON → 单事务逐行更新 → 记录水位）
.venv/bin/python scripts/utc_migrate.py apply \
  --tables od_order.paid_at,od_order.pay_deadline \
  --hours-offset -8 --i-know-offset --apply --yes \
  --backup-dir backups/utc_migrate

# 4) 复扫验证
.venv/bin/python scripts/utc_migrate.py scan --tables od_order
```

说明：
- `--tables`(apply) 必须显式列出 `表.字段`，禁止整表批量。
- 幂等由水位保证：再次执行同一目标时 `id<=水位` 的行自动跳过。
- 中断后可直接重跑同命令，已更新行不会重复偏移。

## 回滚方法

- 行级回滚（首选）：用备份 JSON 反向恢复：

```bash
.venv/bin/python - <<'PY'
import json, sqlalchemy as sa
doc = json.load(open("backups/utc_migrate/<备份文件>.json"))
eng = sa.create_engine("mysql+pymysql://user:pwd@127.0.0.1:3306/mall")
with eng.begin() as conn:
    for r in doc["rows"]:
        conn.execute(sa.text(
            f"UPDATE {r['table']} SET {r['field']}=:old WHERE id=:id AND {r['field']}=:new"),
            {"old": r["old_value"], "new": r["new_value"], "id": r["id"]})
PY
```

- 库级回滚（大面积事故）：停写入 → `mysql mall < mall_pre_utc_<日期>.sql` 整库还原。
- 回滚后删除 `schema_migrate_flags` 中对应 scope 行，否则水位会导致补迁移跳过未处理行。

## 禁止事项

1. 禁止对 scan 显示"疑似已UTC"或已打 `[已迁移]` 标记的列再次 apply。
2. 禁止使用正数或 0 的 `--hours-offset`（工具强校验）；禁止超过 ±14 小时。
3. 禁止跳过 dry-run 直接 `--apply --yes` 上生产。
4. 禁止无备份执行；禁止在业务高峰执行大表迁移。
5. 禁止手工 UPDATE 业务时间列绕过本工具（会破坏水位与备份一致性）。
6. 迁移期间禁止恢复定时任务写入，直至 scan 复核通过。
