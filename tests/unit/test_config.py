"""配置层单元测试。

本项目的配置决策：**只从 .env 读**，不依赖 OS 环境变量。
因此测试里区分两件事，用两个测试分别覆盖：
    1. 字段默认值（pydantic 层）—— 需要显式隔离 .env 才测得准
    2. .env 实际生效值（运行配置）—— 断言读到了 .env，不断言具体数值

隔离手段（实测结论，别再用别的写法）：
    ❌ Settings(_env_file=None)        —— 各分组继承了根的 env_file，管不到它们
    ❌ LLMSettings(_env_file=None)     —— 同上，仍然读 .env
    ❌ patch PROJECT_ROOT              —— env_file 在类定义时就固定成绝对路径了
    ✅ 临时替换分组的 model_config['env_file'] = None  —— 唯一有效，见 env_isolated()

注意：这里用 print 输出测试判定过程是刻意的——
全局禁止事项禁的是「用 print 调试业务代码」，不是禁测试报告结果。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from pydantic import ValidationError

from memagent.config import (
    LOG_LEVELS,
    PLACEHOLDER_KEY_MARKERS,
    Settings,
    _validate_api_key_shape,
    _validate_log_level,
    _validate_workspace,
)


@contextmanager
def env_isolated(*groups: type[Any]) -> Iterator[None]:
    """临时让指定分组不读 .env，测「纯字段默认值」用。

    原理：env_file 在类定义时就被写进 model_config 了，实例级参数改不动它，
    只能临时替换类属性。必须还原，否则污染后续所有测试。
    """
    from memagent.config import _GroupSettings  # noqa: PLC0415 - 只在测试里需要

    targets = groups or tuple(_GroupSettings.__subclasses__())
    saved: list[tuple[type[Any], dict[str, Any]]] = []
    for group in targets:
        saved.append((group, dict(group.model_config)))
        patched = dict(group.model_config)
        patched["env_file"] = None
        group.model_config = patched  # type: ignore[assignment]
    try:
        yield
    finally:
        for group, original in saved:
            group.model_config = original  # type: ignore[assignment]

# 所有会被本测试触及的环境变量。任何测试结束后都必须清干净，
# 否则本机 shell 里真设了同名变量（例如你申请到 key 后 export 了），
# 测试结果会被环境污染，且失败原因极难定位。
_ENV_KEYS = (
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_MODEL",
    "DEEPSEEK_TIMEOUT_SECONDS",
    "DEEPSEEK_MAX_RETRIES",
    "WEB_SEARCH_PROVIDER",
    "WEB_SEARCH_API_KEY",
    "WEB_SEARCH_MAX_RESULTS",
    "SANDBOX_WORKSPACE",
    "SANDBOX_MAX_COMMANDS",
    "SANDBOX_MAX_LOOP_ITERATIONS",
    "SANDBOX_TIMEOUT_SECONDS",
    "SANDBOX_MAX_READ_BYTES",
    "SANDBOX_MAX_WRITE_BYTES",
    "AGENT_MAX_STEPS",
    "TOOL_MAX_RETRIES",
    "LOG_LEVEL",
    "LOG_JSON_OUTPUT",
)


@pytest.fixture(autouse=True)
def sanitize_env():
    """每个测试前清空相关环境变量并清配置缓存，保证相互独立。"""
    saved = {k: os.environ.pop(k, None) for k in _ENV_KEYS}
    from memagent.config import get_settings

    get_settings.cache_clear()
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    get_settings.cache_clear()


def test_log_level_table_matches_stdlib() -> None:
    import logging

    print(f"\n  LOG_LEVELS = {LOG_LEVELS}")
    assert LOG_LEVELS["DEBUG"] == logging.DEBUG
    assert LOG_LEVELS["INFO"] == logging.INFO
    assert LOG_LEVELS["WARNING"] == logging.WARNING
    assert LOG_LEVELS["ERROR"] == logging.ERROR
    assert LOG_LEVELS["CRITICAL"] == logging.CRITICAL
    print("  判定：级别表与 stdlib 一一对应")


def test_validate_log_level_normalizes_and_rejects() -> None:
    assert _validate_log_level("  info ") == "INFO"
    assert _validate_log_level("Warning") == "WARNING"
    print("\n  '  info ' -> 'INFO'")
    print("  'Warning' -> 'WARNING'")
    with pytest.raises(ValueError, match="LOG_LEVEL"):
        _validate_log_level("VERBOSE")
    print("  'VERBOSE' -> ValueError ✓")


def test_validate_workspace_requires_vfs_absolute() -> None:
    assert _validate_workspace("/workspace/") == "/workspace"
    assert _validate_workspace("/") == "/"
    assert _validate_workspace("  /data  ") == "/data"
    print("\n  '/workspace/' -> '/workspace'")
    print("  '/' -> '/'")
    for bad in ("workspace", "C:\\data", "", "  "):
        with pytest.raises(ValueError, match="VFS 绝对路径"):
            _validate_workspace(bad)
        print(f"  {bad!r} -> ValueError ✓")


def test_api_key_shape_rules() -> None:
    assert _validate_api_key_shape(None) is None
    assert _validate_api_key_shape("   ") is None
    assert _validate_api_key_shape(" sk-abc123 ") == "sk-abc123"
    print("\n  None / '   ' -> None（选填：没申请到 key 也能跑测试）")
    print("  ' sk-abc123 ' -> 'sk-abc123'（去空格）")
    with pytest.raises(ValueError, match="sk-"):
        _validate_api_key_shape("my-key-without-prefix")
    print("  'my-key-without-prefix' -> ValueError ✓")


def test_api_key_placeholders_are_rejected() -> None:
    """照抄 .env.example 的占位符必须被拦，否则'token 无效'会伪装成'配置正确'。"""
    print(f"\n  占位符标记 = {PLACEHOLDER_KEY_MARKERS}")
    for placeholder in ("sk-your-key-here", "sk-YOUR_KEY_HERE", "sk-changeme", "sk-xxxxxxxx"):
        with pytest.raises(ValueError, match="占位符"):
            _validate_api_key_shape(placeholder)
        print(f"  {placeholder!r:24s} -> ValueError ✓")
    print("  真实 key 形状：")
    for real in ("sk-9f8a7b6c5d4e", "sk-proj-AbCdEf1234"):
        assert _validate_api_key_shape(real) == real
        print(f"  {real!r:24s} -> 通过 ✓")


def test_model_defaults_are_sane() -> None:
    """字段默认值必须整体自洽（显式隔离 .env 后才测得准）。

    为什么不测 Settings() 的"默认值"：各分组继承了根的 env_file 配置，
    所以 Settings() 和直接实例化分组都会读 .env，拿到的是运行配置而不是默认值。
    实测三种隔离写法里只有「临时替换 model_config['env_file']」有效。
    """
    from memagent.config import (
        AgentSettings,
        LLMSettings,
        LogSettings,
        SandboxSettings,
        WebSearchSettings,
    )

    with env_isolated(LLMSettings, SandboxSettings, AgentSettings, LogSettings, WebSearchSettings):
        llm = LLMSettings()
        sandbox = SandboxSettings()
        agent = AgentSettings()
        log = LogSettings()
        web = WebSearchSettings()
        cs = Settings()

    print(f"\n  llm.model              = {llm.model!r}")
    print(f"  llm.api_key            = {llm.api_key!r}")
    print(f"  llm.base_url           = {llm.base_url!r}")
    print(f"  llm.timeout_seconds    = {llm.timeout_seconds!r}")
    print(f"  llm.max_retries        = {llm.max_retries!r}")
    print(f"  sandbox.workspace      = {sandbox.workspace!r}")
    print(f"  sandbox.max_commands   = {sandbox.max_commands!r}")
    print(f"  sandbox.max_read_bytes = {sandbox.max_read_bytes!r}")
    print(f"  sandbox.max_write_bytes= {sandbox.max_write_bytes!r}")
    print(f"  sandbox.timeout_seconds= {sandbox.timeout_seconds!r}")
    print(f"  web_search.provider    = {web.provider!r}")
    print(f"  web_search.max_results = {web.max_results!r}")
    print(f"  agent.max_steps        = {agent.max_steps!r}")
    print(f"  agent.tool_max_retries = {agent.tool_max_retries!r}")
    print(f"  log.level              = {log.level!r}")
    print(f"  log.json_output        = {log.json_output!r}")
    print(f"  Settings().log_level_int = {cs.log_level_int}")

    assert llm.model == "deepseek-flash"
    assert llm.api_key is None
    assert llm.base_url == "https://api.deepseek.com"
    assert llm.timeout_seconds == 60.0
    assert llm.max_retries == 2
    assert sandbox.workspace == "/workspace"
    assert sandbox.max_commands == 500
    assert sandbox.max_loop_iterations == 5000
    assert sandbox.timeout_seconds == 30.0
    assert sandbox.max_read_bytes < sandbox.max_write_bytes
    assert web.provider == "tavily"
    assert web.max_results == 5
    assert agent.max_steps == 10
    assert agent.tool_max_retries == 2
    assert log.level == "INFO"
    assert log.json_output is True
    assert cs.log_level_int == LOG_LEVELS["INFO"]


def test_defaults_test_independent_of_env_file_content() -> None:
    """回归：默认值测试不能在 .env 内容变化时失败。

    踩过的坑：test_model_defaults_are_sane 最初写成 LLMSettings()，
    而各分组会读 .env，于是它断言的是「.env 的值」却用字段默认值去比。
    表现为：单独跑失败、全量跑通过（因为全量时别的测试先清了环境变量），
    —— 「测试结果依赖执行集合」是测试设计缺陷，不是环境问题。

    本测试用差异值改写 .env，验证隔离手段在 .env 变化时仍然成立。
    """
    from dotenv import dotenv_values

    from memagent.config import PROJECT_ROOT, AgentSettings, LogSettings, SandboxSettings

    env_path = PROJECT_ROOT / ".env"
    backup = env_path.read_text(encoding="utf-8")
    mutated = (
        backup.replace("SANDBOX_MAX_COMMANDS=500", "SANDBOX_MAX_COMMANDS=777")
        .replace("AGENT_MAX_STEPS=10", "AGENT_MAX_STEPS=33")
        .replace("LOG_LEVEL=INFO", "LOG_LEVEL=DEBUG")
    )
    if mutated == backup:
        pytest.skip(".env 中没有可用于判别的键，跳过")

    try:
        env_path.write_text(mutated, encoding="utf-8")
        loaded = dotenv_values(env_path)
        print(f"\n  改写 .env：SANDBOX_MAX_COMMANDS={loaded.get('SANDBOX_MAX_COMMANDS')} "
              f"AGENT_MAX_STEPS={loaded.get('AGENT_MAX_STEPS')} LOG_LEVEL={loaded.get('LOG_LEVEL')}")

        assert SandboxSettings().max_commands == 777, "不隔离时应读到 .env 的值"
        assert AgentSettings().max_steps == 33
        assert LogSettings().level == "DEBUG"
        print("  ✅ 不隔离时确实读到 .env（说明判别值有效）")

        with env_isolated(SandboxSettings, AgentSettings, LogSettings):
            assert SandboxSettings().max_commands == 500, "隔离后应回到字段默认值"
            assert AgentSettings().max_steps == 10
            assert LogSettings().level == "INFO"
        print("  ✅ 隔离后回到字段默认值（env_isolated 有效）")

        assert SandboxSettings().max_commands == 777, "退出上下文必须还原，否则污染后续测试"
        print("  ✅ 退出上下文后已还原")
    finally:
        env_path.write_text(backup, encoding="utf-8")
        print("  .env 已还原")


def test_effective_env_var_names_are_documented() -> None:
    """把「真正生效的环境变量名」锁进测试。

    踩过的坑：
      - LLM_TIMEOUT_SECONDS 不生效（要写 DEEPSEEK_TIMEOUT_SECONDS）
      - LOG_JSON 不生效（字段是 json_output，要写 LOG_JSON_OUTPUT）
      - AGENT_TOOL_MAX_RETRIES 不生效（validation_alias 覆盖了 env_prefix）
    这些错误全部是「静默失效」——extra='ignore' 会把不认识的变量吞掉，不报错。
    """
    import os

    from memagent.config import AgentSettings, LLMSettings, LogSettings

    print("\n  生效的变量名（实测）：")
    print("    DEEPSEEK_API_KEY / DEEPSEEK_MODEL / DEEPSEEK_TIMEOUT_SECONDS / DEEPSEEK_MAX_RETRIES")
    print("    SANDBOX_WORKSPACE / SANDBOX_MAX_COMMANDS /...")
    print("    LOG_LEVEL / LOG_JSON_OUTPUT")
    print("    AGENT_MAX_STEPS / TOOL_MAX_RETRIES（不是 AGENT_TOOL_MAX_RETRIES）")

    os.environ["DEEPSEEK_TIMEOUT_SECONDS"] = "99"
    assert LLMSettings().timeout_seconds == 99.0
    print("  ✅ DEEPSEEK_TIMEOUT_SECONDS 生效")
    os.environ.pop("DEEPSEEK_TIMEOUT_SECONDS")

    os.environ["LOG_JSON_OUTPUT"] = "false"
    assert LogSettings().json_output is False
    print("  ✅ LOG_JSON_OUTPUT 生效")
    os.environ.pop("LOG_JSON_OUTPUT")

    os.environ["TOOL_MAX_RETRIES"] = "4"
    assert AgentSettings().tool_max_retries == 4
    print("  ✅ TOOL_MAX_RETRIES 生效（validation_alias 覆盖了 AGENT_ 前缀）")
    os.environ.pop("TOOL_MAX_RETRIES")

    os.environ["AGENT_TOOL_MAX_RETRIES"] = "4"
    still_default = AgentSettings().tool_max_retries == 2
    os.environ.pop("AGENT_TOOL_MAX_RETRIES")
    assert still_default, "AGENT_TOOL_MAX_RETRIES 竟然生效了？那就该更新配置与文档"
    print("  ✅ AGENT_TOOL_MAX_RETRIES 不生效（符合预期，已用 TOOL_MAX_RETRIES 替代）")


def test_runtime_settings_load_from_dotenv() -> None:
    """Settings() 必须能读到 .env（含各分组的 env_prefix 生效）。

    注意：这里不断言具体数值，因为 .env 是用户在维护的运行配置。
    断言的是「读到了 .env 的值」这个行为本身。
    """
    from dotenv import dotenv_values

    from memagent.config import PROJECT_ROOT

    raw = dotenv_values(PROJECT_ROOT / ".env")
    s = Settings()
    print(f"\n  .env SANDBOX_MAX_COMMANDS   = {raw.get('SANDBOX_MAX_COMMANDS')!r}")
    print(f"  Settings().sandbox.max_commands = {s.sandbox.max_commands!r}")
    print(f"  .env AGENT_MAX_STEPS        = {raw.get('AGENT_MAX_STEPS')!r}")
    print(f"  Settings().agent.max_steps  = {s.agent.max_steps!r}")
    if raw.get("AGENT_MAX_STEPS") is not None:
        assert s.agent.max_steps == int(str(raw["AGENT_MAX_STEPS"]))
        print("  判定：.env → 分组字段的映射通路正常 ✓")
    else:
        assert s.agent.max_steps == 10


def test_env_prefix_mapping_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个分组的 env_prefix + 字段名大写，必须真的能覆盖到。"""
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("DEEPSEEK_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("SANDBOX_MAX_COMMANDS", "42")
    monkeypatch.setenv("AGENT_MAX_STEPS", "7")
    monkeypatch.setenv("TOOL_MAX_RETRIES", "0")
    monkeypatch.setenv("LOG_LEVEL", "warning")
    monkeypatch.setenv("LOG_JSON_OUTPUT", "false")
    monkeypatch.setenv("WEB_SEARCH_MAX_RESULTS", "9")

    s = Settings()
    print(f"\n  DEEPSEEK_MODEL=deepseek-v4-pro      -> {s.llm.model!r}")
    print(f"  DEEPSEEK_TIMEOUT_SECONDS=12.5       -> {s.llm.timeout_seconds!r}")
    print(f"  SANDBOX_MAX_COMMANDS=42             -> {s.sandbox.max_commands!r}")
    print(f"  AGENT_MAX_STEPS=7                   -> {s.agent.max_steps!r}")
    print(f"  TOOL_MAX_RETRIES=0（无 AGENT_ 前缀） -> {s.agent.tool_max_retries!r}")
    print(f"  LOG_LEVEL='warning'                 -> {s.log.level!r} / int={s.log_level_int}")
    print(f"  LOG_JSON_OUTPUT='false'             -> {s.log.json_output!r}")
    print(f"  WEB_SEARCH_MAX_RESULTS=9            -> {s.web_search.max_results!r}")
    assert s.llm.model == "deepseek-v4-pro"
    assert s.llm.timeout_seconds == 12.5
    assert s.sandbox.max_commands == 42
    assert s.agent.max_steps == 7
    assert s.agent.tool_max_retries == 0
    assert s.log.level == "WARNING"
    assert s.log.json_output is False
    assert s.web_search.max_results == 9


def test_env_var_beats_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """OS 环境变量优先于 .env —— 这是测试隔离策略成立的前提，必须锁住。"""
    monkeypatch.setenv("SANDBOX_MAX_COMMANDS", "4242")
    s = Settings()
    print(f"\n  OS env=4242, .env=500 -> {s.sandbox.max_commands!r}")
    assert s.sandbox.max_commands == 4242
    print("  判定：环境变量优先（monkeypatch.setenv 足以隔离测试）✓")


@pytest.mark.parametrize(
    ("env", "match"),
    [
        ({"SANDBOX_WORKSPACE": "relative/path"}, "VFS 绝对路径"),
        ({"AGENT_MAX_STEPS": "0"}, "greater_than_equal"),
        ({"AGENT_MAX_STEPS": "51"}, "less_than_equal"),
        ({"LOG_LEVEL": "LOUD"}, "LOG_LEVEL"),
        ({"DEEPSEEK_API_KEY": "no-prefix"}, "sk-"),
        ({"DEEPSEEK_API_KEY": "sk-your-key-here"}, "占位符"),
        ({"SANDBOX_MAX_READ_BYTES": "99999999"}, "不应大于"),
        ({"SANDBOX_MAX_WRITE_BYTES": "51200"}, "不应大于"),
    ],
)
def test_invalid_config_fails_fast(monkeypatch: pytest.MonkeyPatch, env: dict[str, str], match: str) -> None:
    """AGENTS.md 全局降级路径：配置缺失/非法必须启动时快速失败，不允许运行时静默降级。"""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValidationError, match=match):
        Settings()
    print(f"\n  {env} -> ValidationError(含 {match!r}) ✓")


def test_get_settings_is_cached_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    from memagent.config import get_settings

    monkeypatch.setenv("AGENT_MAX_STEPS", "3")
    first = get_settings()
    monkeypatch.setenv("AGENT_MAX_STEPS", "9")  # 单例已缓存，改环境变量不生效
    second = get_settings()
    print(f"\n  第一次 max_steps={first.agent.max_steps}，改环境变量后第二次={second.agent.max_steps}")
    print(f"  first is second -> {first is second}")
    assert first is second
    assert second.agent.max_steps == 3
    print("  判定：单例生效，改配置必须 cache_clear ✓")


def test_unrelated_env_vars_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """extra='ignore'：本机其他工具的变量（如 ZT_REDIS_HOST）不应让启动失败。"""
    monkeypatch.setenv("ZT_REDIS_HOST", "127.0.0.1")
    monkeypatch.setenv("SOME_UNRELATED_VAR", "whatever")
    s = Settings()
    print("\n  设了 ZT_REDIS_HOST / SOME_UNRELATED_VAR 后仍可构造 Settings ✓")
    print(f"  sandbox.max_commands = {s.sandbox.max_commands!r}（未被污染）")
    assert s.sandbox.max_commands == 500


def test_log_settings_does_not_shadow_basemodel() -> None:
    """字段名若叫 json 会遮蔽 BaseModel.json()，让代码里 `if log.json` 永远为真。

    这是真实踩过的坑：LogSettings 曾有字段 `json: bool = True`，
    于是 trace.py 里 `if resolved.log.json` 取到的是 BaseModel.json 绑定方法
    （永远 truthy），配置里的开关彻底失效，且不报任何错。
    """
    from memagent.config import LogSettings

    log = LogSettings()
    whole = Settings()
    print(f"\n  log.model_dump_json() -> {log.model_dump_json()}")
    assert "json_output" in log.model_dump_json()
    assert isinstance(log.model_dump(), dict)
    print(f"  字段表 = {list(LogSettings.model_fields)}")
    print(f"  hasattr(log, 'json') -> {hasattr(log, 'json')}")
    assert "json" not in LogSettings.model_fields, "字段名 json 会遮蔽 BaseModel.json()"
    assert callable(log.json), "log.json 应为 BaseModel.json 方法，而非被字段覆盖"
    print(f"  整个配置的级别 = {whole.log.level!r} / {whole.log_level_int}")
    print("  判定：无命名遮蔽，json_output 是独立字段 ✓")