"""命令行入口：配置校验与后续子命令调度。"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from app.core.config import (
    DEFAULT_CONFIG_RELATIVE,
    DEFAULT_ENV_RELATIVE,
    AppConfig,
    ConfigError,
    ConfigIssue,
    absolute_database_url,
    check_config,
    config_summary,
    has_errors,
    load_config,
)
from app.core.logging import setup_logging

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_NOT_IMPLEMENTED = 2

# 尚未交付的子命令 → 所属任务
PENDING_COMMANDS: dict[str, str] = {
    "create-admin": "T2-02 默认管理员与强制改密",
    "api": "E2 登录与权限",
    "status": "T1-03 运行时锁与心跳",
    "backup": "T7-02 备份与恢复",
    "restore": "T7-02 备份与恢复",
}


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Telegram 线索运营系统命令行工具",
    )
    parser.add_argument("--project-root", default=None, help="项目根目录（默认取代码所在目录）")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_RELATIVE),
        help="配置文件路径（相对项目根目录）",
    )
    parser.add_argument(
        "--env-file",
        default=str(DEFAULT_ENV_RELATIVE),
        help="环境变量文件路径（相对项目根目录）",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check-config", help="校验配置并打印生效项")
    check.add_argument("--json", action="store_true", help="以 JSON 输出检查结果")

    migrate = subparsers.add_parser("migrate", help="数据库迁移到指定版本")
    migrate.add_argument("--revision", default="head", help="目标版本，默认 head")
    subparsers.add_parser("init-db", help="初始化数据库（等价于迁移到最新版本）")

    for name, owner in PENDING_COMMANDS.items():
        subparsers.add_parser(name, help=f"将在 {owner} 中实现")
    return parser


def command_check_config(args: argparse.Namespace) -> int:
    """校验配置并打印结果。"""
    try:
        config = _load_config(args)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAILURE

    log_path = None
    logging_error: OSError | None = None
    try:
        log_path = setup_logging(config)
    except OSError as exc:
        logging_error = exc

    issues = check_config(config)
    if logging_error is not None:
        issues.append(ConfigIssue("warning", "logging.file", f"日志文件暂不可写：{logging_error}"))

    if args.json:
        print(
            json.dumps(
                {
                    "ok": not has_errors(issues),
                    "project_root": str(config.project_root),
                    "config": dict(config_summary(config)),
                    "issues": [
                        {
                            "level": issue.level,
                            "field": issue.field,
                            "message": issue.message,
                            "hint": issue.hint,
                        }
                        for issue in issues
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print("== 生效配置 ==")
        for label, value in config_summary(config):
            print(f"{label}：{value}")
        print()
        print("== 检查结果 ==")
        if issues:
            for issue in issues:
                print(issue.render())
        else:
            print("全部通过，没有发现问题。")
        if log_path is not None:
            print(f"\n日志文件：{log_path}")

    return EXIT_FAILURE if has_errors(issues) else EXIT_OK


def command_pending(args: argparse.Namespace) -> int:
    """尚未交付的子命令占位。"""
    owner = PENDING_COMMANDS.get(args.command, "后续任务")
    print(f"命令 {args.command} 尚未实现，将在 {owner} 中交付。", file=sys.stderr)
    return EXIT_NOT_IMPLEMENTED


def command_migrate(args: argparse.Namespace) -> int:
    """把数据库迁移到目标版本（默认最新）。"""
    try:
        config = _load_config(args)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAILURE

    try:
        setup_logging(config)
    except OSError as exc:
        print(f"警告：日志文件不可写（{exc}），继续执行迁移。", file=sys.stderr)

    revision = getattr(args, "revision", "head")
    try:
        alembic_config = _alembic_config(config)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAILURE

    from alembic import command as alembic_command

    try:
        alembic_command.upgrade(alembic_config, revision)
    except Exception as exc:  # noqa: BLE001 - 迁移失败原因需要原样展示给运维
        print(f"数据库迁移失败：{exc}", file=sys.stderr)
        return EXIT_FAILURE

    print(f"数据库已迁移到 {revision}：{config.database.url}")
    return EXIT_OK


def command_init_db(args: argparse.Namespace) -> int:
    """初始化数据库：等价于迁移到最新版本，首次部署用。"""
    print("初始化数据库（执行迁移到最新版本）…")
    result = command_migrate(args)
    if result == EXIT_OK:
        print("提示：后续结构变更请执行 python main.py migrate。")
    return result


def _alembic_config(config: AppConfig):
    """构造 Alembic 配置；延迟导入，避免 check-config 也强依赖 Alembic。"""
    from alembic.config import Config as AlembicConfig

    ini_path = config.project_root / "alembic.ini"
    if not ini_path.is_file():
        raise ConfigError(f"未找到 alembic.ini：{ini_path}")
    alembic_config = AlembicConfig(str(ini_path))
    alembic_config.set_main_option("script_location", str(config.project_root / "migrations"))
    alembic_config.set_main_option("sqlalchemy.url", absolute_database_url(config))
    return alembic_config


def main(argv: Sequence[str] | None = None) -> int:
    """命令行主入口，返回进程退出码。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "check-config":
            return command_check_config(args)
        if args.command == "migrate":
            return command_migrate(args)
        if args.command == "init-db":
            return command_init_db(args)
        return command_pending(args)
    except KeyboardInterrupt:
        print("已中断。", file=sys.stderr)
        return 130


def _load_config(args: argparse.Namespace) -> AppConfig:
    return load_config(
        args.config,
        env_file=args.env_file,
        project_root=args.project_root,
    )
