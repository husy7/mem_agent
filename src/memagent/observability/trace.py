"""日志与 Trace 上下文。

为什么 trace_id 用 contextvars 而不是参数传递：
asyncio 下 contextvars 是「协程隔离」的——阶段二 update_memory 作为后台任务
并发跑时，它的 trace_id 不会污染主对话的日志。threading.local 在 async 下会串。

为什么脱敏放在 processor 层而不是调用点：
调用点会漏。放进 processor 后，所有日志自动受保护，写新模块时不需要记得这件事。
"""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, TextIO

import structlog
from structlog.contextvars import (
    bind_contextvars,
    clear_contextvars,
    get_contextvars,
    unbind_contextvars,
)

from memagent.config import PROJECT_ROOT, Settings, get_settings

# 输出中被替换成 [REDACTED] 的字段名（子串匹配，大小写不敏感）
SENSITIVE_KEY_MARKERS: tuple[str, ...] = (
    "api_key",
    "apikey",
    "authorization",
    "password",
    "secret",
    "dsn",
    "credential",
)

# "token" 需要单独处理，不能直接丢进上面的子串表。
# 实测教训：把 "token" 当子串匹配会命中 prompt_tokens / output_tokens /
# total_tokens 等**计量字段**并全部脱敏，而阶段三的验收正要看 token 消耗
# （AGENTS.md §4.8 要求 generation 记录 model / token / cost）。
# 误脱敏 = 可观测性损失；漏脱敏 = 安全事件。两者都要避免。
_TOKEN_METRIC_SUFFIXES: tuple[str, ...] = ("_tokens", "_tokens_total")

# 所有 token 凭据字段的判定：先看是否计量，再看是否含 token 词素
_TOKEN_FIELD_EXACT: frozenset[str] = frozenset({"token", "tokens"})

_SENSITIVE_PLACEHOLDER = "[REDACTED]"

# 单个日志字段的最大字符数。实测把 10 万字符字段写进日志会产出 20 万字符的日志行。
DEFAULT_FIELD_LIMIT = 500


class TruncatedRepr:
    """把任意值转成截断后的 repr 字符串。

    实测（scripts 探测）：未截断时 100_000 字符的字段产生 100_098 字符的日志行。
    截断后字段恒为 JSON 字符串，不破坏日志 schema。
    """

    __slots__ = ("_text",)

    def __init__(self, value: object, limit: int = DEFAULT_FIELD_LIMIT) -> None:
        text = repr(value)
        if len(text) <= limit:
            self._text = text
        else:
            self._text = f"{text[:limit]}...<truncated {len(text) - limit} chars>"

    def __repr__(self) -> str:
        return self._text


def _is_sensitive(key: str) -> bool:
    """判断字段名是否属于敏感信息。

    分两步，因为 token 这一个词同时出现在「凭据」和「计量」两种语义里：
      - access_token / refresh_token / TOKEN  -> 凭据，必须脱敏
      - prompt_tokens / output_tokens / total_tokens -> 计量，必须保留
    """
    lowered = key.lower()
    if any(marker in lowered for marker in SENSITIVE_KEY_MARKERS):
        return True

    # token 计量字段：保留（阶段三要看 token 消耗）
    if lowered.endswith(_TOKEN_METRIC_SUFFIXES):
        return False
    # token 凭据字段：脱敏
    if lowered in _TOKEN_FIELD_EXACT:
        return True
    return "_token" in lowered or "token_" in lowered


def redact_sensitive(
    _logger: object,
    _method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """structlog processor：对敏感 key 脱敏，同时保持其他字段的类型不变。

    为什么保持类型：实测把 step=1 变成 '1'、elapsed_ms=18.3 变成 '18.3'
    会让日志无法做数值聚合——而阶段三的性能验证（记忆检索 P95）正是靠
    对 elapsed_ms 做聚合。日志 schema 一破，可观测性就是假的。
    """
    cleaned: dict[str, Any] = {}
    for key, value in event_dict.items():
        if _is_sensitive(key):
            cleaned[key] = _SENSITIVE_PLACEHOLDER
        elif isinstance(value, dict):
            cleaned[key] = _redact_mapping(value)
        else:
            cleaned[key] = value
    return cleaned


def _redact_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    """递归处理嵌套 dict，让 {'llm': {'api_key': ...}} 这类结构也能脱敏。"""
    out: dict[str, Any] = {}
    for key, value in mapping.items():
        if _is_sensitive(key):
            out[key] = _SENSITIVE_PLACEHOLDER
        elif isinstance(value, dict):
            out[key] = _redact_mapping(value)
        else:
            out[key] = value
    return out


def configure_logging(
    settings: Settings | None = None,
    *,
    stream: TextIO | None = None,
) -> None:
    """配置全局 structlog。

    Args:
        settings: 显式传入配置（测试用）；缺省走 get_settings()。
        stream: 日志输出流；缺省 stderr。为什么是 stderr 不是 stdout：
            stdout 在 CLI 场景要留给程序自身输出，日志走 stderr 才能干净地管道分离。

    幂等：可安全重复调用，每次都会重建配置。
    不做「只在首次生效」的优化——那会让测试里改级别后重新配置静默失效，
    而实测确认 structlog.configure() 并没有 force 形参，
    任何"force 开关"都得自己实现；自己实现的开关若没有测试覆盖，
    就会变成一个会说谎的接口（传了参数、什么都没发生）。

    日志系统本身失败时降级为最简配置——否则"日志挂了导致业务挂了"。
    **降级路径同样必须保留 redact_sensitive**：实测降级时去掉它，
    api_key 会以明文写进日志。安全保护不能在降级路径上静默消失。
    """
    fallback_reason: str | None = None
    try:
        resolved = settings or get_settings()
        level_int = resolved.log_level_int
        renderer: Any = (
            structlog.processors.JSONRenderer(ensure_ascii=False)
            if resolved.log.json_output
            else structlog.dev.ConsoleRenderer()
        )
        processors: list[Any] = [
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_sensitive,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ]
    except Exception as exc:  # noqa: BLE001 - 配置读取失败时必须降级，不能连日志都没有
        fallback_reason = f"{type(exc).__name__}: {exc}"
        level_int = logging.INFO
        processors = [
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            # 降级路径必须保留脱敏，否则密钥会明文落盘
            redact_sensitive,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ]

    structlog.reset_defaults()
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level_int),
        logger_factory=structlog.WriteLoggerFactory(file=stream or sys.stderr),
        cache_logger_on_first_use=False,
    )

    # 降级了要说话：静默降级让人以为配置生效了，是最难查的一类问题。
    if fallback_reason is not None:
        get_logger(__name__).warning("logging_degraded_to_fallback", reason=fallback_reason)


def get_logger(name: str | None = None, **initial_context: Any) -> Any:
    """取 logger。名字建议用调用模块名，如 get_logger(__name__)。"""
    logger = structlog.get_logger(name) if name else structlog.get_logger()
    if initial_context:
        return logger.bind(**initial_context)
    return logger


def new_trace_id() -> str:
    return uuid.uuid4().hex


def bind_trace(trace_id: str, user_id: str | None = None, **extra: Any) -> None:
    """把 trace_id 绑到当前协程上下文，之后所有日志自动带上。"""
    payload: dict[str, Any] = {"trace_id": trace_id}
    if user_id is not None:
        payload["user_id"] = user_id
    payload.update(extra)
    bind_contextvars(**payload)


def bind_node(node: str, **extra: Any) -> None:
    """标记当前 LangGraph 节点名，便于按节点过滤日志。"""
    bind_contextvars(node=node, **extra)


def unbind(*keys: str) -> None:
    unbind_contextvars(*keys)


def current_context() -> dict[str, Any]:
    """读取当前 trace 上下文（阶段三写 Langfuse trace 时会用到）。"""
    return dict(get_contextvars())


@contextmanager
def trace_scope(trace_id: str | None = None, user_id: str | None = None, **extra: Any) -> Iterator[str]:
    """一次性会话的 trace 作用域。

    用 contextmanager 而不是要求调用方手写 bind/clear：
    手写必然出现某条异常路径忘记 clear，导致 trace_id 泄漏到下一次请求——
    这类 bug 在日志里表现为"两个不同请求共用一个 trace_id"，极难排查。
    """
    resolved = trace_id or new_trace_id()
    bind_trace(resolved, user_id=user_id, **extra)
    try:
        yield resolved
    finally:
        clear_contextvars()


def project_root() -> str:
    return str(PROJECT_ROOT)