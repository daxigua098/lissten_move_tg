"""配置加载、校验与摘要输出。

优先级：代码内默认值 < configs/config.yaml < .env 文件 < 进程环境变量。
敏感项只放 .env，运行参数放 YAML。
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, Field, ValidationError, field_validator

from app.core.paths import ensure_dir, resolve_path
from app.core.paths import project_root as resolve_root

DEFAULT_CONFIG_RELATIVE = Path("configs") / "config.yaml"
DEFAULT_ENV_RELATIVE = Path(".env")

DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin123"

# Fernet 密钥形态：43 位 urlsafe base64 字符 + 结尾等号
FERNET_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{43}=$")

# Telegram 官方 API 凭据形态
TELEGRAM_API_ID_MAX = 2_147_483_647  # Telethon 按 32 位有符号整数打包
TELEGRAM_API_HASH_PATTERN = re.compile(r"^[0-9a-fA-F]{32}$")

LOG_LEVELS = ("TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL")

# 环境变量 → 配置项映射（只有列出的键会覆盖 YAML）
ENV_OVERRIDES: dict[str, tuple[str, str]] = {
    "SECRET_KEY": ("secrets", "secret_key"),
    "ADMIN_USERNAME": ("secrets", "admin_username"),
    "ADMIN_PASSWORD": ("secrets", "admin_password"),
    "ADMIN_API_TOKEN": ("secrets", "admin_api_token"),
    "DATABASE_URL": ("database", "url"),
    "LOG_LEVEL": ("logging", "level"),
    "TG_API_ID": ("telegram", "api_id"),
    "TG_API_HASH": ("telegram", "api_hash"),
    "TG_ADMIN_IDS": ("telegram", "admin_ids"),
    "DEMO_MODE": ("app", "demo_mode"),
}


class ConfigError(RuntimeError):
    """配置缺失、无法解析或结构非法。"""


class AppSection(BaseModel):
    """应用级元信息。"""

    name: str = "tg-lead-system"
    environment: Literal["development", "production"] = "development"
    timezone: str = "Asia/Shanghai"
    access_mode: Literal["local", "public"] = "local"
    # 本地演练：用模拟客户端代替 Telegram，不连接网络、发送只写日志
    demo_mode: bool = False


class ServerSection(BaseModel):
    """HTTP 服务监听与访问控制。"""

    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    allowed_ips: list[str] = Field(default_factory=list)
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )

    @field_validator("allowed_ips", "cors_origins")
    @classmethod
    def _clean_list(cls, value: list[str]) -> list[str]:
        return [str(item).strip() for item in value if str(item).strip()]


class DatabaseSection(BaseModel):
    """数据库连接参数。"""

    url: str = "sqlite+aiosqlite:///./data/app.db"
    echo: bool = False
    wal: bool = True


class LoggingSection(BaseModel):
    """日志输出参数。"""

    level: str = "INFO"
    file: str = "logs/app.log"
    rotation_mb: int = Field(default=10, ge=1)
    retention_days: int = Field(default=30, ge=1)

    @field_validator("level")
    @classmethod
    def _normalise_level(cls, value: str) -> str:
        level = str(value).strip().upper()
        if level not in LOG_LEVELS:
            raise ValueError(f"日志级别必须是 {'/'.join(LOG_LEVELS)} 之一")
        return level


class SecuritySection(BaseModel):
    """登录与会话安全参数。"""

    session_hours: int = Field(default=24, ge=1, le=720)
    max_login_failures: int = Field(default=5, ge=1, le=100)
    login_window_minutes: int = Field(default=15, ge=1, le=1440)
    password_min_length: int = Field(default=8, ge=6, le=128)


class TelegramSection(BaseModel):
    """Telegram API 凭据（my.telegram.org 申请），用于执行账号与 Bot 校验。"""

    api_id: int = Field(default=0, ge=0)
    api_hash: str = ""
    proxy: str | None = None
    # 默认管理员 TG 用户 ID：绑定控制 Bot 时若未单独指定就用这里
    admin_ids: list[int] = Field(default_factory=list)

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _parse_admin_ids(cls, value: object) -> object:
        """支持 `123,456` 这种逗号分隔写法（.env 里只能写字符串）。"""
        if isinstance(value, str):
            parts = value.replace("，", ",").replace(" ", ",").split(",")
            return [int(item) for item in parts if item.strip().isdigit()]
        return value

    @property
    def configured(self) -> bool:
        return self.api_id > 0 and bool(self.api_hash.strip())

    @property
    def api_id_looks_valid(self) -> bool:
        """api_id 是否在 Telethon 能打包的范围内。"""
        return 0 < self.api_id <= TELEGRAM_API_ID_MAX

    @property
    def api_hash_looks_valid(self) -> bool:
        """api_hash 是否为官方形态（32 位十六进制）。"""
        return bool(TELEGRAM_API_HASH_PATTERN.match(self.api_hash.strip()))


class RuntimeSection(BaseModel):
    """运行时锁、心跳与控制文件位置。"""

    heartbeat_seconds: int = Field(default=10, ge=1, le=3600)
    lock_file: str = "data/runtime.lock"
    status_file: str = "data/runtime_status.json"
    control_file: str = "data/runtime_control.json"


class RetentionSection(BaseModel):
    """数据保留策略（天）。"""

    messages_raw_days: int = Field(default=3, ge=1, le=365)
    member_profiles_days: int = Field(default=3, ge=1, le=365)
    leads_days: int = Field(default=3, ge=1, le=365)
    archive_days: int = Field(default=30, ge=1, le=3650)
    audit_days: int = Field(default=180, ge=1, le=3650)
    login_history_days: int = Field(default=90, ge=1, le=3650)


class ResourceSection(BaseModel):
    """资源发现模块的口径与限速（全部可配，改配置即生效）。

    默认值取自《资源发现模块 需求说明书 v1.1》第 10 章的"待确认"表。
    """

    # 探测采样深度（可选 20 / 100 / 200）
    sample_depth: int = Field(default=100, ge=5, le=500)
    # 索引型群判定（F-R07）
    index_member_threshold: int = Field(default=5000, ge=0)
    index_feature_threshold: int = Field(default=3, ge=1, le=5)
    activity_threshold: float = Field(default=30.0, ge=0, le=100)
    link_density_threshold: float = Field(default=30.0, ge=0, le=100)
    # 加群限速（F-R12）
    join_hourly_limit: int = Field(default=10, ge=1, le=1000)
    join_daily_limit: int = Field(default=50, ge=1, le=10000)
    # 每日配额（F-R16）：超限排队而不是报错
    search_daily_limit: int = Field(default=200, ge=1, le=10000)
    probe_daily_limit: int = Field(default=500, ge=1, le=100000)
    # 同一个关键词多久不重复搜（F-R02）
    search_repeat_hours: int = Field(default=24, ge=1, le=720)
    # 分层刷新（F-R10）
    refresh_adopted_days: int = Field(default=1, ge=1, le=365)
    refresh_candidate_days: int = Field(default=7, ge=1, le=365)
    refresh_low_days: int = Field(default=30, ge=1, le=3650)
    # 采纳后回看（F-R14）
    review_days: int = Field(default=7, ge=1, le=365)
    # 探测日志保留天数（资源库本身长期保留）
    probe_log_days: int = Field(default=90, ge=1, le=3650)
    # 加群失败后的退避（秒）
    join_retry_backoff_seconds: int = Field(default=900, ge=10, le=86400)
    max_join_attempts: int = Field(default=3, ge=1, le=10)
    # 索引型群的链接优先级更高（F-R03）
    index_source_priority: bool = True


class Secrets(BaseModel):
    """来自 .env 的敏感配置。"""

    secret_key: str = ""
    admin_username: str = DEFAULT_ADMIN_USERNAME
    admin_password: str = DEFAULT_ADMIN_PASSWORD
    admin_api_token: str = ""

    @property
    def secret_key_configured(self) -> bool:
        return bool(self.secret_key.strip())


class AppConfig(BaseModel):
    """完整生效配置。"""

    app: AppSection = Field(default_factory=AppSection)
    server: ServerSection = Field(default_factory=ServerSection)
    database: DatabaseSection = Field(default_factory=DatabaseSection)
    logging: LoggingSection = Field(default_factory=LoggingSection)
    security: SecuritySection = Field(default_factory=SecuritySection)
    telegram: TelegramSection = Field(default_factory=TelegramSection)
    runtime: RuntimeSection = Field(default_factory=RuntimeSection)
    retention: RetentionSection = Field(default_factory=RetentionSection)
    resource: ResourceSection = Field(default_factory=ResourceSection)
    secrets: Secrets = Field(default_factory=Secrets)
    project_root: Path
    config_path: Path | None = None
    env_path: Path | None = None

    def path(self, value: str | Path) -> Path:
        """把配置中的相对路径解析为绝对路径。"""
        return resolve_path(value, self.project_root)


@dataclass(frozen=True)
class ConfigIssue:
    """一条配置检查结论。"""

    level: Literal["error", "warning"]
    field: str
    message: str
    hint: str = ""

    def render(self) -> str:
        label = "错误" if self.level == "error" else "警告"
        text = f"[{label}] {self.field}：{self.message}"
        return f"{text}（{self.hint}）" if self.hint else text


def load_config(
    config_path: str | Path | None = None,
    *,
    env_file: str | Path | None = None,
    project_root: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> AppConfig:
    """加载配置。

    参数：
        config_path: YAML 路径（相对项目根目录），缺省 `configs/config.yaml`。
        env_file: 环境文件路径（相对项目根目录），缺省 `.env`。
        project_root: 项目根目录，缺省取代码所在目录。
        environ: 覆盖用的环境变量映射；缺省读取进程环境。测试可传空字典隔离。

    异常：
        ConfigError：YAML 无法解析，或合并后的配置不满足校验规则。
    """
    root = resolve_root(project_root)
    config_file = resolve_path(config_path or DEFAULT_CONFIG_RELATIVE, root)
    env_path = resolve_path(env_file or DEFAULT_ENV_RELATIVE, root)

    raw: dict[str, Any] = {}
    if config_file.is_file():
        raw = _read_yaml(config_file)

    env_values: dict[str, str] = {}
    if env_path.is_file():
        for key, value in (dotenv_values(env_path) or {}).items():
            if value is not None and value.strip():
                env_values[str(key).strip()] = value.strip()
    process_env = os.environ if environ is None else environ
    for key, value in process_env.items():
        if value is None or not str(value).strip():
            continue
        env_values[str(key).strip()] = str(value).strip()

    for env_key, (section, field) in ENV_OVERRIDES.items():
        value = env_values.get(env_key)
        if value is None:
            continue
        bucket = raw.get(section)
        if not isinstance(bucket, dict):
            bucket = {}
            raw[section] = bucket
        bucket[field] = value

    payload = dict(raw)
    payload["project_root"] = root
    payload["config_path"] = config_file if config_file.is_file() else None
    payload["env_path"] = env_path if env_path.is_file() else None

    try:
        return AppConfig.model_validate(payload)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc)) from exc


def check_config(config: AppConfig) -> list[ConfigIssue]:
    """执行配置检查，返回问题清单（error 会阻止启动，warning 只提示）。"""
    issues: list[ConfigIssue] = []

    secret_key = config.secrets.secret_key.strip()
    if not secret_key:
        issues.append(
            ConfigIssue(
                "error",
                "SECRET_KEY",
                "未配置字段加密密钥",
                '生成命令：python -c "from cryptography.fernet import Fernet;'
                'print(Fernet.generate_key().decode())"',
            )
        )
    elif not FERNET_KEY_PATTERN.match(secret_key):
        issues.append(
            ConfigIssue(
                "error",
                "SECRET_KEY",
                "格式不是有效的 Fernet 密钥",
                "应为 44 位 urlsafe base64 字符串（以 = 结尾），请重新生成",
            )
        )

    if config.secrets.admin_password == DEFAULT_ADMIN_PASSWORD:
        issues.append(
            ConfigIssue(
                "warning",
                "ADMIN_PASSWORD",
                "仍在使用默认密码 admin123",
                "首次登录后必须修改，或在 .env 中改为专用密码",
            )
        )

    if not config.telegram.configured:
        issues.append(
            ConfigIssue(
                "warning",
                "telegram.api_id",
                "未配置 Telegram API 凭据（TG_API_ID / TG_API_HASH）",
                "绑定执行账号与校验 Bot Token 时需要，到 my.telegram.org 申请",
            )
        )
    elif not config.telegram.api_id_looks_valid:
        issues.append(
            ConfigIssue(
                "error",
                "telegram.api_id",
                f"API ID 超出范围（当前 {config.telegram.api_id}）",
                f"必须是小于 {TELEGRAM_API_ID_MAX} 的数字（通常是 7~8 位）；"
                "如果这是你的 Telegram 用户 ID，说明填错了位置",
            )
        )
    elif not config.telegram.api_hash_looks_valid:
        issues.append(
            ConfigIssue(
                "error",
                "telegram.api_hash",
                f"API Hash 形态不对（当前 {len(config.telegram.api_hash.strip())} 位）",
                "官方 api_hash 是 32 位十六进制字符，请到 my.telegram.org 复制",
            )
        )

    if config.app.access_mode == "public" and not config.server.allowed_ips:
        issues.append(
            ConfigIssue(
                "error",
                "server.allowed_ips",
                "公网模式必须配置 IP 白名单",
                "否则后台将对全网开放",
            )
        )

    if config.app.demo_mode:
        issues.append(
            ConfigIssue(
                "warning",
                "app.demo_mode",
                "已开启本地演练模式（不连接 Telegram，发送只写日志）",
                "生产环境务必关闭",
            )
        )

    issues.extend(_check_writable(config, config.path("data"), "数据目录"))
    issues.extend(_check_writable(config, config.path("logs"), "日志目录"))

    database_parent = _sqlite_parent(config)
    if database_parent is not None:
        issues.extend(_check_writable(config, database_parent, "数据库目录"))

    if config.app.environment == "production" and config.database.url.startswith("sqlite"):
        issues.append(
            ConfigIssue(
                "warning",
                "database.url",
                "生产环境仍在使用 SQLite",
                "单机小量可用；并发与数据量上升后建议切换 PostgreSQL",
            )
        )

    return issues


def format_issues(issues: list[ConfigIssue]) -> str:
    """把问题清单拼成多行文本。"""
    if not issues:
        return "全部通过，没有发现问题。"
    return "\n".join(issue.render() for issue in issues)


def has_errors(issues: list[ConfigIssue]) -> bool:
    """是否存在阻止启动的错误。"""
    return any(issue.level == "error" for issue in issues)


def config_summary(config: AppConfig) -> list[tuple[str, str]]:
    """输出可打印的生效配置摘要（敏感值已掩码）。"""
    access_mode = (
        "local（仅本机 / SSH 隧道）"
        if config.app.access_mode == "local"
        else "public（HTTPS + IP 白名单）"
    )
    return [
        ("项目名称", config.app.name),
        ("运行环境", config.app.environment),
        ("访问模式", access_mode),
        ("服务监听", f"{config.server.host}:{config.server.port}"),
        ("数据库", config.database.url),
        ("数据目录", str(config.path("data"))),
        ("日志文件", str(config.path(config.logging.file))),
        ("日志级别", config.logging.level),
        ("会话时长", f"{config.security.session_hours} 小时"),
        (
            "登录锁定",
            f"{config.security.login_window_minutes} 分钟内失败 "
            f"{config.security.max_login_failures} 次",
        ),
        (
            "保留策略",
            f"原文 {config.retention.messages_raw_days} 天 / "
            f"线索 {config.retention.leads_days} 天 / "
            f"归档 {config.retention.archive_days} 天",
        ),
        ("加密密钥", mask_secret(config.secrets.secret_key)),
        ("内置管理员", f"{config.secrets.admin_username}（密码来自 .env）"),
        (
            "Telegram API",
            "已配置" if config.telegram.configured else "未配置（绑定账号与校验 Bot 需要）",
        ),
        (
            "Bot 管理员",
            ", ".join(str(item) for item in config.telegram.admin_ids) or "未配置",
        ),
        ("配置文件", str(config.config_path) if config.config_path else "未找到，使用默认值"),
        ("环境文件", str(config.env_path) if config.env_path else "未找到"),
    ]


def mask_secret(value: str, *, keep: int = 4) -> str:
    """对密钥类文本做掩码，保留首尾便于比对。"""
    text = (value or "").strip()
    if not text:
        return "未配置"
    if len(text) <= keep * 2:
        return "*" * len(text)
    return f"{text[:keep]}...{text[-keep:]}"


def sqlite_database_path(config: AppConfig) -> Path | None:
    """返回 SQLite 数据库文件路径；非 SQLite 或内存库返回 None。"""
    url = config.database.url
    if not url.startswith("sqlite"):
        return None
    _, separator, tail = url.partition("///")
    if not separator:
        return None
    raw = tail.split("?", 1)[0]
    if raw in {"", ":memory:"}:
        return None
    return config.path(raw)


def absolute_database_url(config: AppConfig) -> str:
    """把 SQLite 的相对地址解析成基于项目根目录的绝对地址；其他情况原样返回。

    必要性：SQLAlchemy 按进程当前工作目录解析 `./data/app.db` 这类相对路径，
    而服务可能从任意目录启动并指定 `--project-root`，不解析会出现"数据库跑到
    意料之外的位置"这种隐蔽问题。
    """
    url = config.database.url
    if not url.startswith("sqlite") or ":///" not in url:
        return url
    prefix, _, tail = url.partition(":///")
    raw, _, query = tail.partition("?")
    if raw in {"", ":memory:"}:
        return url
    resolved = config.path(raw).as_posix()
    suffix = f"?{query}" if query else ""
    return f"{prefix}:///{resolved}{suffix}"


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件解析失败：{path}（{exc}）") from exc
    except OSError as exc:
        raise ConfigError(f"配置文件读取失败：{path}（{exc}）") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"配置文件顶层必须是键值结构：{path}")
    return loaded


def _format_validation_error(exc: ValidationError) -> str:
    lines = ["配置校验失败："]
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ())) or "<root>"
        lines.append(f"  - {location}：{error.get('msg', '取值非法')}")
    return "\n".join(lines)


def _check_writable(config: AppConfig, directory: Path, label: str) -> list[ConfigIssue]:
    try:
        ensure_dir(directory)
    except OSError as exc:
        return [ConfigIssue("error", str(directory), f"{label}无法创建：{exc}")]
    probe = directory / ".write-probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return [ConfigIssue("error", str(directory), f"{label}不可写：{exc}")]
    return []


def _sqlite_parent(config: AppConfig) -> Path | None:
    database_path = sqlite_database_path(config)
    return database_path.parent if database_path is not None else None
