"""配置层——项目所有配置的唯一入口。

================================================================================
这个模块做什么
================================================================================
把 ``.env`` 文件里的配置读进来，做类型转换 + 合法性校验，然后以分组对象的形式
提供给其他模块使用。它同时是整个项目的**启动开关**：

    配置有问题 → 构造 Settings 时立刻抛 ValidationError → 进程起不来

这是有意设计的。AGENTS.md 的全局降级路径写得很明确：
「配置缺失 → 启动时快速失败，不允许运行时静默降级」。
理由：一个配置错误的服务如果还能启动，它会带着错误的参数跑起来，
然后在某个不确定的时刻以某种不确定的方式出错——排查成本远高于启动时直接报错。

================================================================================
怎么用
================================================================================
    from memagent.config import get_settings

    settings = get_settings()
    settings.llm.model                     # 'deepseek-flash'
    settings.sandbox.max_commands          # 500
    settings.sandbox.workspace             # '/workspace'
    settings.agent.max_steps               # 10
    settings.log_level_int                 # 20（structlog 需要的整数值）

    # 测试里需要临时改配置时：
    get_settings.cache_clear()             # 清缓存，下次重新读取
    get_settings.cache_clear()             # 用完记得再清一次

**禁止**在任何其他模块里直接读 os.environ。所有配置必须经由本模块流入，
否则「配置从哪来」会散落到几十个文件里，改配置时无法确定影响范围。

================================================================================
环境变量命名规则（这是本项目最容易踩坑的地方，务必看懂）
================================================================================
每个分组有自己的 ``env_prefix``，变量名 = 前缀 + 字段名大写：

    组          前缀            字段                环境变量
    ----------  --------------  ------------------  ---------------------------
    llm         DEEPSEEK_       model               DEEPSEEK_MODEL
    llm         DEEPSEEK_       api_key             DEEPSEEK_API_KEY
    llm         DEEPSEEK_       timeout_seconds     DEEPSEEK_TIMEOUT_SECONDS
    sandbox     SANDBOX_        max_commands        SANDBOX_MAX_COMMANDS
    sandbox     SANDBOX_        workspace           SANDBOX_WORKSPACE
    web_search  WEB_SEARCH_     provider            WEB_SEARCH_PROVIDER
    agent       AGENT_          max_steps           AGENT_MAX_STEPS
    log         LOG_            level               LOG_LEVEL
    log         LOG_            json_output         LOG_JSON_OUTPUT

三个**必须记住的坑**（都是实测踩出来的，且都属于「静默失效」——不报错、不生效）：

    1. 变量名对不上就静默失效。
       曾经写 LLM_TIMEOUT_SECONDS（应为 DEEPSEEK_TIMEOUT_SECONDS），
       值没被读进来，程序照常启动，用默认值跑。因为 extra='ignore'
       会把所有不认识的变量直接吞掉，不报警。

    2. validation_alias 不是「前缀」，它是「一个完整的环境变量名」。
       在根 Settings 上给嵌套字段写 Field(validation_alias='DEEPSEEK')
       会让它去找一个叫 DEEPSEEK 的变量，永远匹配不到 DEEPSEEK_API_KEY。
       结果是全部配置静默失效，一个都不生效。

    3. validation_alias 会**覆盖** env_prefix。
       agent 组的 tool_max_retries 写了 validation_alias='TOOL_MAX_RETRIES'，
       所以生效的是 TOOL_MAX_RETRIES，而不是 AGENT_TOOL_MAX_RETRIES。

配置来源决策：**只从 .env 读，不依赖 OS 环境变量**。
OS 环境变量虽然优先级更高（pydantic-settings 的既定行为），但本项目不依赖它，
以免"同样的 .env 在不同机器上跑出不同结果"。
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from pydantic import Field, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ==============================================================================
# 常量
# ==============================================================================

# 项目根目录。
# __file__ 是 .../src/memagent/config.py
#   parents[0] -> .../src/memagent
#   parents[1] -> .../src
#   parents[2] -> 项目根（.env 和 pyproject.toml 所在处）
# 为什么不硬编码绝对路径：这样 checkout 到任意目录都能工作。
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]

# .env 文件路径。构建成绝对路径，避免受进程工作目录影响。
ENV_FILE: Path = PROJECT_ROOT / ".env"

# 日志级别名 → standard logging 的整数值。
# 为什么要这张表：structlog 的 make_filtering_bound_logger() 需要 int，
# 而 .env 里写的是人可读的字符串 'INFO'。表放在这里而不是散落的 if/elif，
# 好处是单元测试可以直接验证这张表，不需要把映射逻辑重写一遍。
LOG_LEVELS: dict[str, int] = {
    "DEBUG": logging.DEBUG,        # 10
    "INFO": logging.INFO,          # 20
    "WARNING": logging.WARNING,    # 30
    "ERROR": logging.ERROR,        # 40
    "CRITICAL": logging.CRITICAL,  # 50
}

# 被判定为「没填真实 key」的占位符特征（子串匹配，大小写不敏感）。
#
# 为什么需要这个名单：.env 通常是照抄 .env.example 来的，而 `sk-your-key-here`
# 完全符合 `sk-` 前缀规则，会被当成真 key 放行。后果是把「你还没配 key」
# 伪装成「key 有效」，直到真正调用 LLM 时才报 401，排查成本高得多。
# 宁可启动时明确说「你填的是占位符」。
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

# 分组级 BaseSettings 的公共配置。
#
# 为什么每行都写一遍而不是抽成一个 dict 变量共享：
# pydantic-settings 在**类创建时**读取 model_config，且各分组的 env_prefix 不同，
# 所以每组都得有自己的完整 model_config。共享一个 dict 再修改会互相污染。
_COMMON_CONFIG: dict[str, object] = {
    "env_file": ENV_FILE,
    "env_file_encoding": "utf-8",
    "extra": "ignore",        # 不认识的环境变量直接忽略（本机常有很多无关变量）
    "case_sensitive": False,  # LOG_LEVEL 和 log_level 都认
}


# ==============================================================================
# 校验函数（纯函数，独立于任何类，便于单元测试直接调用）
# ==============================================================================
#
# 为什么把这些写成模块级纯函数，而不是直接写在 field_validator 里：
# 纯函数可以被测试直接调用，不需要构造 Settings、不需要处理 lru_cache 缓存、
# 不需要 monkeypatch 环境变量。可测试性是设计出来的，不是事后补出来的。


def _validate_log_level(value: str) -> str:
    """把日志级别字符串规范化，并拒绝非法值。

    Args:
        value: 待校验的日志级别，大小写不敏感，允许首尾空格。

    Returns:
        规范化后的大写级别名，例如 'INFO'。

    Raises:
        ValueError: 值不在 LOG_LEVELS 中时抛出。
    """
    normalized = value.strip().upper()
    if normalized not in LOG_LEVELS:
        allowed = " / ".join(LOG_LEVELS)
        raise ValueError(f"LOG_LEVEL 必须是 {allowed} 之一，收到 {value!r}")
    return normalized


def _validate_workspace(value: str) -> str:
    """校验沙箱工作目录，必须是 VFS（虚拟文件系统）内的绝对路径。

    为什么必须是 `/` 开头的绝对路径：
    bashkit 的 VFS 与宿主机文件系统完全隔离。写 `workspace`（相对路径）
    或 `C:\\\\data`（Windows 路径）会让 read/write 工具拼出不可预期的结果——
    它们是在 VFS 里找路径，而 VFS 的根就是 `/`。

    Args:
        value: 待校验的 VFS 路径。

    Returns:
        规范化后的路径：去掉首尾空格、去掉结尾多余的 `/`，根目录归一为 '/'。

    Raises:
        ValueError: 不是以 `/` 开头的绝对路径时抛出。
    """
    normalized = value.strip()
    if not normalized.startswith("/"):
        raise ValueError(
            f"SANDBOX_WORKSPACE 必须是 VFS 绝对路径（以 / 开头），收到 {value!r}。"
            "注意这是沙箱内路径，不是宿主机路径。"
        )
    # 结尾的 '/' 去掉，但根目录 '/' 要保留
    return normalized.rstrip("/") or "/"


def _validate_api_key_shape(key: str | None) -> str | None:
    """校验 API Key 的**形状**，不校验真实性。

    真实性无法在本地判断，只能靠一次真实请求验证。这里只挡住两类必然错误：
    占位符（没填 key）和前缀不对（填错了别的服务的 key）。

    Args:
        key: 待校验的 key。None 或纯空白表示「未配置」，是合法状态。

    Returns:
        去空格后的 key；未配置时返回 None。

    Raises:
        ValueError: 命中占位符名单，或不是以 'sk-' 开头时抛出。
    """
    # 未配置是合法的：单元测试和部分功能不需要真实 key
    if key is None:
        return None
    cleaned = key.strip()
    if not cleaned:
        return None

    # 先查占位符，再查前缀——这样报错信息更贴近真实原因
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


# ==============================================================================
# 配置分组
# ==============================================================================


class _GroupSettings(BaseSettings):
    """所有配置分组的共同基类。

    **为什么每个分组必须继承 BaseSettings 而不是 BaseModel**（实测踩过的坑）：
    pydantic-settings 只对 BaseSettings 的子类做「环境变量 → 字段」的映射。
    如果分组写成 BaseModel，它只做结构校验、完全不读环境变量，
    表现为所有配置静默失效且不报任何错——极难排查。

    **为什么 .env 是绝对路径**：
    相对路径会随进程工作目录变化。写成绝对路径后，无论从哪里启动都能读到同一个 .env。
    """

    model_config = SettingsConfigDict(**_COMMON_CONFIG)  # type: ignore[arg-type]


class LLMSettings(_GroupSettings):
    """LLM 相关配置。

    环境变量前缀：``DEEPSEEK_``
        字段                环境变量                    默认值
        ------------------  --------------------------  ------------------------------
        api_key             DEEPSEEK_API_KEY            None（未配置）
        base_url            DEEPSEEK_BASE_URL           https://api.deepseek.com
        model               DEEPSEEK_MODEL              deepseek-flash
        timeout_seconds     DEEPSEEK_TIMEOUT_SECONDS    60.0
        max_retries         DEEPSEEK_MAX_RETRIES        2

    注意模型名：官方现行模型名是 ``deepseek-flash``（底层 DeepSeek-V4.1-Flash）。
    旧名 ``deepseek-v4-flash`` 仍可调用但会被路由到新模型，不要再用。
    """

    model_config = SettingsConfigDict(**_COMMON_CONFIG, env_prefix="DEEPSEEK_")  # type: ignore[arg-type]

    # 选填：没申请到 key 也能跑全部单元测试，只有真正调用 LLM 时才需要
    api_key: str | None = None
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    timeout_seconds: float = Field(default=60.0, gt=0)
    max_retries: int = Field(default=2, ge=0, le=10)

    @field_validator("api_key", mode="before")
    @classmethod
    def _check_key_shape(cls, value: object) -> object:
        """mode='before' 表示在类型转换**之前**执行，此时值可能还不是 str。"""
        return _validate_api_key_shape(
            value if isinstance(value, str) or value is None else str(value)
        )


class WebSearchSettings(_GroupSettings):
    """web_search 工具配置。

    环境变量前缀：``WEB_SEARCH_``
        字段         环境变量                  默认值
        -----------  ------------------------  ----------
        provider     WEB_SEARCH_PROVIDER       tavily
        api_key      WEB_SEARCH_API_KEY        None
        max_results  WEB_SEARCH_MAX_RESULTS    5

    **为什么 web_search 是 client-side 实现（本项目自己调用搜索 API），
    而不是像原设计那样声明成「DeepSeek 服务端内置工具」**：
    DeepSeek Responses API 的 ``tools`` 参数只支持 ``function`` 类型。
    ``web_search`` / ``file_search`` / ``code_interpreter`` 等内置工具
    会被服务端**静默忽略**——不报错，也不生效（官方兼容性文档明确列出）。
    所以工具名对模型保持不变，但执行方改成我们自己的代码。
    """

    model_config = SettingsConfigDict(**_COMMON_CONFIG, env_prefix="WEB_SEARCH_")  # type: ignore[arg-type]

    provider: str = "tavily"
    api_key: str | None = None
    max_results: int = Field(default=5, ge=1, le=20)

    @field_validator("api_key", mode="before")
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        """把 `WEB_SEARCH_API_KEY=` 这种空值归一成 None，而不是空字符串。

        为什么：空字符串和 None 在业务代码里语义不同但很容易混淆。
        统一成 None，上层只需判断 `if settings.web_search.api_key is None` 一种情况。
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value


class SandboxSettings(_GroupSettings):
    """bashkit 沙箱配置。

    环境变量前缀：``SANDBOX_``
        字段                环境变量                       默认值
        ------------------  -----------------------------  ----------
        workspace           SANDBOX_WORKSPACE              /workspace
        max_commands        SANDBOX_MAX_COMMANDS           500
        max_loop_iterations SANDBOX_MAX_LOOP_ITERATIONS    5000
        timeout_seconds     SANDBOX_TIMEOUT_SECONDS        30.0
        max_read_bytes      SANDBOX_MAX_READ_BYTES         51200（50 KB）
        max_write_bytes     SANDBOX_MAX_WRITE_BYTES        1048576（1 MB）
    """

    model_config = SettingsConfigDict(**_COMMON_CONFIG, env_prefix="SANDBOX_")  # type: ignore[arg-type]

    workspace: str = "/workspace"
    max_commands: int = Field(default=500, ge=1)
    max_loop_iterations: int = Field(default=5000, ge=1)
    timeout_seconds: float = Field(default=30.0, gt=0)
    max_read_bytes: int = Field(default=51_200, ge=1)
    max_write_bytes: int = Field(default=1_048_576, ge=1)

    @field_validator("workspace")
    @classmethod
    def _check_workspace(cls, value: str) -> str:
        return _validate_workspace(value)

    @model_validator(mode="after")
    def _check_read_write_bounds(self) -> SandboxSettings:
        """跨字段校验：读取上限必须小于写入上限。

        两个字段的语义（理解它们才能看懂这条规则）：
            max_write_bytes  单个文件允许写入的**总大小**上限
            max_read_bytes   单次 read 调用返回的**分块长度**上限

        为什么要求 read < write 而不是反过来：
            一个合法写入的文件（大小 ≤ write）很可能大于单次读取上限，
            这由 offset/limit 分块读取解决，是正常场景。
            但如果 read ≥ write，就意味着「读取规模」的边界认知
            比「写入规模」还大——这说明配置者没想清楚这两个值的含义，
            应当快速失败，而不是让它跑到线上才暴露。

        注意 model_validator(mode='after') 必须**写在类内部**。
        曾经因为缩进错误写在模块顶层，导致校验器从未注册、
        非法配置全部静默通过（这个 bug 由单元测试抓出）。
        """
        if self.max_read_bytes >= self.max_write_bytes:
            raise ValueError(
                "SANDBOX_MAX_READ_BYTES 不应大于等于 SANDBOX_MAX_WRITE_BYTES："
                f"当前 read={self.max_read_bytes} write={self.max_write_bytes}。"
                "两个值的语义分别是「单次读取分块上限」和「单文件写入总上限」，"
                "请确认后重新配置。"
            )
        return self


class AgentSettings(_GroupSettings):
    """Agent 循环与工具重试的终止条件。

    环境变量前缀：``AGENT_``
        字段              环境变量              默认值
        ----------------  --------------------  ----------
        max_steps         AGENT_MAX_STEPS       10
        tool_max_retries  TOOL_MAX_RETRIES      2   ← 注意：不带 AGENT_ 前缀

    为什么 tool_max_retries 的变量名不带 AGENT_ 前缀：
    它用了 validation_alias='TOOL_MAX_RETRIES'，而 **validation_alias 会覆盖
    env_prefix**（实测确认）。所以 AGENT_TOOL_MAX_RETRIES 反而**不生效**。
    这是刻意保留的：该字段描述的是「工具」的重试策略，脱开 agent 前缀更贴切。
    """

    model_config = SettingsConfigDict(**_COMMON_CONFIG, env_prefix="AGENT_")  # type: ignore[arg-type]

    max_steps: int = Field(default=10, ge=1, le=50)
    tool_max_retries: int = Field(default=2, ge=0, le=5, validation_alias="TOOL_MAX_RETRIES")


class LogSettings(_GroupSettings):
    """日志配置。

    环境变量前缀：``LOG_``
        字段         环境变量             默认值
        -----------  -------------------  ----------
        level        LOG_LEVEL            INFO
        json_output  LOG_JSON_OUTPUT      true

    **为什么字段名叫 json_output 而不是 json**（踩过的坑）：
    如果字段叫 `json`，会遮蔽 pydantic BaseModel 自带的 `json()` 方法，
    pydantic 会发警告 "Field name json shadows an attribute in parent BaseModel"。
    更严重的是：`if settings.log.json:` 取到的是**绑定方法对象**（永远为真值），
    配置里的开关彻底失效且不报任何错。改名后同时消除了警告和这个隐患。
    """

    model_config = SettingsConfigDict(**_COMMON_CONFIG, env_prefix="LOG_")  # type: ignore[arg-type]

    level: str = "INFO"
    json_output: bool = True

    @field_validator("level")
    @classmethod
    def _check_level(cls, value: str) -> str:
        return _validate_log_level(value)


# ==============================================================================
# 根配置
# ==============================================================================


class Settings(BaseSettings):
    """项目配置的根对象，把所有分组组装在一起。

    使用方式：
        settings = get_settings()          # 推荐（进程内单例）
        settings.llm.model                 # 分组访问
        settings.log_level_int             # 计算属性

    各组的环境变量前缀由**分组自己**声明，根只负责组装。
    """

    model_config = SettingsConfigDict(**_COMMON_CONFIG)  # type: ignore[arg-type]

    llm: LLMSettings = Field(default_factory=LLMSettings)
    web_search: WebSearchSettings = Field(default_factory=WebSearchSettings)
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    log: LogSettings = Field(default_factory=LogSettings)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def log_level_int(self) -> int:
        """日志级别的整数值，供 structlog 的 make_filtering_bound_logger() 使用。

        为什么做成计算属性而不是独立字段：
        它是由 log.level 完全决定的派生值，独立字段会产生「两者不一致」的可能。
        计算属性保证两者永远同步。
        """
        return LOG_LEVELS[self.log.level]


# ==============================================================================
# 单例入口
# ==============================================================================


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取配置单例（进程内只构造一次）。

    为什么用 lru_cache 而不是模块级 `_settings = Settings()`：
    模块级写法在 import 时就执行构造，测试无法重置。
    lru_cache 是惰性的，且提供 cache_clear() 让测试可以重新读取配置。

    Returns:
        Settings 实例。

    Raises:
        ValidationError: 任何配置非法时抛出。这是**有意的快速失败**，
            调用方不要 try/except 把它吞掉——配置错误必须让进程起不来。

    测试里的用法：
        get_settings.cache_clear()     # 改动环境变量后重新读取
        ...
        get_settings.cache_clear()     # 测试结束记得再清一次
    """
    return Settings()