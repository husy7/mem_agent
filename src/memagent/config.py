"""集中配置层。

设计原则：
1. 所有配置只从这里流入，禁止模块内散落 os.environ 读取（AGENTS.md 原则 5）。
2. 配置缺失/非法 → 启动时快速失败，不静默降级（AGENTS.md 全局降级路径）。
3. 环境变量名 = 各组 env_prefix + 字段名大写，不做隐式映射，
   避免"改了 .env 没生效"却查不出原因。
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from pydantic import Field, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录：src/memagent/config.py -> parents[0]=memagent, [1]=src, [2]=项目根
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 日志级别 → structlog 需要的整数值。做成常量表而不是散落的 if/elif，
# 这样测试可以直接验证这张表，而不是重复实现一遍映射逻辑。
LOG_LEVELS: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

# .env.example 照抄常见占位符。命中即拒绝，避免"没配 key"被伪装成"key 无效"。
PLACEHOLDER_KEY_MARKERS: tuple[str, ...] = (
    "your-key",
    "your_key",
    "yourkey",
    "placeholder",
    "changeme",
    "change-me",
    "todo",
    "xxxx",
    "example",
)


def _validate_log_level(value: str) -> str:
    """纯函数形式的校验，便于单元测试直接调用（不依赖 Settings 的生命周期）。"""
    normalized = value.strip().upper()
    if normalized not in LOG_LEVELS:
        allowed = " / ".join(LOG_LEVELS)
        raise ValueError(f"LOG_LEVEL 必须是 {allowed} 之一，收到 {value!r}")
    return normalized


def _validate_workspace(value: str) -> str:
    """VFS 工作目录必须是沙箱内绝对路径。

    为什么必须拦：bashkit 的 VFS 是和宿主机隔离的，写 `workspace` 或
    `C:\\data` 会让 read/write 的路径拼出不可预期的结果。
    """
    normalized = value.strip()
    if not normalized.startswith("/"):
        raise ValueError(
            f"SANDBOX_WORKSPACE 必须是 VFS 绝对路径（以 / 开头），收到 {value!r}。"
            "注意这是沙箱内路径，不是宿主机路径。"
        )
    return normalized.rstrip("/") or "/"


def _validate_api_key_shape(key: str | None) -> str | None:
    """只校验形状，不校验真实性（真实性只能靠真实调用验证）。

    为什么必须拦占位符：.env 通常是照抄 .env.example 来的，
    `sk-your-key-here` 完全符合 `sk-` 前缀，会被当成真 key 放行；
    结果是把"没配 key"变成了"调用时才 401"，排查成本高得多。
    宁可启动时明确告诉他"你还没填 key"。
    """
    if key is None:
        return None
    cleaned = key.strip()
    if not cleaned:
        return None

    lowered = cleaned.lower()
    for marker in PLACEHOLDER_KEY_MARKERS:
        if marker in lowered:
            raise ValueError(
                f"DEEPSEEK_API_KEY 看起来是占位符（命中 {marker!r}）：{key!r}。"
                "有两种正确做法：填真实 key，或留空（DEEPSEEK_API_KEY=）。"
                "留空不会导致启动失败——只有真正调用 LLM 时才需要 key。"
            )

    if not cleaned.startswith("sk-"):
        raise ValueError(
            "DEEPSEEK_API_KEY 形状不对：应以 'sk-' 开头。"
            "若你还没申请到 key，请把该行留空（DEEPSEEK_API_KEY=），不要填占位符。"
        )
    return cleaned


class _GroupSettings(BaseSettings):
    """所有配置分组的共同基类。

    为什么每个分组都要继承 BaseSettings（这是踩过的坑）：
    pydantic-settings 只给 BaseSettings 子类做「环境变量 → 字段」的映射。
    如果分组写成 BaseModel，它只做结构校验、完全不读环境变量——
    表现为所有环境变量静默失效，且不报任何错，极难排查。
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )


class LLMSettings(_GroupSettings):
    """前缀 DEEPSEEK_ → DEEPSEEK_API_KEY / DEEPSEEK_MODEL / DEEPSEEK_BASE_URL ……"""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        env_prefix="DEEPSEEK_",
    )

    api_key: str | None = None
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    timeout_seconds: float = Field(default=60.0, gt=0)
    max_retries: int = Field(default=2, ge=0, le=10)

    @field_validator("api_key", mode="before")
    @classmethod
    def _check_key(cls, v: object) -> object:
        return _validate_api_key_shape(v if isinstance(v, str) or v is None else str(v))


class WebSearchSettings(_GroupSettings):
    """web_search 由本项目自行调用搜索 API。

    为什么不是服务端工具：DeepSeek Responses API 的 tools 只支持 function 类型，
    web_search/file_search 等内置工具会被静默忽略（官方兼容性文档明确列出）。
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        env_prefix="WEB_SEARCH_",
    )

    provider: str = "tavily"
    api_key: str | None = None
    max_results: int = Field(default=5, ge=1, le=20)

    @field_validator("api_key", mode="before")
    @classmethod
    def _empty_to_none(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v


class SandboxSettings(_GroupSettings):
    """前缀 SANDBOX_ → SANDBOX_WORKSPACE / SANDBOX_MAX_COMMANDS ……"""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        env_prefix="SANDBOX_",
    )

    workspace: str = "/workspace"
    max_commands: int = Field(default=500, ge=1)
    max_loop_iterations: int = Field(default=5000, ge=1)
    timeout_seconds: float = Field(default=30.0, gt=0)
    max_read_bytes: int = Field(default=51_200, ge=1)
    max_write_bytes: int = Field(default=1_048_576, ge=1)

    @field_validator("workspace")
    @classmethod
    def _check_workspace(cls, v: str) -> str:
        return _validate_workspace(v)

    @model_validator(mode="after")
    def _check_read_write_bounds(self) -> SandboxSettings:
        """R 是单次 read 返回的字节上限（超出即截断），W 是单个文件可写入的上限。

        R < W 是自洽的：一个合法写入的文件（≤ W）可能无法被单次读完。
        阶段一用 offset/limit 分块读，所以这不是错误。

        R >= W 才说明配置意图矛盾：读上限比写上限还大，意味着
        「文件的写入规模」和「读取规模」的边界认知不一致 —— 那是设计没想清楚，
        应当快速失败而不是运行到线上才发现。
        """
        if self.max_read_bytes >= self.max_write_bytes:
            raise ValueError(
                "SANDBOX_MAX_READ_BYTES 不应大于等于 SANDBOX_MAX_WRITE_BYTES："
                f"当前 read={self.max_read_bytes} write={self.max_write_bytes}。"
                "读上限超过写上限意味着写入规模与读取规模的边界认知不一致，"
                "请确认这两个值的语义后重新配置。"
            )
        return self


class AgentSettings(_GroupSettings):
    """前缀 AGENT_ → AGENT_MAX_STEPS / TOOL_MAX_RETRIES。"""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        env_prefix="AGENT_",
    )

    max_steps: int = Field(default=10, ge=1, le=50)
    # TOOL_MAX_RETRIES 不带 AGENT_ 前缀，显式声明环境变量名
    tool_max_retries: int = Field(default=2, ge=0, le=5, validation_alias="TOOL_MAX_RETRIES")


class LogSettings(_GroupSettings):
    """前缀 LOG_ → LOG_LEVEL / LOG_JSON。"""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        env_prefix="LOG_",
    )

    level: str = "INFO"
    # 字段名不能叫 json：会遮蔽 BaseModel.json()，触发
    # "Field name json shadows an attribute in parent BaseModel" 警告，
    # 且让 model_dump() / model_dump_json() 的行为变得不可预期。
    json_output: bool = True

    @field_validator("level")
    @classmethod
    def _check_level(cls, v: str) -> str:
        return _validate_log_level(v)


class Settings(BaseSettings):
    """根配置。

    各分组的 env_prefix 由分组自己声明，根只负责组装。
    为什么不在这里写 validation_alias="DEEPSEEK"：
    validation_alias 的语义是「一个完整的环境变量名」，不是前缀。
    对嵌套字段用它，pydantic-settings 会去找名为 DEEPSEEK 的环境变量，
    永远匹配不到 DEEPSEEK_API_KEY —— 表现为全部配置静默失效（实测确认）。
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    llm: LLMSettings = Field(default_factory=LLMSettings)
    web_search: WebSearchSettings = Field(default_factory=WebSearchSettings)
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    log: LogSettings = Field(default_factory=LogSettings)

    @computed_field
    @property
    def log_level_int(self) -> int:
        return LOG_LEVELS[self.log.level]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例。校验失败会抛 ValidationError —— 这是有意的快速失败。"""
    return Settings()