"""沙箱会话单元测试。

本测试文件的核心目的：证明 **VFS 一致性**（AGENTS.md 核心设计原则 1，
stage1 验证标准第 8 条）。

VFS 一致性为什么需要测试来保证：
    `read` / `write` / `bash` 三者必须操作同一个虚拟文件系统。
    如果它们各自持有不同的实例，`write` 写的文件 `bash` 看不见——
    **而这不报错**，只表现为"文件不存在"。这类缺陷靠代码审查很难发现，
    只能靠"写进去再从另一条路径读出来"的测试来锁死。

测试隔离策略：
    每个测试用独立的 `SandboxSession`（而不是共享单例），
    并在 fixture 里 `reset()`，保证测试之间互不影响。
"""

from __future__ import annotations

import pytest

from memagent.config import SandboxSettings
from memagent.sandbox import SandboxError, SandboxSession, get_session, reset_session


@pytest.fixture
def sandbox() -> SandboxSession:
    """一个干净、上限宽松的沙箱会话。"""
    reset_session()
    session = SandboxSession(
        SandboxSettings(max_commands=500, max_loop_iterations=5000, timeout_seconds=10.0)
    )
    session.reset()
    return session


# ==============================================================================
# 核心：VFS 一致性
# ==============================================================================


async def test_write_then_bash_sees_file(sandbox: SandboxSession) -> None:
    """stage1 验证标准 8（正向）：write 写入的文件能被 bash 读取。"""
    await sandbox.write("a.txt", "from-write\n")
    code, out, err = await sandbox.bash("cat /workspace/a.txt")
    print(f"\n  bash cat -> exit={code} stdout={out!r} stderr={err!r}")
    assert code == 0
    assert out == "from-write\n"


async def test_bash_write_then_read_sees_file(sandbox: SandboxSession) -> None:
    """stage1 验证标准 8（反向）：bash 写的文件能被 read 读取。

    反向测试不是冗余——它证明的是同一个不变量，但如果将来有人把
    `SandboxSession` 拆成"只读会话"和"可写会话"，只有这条测试会失败。
    """
    await sandbox.bash("echo from-bash > /workspace/b.txt")
    content = await sandbox.read("b.txt")
    print(f"\n  read ->\n{content}")
    assert "from-bash" in content


async def test_vfs_is_not_host_filesystem(sandbox: SandboxSession) -> None:
    """沙箱必须与宿主机文件系统隔离（AGENTS.md 核心设计原则 1）。

    验证方式：在沙箱里读宿主机上**确定存在**的文件（本项目自己的 pyproject.toml），
    应当读不到。若这条失败，说明沙箱泄漏到了宿主机——属于严重问题。
    """
    from memagent.config import PROJECT_ROOT

    assert (PROJECT_ROOT / "pyproject.toml").exists(), "前提：宿主机上该文件确实存在"
    code, out, err = await sandbox.bash("cat /pyproject.toml")
    print(f"\n  沙箱内 cat /pyproject.toml -> exit={code} stderr={err!r}")
    assert code != 0, "沙箱竟然读到了宿主机的文件！隔离失效"
    assert "file not found" in err


# ==============================================================================
# 读取：行号 / 分块 / 边界
# ==============================================================================


async def test_read_has_line_numbers(sandbox: SandboxSession) -> None:
    await sandbox.write("lines.txt", "\n".join(f"line-{i}" for i in range(1, 11)))
    full = await sandbox.read("lines.txt")
    print(f"\n  全文:\n{full}")
    assert "1 | line-1" in full
    assert "10 | line-10" in full


async def test_read_offset_and_limit(sandbox: SandboxSession) -> None:
    """分块读取：模型读大文件时靠它控制上下文消耗。"""
    await sandbox.write("lines.txt", "\n".join(f"line-{i}" for i in range(1, 11)))
    window = await sandbox.read("lines.txt", offset=4, limit=3)
    print(f"\n  offset=4 limit=3:\n{window}")
    assert "5 | line-5" in window
    assert "7 | line-7" in window
    assert "line-4" not in window
    assert "line-8" not in window
    # 首行说明要提示还有多少行未显示，模型才知道可以继续读
    assert "还有" in window


async def test_read_without_line_numbers(sandbox: SandboxSession) -> None:
    await sandbox.write("plain.txt", "alpha\nbeta\n")
    content = await sandbox.read("plain.txt", with_line_numbers=False)
    print(f"\n  {content!r}")
    assert "alpha" in content
    assert "|" not in content


async def test_read_directory_lists_names(sandbox: SandboxSession) -> None:
    """读目录返回条目名。

    实测（EXP-08 实验 2 ③）：`read_dir()` 返回
    [{'name': 'a.txt', 'metadata': {...}}, ...]，所以取 `name` 字段。
    """
    await sandbox.write("d/x.txt", "1")
    await sandbox.write("d/y.txt", "2")
    listing = await sandbox.read("d")
    print(f"\n  目录列表:\n{listing}")
    assert "目录" in listing
    assert "x.txt" in listing
    assert "y.txt" in listing
    assert "?" not in listing, "条目名解析失败（说明字段名假设错了）"


async def test_read_size_limit_suggests_chunking(sandbox: SandboxSession) -> None:
    """超读上限时给出可执行的建议，而不是一句"太大了"。"""
    small = SandboxSession(SandboxSettings(max_read_bytes=50, max_write_bytes=1000))
    await small.write("m.txt", "y" * 200)
    with pytest.raises(SandboxError, match="offset/limit"):
        await small.read("m.txt")
    print("\n  超读上限 -> SandboxError 且提示用 offset/limit ✓")


# ==============================================================================
# 写入：父目录 / 上限
# ==============================================================================


async def test_write_creates_parent_directories(sandbox: SandboxSession) -> None:
    """bashkit 的 write_file 不建父目录（实测报 parent directory not found）。"""
    await sandbox.write("deep/nested/dir/c.txt", "nested\n")
    assert sandbox.exists("deep/nested/dir/c.txt")
    code, out, _ = await sandbox.bash("cat /workspace/deep/nested/dir/c.txt")
    print(f"\n  嵌套写入后 bash 读取 -> exit={code} {out!r}")
    assert code == 0
    assert out == "nested\n"


async def test_write_overwrites_not_appends(sandbox: SandboxSession) -> None:
    """写是覆盖语义：第二次写不应保留第一次的内容。"""
    await sandbox.write("o.txt", "first\n")
    await sandbox.write("o.txt", "second\n")
    content = await sandbox.read("o.txt", with_line_numbers=False)
    print(f"\n  覆盖后内容 = {content!r}")
    assert "second" in content
    assert "first" not in content


async def test_write_size_limit(sandbox: SandboxSession) -> None:
    small = SandboxSession(SandboxSettings(max_write_bytes=100, max_read_bytes=10))
    with pytest.raises(SandboxError, match="超过写上限"):
        await small.write("big.txt", "x" * 500)
    print("\n  超限写入 -> SandboxError ✓")


# ==============================================================================
# 路径约束
# ==============================================================================


async def test_path_escape_is_rejected(sandbox: SandboxSession) -> None:
    """工具参数由模型生成，必须拦在工作目录内。

    这里每个用例都对应一次真实的绕过尝试。
    注意 `../outside.txt` 和 `/workspace/../outside.txt` 这两条：
    它们曾经**成功穿出**工作目录——因为最初的实现只做了字符串前缀检查
    （`"/workspace/../outside.txt".startswith("/workspace/")` 是 True），
    没有用 posixpath.normpath 解析 `..`。这是安全缺陷，不是测试写错。
    """
    bad_paths = (
        "../outside.txt",              # 相对路径向上穿越
        "/workspace/../outside.txt",   # 绝对路径 + .. 穿越（曾绕过成功）
        "a/../../up.txt",              # 穿过工作目录后继续向上
        "/etc/passwd",                 # 工作目录外的绝对路径
        "/tmp/x.txt",
        "/workspaceX/a.txt",           # 前缀相似但不同目录（必须拒绝）
        "..",                          # 规范化后是根目录 /
        "",                            # 空路径
        "   ",                         # 纯空白
    )
    for bad in bad_paths:
        with pytest.raises(SandboxError):
            await sandbox.write(bad, "x")
        print(f"\n  {bad!r:32s} -> SandboxError ✓")

    # 确认没有真的写出界
    code, _, _ = await sandbox.bash("ls /workspace/../outside.txt")
    print(f"\n  穿越尝试后 /workspace/../outside.txt 是否存在 -> exit={code}（应为非 0）")
    assert code != 0


async def test_dot_and_redundant_slashes_are_normalized(sandbox: SandboxSession) -> None:
    """规范化不该误伤合法路径：`.` 和多余斜杠都应正常放行。"""
    for good in ("a.txt", "./a.txt", "d//x.txt", "/workspace/a.txt", "/workspace/./a.txt"):
        await sandbox.write(good, "ok\n")
        print(f"\n  {good!r:26s} -> 放行 ✓")
    listing = await sandbox.read("/workspace")
    print(f"\n  工作目录内容:\n{listing}")
    assert "a.txt" in listing


async def test_relative_path_resolves_into_workspace(sandbox: SandboxSession) -> None:
    await sandbox.write("rel.txt", "relative ok\n")
    code, out, _ = await sandbox.bash("cat /workspace/rel.txt")
    print(f"\n  相对路径落在 /workspace -> exit={code} {out!r}")
    assert code == 0
    assert out == "relative ok\n"


async def test_workspace_path_itself_is_allowed(sandbox: SandboxSession) -> None:
    """工作目录本身是合法路径（读它应当返回条目列表，而不是越界错误）。"""
    await sandbox.write("in-ws.txt", "x")
    listing = await sandbox.read("/workspace")
    print(f"\n  读工作目录本身:\n{listing}")
    assert "in-ws.txt" in listing


# ==============================================================================
# bash：失败语义
# ==============================================================================


async def test_bash_failure_returns_stderr_not_exception(sandbox: SandboxSession) -> None:
    """命令失败不抛异常——错误详情要交回给模型决策。

    AGENTS.md 全局降级路径：「工具调用超时 → 返回错误信息给模型，
    由模型决定换工具或放弃」。若沙箱层抛异常，模型拿不到失败详情。
    """
    code, out, err = await sandbox.bash("cat /workspace/does-not-exist")
    print(f"\n  cat 不存在的文件 -> exit={code} stderr={err!r}")
    assert code == 1
    assert "file not found" in err


async def test_resource_limit_returns_error_not_exception(sandbox: SandboxSession) -> None:
    """stage1 验证标准 9：超过 max_commands 时返回错误（而不是崩掉对话）。

    本测试锁住的语义：**两种失败被归一成同一个表示**——
        `bash()` 永远返回 (exit_code, stdout, stderr) 三元组，不抛异常。
    上层只需要判断 `exit_code != 0`，不必分别处理"命令失败"和"资源超限"。

    实测的 stderr 内容（EXP-08）：
        'resource limit exceeded: maximum command count exceeded (2)'
    """
    tiny = SandboxSession(SandboxSettings(max_commands=2))
    code, out, err = await tiny.bash("echo 1; echo 2; echo 3; echo 4")
    print(f"\n  max_commands=2 跑 4 条 -> exit={code}")
    print(f"  stdout={out!r}")
    print(f"  stderr={err!r}")
    assert code == 1, "资源超限也必须返回明确失败，不能让上层以为成功"
    assert out == ""
    assert "resource limit exceeded" in err
    assert "maximum command count exceeded" in err


async def test_bash_multiline_commands(sandbox: SandboxSession) -> None:
    code, out, _ = await sandbox.bash("echo one\necho two\n")
    print(f"\n  多行命令 -> exit={code} stdout={out!r}")
    assert code == 0
    assert "one" in out
    assert "two" in out


# ==============================================================================
# 元信息
# ==============================================================================


def test_stat_file_and_directory(sandbox: SandboxSession) -> None:
    """stat 的收敛：FileStat.from_raw 假设的字段名由 EXP-08 实验 2 ② 确认。"""

    async def _run() -> None:
        await sandbox.write("s.txt", "12345678")
        fstat = sandbox.stat("s.txt")
        dstat = sandbox.stat(".")
        print(f"\n  文件 stat = {fstat}")
        print(f"  目录 stat = {dstat}")
        assert fstat.is_dir is False
        assert fstat.size == 8
        assert fstat.modified > 0
        assert dstat.is_dir is True
        assert dstat.size == 0

    import asyncio

    asyncio.run(_run())


def test_limits_reports_effective_settings(sandbox: SandboxSession) -> None:
    limits = sandbox.limits
    print(f"\n  limits = {limits}")
    assert limits["workspace"] == "/workspace"
    assert limits["max_commands"] == 500
    assert limits["timeout_seconds"] == 10.0


# ==============================================================================
# 单例与重置
# ==============================================================================


def test_session_is_singleton() -> None:
    """VFS 一致性依赖单例：若每次 new，write 写的文件 bash 看不见。"""
    reset_session()
    first = get_session()
    second = get_session()
    print(f"\n  get_session() 两次同一对象? {first is second}")
    assert first is second

    reset_session()
    third = get_session()
    print(f"  reset_session() 后是同一对象? {third is first}")
    assert third is not first
    reset_session()


async def test_reset_clears_files_and_restores_workspace(sandbox: SandboxSession) -> None:
    """实测语义（EXP-08 实验 3）：reset 会连工作目录一起清掉，但 VFS 根还在。"""
    await sandbox.write("temp.txt", "x")
    assert sandbox.exists("temp.txt")

    sandbox.reset()

    print(f"\n  reset 后 temp.txt 还在?  {sandbox.exists('temp.txt')}")
    print(f"  reset 后 workspace 还在? {sandbox.exists('.')}")
    assert not sandbox.exists("temp.txt")
    assert sandbox.exists("."), "VFS 根应当仍然存在"

    # 关键：reset 后沙箱必须立即可用（工作目录被重建）
    await sandbox.write("after-reset.txt", "ok\n")
    code, out, _ = await sandbox.bash("cat /workspace/after-reset.txt")
    print(f"  reset 后写入并读取 -> exit={code} {out!r}")
    assert code == 0
    assert out == "ok\n"