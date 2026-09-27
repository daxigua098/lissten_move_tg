"""生产库升级演练（P6-02）：只读生产库、只改副本。

用法：

    .\.venv\Scripts\python.exe scripts\rehearse-upgrade.py [工作目录]

默认工作目录是系统临时目录下的 ``tg-migrate-rehearsal``。流程：

1. 用 SQLite 备份 API 从生产库取一份**含 WAL** 的完整副本（生产库只读打开）；
2. 记录升级前的 ``alembic_version``、关键表行数、表清单与 ``tenants``/``users`` 列；
3. 对副本执行 ``alembic upgrade head``；
4. 再记录一次，并输出新增/删除的表、新增列与行数差异，写入 ``report.json``。

生产库本身不会被写入；真要改生产前请先得到授权并停服。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROD = REPO / "data" / "app.db"
DEFAULT_WORK = Path(tempfile.gettempdir()) / "tg-migrate-rehearsal"

# 关注行数变化的业务表（前后应完全一致，只有被拆分的 chats 例外）
WATCHED_TABLES = (
    "users",
    "tenants",
    "routes",
    "leads",
    "tg_accounts",
    "control_bots",
    "agent_quotas",
    "quota_ledger",
    "tenant_modules",
    "chat_directory",
    "tenant_chats",
)


def backup(source: Path, target: Path) -> None:
    """用 SQLite 备份 API 复制（会带上未落盘的 WAL 内容）。"""
    if target.exists():
        target.unlink()
    origin = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    clone = sqlite3.connect(target.as_posix())
    try:
        with clone:
            origin.backup(clone)
    finally:
        origin.close()
        clone.close()


def snapshot(path: Path) -> dict:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        counts: dict[str, int | str] = {}
        for table in WATCHED_TABLES:
            try:
                counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error as exc:
                counts[table] = f"<{exc}>"
        return {
            "alembic_version": [
                row[0] for row in conn.execute("SELECT version_num FROM alembic_version")
            ],
            "counts": counts,
            "tables": [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            ],
            "columns": {
                table: [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
                for table in ("tenants", "users")
            },
        }
    finally:
        conn.close()


def upgrade(path: Path) -> None:
    from alembic import command
    from alembic.config import Config as AlembicConfig

    cfg = AlembicConfig(str(REPO / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path.as_posix()}")
    command.upgrade(cfg, "head")


def main(argv: list[str]) -> int:
    work = Path(argv[1]) if len(argv) > 1 else DEFAULT_WORK
    work.mkdir(parents=True, exist_ok=True)
    copy_path = work / "app-copy.db"

    if not PROD.exists():
        print(f"找不到生产库：{PROD}", file=sys.stderr)
        return 1

    report: dict = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "prod_db": str(PROD),
        "prod_db_size": PROD.stat().st_size,
    }

    backup(PROD, copy_path)
    report["copy_db"] = str(copy_path)
    report["copy_db_size"] = copy_path.stat().st_size
    report["before"] = snapshot(copy_path)

    upgrade(copy_path)
    report["after"] = snapshot(copy_path)

    before, after = report["before"], report["after"]
    report["new_tables"] = sorted(set(after["tables"]) - set(before["tables"]))
    report["dropped_tables"] = sorted(set(before["tables"]) - set(after["tables"]))
    report["new_tenant_columns"] = sorted(
        set(after["columns"]["tenants"]) - set(before["columns"]["tenants"])
    )
    report["new_user_columns"] = sorted(
        set(after["columns"]["users"]) - set(before["columns"]["users"])
    )
    report["count_deltas"] = {
        table: [before["counts"][table], after["counts"][table]]
        for table in WATCHED_TABLES
        if before["counts"][table] != after["counts"][table]
    }

    (work / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\n报告已写入：{work / 'report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
