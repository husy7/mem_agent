"""日志与 Trace 上下文单元测试。"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Generator

import pytest

from memagent.observability.trace import (
    DEFAULT_FIELD_LIMIT,
    SENSITIVE_KEY_MARKERS,
    TruncatedRepr,
    bind_node,
    bind_trace,
    configure_logging,
    current_context,
    get_logger,
    new_trace_id,
    redact_sensitive,
    trace_scope,
)


class _Capture(io.StringIO):
    """捕获日志输出并逐行解析为 JSON。"""

    def json_lines(self) -> list[dict]:
        out: list[dict] = []
        for line in self.getvalue().splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out


@pytest.fixture
def captured() -> Generator[_Capture, None, None]:
    buf = _Capture()
    configure_logging(stream=buf)
    yield buf
    import structlog

    structlog.reset_defaults()


def test_json_output_is_valid_and_has_required_fields(captured: _Capture) -> None:
    """AGENTS.md 编码规范：日志必须含 trace_id / user_id / node 字段，JSON 格式。"""
    bind_trace("t-abc", user_id="u-1")
    bind_node("call_llm")
    get_logger(__name__).info("node_started", step=1)

    lines = captured.json_lines()
    assert len(lines) == 1, f"期望 1 行日志，实际 {len(lines)}"
    record = lines[0]
    print(f"\n  日志记录 = {json.dumps(record, ensure_ascii=False)}")
    for field in ("trace_id", "user_id", "node", "level", "event", "timestamp"):
        assert field in record, f"缺少必需字段 {field}"
        print(f"  ✓ {field:10s} = {record[field]!r}")
    assert record["trace_id"] == "t-abc"
    assert record["node"] == "call_llm"
    assert record["step"] == 1


def test_level_filtering_actually_drops(captured: _Capture, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    from memagent.config import get_settings

    get_settings.cache_clear()
    configure_logging(stream=captured)
    log = get_logger("level-test")
    log.debug("dropped-debug")
    log.info("dropped-info")
    log.warning("kept-warning")
    events = [r["event"] for r in captured.json_lines()]
    print(f"\n  捕获到的事件 = {events}")
    assert events == ["kept-warning"], f"级别过滤没生效：{events}"
    get_settings.cache_clear()


def test_sensitive_fields_are_redacted(captured: _Capture) -> None:
    """全局禁止事项：敏感信息不得写入日志。"""
    get_logger("sec").info(
        "llm_call",
        api_key="sk-real-secret-value",
        nested={"db_password": "p@ss", "model": "deepseek-flash"},
        Authorization="Bearer abc",
        safe_field="keep-me",
    )
    record = captured.json_lines()[0]
    print(f"\n  {json.dumps(record, ensure_ascii=False)}")
    assert record["api_key"] == "[REDACTED]"
    assert record["Authorization"] == "[REDACTED]"
    assert record["nested"]["db_password"] == "[REDACTED]"
    assert record["safe_field"] == "keep-me"
    assert "sk-real-secret-value" not in captured.getvalue(), "密钥泄漏到日志里了！"
    print("  判定：密钥未出现在原始输出中 ✓")


def test_sensitive_markers_cover_common_credential_names() -> None:
    for name in ("deepseek_api_key", "LANGFUSE_SECRET_KEY", "postgres_dsn", "access_token", "refresh_token"):
        event = redact_sensitive(None, "info", {name: "x"})
        assert event[name] == "[REDACTED]", f"{name} 未被脱敏"
    print(f"\n  已覆盖标记 = {SENSITIVE_KEY_MARKERS}")
    print("  判定：常见凭据字段名全部命中 ✓")


def test_token_metrics_are_not_redacted(captured: _Capture) -> None:
    """token 计量字段必须保留——阶段三的验收要看 token 消耗。

    实测教训：'token' 作为子串标记会命中 prompt_tokens / output_tokens /
    total_tokens / reasoning_tokens / cached_tokens 并全部脱敏，
    导致 AGENTS.md §4.8「generation 记录 model、token、cost」无法实现。
    """
    get_logger("usage").info(
        "llm_response",
        prompt_tokens=1234,
        output_tokens=567,
        total_tokens=1801,
        reasoning_tokens=321,
        cached_tokens=100,
        api_key="sk-must-not-leak",
        access_token="tok-must-not-leak",
    )
    record = captured.json_lines()[0]
    print(f"\n  {json.dumps(record, ensure_ascii=False)}")
    for metric in ("prompt_tokens", "output_tokens", "total_tokens", "reasoning_tokens", "cached_tokens"):
        assert record[metric] != "[REDACTED]", f"{metric} 是计量字段，不该被脱敏"
        assert isinstance(record[metric], int), f"{metric} 类型被破坏：{type(record[metric])}"
        print(f"  ✓ {metric:18s} = {record[metric]}（保留，且是 int）")
    for credential in ("api_key", "access_token"):
        assert record[credential] == "[REDACTED]", f"{credential} 是凭据，必须脱敏"
        print(f"  ✓ {credential:18s} = [REDACTED]")
    raw = captured.getvalue()
    assert "sk-must-not-leak" not in raw and "tok-must-not-leak" not in raw
    print("  判定：凭据脱敏 + token 计量保留，两类诉求同时满足 ✓")


def test_is_sensitive_classification_table() -> None:
    """把分类规则本身锁进测试，避免以后有人把 token 塞回子串表。"""
    from memagent.observability.trace import _is_sensitive

    should_redact = [
        "api_key",
        "apikey",
        "access_token",
        "auth_token",
        "refresh_token",
        "TOKEN",
        "token",
        "Authorization",
        "db_password",
        "LANGFUSE_SECRET_KEY",
        "postgres_dsn",
    ]
    should_keep = [
        "prompt_tokens",
        "output_tokens",
        "input_tokens",
        "total_tokens",
        "reasoning_tokens",
        "cached_tokens",
        "max_output_tokens",
        "elapsed_ms",
        "step",
        "tool",
        "model",
    ]
    print()
    for key in should_redact:
        assert _is_sensitive(key), f"{key} 应该被判定为敏感"
        print(f"  🔒 {key}")
    for key in should_keep:
        assert not _is_sensitive(key), f"{key} 不该被判定为敏感"
        print(f"  🔓 {key}")
    print("  判定：分类表正确 ✓")


def test_truncated_repr_bounds_log_line_size(captured: _Capture) -> None:
    """实测：不截断时 10 万字符字段 → 10 万字符日志行。"""
    big = "x" * 100_000
    get_logger("big").info("tool_output", payload=TruncatedRepr(big))
    record = captured.json_lines()[0]
    limit = DEFAULT_FIELD_LIMIT
    print(f"\n  原始 100000 字符 -> 字段 {len(record['payload'])} 字符（上限 {limit} + 截断标记）")
    assert len(record["payload"]) < limit + 100
    assert "truncated" in record["payload"]


def test_truncated_repr_respects_explicit_limit() -> None:
    """这个测试专门抓「参数存在但不生效」的 bug。"""
    wrapped = TruncatedRepr("y" * 5000, limit=50)
    text = repr(wrapped)
    print(f"\n  limit=50 -> 实际长度 {len(text)}")
    assert len(text) < 200, f"limit 参数没生效，长度 {len(text)}"


def test_trace_scope_cleans_up_on_exception() -> None:
    """异常路径必须也清理上下文，否则 trace_id 会泄漏到下一次请求。"""
    with pytest.raises(RuntimeError):
        with trace_scope(user_id="u-9") as tid:
            assert current_context()["trace_id"] == tid
            raise RuntimeError("节点炸了")
    leftover = current_context()
    print(f"\n  异常后残留上下文 = {leftover}")
    assert leftover == {}, f"上下文泄漏：{leftover}"


def test_trace_scope_isolates_concurrent_tasks() -> None:
    """contextvars 的协程隔离性——阶段二异步 update_memory 依赖这个行为。"""
    import asyncio

    seen: dict[str, str] = {}

    async def worker(name: str) -> None:
        with trace_scope(user_id=name) as tid:
            seen[name] = tid
            await asyncio.sleep(0.01)
            assert current_context()["user_id"] == name, "协程间上下文串了"

    async def main() -> None:
        await asyncio.gather(worker("alice"), worker("bob"))

    asyncio.run(main())
    print(f"\n  alice={seen['alice']}  bob={seen['bob']}")
    assert seen["alice"] != seen["bob"]
    print("  判定：并发任务上下文互不污染 ✓")


def test_new_trace_id_is_unique_hex() -> None:
    a, b = new_trace_id(), new_trace_id()
    print(f"\n  {a} != {b}")
    assert a != b
    assert len(a) == 32
    int(a, 16)  # 必须是合法十六进制

def test_redaction_preserves_value_types(captured: _Capture) -> None:
    """脱敏不得破坏其他字段的类型。

    动机：阶段三要对 elapsed_ms 做 P95 聚合。若脱敏把所有值 repr 成字符串，
    日志就无法做数值分析了。
    """
    get_logger("types").info(
        "tool_call",
        step=1,
        elapsed_ms=18.3,
        retry=False,
        none_val=None,
        count_list=[1, 2, 3],
        tool="read",
        api_key="sk-secret",
    )
    record = captured.json_lines()[0]
    print(f"\n  {json.dumps(record, ensure_ascii=False)}")
    assert record["step"] == 1 and isinstance(record["step"], int)
    assert record["elapsed_ms"] == 18.3 and isinstance(record["elapsed_ms"], float)
    assert record["retry"] is False
    assert record["none_val"] is None
    assert record["count_list"] == [1, 2, 3]
    assert record["tool"] == "read", f"字符串被加了多余引号：{record['tool']!r}"
    assert record["api_key"] == "[REDACTED]"
    print("  判定：敏感字段脱敏 + 其他字段类型全部保真 ✓")


def test_repeated_configure_takes_effect(captured: _Capture, monkeypatch: pytest.MonkeyPatch) -> None:
    """重复调用 configure_logging 必须真的生效（抓「参数存在但无效」类缺陷）。"""
    from memagent.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    configure_logging(stream=captured)
    log = get_logger("again")
    log.info("info-should-be-dropped")
    log.error("error-should-appear")
    events = [r["event"] for r in captured.json_lines()]
    print(f"\n  第二次 configure 后的事件 = {events}")
    assert events == ["error-should-appear"]
    get_settings.cache_clear()


def test_fallback_path_still_redacts_secrets(captured: _Capture, monkeypatch: pytest.MonkeyPatch) -> None:
    """降级路径必须保留脱敏。

    实测教训：最初降级 processors 里没有 redact_sensitive，
    一旦配置读取失败，api_key 就会以明文写进日志——
    安全保护在降级路径上静默消失，这是最危险的一类降级。
    """
    import memagent.observability.trace as trace_module

    def boom() -> None:
        raise RuntimeError("模拟配置读取失败")

    monkeypatch.setattr(trace_module, "get_settings", boom)
    configure_logging(stream=captured)

    records = captured.json_lines()
    print(f"\n  降级警告 = {json.dumps(records[0], ensure_ascii=False)}")
    assert records[0]["event"] == "logging_degraded_to_fallback"
    assert "RuntimeError" in records[0]["reason"]

    get_logger("fallback").info("llm_call", api_key="sk-must-not-leak", safe="ok")
    secret_record = captured.json_lines()[1]
    print(f"  降级下的日志 = {json.dumps(secret_record, ensure_ascii=False)}")
    assert secret_record["api_key"] == "[REDACTED]", "降级路径下密钥没被脱敏！"
    assert "sk-must-not-leak" not in captured.getvalue(), "密钥明文出现在降级日志里！"
    print("  判定：降级路径仍受脱敏保护 ✓")


def test_configure_logging_has_no_dead_parameters() -> None:
    """锁住「参数存在但无效」这个缺陷类别。

    曾有一个 force 参数：structlog.configure() 并没有 force 形参，
    我们的函数也没用它，于是这个参数传了等于没传——
    而当时没有任何测试覆盖它，所以缺陷长期潜伏。
    """
    import inspect

    params = list(inspect.signature(configure_logging).parameters)
    print(f"\n  configure_logging 形参 = {params}")
    assert "force" not in params, "force 是死参数：传了不会生效，属于会说谎的接口"
    assert params[:1] == ["settings"]
    assert "stream" in params