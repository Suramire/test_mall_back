"""历史本地时区 DATETIME 受控迁移工具（本地时区 → UTC）。

用法：
  python scripts/utc_migrate.py scan [--tables t1,t2]
  python scripts/utc_migrate.py apply --tables t1.f1,t2.f2 --hours-offset -8 --i-know-offset \
      [--backup-dir backups/utc_migrate] [--apply] [--yes]

默认 dry-run：不带 --apply 只打印计划，不写库、不写备份。
执行顺序：先写备份 JSON，再单事务逐行更新并记录 schema_migrate_flags 水位。
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TextIO

import sqlalchemy as sa

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_BACKUP_DIR = "backups/utc_migrate"
FLAG_TABLE = "schema_migrate_flags"
SCAN_SAMPLE_LIMIT = 500
PLAN_SAMPLE_LIMIT = 5
WARN_UTC_RATIO = 0.6
FRESH_WINDOW_HOURS = 48


class MigrateConfigError(ValueError):
    """参数或迁移目标不合法。"""


_FLAG_META = sa.MetaData()
flag_table = sa.Table(
    FLAG_TABLE,
    _FLAG_META,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("scope", sa.String(128), nullable=False, unique=True),
    sa.Column("last_id", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("rows_updated", sa.Integer, nullable=False, server_default="0"),
    sa.Column("hours_offset", sa.Integer, nullable=False, server_default="0"),
    sa.Column("run_id", sa.String(64), nullable=False, server_default=""),
    sa.Column("applied_at", sa.DateTime, nullable=True),
)


def parse_bare_tables(spec: str | None) -> list[str]:
    if not spec or not spec.strip():
        return []
    return [token.strip() for token in spec.split(",") if token.strip()]


def parse_table_fields(spec: str | None) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for token in parse_bare_tables(spec):
        if "." not in token:
            raise MigrateConfigError(f"--tables 必须是 表.字段 格式，收到 {token!r}")
        table, field = (part.strip() for part in token.split(".", 1))
        if not table or not field:
            raise MigrateConfigError(f"--tables 必须是 表.字段 格式，收到 {token!r}")
        pair = (table, field)
        if pair not in seen:
            seen.add(pair)
            pairs.append(pair)
    return pairs


def shift_value(value: datetime, hours_offset: int) -> datetime:
    return value + timedelta(hours=hours_offset)


def coerce_datetime(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise MigrateConfigError(f"无法解析时间值: {value!r}")


def build_backup_record(
    table: str, field: str, row_id: int, old_value: datetime, new_value: datetime
) -> dict:
    return {
        "table": table,
        "field": field,
        "id": int(row_id),
        "old_value": old_value.isoformat(),
        "new_value": new_value.isoformat(),
    }


def build_backup_document(
    run_id: str, hours_offset: int, targets: list[tuple[str, str]], records: list[dict]
) -> dict:
    return {
        "run_id": run_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "direction": "local_to_utc",
        "hours_offset": hours_offset,
        "targets": [f"{t}.{f}" for t, f in targets],
        "row_count": len(records),
        "rows": records,
    }


def classify_timestamp(value: datetime, now_utc: datetime, now_local: datetime) -> str:
    if value > now_utc and value > now_local:
        return "future"
    age_utc = (now_utc - value).total_seconds()
    age_local = (now_local - value).total_seconds()
    window = FRESH_WINDOW_HOURS * 3600
    if age_utc > window and age_local > window:
        return "stale"
    if abs(age_utc) <= abs(age_local):
        return "utc-like"
    return "local-like"


def utc_like_ratio_ex(
    values: list[datetime | None], now_utc: datetime, now_local: datetime
) -> tuple[float, int, int]:
    present = [v for v in values if v is not None]
    labels = [classify_timestamp(v, now_utc, now_local) for v in present]
    decidable = [
        label for label in labels if label in ("utc-like", "local-like")
    ]
    undecided = len(labels) - len(decidable)
    stale = sum(1 for label in labels if label == "stale")
    future = sum(1 for label in labels if label == "future")
    if not decidable:
        return 0.0, undecided, stale + future
    hits = sum(1 for label in decidable if label == "utc-like")
    return hits / len(decidable), undecided, stale + future


def utc_like_ratio(
    values: list[datetime | None], now_utc: datetime, now_local: datetime
) -> float:
    ratio, _, _ = utc_like_ratio_ex(values, now_utc, now_local)
    return ratio


def validate_apply_request(
    pairs: list[tuple[str, str]], hours_offset: int, i_know_offset: bool
) -> None:
    if not pairs:
        raise MigrateConfigError(
            "缺少 --tables：必须显式指定 表.字段 列表，禁止无条件批量执行"
        )
    if not i_know_offset:
        raise MigrateConfigError(
            "缺少 --i-know-offset：必须确认库内历史时间为本地时区(UTC+8)后才能执行"
        )
    if hours_offset >= 0:
        raise MigrateConfigError(
            f"--hours-offset 必须为负数(方向=本地→UTC, 如 -8)，收到 {hours_offset}"
        )
    if abs(hours_offset) > 14:
        raise MigrateConfigError(f"--hours-offset 偏移量超出合理范围: {hours_offset}")


def resolve_database_url(explicit: str | None) -> str:
    if explicit:
        return explicit
    from app.core.config import settings

    return settings.DATABASE_URL


def build_engine(url: str) -> sa.Engine:
    kwargs: dict = {}
    if not url.startswith("sqlite"):
        kwargs["pool_pre_ping"] = True
    return sa.create_engine(url, **kwargs)


def flag_table_exists(engine: sa.Engine) -> bool:
    return FLAG_TABLE in sa.inspect(engine).get_table_names()


def get_watermark(conn: sa.Connection, scope: str) -> int:
    row = conn.execute(
        sa.text(f"SELECT last_id FROM {FLAG_TABLE} WHERE scope = :s"), {"s": scope}
    ).first()
    return int(row[0]) if row else 0


def record_watermark(
    conn: sa.Connection,
    scope: str,
    last_id: int,
    rows_updated: int,
    hours_offset: int,
    run_id: str,
) -> None:
    existing = conn.execute(
        sa.text(f"SELECT id FROM {FLAG_TABLE} WHERE scope = :s"), {"s": scope}
    ).first()
    applied_at = datetime.now(UTC).replace(tzinfo=None)
    params = {
        "s": scope,
        "l": last_id,
        "r": rows_updated,
        "h": hours_offset,
        "rid": run_id,
        "a": applied_at,
    }
    if existing:
        conn.execute(
            sa.text(
                f"UPDATE {FLAG_TABLE} SET last_id=:l, "
                "rows_updated=rows_updated+:r, hours_offset=:h, run_id=:rid, "
                "applied_at=:a WHERE scope=:s"
            ),
            params,
        )
    else:
        conn.execute(
            sa.text(
                f"INSERT INTO {FLAG_TABLE} "
                "(scope, last_id, rows_updated, hours_offset, run_id, applied_at) "
                "VALUES (:s, :l, :r, :h, :rid, :a)"
            ),
            params,
        )


def ensure_flag_table(engine: sa.Engine) -> None:
    _FLAG_META.create_all(engine, checkfirst=True)


def has_id_column(engine: sa.Engine, table: str) -> bool:
    cols = {c["name"] for c in sa.inspect(engine).get_columns(table)}
    return "id" in cols


def list_datetime_columns(
    engine: sa.Engine, tables: list[str] | None
) -> dict[str, list[str]]:
    insp = sa.inspect(engine)
    available = set(insp.get_table_names())
    wanted = [t for t in (tables or []) if t] or sorted(available)
    result: dict[str, list[str]] = {}
    for table in wanted:
        if table not in available:
            continue
        cols = [
            c["name"] for c in insp.get_columns(table) if isinstance(c["type"], sa.DateTime)
        ]
        if cols:
            result[table] = cols
    return result


def verify_targets(engine: sa.Engine, pairs: list[tuple[str, str]]) -> None:
    insp = sa.inspect(engine)
    available = set(insp.get_table_names())
    for table, field in pairs:
        if table not in available:
            raise MigrateConfigError(f"表不存在: {table}")
        cols = {c["name"]: c["type"] for c in insp.get_columns(table)}
        if field not in cols:
            raise MigrateConfigError(f"字段不存在: {table}.{field}")
        if not isinstance(cols[field], sa.DateTime):
            raise MigrateConfigError(
                f"目标列不是 DATETIME 类型: {table}.{field} ({cols[field]!r})"
            )


def fetch_candidates(
    conn: sa.Connection,
    preparer: sa.sql.compiler.IdentifierPreparer,
    table: str,
    field: str,
    watermark: int,
) -> list[tuple[int, datetime]]:
    qt = preparer.quote(table)
    qf = preparer.quote(field)
    sql = f"SELECT id, {qf} FROM {qt} WHERE id > :wm AND {qf} IS NOT NULL ORDER BY id"
    rows = conn.execute(sa.text(sql), {"wm": watermark}).fetchall()
    return [(int(row[0]), coerce_datetime(row[1])) for row in rows]


def run_scan(
    engine: sa.Engine, tables: list[str] | None, out: TextIO | None = None
) -> dict:
    out = out or sys.stdout
    now_utc = datetime.now(UTC).replace(tzinfo=None)
    now_local = datetime.now()  # noqa: DTZ005 本地墙钟时间用于启发式对比
    preparer = engine.dialect.identifier_preparer
    cols_map = list_datetime_columns(engine, tables)
    flags_exist = flag_table_exists(engine)
    flagged: set[str] = set()
    if flags_exist:
        with engine.connect() as conn:
            flagged = {
                row[0] for row in conn.execute(sa.text(f"SELECT scope FROM {FLAG_TABLE}"))
            }
    print("== UTC 迁移扫描报告(只读) ==", file=out)
    print(f"生成时间: {datetime.now(UTC).isoformat()}", file=out)
    print(f"标志表: {'存在' if flags_exist else '不存在'}", file=out)
    total_values = 0
    total_utc_like = 0
    warnings: list[str] = []
    for table in sorted(cols_map):
        print(f"\n表 {table}", file=out)
        order_by = " ORDER BY id DESC" if has_id_column(engine, table) else ""
        with engine.connect() as conn:
            for field in cols_map[table]:
                qf = preparer.quote(field)
                qt = preparer.quote(table)
                nonnull = conn.execute(
                    sa.text(f"SELECT COUNT({qf}) FROM {qt}")
                ).scalar_one()
                total_rows = conn.execute(sa.text(f"SELECT COUNT(*) FROM {qt}")).scalar_one()
                rows = conn.execute(
                    sa.text(
                        f"SELECT {qf} FROM {qt} WHERE {qf} IS NOT NULL{order_by} LIMIT :l"
                    ),
                    {"l": SCAN_SAMPLE_LIMIT},
                ).fetchall()
                values = [coerce_datetime(row[0]) for row in rows]
                ratio, undecided, _ = utc_like_ratio_ex(values, now_utc, now_local)
                total_values += nonnull
                total_utc_like += round(ratio * (len(values) - undecided))
                mark = "[已迁移] " if f"{table}.{field}" in flagged else ""
                warn = ""
                decidable = len(values) - undecided
                if decidable and ratio >= WARN_UTC_RATIO and f"{table}.{field}" not in flagged:
                    warn = f" ⚠️疑似已UTC({ratio:.0%})，重复迁移将二次偏移"
                    warnings.append(f"{table}.{field}: 疑似已UTC比例 {ratio:.0%}")
                note = f"，未定样本 {undecided}(过旧/未来)" if undecided else ""
                if values and decidable == 0:
                    warnings.append(
                        f"{table}.{field}: 样本全部过旧或为未来时间，启发式不可判"
                    )
                sample = ", ".join(v.isoformat() for v in values[:3]) or "-"
                print(
                    f"  {mark}{field:<20} 非空 {nonnull}/{total_rows}  "
                    f"疑似已UTC {ratio:.0%}{note}{warn}\n    样本: {sample}",
                    file=out,
                )
    scanned = sum(len(cols) for cols in cols_map.values())
    print("\n== 审计摘要 ==", file=out)
    print(f"扫描表数: {len(cols_map)}, datetime 列数: {scanned}, 非空时间值: {total_values}", file=out)
    print(f"样本内疑似已UTC值: {total_utc_like}", file=out)
    if warnings:
        print("启发式警告:", file=out)
        for w in warnings:
            print(f"  - {w}", file=out)
    else:
        print("启发式警告: 无", file=out)
    return {"tables": len(cols_map), "columns": scanned, "values": total_values}


def confirm_execute(
    assume_yes: bool, out: TextIO | None = None, inp: Callable[[str], str] = input
) -> bool:
    out = out or sys.stdout
    if assume_yes:
        return True
    try:
        answer = inp("确认对上述目标执行本地→UTC 迁移？输入 YES 继续: ")
    except EOFError:
        return False
    ok = answer.strip() == "YES"
    if not ok:
        print("未确认，已取消。", file=out)
    return ok


def write_backup(
    backup_dir: Path,
    run_id: str,
    hours_offset: int,
    targets: list[tuple[str, str]],
    records: list[dict],
) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = backup_dir / f"{stamp}_{run_id}_utc_migrate.json"
    document = build_backup_document(run_id, hours_offset, targets, records)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def run_apply(
    engine: sa.Engine,
    pairs: list[tuple[str, str]],
    hours_offset: int,
    backup_dir: str,
    execute: bool,
    assume_yes: bool,
    out: TextIO | None = None,
) -> int:
    out = out or sys.stdout
    verify_targets(engine, pairs)
    preparer = engine.dialect.identifier_preparer
    run_id = uuid.uuid4().hex[:16]
    flags_ready = flag_table_exists(engine)
    plans: list[dict] = []
    with engine.connect() as conn:
        for table, field in pairs:
            watermark = (
                get_watermark(conn, f"{table}.{field}") if flags_ready else 0
            )
            candidates = fetch_candidates(conn, preparer, table, field, watermark)
            plans.append(
                {
                    "table": table,
                    "field": field,
                    "watermark": watermark,
                    "candidates": candidates,
                }
            )

    print("== 迁移计划 ==", file=out)
    print(f"方向: 本地时区 → UTC (偏移 {hours_offset} 小时)，run_id={run_id}", file=out)
    would_change = 0
    samples: list[str] = []
    for plan in plans:
        table, field = plan["table"], plan["field"]
        candidates: list[tuple[int, datetime]] = plan["candidates"]
        would_change += len(candidates)
        print(
            f"- {table}.{field}: 已迁移水位 id<={plan['watermark']}, "
            f"本次待变更 {len(candidates)} 行",
            file=out,
        )
        for row_id, old in candidates[:PLAN_SAMPLE_LIMIT]:
            new = shift_value(old, hours_offset)
            samples.append(f"{row_id}  {old.isoformat()} -> {new.isoformat()}")
    print("\n抽样前后对照:", file=out)
    for line in samples or ["-"]:
        print(f"  {line}", file=out)

    if not execute:
        print("\n[dry-run] 未写库、未写备份；确认无误后追加 --apply 执行。", file=out)
        return 0

    print("\n== 执行前审计摘要 ==", file=out)
    print(f"扫描候选行数: {would_change}, 将变更行数: {would_change}", file=out)
    if not confirm_execute(assume_yes, out):
        return 3

    backup_records: list[dict] = []
    for plan in plans:
        for row_id, old in plan["candidates"]:
            backup_records.append(
                build_backup_record(
                    plan["table"], plan["field"], row_id, old, shift_value(old, hours_offset)
                )
            )
    backup_path = write_backup(
        Path(backup_dir), run_id, hours_offset, pairs, backup_records
    )
    print(f"备份已写入: {backup_path}", file=out)

    ensure_flag_table(engine)
    updated_total = 0
    with engine.begin() as conn:
        for plan in plans:
            qt = preparer.quote(plan["table"])
            qf = preparer.quote(plan["field"])
            last_id = plan["watermark"]
            for row_id, old in plan["candidates"]:
                new = shift_value(old, hours_offset)
                result = conn.execute(
                    sa.text(
                        f"UPDATE {qt} SET {qf} = :new "
                        "WHERE id = :id AND " + qf + " = :old"
                    ),
                    {"new": new, "id": row_id, "old": old},
                )
                updated_total += result.rowcount
                last_id = max(last_id, row_id)
            record_watermark(
                conn,
                f"{plan['table']}.{plan['field']}",
                last_id,
                len(plan["candidates"]),
                hours_offset,
                run_id,
            )

    print("\n== 执行结果 ==", file=out)
    print(f"实际更新行数: {updated_total}/{would_change}", file=out)
    for plan in plans:
        scope = f"{plan['table']}.{plan['field']}"
        last_id = plan["watermark"]
        if plan["candidates"]:
            last_id = max(plan["watermark"], plan["candidates"][-1][0])
        print(f"  {scope}: 水位推进至 id<={last_id}", file=out)
    print(f"备份文件: {backup_path}", file=out)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="utc_migrate",
        description="历史本地时区 DATETIME 受控迁移为 UTC 的运维工具",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--database-url", default=None, help="缺省复用 settings.DATABASE_URL")
    sub = parser.add_subparsers(dest="command", required=True)

    scan_parser = sub.add_parser(
        "scan", parents=[common], help="只读扫描与启发式预检"
    )
    scan_parser.add_argument("--tables", default=None, help="逗号分隔表名，缺省全部")

    apply_parser = sub.add_parser(
        "apply", parents=[common], help="受控执行本地→UTC 偏移"
    )
    apply_parser.add_argument("--tables", default=None, help="逗号分隔的 表.字段 列表")
    apply_parser.add_argument("--hours-offset", type=int, default=-8)
    apply_parser.add_argument("--i-know-offset", action="store_true")
    apply_parser.add_argument("--backup-dir", default=DEFAULT_BACKUP_DIR)
    apply_parser.add_argument(
        "--apply", action="store_true", help="真实执行；缺省 dry-run 只打印计划"
    )
    apply_parser.add_argument("--yes", action="store_true", help="跳过交互确认")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "scan":
            engine = build_engine(resolve_database_url(args.database_url))
            run_scan(engine, parse_bare_tables(args.tables))
            return 0
        pairs = parse_table_fields(args.tables)
        validate_apply_request(pairs, args.hours_offset, args.i_know_offset)
        engine = build_engine(resolve_database_url(args.database_url))
        return run_apply(
            engine,
            pairs,
            args.hours_offset,
            args.backup_dir,
            execute=args.apply,
            assume_yes=args.yes,
        )
    except MigrateConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"[执行失败] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
