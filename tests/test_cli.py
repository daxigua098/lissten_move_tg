"""命令行入口测试。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from app.cli import EXIT_FAILURE, EXIT_NOT_IMPLEMENTED, EXIT_OK, main

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _args(project_root, *rest: str) -> list[str]:
    return ["--project-root", str(project_root), *rest]


def _copy_migration_assets(project_root: Path) -> None:
    """把 alembic.ini 与迁移脚本复制到临时项目根目录，让 CLI 能独立运行迁移。"""
    shutil.copy2(PROJECT_ROOT / "alembic.ini", project_root / "alembic.ini")
    shutil.copytree(
        PROJECT_ROOT / "migrations",
        project_root / "migrations",
        ignore=shutil.ignore_patterns("__pycache__"),
    )


def test_check_config_fails_without_secret_key(project_root, capsys) -> None:
    code = main(_args(project_root, "check-config"))

    captured = capsys.readouterr()
    assert code == EXIT_FAILURE
    assert "SECRET_KEY" in captured.out
    assert "检查结果" in captured.out


def test_check_config_passes_with_env_file(
    project_root, write_env, valid_secret_key, capsys
) -> None:
    write_env(
        [
            f"SECRET_KEY={valid_secret_key}",
            "ADMIN_PASSWORD=custom-pass",
            "TG_API_ID=123456",
            "TG_API_HASH=0123456789abcdef0123456789abcdef",
        ]
    )

    code = main(_args(project_root, "check-config"))

    captured = capsys.readouterr()
    assert code == EXIT_OK
    assert "全部通过" in captured.out
    assert valid_secret_key not in captured.out


def test_check_config_json_output(project_root, write_env, valid_secret_key, capsys) -> None:
    write_env(
        [
            f"SECRET_KEY={valid_secret_key}",
            "ADMIN_PASSWORD=custom-pass",
            "TG_API_ID=123456",
            "TG_API_HASH=0123456789abcdef0123456789abcdef",
        ]
    )

    code = main(_args(project_root, "check-config", "--json"))

    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_OK
    assert payload["ok"] is True
    assert payload["issues"] == []
    assert payload["config"]["加密密钥"] != valid_secret_key


def test_check_config_reports_broken_yaml(project_root, write_config, capsys) -> None:
    write_config("server: [unclosed\n")

    code = main(_args(project_root, "check-config"))

    captured = capsys.readouterr()
    assert code == EXIT_FAILURE
    assert "解析失败" in captured.err


def test_pending_command_returns_not_implemented(project_root, capsys) -> None:
    code = main(_args(project_root, "backup"))

    captured = capsys.readouterr()
    assert code == EXIT_NOT_IMPLEMENTED
    assert "尚未实现" in captured.err
    assert "T7-02" in captured.err


def test_migrate_creates_database_inside_project_root(project_root, capsys) -> None:
    _copy_migration_assets(project_root)

    code = main(_args(project_root, "migrate"))

    database_path = project_root / "data" / "app.db"
    captured = capsys.readouterr()
    assert code == EXIT_OK
    assert "已迁移到 head" in captured.out
    assert database_path.is_file()

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert {"users", "web_sessions", "login_history", "audit_logs", "system_settings"} <= tables


def test_init_db_prints_follow_up_hint(project_root, capsys) -> None:
    _copy_migration_assets(project_root)

    code = main(_args(project_root, "init-db"))

    captured = capsys.readouterr()
    assert code == EXIT_OK
    assert "初始化数据库" in captured.out
    assert "main.py migrate" in captured.out


def test_migrate_without_alembic_ini_fails(project_root, capsys) -> None:
    code = main(_args(project_root, "migrate"))

    captured = capsys.readouterr()
    assert code == EXIT_FAILURE
    assert "alembic.ini" in captured.err


def test_set_password_updates_stored_hash(project_root, write_env, valid_secret_key) -> None:
    from sqlalchemy import create_engine, text

    from app.core.security import verify_password

    _copy_migration_assets(project_root)
    write_env([f"SECRET_KEY={valid_secret_key}"])

    assert main(_args(project_root, "migrate")) == EXIT_OK
    assert main(_args(project_root, "create-admin")) == EXIT_OK

    code = main(
        _args(
            project_root,
            "set-password",
            "--username",
            "admin",
            "--password",
            "NewPass1234",
        )
    )

    assert code == EXIT_OK
    database_path = project_root / "data" / "app.db"
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "select password_hash, must_change_password from users where username = 'admin'"
                )
            ).one()
    finally:
        engine.dispose()

    assert verify_password("NewPass1234", row[0]) is True
    assert verify_password("admin123", row[0]) is False
    assert not row[1]


def test_set_password_reports_unknown_user(
    project_root, write_env, valid_secret_key, capsys
) -> None:
    _copy_migration_assets(project_root)
    write_env([f"SECRET_KEY={valid_secret_key}"])
    assert main(_args(project_root, "migrate")) == EXIT_OK

    code = main(
        _args(project_root, "set-password", "--username", "ghost", "--password", "NewPass1234")
    )

    assert code == EXIT_FAILURE
    assert "账号不存在" in capsys.readouterr().err


def test_missing_command_exits_with_usage_error(project_root) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(_args(project_root))

    assert excinfo.value.code == 2
