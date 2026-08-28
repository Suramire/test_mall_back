"""utc_migrate 工具回归：dry-run 安全性、偏移正确性、备份完整性、幂等水位、参数校验。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa

from scripts import utc_migrate as um

BASE = datetime(2026, 8, 1, 18, 30, 15, 123000)  # noqa: DTZ001 库内即 naive 值
SHIFTED = BASE - timedelta(hours=8)


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path}/m.db"


@pytest.fixture
def engine(db_url: str):
    eng = um.build_engine(db_url)
    with eng.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE demo_a (id INTEGER PRIMARY KEY, ts DATETIME, label VARCHAR(20))"
            )
        )
        conn.execute(
            sa.text(
                "CREATE TABLE demo_b (id INTEGER PRIMARY KEY, ts DATETIME, label VARCHAR(20))"
            )
        )
        for i in range(1, 4):
            for table in ("demo_a", "demo_b"):
                conn.execute(
                    sa.text(
                        f"INSERT INTO {table} (id, ts, label) VALUES (:i, :t, :l)"
                    ),
                    {"i": i, "t": BASE.isoformat(sep=" "), "l": f"{table}-{i}"},
                )
    yield eng
    eng.dispose()


def read_values(engine, table: str) -> dict[int, datetime]:
    with engine.connect() as conn:
        rows = conn.execute(sa.text(f"SELECT id, ts FROM {table} ORDER BY id")).fetchall()
    return {int(rid): um.coerce_datetime(v) for rid, v in rows}


def apply_argv(db_url: str, backup_dir: str | None = None, extra: list[str] | None = None) -> list[str]:
    argv = [
        "apply",
        "--database-url",
        db_url,
        "--tables",
        "demo_a.ts,demo_b.ts",
        "--hours-offset",
        "-8",
        "--i-know-offset",
    ]
    if backup_dir is not None:
        argv += ["--backup-dir", backup_dir]
    if extra:
        argv += extra
    return argv


def test_shift_value_minus_eight_hours():
    assert um.shift_value(BASE, -8) == SHIFTED
    assert um.shift_value(SHIFTED, 8) == BASE


def test_build_backup_record_fields():
    record = um.build_backup_record("demo_a", "ts", 7, BASE, SHIFTED)
    assert record["table"] == "demo_a"
    assert record["field"] == "ts"
    assert record["id"] == 7
    assert record["old_value"] == BASE.isoformat()
    assert record["new_value"] == SHIFTED.isoformat()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (datetime(2026, 8, 1, 2, 0), "utc-like"),  # noqa: DTZ001
        (datetime(2026, 8, 1, 10, 0), "local-like"),  # noqa: DTZ001
    ],
)
def test_classify_timestamp(value, expected):
    now_utc = datetime(2026, 8, 1, 2, 30)  # noqa: DTZ001
    now_local = datetime(2026, 8, 1, 10, 30)  # noqa: DTZ001
    assert um.classify_timestamp(value, now_utc, now_local) == expected


def test_classify_future_value_is_not_utc_like():
    now_utc = datetime(2026, 8, 1, 2, 30)  # noqa: DTZ001
    now_local = datetime(2026, 8, 1, 10, 30)  # noqa: DTZ001
    assert um.classify_timestamp(datetime(2026, 9, 1), now_utc, now_local) == "future"  # noqa: DTZ001


def test_classify_stale_history_does_not_default_to_utc():
    now_utc = datetime(2026, 8, 26, 8, 45)  # noqa: DTZ001
    now_local = datetime(2026, 8, 26, 16, 45)  # noqa: DTZ001
    assert um.classify_timestamp(datetime(2026, 8, 18, 10, 33), now_utc, now_local) == "stale"  # noqa: DTZ001


def test_classify_fresh_values_distinguish_clocks():
    now_utc = datetime(2026, 8, 26, 8, 45)  # noqa: DTZ001
    now_local = datetime(2026, 8, 26, 16, 45)  # noqa: DTZ001
    assert um.classify_timestamp(datetime(2026, 8, 26, 8, 15), now_utc, now_local) == "utc-like"  # noqa: DTZ001
    assert um.classify_timestamp(datetime(2026, 8, 26, 14, 45), now_utc, now_local) == "local-like"  # noqa: DTZ001


def test_utc_like_ratio_ex_reports_undecided_samples():
    now_utc = datetime(2026, 8, 26, 8, 45)  # noqa: DTZ001
    now_local = datetime(2026, 8, 26, 16, 45)  # noqa: DTZ001
    values = [
        datetime(2026, 8, 26, 8, 15),  # noqa: DTZ001
        datetime(2026, 8, 26, 14, 45),  # noqa: DTZ001
        datetime(2026, 9, 7, 7, 54),  # noqa: DTZ001 future
        None,
    ]
    ratio, undecided, excluded = um.utc_like_ratio_ex(values, now_utc, now_local)
    assert ratio == 0.5
    assert undecided == 1
    assert excluded == 1


def test_scan_marks_undecided_when_all_samples_stale(db_url: str, capsys):
    eng = um.build_engine(db_url)
    with eng.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE demo_old (id INTEGER PRIMARY KEY, ts DATETIME)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO demo_old (id, ts) VALUES (1, '2020-01-01 00:00:00')"
            )
        )
    rc = um.main(["scan", "--database-url", db_url, "--tables", "demo_old"])
    out = capsys.readouterr().out
    eng.dispose()
    assert rc == 0
    assert "未定样本 1" in out
    assert "启发式不可判" in out


def test_scan_prints_nonnull_over_total_rows(db_url: str, capsys, engine):
    rc = um.main(["scan", "--database-url", db_url, "--tables", "demo_a"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "非空 3/3" in out


def test_utc_like_ratio_empty_is_zero():
    now = datetime(2026, 8, 1, 2, 30)  # noqa: DTZ001
    assert um.utc_like_ratio([], now, now) == 0.0


def test_validate_apply_request_rejects_bad_input():
    with pytest.raises(um.MigrateConfigError):
        um.validate_apply_request([], -8, True)
    with pytest.raises(um.MigrateConfigError):
        um.validate_apply_request([("demo_a", "ts")], -8, False)
    with pytest.raises(um.MigrateConfigError):
        um.validate_apply_request([("demo_a", "ts")], 8, True)


def test_parse_table_fields_requires_dot_format():
    assert um.parse_table_fields("a.b, c.d ,a.b") == [("a", "b"), ("c", "d")]
    with pytest.raises(um.MigrateConfigError):
        um.parse_table_fields("demo_a")


def test_apply_without_tables_exits_nonzero(db_url: str):
    rc = um.main(
        ["apply", "--database-url", db_url, "--hours-offset", "-8", "--i-know-offset"]
    )
    assert rc != 0


def test_apply_default_dry_run_does_not_touch_db(db_url: str, tmp_path: Path, engine):
    backup_dir = tmp_path / "bk"
    rc = um.main(apply_argv(db_url, str(backup_dir)))
    assert rc == 0
    for table in ("demo_a", "demo_b"):
        assert all(v == BASE for v in read_values(engine, table).values())
    assert not backup_dir.exists() or not list(backup_dir.glob("*.json"))
    with engine.connect() as conn:
        tables = sa.inspect(conn).get_table_names()
    assert um.FLAG_TABLE not in tables


def test_scan_is_read_only(db_url: str, capsys, engine):
    rc = um.main(["scan", "--database-url", db_url, "--tables", "demo_a,demo_b"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "UTC 迁移扫描报告" in captured.out
    for table in ("demo_a", "demo_b"):
        assert all(v == BASE for v in read_values(engine, table).values())


def test_apply_shifts_values_minus_eight_hours(db_url: str, tmp_path: Path, engine):
    backup_dir = tmp_path / "bk"
    rc = um.main(apply_argv(db_url, str(backup_dir), ["--apply", "--yes"]))
    assert rc == 0
    for table in ("demo_a", "demo_b"):
        values = read_values(engine, table)
        assert len(values) == 3
        assert all(v == SHIFTED for v in values.values())
    with engine.connect() as conn:
        flags = conn.execute(
            sa.text(f"SELECT scope, last_id, rows_updated FROM {um.FLAG_TABLE}")
        ).fetchall()
    assert {(r[0], r[1], r[2]) for r in flags} == {
        ("demo_a.ts", 3, 3),
        ("demo_b.ts", 3, 3),
    }


def test_backup_json_complete_with_ids_and_before_after(
    db_url: str, tmp_path: Path, engine
):
    backup_dir = tmp_path / "bk"
    rc = um.main(apply_argv(db_url, str(backup_dir), ["--apply", "--yes"]))
    assert rc == 0
    files = list(backup_dir.glob("*.json"))
    assert len(files) == 1
    document = json.loads(files[0].read_text(encoding="utf-8"))
    assert document["direction"] == "local_to_utc"
    assert document["hours_offset"] == -8
    assert document["row_count"] == 6
    assert document["targets"] == ["demo_a.ts", "demo_b.ts"]
    rows = document["rows"]
    assert len(rows) == 6
    expected_ids = {1, 2, 3}
    by_target: dict[str, set] = {"demo_a.ts": set(), "demo_b.ts": set()}
    for record in rows:
        key = f"{record['table']}.{record['field']}"
        assert key in by_target
        by_target[key].add(record["id"])
        assert record["old_value"] == BASE.isoformat()
        assert record["new_value"] == SHIFTED.isoformat()
    for ids in by_target.values():
        assert ids == expected_ids


def test_second_apply_skips_done_ranges(db_url: str, tmp_path: Path, engine):
    backup_dir = tmp_path / "bk"
    first_rc = um.main(apply_argv(db_url, str(backup_dir), ["--apply", "--yes"]))
    assert first_rc == 0
    second_rc = um.main(
        apply_argv(db_url, str(tmp_path / "bk2"), ["--apply", "--yes"])
    )
    assert second_rc == 0
    for table in ("demo_a", "demo_b"):
        assert all(v == SHIFTED for v in read_values(engine, table).values())
    with engine.connect() as conn:
        flags = conn.execute(
            sa.text(f"SELECT scope, rows_updated FROM {um.FLAG_TABLE} ORDER BY scope")
        ).fetchall()
    assert [r[1] for r in flags] == [3, 3]


def test_second_run_reports_zero_pending_in_plan(db_url: str, tmp_path: Path, capsys, engine):
    um.main(apply_argv(db_url, str(tmp_path / "bk1"), ["--apply", "--yes"]))
    capsys.readouterr()
    rc = um.main(apply_argv(db_url, str(tmp_path / "bk2")))
    out = capsys.readouterr().out
    assert rc == 0
    assert "本次待变更 0 行" in out
    assert "[dry-run]" in out
