"""阶段一步骤 2 的成品演示：真实 JSON 日志长什么样。

这是最终产物——看到它才算这一步真的做完了。
"""

from __future__ import annotations

from memagent.config import get_settings
from memagent.observability.trace import (
    TruncatedRepr,
    bind_node,
    bind_trace,
    configure_logging,
    current_context,
    get_logger,
    trace_scope,
)

configure_logging()
settings = get_settings()
log = get_logger("demo")

print("#" * 78, flush=True)
print("# 以下每行都是 stderr 上的合法 JSON —— 这就是你的日志形态", flush=True)
print("#" * 78, flush=True)

log.info(
    "memagent_starting",
    model=settings.llm.model,
    workspace=settings.sandbox.workspace,
    max_steps=settings.agent.max_steps,
    log_level=settings.log.level,
)

with trace_scope(user_id="husy7") as trace_id:
    bind_node("call_llm")
    log.info("node_started", step=1, tool_count=4)

    log.info(
        "tool_invoked",
        tool="read",
        args={"path": "/workspace/report.md", "offset": 0, "limit": 200},
        elapsed_ms=18.3,
        retry=False,
    )

    # 大字段走 TruncatedRepr，防止日志行爆炸（实测 10 万字符 -> 20 万字符日志行）
    log.info("tool_result", tool="bash", output=TruncatedRepr("x" * 10_000))

    # 敏感字段自动脱敏（processor 层保护，不依赖调用点自觉）
    log.info("llm_request", api_key=settings.llm.api_key, model=settings.llm.model, prompt_tokens=1234)

    log.warning("sandbox_slow", tool="bash", elapsed_ms=1830.5, threshold_ms=1000)

    try:
        raise ValueError("模拟工具执行失败")
    except ValueError:
        log.error("tool_failed", tool="bash", fallback="read", exc_info=True)

    log.info("node_finished", step=1, context=current_context())

print("#" * 78, flush=True)
print("# 日志结束。注意 trace_id 贯穿所有行，user_id/node 自动带上，", flush=True)
print("# 密钥显示为 [REDACTED]，elapsed_ms 仍是数字而不是字符串。", flush=True)
print("#" * 78, flush=True)