"""命令行入口：配置校验与后续子命令调度。"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import json
import secrets
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

    admin = subparsers.add_parser("create-admin", help="创建内置管理员（幂等）")
    admin.add_argument("--username", default=None, help="覆盖 .env 中的 ADMIN_USERNAME")
    admin.add_argument("--password", default=None, help="覆盖 .env 中的 ADMIN_PASSWORD")

    setpw = subparsers.add_parser("set-password", help="在服务器上设置指定账号的密码")
    setpw.add_argument("--username", required=True, help="目标账号用户名")
    setpw.add_argument("--password", default=None, help="新密码；不填则随机生成并打印")
    setpw.add_argument(
        "--force-change",
        action="store_true",
        help="要求该账号下次登录后修改密码",
    )

    login = subparsers.add_parser("account-login", help="交互式登录执行账号（生成 session）")
    login.add_argument("--account-id", type=int, default=None, help="账号 ID")
    login.add_argument("--name", default=None, help="账号别名（与 --account-id 二选一）")

    sync_history = subparsers.add_parser("sync-history", help="补齐 A 线历史消息")
    sync_history.add_argument("--route-id", type=int, default=None, help="只补指定线路")
    sync_history.add_argument("--all", action="store_true", help="补齐所有启用的 A 线")

    subparsers.add_parser("run", help="启动搬运运行时（实时监听 + 串行投递）")
    subparsers.add_parser("status", help="查看运行时状态与投递统计")
    subparsers.add_parser("pause", help="暂停投递（保留监听）")
    subparsers.add_parser("resume", help="恢复投递")
    subparsers.add_parser("stop", help="请求运行时退出")

    api = subparsers.add_parser("api", help="启动后台 Web 服务")
    api.add_argument("--host", default=None, help="监听地址，默认取配置")
    api.add_argument("--port", type=int, default=None, help="监听端口，默认取配置")
    api.add_argument("--reload", action="store_true", help="开发模式热重载")

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


def command_create_admin(args: argparse.Namespace) -> int:
    """创建内置管理员：已存在则不做修改。"""
    try:
        config = _load_config(args)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAILURE

    try:
        setup_logging(config)
    except OSError as exc:
        print(f"警告：日志文件不可写（{exc}），继续执行。", file=sys.stderr)

    username = (getattr(args, "username", None) or config.secrets.admin_username).strip()
    password = getattr(args, "password", None) or config.secrets.admin_password
    return asyncio.run(_create_admin(config, username=username, password=password))


async def _create_admin(config: AppConfig, *, username: str, password: str) -> int:
    from app.core.errors import AppError
    from app.db.models import ROLE_SUPER_ADMIN
    from app.db.session import dispose_database, get_session_factory, init_database
    from app.services import user_service

    await init_database(config)
    try:
        factory = get_session_factory()
        async with factory() as session:
            existing = await user_service.get_user_by_username(session, username)
            if existing is not None:
                print(f"账号 {existing.username} 已存在，未做修改。")
                return EXIT_OK
            try:
                user = await user_service.create_user(
                    session,
                    config,
                    username=username,
                    password=password,
                    role=ROLE_SUPER_ADMIN,
                    display_name="超级管理员",
                    is_builtin=True,
                    # 内置管理员密码在 .env 维护，因此不设"首次登录改密"门槛
                    must_change_password=False,
                )
            except AppError as exc:
                print(f"创建失败：{exc.detail}", file=sys.stderr)
                return EXIT_FAILURE
            print(f"已创建内置管理员：{user.username}（密码在服务器 .env 中维护）")
            return EXIT_OK
    except Exception as exc:  # noqa: BLE001 - 需要把原始错误展示给运维
        print(f"创建失败：{exc}", file=sys.stderr)
        print("提示：如果数据表不存在，请先执行 python main.py migrate", file=sys.stderr)
        return EXIT_FAILURE
    finally:
        await dispose_database()


def command_set_password(args: argparse.Namespace) -> int:
    """在服务器上设置指定账号的密码（内置账号的唯一改密途径）。"""
    try:
        config = _load_config(args)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAILURE

    try:
        setup_logging(config)
    except OSError as exc:
        print(f"警告：日志文件不可写（{exc}），继续执行。", file=sys.stderr)

    supplied = getattr(args, "password", None)
    password = supplied or secrets.token_urlsafe(12)
    force_change = bool(getattr(args, "force_change", False))

    code = asyncio.run(
        _set_password(
            config,
            username=args.username,
            password=password,
            force_change=force_change,
        )
    )
    if code == EXIT_OK:
        if supplied:
            print(f"已更新 {args.username} 的密码。")
        else:
            print(f"已生成并设置新密码：{password}")
        if force_change:
            print("该账号下次登录后需要修改密码。")
    return code


async def _set_password(
    config: AppConfig,
    *,
    username: str,
    password: str,
    force_change: bool,
) -> int:
    from app.core.errors import AppError
    from app.core.security import hash_password
    from app.db.session import dispose_database, get_session_factory, init_database
    from app.services import user_service

    await init_database(config)
    try:
        factory = get_session_factory()
        async with factory() as session:
            user = await user_service.get_user_by_username(session, username)
            if user is None:
                print(f"账号不存在：{username}", file=sys.stderr)
                return EXIT_FAILURE
            try:
                user_service.validate_password(
                    password,
                    min_length=config.security.password_min_length,
                )
            except AppError as exc:
                print(f"密码不符合要求：{exc.detail}", file=sys.stderr)
                return EXIT_FAILURE
            user.password_hash = hash_password(password)
            user.must_change_password = force_change
            await session.commit()
            return EXIT_OK
    except Exception as exc:  # noqa: BLE001 - 需要把原始错误展示给运维
        print(f"设置失败：{exc}", file=sys.stderr)
        print("提示：如果数据表不存在，请先执行 python main.py migrate", file=sys.stderr)
        return EXIT_FAILURE
    finally:
        await dispose_database()


def command_account_login(args: argparse.Namespace) -> int:
    """交互式登录执行账号（生成/复用 session 文件）。"""
    try:
        config = _load_config(args)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAILURE

    try:
        setup_logging(config)
    except OSError as exc:
        print(f"警告：日志文件不可写（{exc}），继续执行。", file=sys.stderr)

    if args.account_id is None and not args.name:
        print("请用 --account-id 或 --name 指定要登录的账号。", file=sys.stderr)
        return EXIT_FAILURE
    return asyncio.run(_account_login(config, account_id=args.account_id, name=args.name))


async def _account_login(
    config: AppConfig,
    *,
    account_id: int | None,
    name: str | None,
) -> int:
    from app.core.paths import ensure_dir
    from app.core.telegram_client import (
        build_user_client,
        fetch_account_profile,
        session_file_path,
    )
    from app.db.models import ACCOUNT_DISABLED
    from app.db.session import dispose_database, get_session_factory, init_database
    from app.services import tg_account_service

    await init_database(config)
    try:
        factory = get_session_factory()
        async with factory() as session:
            account = (
                await tg_account_service.get_account(session, account_id)
                if account_id is not None
                else await tg_account_service.get_account_by_name(session, name or "")
            )
            if account is None:
                print(
                    "未找到执行账号：请先在后台「执行账号池」登记，或确认 --account-id/--name。",
                    file=sys.stderr,
                )
                return EXIT_FAILURE
            if account.status == ACCOUNT_DISABLED:
                print("该账号已停用，请先在后台启用后再登录。", file=sys.stderr)
                return EXIT_FAILURE

            phone, api_id, api_hash = tg_account_service.decrypt_credentials(config, account)
            session_path = session_file_path(config, account.session_name)
            ensure_dir(session_path.parent)

            print(f"登录账号：{account.name}（{account.phone_masked}）")
            print("首次登录需输入 Telegram 发来的验证码；开启两步验证时会再提示输入密码。")

            client = build_user_client(
                config,
                api_id=api_id,
                api_hash=api_hash,
                session_path=session_path,
            )
            try:
                await client.start(
                    phone=phone,
                    code_callback=lambda: input("Telegram 验证码： ").strip(),
                    password=lambda: getpass.getpass("两步验证密码： "),
                )
                profile = await fetch_account_profile(client)
            except Exception as exc:  # noqa: BLE001 - 登录失败原因需要原样展示
                await tg_account_service.mark_failure(session, account, error=str(exc))
                print(f"登录失败：{exc}", file=sys.stderr)
                return EXIT_FAILURE
            finally:
                with contextlib.suppress(Exception):
                    await client.disconnect()

            await tg_account_service.mark_login_success(session, account, profile=profile)
            print(f"登录成功：@{profile.username or '-'}（用户 ID {profile.tg_user_id}）")
            print(f"session 已保存：{session_path}.session")
            return EXIT_OK
    finally:
        await dispose_database()


def command_sync_history(args: argparse.Namespace) -> int:
    """补齐 A 线历史消息（按目标级水位线，不重复搬运）。"""
    try:
        config = _load_config(args)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAILURE

    try:
        setup_logging(config)
    except OSError as exc:
        print(f"警告：日志文件不可写（{exc}），继续执行。", file=sys.stderr)

    return asyncio.run(_sync_history(config, route_id=getattr(args, "route_id", None)))


async def _sync_history(config: AppConfig, *, route_id: int | None) -> int:
    import contextlib

    from app.core.telegram_client import connect_user_client, session_file_path
    from app.db.session import dispose_database, init_database, session_scope
    from app.services import history_service, tg_account_service

    await init_database(config)
    try:
        if config.app.demo_mode:
            from app.core.demo_client import DemoAccountClient

            client = DemoAccountClient()
            async with session_scope() as session:
                results = await history_service.sync_all_routes(
                    session,
                    config,
                    client=client,
                    route_ids=[route_id] if route_id else None,
                )
            print("（本地演练模式：使用模拟客户端，未连接 Telegram）")
            for item in results:
                if item.get("error"):
                    print(f"[失败] {item['route']}：{item['error']}", file=sys.stderr)
                else:
                    print(
                        f"[完成] {item['route']}：扫描 {item['inspected']} 条，"
                        f"入队 {item['enqueued']} 条，过滤 {item.get('filtered', 0)} 条"
                    )
            return EXIT_OK

        async with session_scope() as session:
            account = await tg_account_service.get_default_account(session)
            if account is None:
                print("没有可用的执行账号，请先登记并登录。", file=sys.stderr)
                return EXIT_FAILURE
            _phone, api_id, api_hash = tg_account_service.decrypt_credentials(config, account)
            session_path = session_file_path(config, account.session_name)
            account_name = account.name

        client = await connect_user_client(
            config,
            api_id=api_id,
            api_hash=api_hash,
            session_path=session_path,
        )
        try:
            async with session_scope() as session:
                results = await history_service.sync_all_routes(
                    session,
                    config,
                    client=client,
                    route_ids=[route_id] if route_id else None,
                )
        finally:
            with contextlib.suppress(Exception):
                await client.disconnect()
    finally:
        await dispose_database()

    total = 0
    for item in results:
        if item.get("error"):
            print(f"[失败] {item['route']}：{item['error']}", file=sys.stderr)
            continue
        total += int(item.get("enqueued", 0))
        summary = (
            f"[完成] {item['route']}：扫描 {item['inspected']} 条，"
            f"入队 {item['enqueued']} 条，过滤 {item.get('filtered', 0)} 条，"
            f"目标 {item.get('targets', 0)} 个"
        )
        print(summary)
    print(f"账号：{account_name}；共入队 {total} 条。执行 python main.py run 开始投递。")
    return EXIT_OK


def command_run(args: argparse.Namespace) -> int:
    """启动搬运运行时：实时监听 + 串行投递。"""
    try:
        config = _load_config(args)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAILURE

    try:
        setup_logging(config)
    except OSError as exc:
        print(f"警告：日志文件不可写（{exc}），继续启动。", file=sys.stderr)

    return asyncio.run(_run_runtime(config))


async def _run_runtime(config: AppConfig) -> int:
    from app.db.session import dispose_database, init_database
    from app.services.runtime_service import RuntimeService

    await init_database(config)
    try:
        return await RuntimeService(config).run()
    except RuntimeError as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return EXIT_FAILURE
    finally:
        await dispose_database()


def command_status(args: argparse.Namespace) -> int:
    """查看运行时状态与投递统计。"""
    try:
        config = _load_config(args)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAILURE
    return asyncio.run(_print_status(config))


async def _print_status(config: AppConfig) -> int:
    from app.core.heartbeat import heartbeat_age_seconds, read_status
    from app.core.runtime_control import read_control
    from app.db.session import dispose_database, init_database, session_scope
    from app.services import delivery_service

    status = read_status(config.path(config.runtime.status_file))
    control = read_control(config.path(config.runtime.control_file))
    print("== 运行时 ==")
    if status:
        age = heartbeat_age_seconds(status)
        age_text = f"，最近心跳 {age:.0f} 秒前" if age is not None else ""
        print(f"状态：{status.get('status', 'unknown')}{age_text}")
    else:
        print("状态：未启动（没有状态文件）")
    paused_text = "是" if control["paused"] else "否"
    stop_text = "是" if control["stop_requested"] else "否"
    print(f"暂停：{paused_text}｜停止请求：{stop_text}")

    await init_database(config)
    try:
        async with session_scope() as session:
            stats = await delivery_service.job_stats(session)
    finally:
        await dispose_database()

    print("== 投递统计 ==")
    for key in ("pending", "retrying", "success", "failed", "skipped"):
        print(f"{key}：{stats.get(key, 0)}")
    return EXIT_OK


def command_pause(args: argparse.Namespace) -> int:
    """暂停投递（监听继续，任务继续入队）。"""
    from app.core.runtime_control import set_paused

    try:
        config = _load_config(args)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAILURE
    set_paused(config.path(config.runtime.control_file), True)
    print("已暂停投递。恢复请执行：python main.py resume")
    return EXIT_OK


def command_resume(args: argparse.Namespace) -> int:
    """恢复投递。"""
    from app.core.runtime_control import set_paused, set_stop_requested

    try:
        config = _load_config(args)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAILURE
    control_path = config.path(config.runtime.control_file)
    set_paused(control_path, False)
    set_stop_requested(control_path, False)
    print("已恢复投递。")
    return EXIT_OK


def command_stop(args: argparse.Namespace) -> int:
    """请求运行时进程退出。"""
    from app.core.runtime_control import set_paused, set_stop_requested

    try:
        config = _load_config(args)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAILURE
    control_path = config.path(config.runtime.control_file)
    set_stop_requested(control_path, True)
    set_paused(control_path, True)
    print("已发出停止请求，运行时会在当前任务结束后退出。")
    return EXIT_OK


def command_api(args: argparse.Namespace) -> int:
    """启动后台 Web 服务。"""
    try:
        config = _load_config(args)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAILURE

    try:
        setup_logging(config)
    except OSError as exc:
        print(f"警告：日志文件不可写（{exc}），继续启动。", file=sys.stderr)

    import uvicorn

    from app.api.app import create_app

    host = getattr(args, "host", None) or config.server.host
    port = getattr(args, "port", None) or config.server.port
    print(f"后台服务启动中：http://{host}:{port}（访问模式 {config.app.access_mode}）")

    uvicorn.run(
        create_app(config),
        host=host,
        port=port,
        reload=bool(getattr(args, "reload", False)),
        log_config=None,
    )
    return EXIT_OK


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
        if args.command == "create-admin":
            return command_create_admin(args)
        if args.command == "set-password":
            return command_set_password(args)
        if args.command == "account-login":
            return command_account_login(args)
        if args.command == "sync-history":
            return command_sync_history(args)
        if args.command == "run":
            return command_run(args)
        if args.command == "status":
            return command_status(args)
        if args.command == "pause":
            return command_pause(args)
        if args.command == "resume":
            return command_resume(args)
        if args.command == "stop":
            return command_stop(args)
        if args.command == "api":
            return command_api(args)
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
