"""配置加载与校验测试。"""

from __future__ import annotations

import pytest

from app.core.config import (
    DEFAULT_ADMIN_PASSWORD,
    AppConfig,
    ConfigError,
    absolute_database_url,
    check_config,
    config_summary,
    format_issues,
    has_errors,
    load_config,
    mask_secret,
    sqlite_database_path,
)


def _load(project_root, **kwargs) -> AppConfig:
    kwargs.setdefault("environ", {})
    return load_config(project_root=project_root, **kwargs)


def test_defaults_when_no_config_file(project_root) -> None:
    config = _load(project_root)

    assert config.app.name == "tg-lead-system"
    assert config.app.access_mode == "local"
    assert config.server.port == 8000
    assert config.database.url.startswith("sqlite")
    assert config.logging.level == "INFO"
    assert config.retention.messages_raw_days == 3
    assert config.retention.leads_days == 3
    assert config.secrets.admin_username == "admin"
    assert config.config_path is None
    assert config.env_path is None
    assert config.project_root == project_root.resolve()


def test_yaml_overrides_defaults(project_root, write_config) -> None:
    write_config(
        "app:\n"
        "  access_mode: public\n"
        "server:\n"
        "  port: 9001\n"
        "  allowed_ips:\n"
        "    - 1.2.3.4\n"
        "retention:\n"
        "  leads_days: 7\n"
    )

    config = _load(project_root)

    assert config.app.access_mode == "public"
    assert config.server.port == 9001
    assert config.server.allowed_ips == ["1.2.3.4"]
    assert config.retention.leads_days == 7
    assert config.config_path is not None


def test_env_file_overrides_yaml(project_root, write_config, write_env, valid_secret_key) -> None:
    write_config("logging:\n  level: WARNING\n")
    write_env([f"SECRET_KEY={valid_secret_key}", "LOG_LEVEL=DEBUG", "ADMIN_PASSWORD=secret-pass"])

    config = _load(project_root)

    assert config.logging.level == "DEBUG"
    assert config.secrets.secret_key == valid_secret_key
    assert config.secrets.admin_password == "secret-pass"
    assert config.env_path is not None


def test_process_environ_overrides_env_file(project_root, write_env) -> None:
    write_env(["LOG_LEVEL=DEBUG"])

    config = load_config(project_root=project_root, environ={"LOG_LEVEL": "ERROR"})

    assert config.logging.level == "ERROR"


def test_blank_env_value_keeps_yaml_or_default(project_root) -> None:
    config = load_config(
        project_root=project_root,
        environ={"SECRET_KEY": "   ", "DATABASE_URL": ""},
    )

    assert config.secrets.secret_key == ""
    assert config.database.url.startswith("sqlite")


def test_database_url_env_override(project_root) -> None:
    url = "postgresql+asyncpg://user:pass@localhost:5432/app"

    config = load_config(project_root=project_root, environ={"DATABASE_URL": url})

    assert config.database.url == url


def test_invalid_port_raises_with_field_name(project_root, write_config) -> None:
    write_config("server:\n  port: 70000\n")

    with pytest.raises(ConfigError) as excinfo:
        _load(project_root)

    assert "server.port" in str(excinfo.value)


def test_invalid_log_level_raises(project_root, write_config) -> None:
    write_config("logging:\n  level: VERBOSE\n")

    with pytest.raises(ConfigError) as excinfo:
        _load(project_root)

    assert "logging.level" in str(excinfo.value)


def test_broken_yaml_raises(project_root, write_config) -> None:
    write_config("app: [unclosed\n")

    with pytest.raises(ConfigError) as excinfo:
        _load(project_root)

    assert "解析失败" in str(excinfo.value)


def test_config_path_helper_resolves_relative_to_root(project_root) -> None:
    config = _load(project_root)

    assert config.path("data/runtime.lock") == project_root / "data" / "runtime.lock"
    assert config.path(config.logging.file) == project_root / "logs" / "app.log"


def test_check_config_reports_missing_secret_key(project_root) -> None:
    issues = check_config(_load(project_root))

    assert has_errors(issues)
    assert any(issue.field == "SECRET_KEY" and issue.level == "error" for issue in issues)
    assert "Fernet" in format_issues(issues)


def test_check_config_rejects_bad_secret_key_format(project_root) -> None:
    config = load_config(project_root=project_root, environ={"SECRET_KEY": "not-a-fernet-key"})

    issues = check_config(config)

    assert any(issue.field == "SECRET_KEY" and "格式" in issue.message for issue in issues)


def test_check_config_requires_allowed_ips_in_public_mode(project_root, valid_secret_key) -> None:
    config = load_config(
        project_root=project_root,
        environ={
            "SECRET_KEY": valid_secret_key,
            "ADMIN_PASSWORD": "custom-pass",
        },
    )
    config.app.access_mode = "public"

    issues = check_config(config)

    assert any(issue.field == "server.allowed_ips" for issue in issues)


def test_check_config_warns_default_admin_password(project_root, valid_secret_key) -> None:
    config = load_config(project_root=project_root, environ={"SECRET_KEY": valid_secret_key})

    issues = check_config(config)

    assert not has_errors(issues)
    assert any(
        issue.field == "ADMIN_PASSWORD"
        and issue.level == "warning"
        and DEFAULT_ADMIN_PASSWORD in issue.message
        for issue in issues
    )


def test_check_config_passes_with_complete_setup(
    project_root, write_config, valid_secret_key
) -> None:
    write_config("app:\n  access_mode: public\nserver:\n  allowed_ips:\n    - 10.0.0.1\n")
    config = load_config(
        project_root=project_root,
        environ={
            "SECRET_KEY": valid_secret_key,
            "ADMIN_PASSWORD": "custom-pass",
            "TG_API_ID": "123456",
            "TG_API_HASH": "0123456789abcdef0123456789abcdef",
        },
    )

    issues = check_config(config)

    assert issues == []
    assert format_issues(issues) == "全部通过，没有发现问题。"


def test_check_config_detects_data_dir_blocked_by_file(project_root, valid_secret_key) -> None:
    config = load_config(
        project_root=project_root,
        environ={"SECRET_KEY": valid_secret_key, "ADMIN_PASSWORD": "custom-pass"},
    )
    (project_root / "data").write_text("not a directory", encoding="utf-8")

    issues = check_config(config)

    assert any("数据目录" in issue.message for issue in issues)


def test_config_summary_masks_secrets(project_root, valid_secret_key) -> None:
    config = load_config(project_root=project_root, environ={"SECRET_KEY": valid_secret_key})

    summary = dict(config_summary(config))
    display = summary["加密密钥"]

    assert valid_secret_key not in display
    assert "..." in display


def test_mask_secret_variants() -> None:
    assert mask_secret("") == "未配置"
    assert mask_secret("short") == "*****"
    assert mask_secret("abcdefghijkl") == "abcd...ijkl"


def test_sqlite_database_path_resolution(project_root) -> None:
    config = _load(project_root)
    assert sqlite_database_path(config) == project_root / "data" / "app.db"

    memory = load_config(
        project_root=project_root,
        environ={"DATABASE_URL": "sqlite+aiosqlite:///:memory:"},
    )
    assert sqlite_database_path(memory) is None

    postgres = load_config(
        project_root=project_root,
        environ={"DATABASE_URL": "postgresql+asyncpg://user:pass@localhost:5432/app"},
    )
    assert sqlite_database_path(postgres) is None


def test_absolute_database_url_resolves_relative_path(project_root) -> None:
    config = _load(project_root)

    resolved = absolute_database_url(config)

    assert resolved == f"sqlite+aiosqlite:///{(project_root / 'data' / 'app.db').as_posix()}"
    assert resolved.endswith("data/app.db")


def test_absolute_database_url_keeps_other_urls(project_root) -> None:
    memory = load_config(
        project_root=project_root,
        environ={"DATABASE_URL": "sqlite+aiosqlite:///:memory:"},
    )
    postgres = load_config(
        project_root=project_root,
        environ={"DATABASE_URL": "postgresql+asyncpg://user:pass@localhost:5432/app"},
    )

    assert absolute_database_url(memory) == "sqlite+aiosqlite:///:memory:"
    assert absolute_database_url(postgres).startswith("postgresql+asyncpg://")
