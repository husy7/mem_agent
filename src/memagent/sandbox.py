"""bashkit 沙箱会话——read / write / bash 三个工具共享的唯一虚拟文件系统。

================================================================================
这个模块做什么
================================================================================
封装一个 **bashkit 沙箱会话**，对外提供 read / write / bash 三种能力。
它是整个项目**唯一**允许接触 bashkit 的地方。

AGENTS.md「核心设计原则 1」要求：
    read / write / bash 三个工具操作**同一个** bashkit 实例的虚拟文件系统，
    禁止使用宿主机文件系统。

本模块就是这条原则的落地实现。

================================================================================
为什么必须有这一层（不是多余的抽象）
================================================================================
实测结论（详见 `docs/设计决策记录.md` 的 EXP-08）：

    同一个 BashTool 的 .fs() 与 .execute()  → ✅ 共享同一 VFS（双向）
    独立的 FileSystem() 与 BashTool          → ❌ 完全隔离
                                               （bash 读独立 fs 写的文件报 file not found）

所以三者必须由**同一个对象**统一持有同一个 `BashTool`。
如果 `read` 工具自己 new 一个 FileSystem、`bash` 工具自己 new 一个 BashTool，
那么 `write` 写进去的文件 `bash` 根本看不见——**然而不会报错**，
只会表现为"文件不存在"，属于最难排查的一类缺陷。

这就是本模块存在的全部理由：**把"必须共享同一个实例"这个约束，
从"约定"变成"类型系统保证"**（外部拿不到 BashTool，只能通过本类操作）。

================================================================================
怎么用
================================================================================
    from memagent.sandbox import get_session

    sandbox = get_session()                      # 进程内单例

    await sandbox.write("report.md", "# 标题")    # 自动建父目录
    await sandbox.read("report.md")               # 带行号返回
    code, out, err = await sandbox.bash("ls -la /workspace")
    sandbox.exists("report.md")                   # True
    sandbox.limits                                # 当前资源上限（排查问题时有用）

    # 测试里需要干净环境时：
    reset_session()                               # 销毁单例，下次重新创建

**禁止**在项目其他任何地方 import bashkit。所有沙箱操作经由本模块。

================================================================================
全局禁止事项的落实
================================================================================
AGENTS.md 规定「禁止在异步节点中执行阻塞 I/O」。
实测 `bashkit` 的 `fs()` 系列是**同步阻塞**调用
（2MB 写入把事件循环 ticker 从 30 压到 21，详见 EXP-08），
所以本模块所有 fs 操作统一用 `asyncio.to_thread()` 下放线程。
"""

from __future__ import annotations

import asyncio
import posixpath
from dataclasses import dataclass
from typing import Any

from bashkit import BashError, BashTool, FileSystem

from memagent.config import SandboxSettings, get_settings
from memagent.observability.trace import TruncatedRepr, get_logger

# ==============================================================================
# 异常
# ==============================================================================


class SandboxError(RuntimeError):
    """沙箱层的错误。

    为什么单独定义而不是直接用 RuntimeError：
    沙箱故障属于**基础设施故障**，与业务错误（参数不对、文件不存在）性质不同，
    上层对两者的处理也不同——基础设施故障要降级，业务错误要交给模型决策。
    用独立类型让上层可以精确捕获。
    """


# ==============================================================================
# 值对象
# ==============================================================================


@dataclass(frozen=True)
class FileStat:
    """文件元信息的收敛视图。

    为什么要收敛而不是直接把 bashkit 返回的 dict 透出去：
    `fs.stat()` 返回的是第三方库的 dict，字段名会随版本变化
    （本项目已实测过 bashkit 的 `__version__` 报 0.1.2 而实际 wheel 是 0.18.1）。
    收敛成自己的类型后，升级 bashkit 只需要改本类的 `from_raw` 一个地方，
    而不是去搜索整个项目里所有 `.stat()` 的调用点。
    """

    is_dir: bool
    size: int
    modified: float

    @classmethod
    def from_raw(cls, raw: dict[str, Any], path: str) -> FileStat:
        """从 bashkit 的原始 dict 构造。

        实测的原始结构（bashkit 0.18.1，见 docs/设计决策记录.md EXP-08）：
            文件: {'file_type': 'file',      'size': 8, 'mode': 420,
                   'modified': 1789738047.98, 'created': 1789738047.98}
            目录: {'file_type': 'directory', 'size': 0, 'mode': 493, ...}

        Args:
            raw: `fs.stat()` 的返回值。
            path: 该文件路径（仅用于报错信息，让错误可定位）。

        Raises:
            SandboxError: 无法从原始 dict 解析出所需字段时。
                报错时把原始内容带上，方便直接看出 bashkit 换了什么字段名。
        """
        try:
            file_type = str(raw.get("file_type", "file"))
            return cls(
                is_dir=file_type == "directory",
                size=int(raw.get("size", 0) or 0),
                modified=float(raw.get("modified", 0.0) or 0.0),
            )
        except (TypeError, ValueError, AttributeError) as exc:
            raise SandboxError(
                f"无法解析 stat 结果（path={path!r}）：{exc}\n"
                f"bashkit 返回的原始内容 = {raw!r}\n"
                "这通常意味着 bashkit 版本变化导致字段名改变，请更新 FileStat.from_raw()。"
            ) from exc

    @staticmethod
    def names_from_entries(entries: list[Any], path: str) -> list[str]:
        """从 `fs.read_dir()` 的返回值里取出条目名。

        实测的原始结构（bashkit 0.18.1）：每个条目是
            {'name': 'a.txt', 'metadata': {'file_type': 'file', 'size': 8, ...}}
        即 `name` 是条目名，`metadata` 是该项的 stat 结果。

        Args:
            entries: `fs.read_dir()` 的返回值。
            path: 目录路径（仅用于报错信息）。

        Returns:
            条目名列表。只取名字，不含 metadata——
            给模型看的目录列表不需要每项的完整元数据，那会浪费上下文。

        Raises:
            SandboxError: 条目结构与预期不符时（附完整原始返回，便于定位）。
        """
        names: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                names.append(str(entry))
                continue
            name = entry.get("name") or entry.get("path")
            if name is None:
                raise SandboxError(
                    f"无法从 read_dir 结果解析条目名（path={path!r}）：{entry!r}\n"
                    f"完整返回 = {entries!r}\n"
                    "这通常意味着 bashkit 版本变化导致结构改变，"
                    "请更新 FileStat.names_from_entries()。"
                )
            names.append(str(name))
        return names


# ==============================================================================
# 沙箱会话
# ==============================================================================


class SandboxSession:
    """一次沙箱会话，持有唯一的 BashTool 实例。

    本类的核心不变量（不变量 = 任何时候都必须成立的条件）：
        self._fs 与 self._bash 属于**同一个** bashkit 沙箱。
    所有方法都基于这个前提工作，破坏它会导致文件"凭空消失"。

    线程/并发说明：
        bashkit 的 VFS 是**同一份内存状态**，本类不做额外锁保护。
        当前设计下工具是顺序执行的（LangGraph 的 ToolNode 默认串行），
        所以不需要锁。若将来改为并发执行工具，需要重新评估。
    """

    def __init__(self, settings: SandboxSettings | None = None) -> None:
        """创建沙箱会话。

        Args:
            settings: 沙箱配置。缺省时从全局配置读取（生产路径）。
                显式传入主要给单元测试用——测试需要构造小上限的沙箱
                （例如 max_commands=2）来验证资源超限的处理。
        """
        self._settings = settings or get_settings().sandbox
        self._log = get_logger(__name__)

        # 唯一的沙箱实例。read/write 走它的 fs()，bash 走它的 execute()。
        # 这两者共享同一 VFS —— 这是本类存在的前提，也是 EXP-08 实测确认的。
        self._bash = BashTool(
            max_commands=self._settings.max_commands,
            max_loop_iterations=self._settings.max_loop_iterations,
            timeout_seconds=self._settings.timeout_seconds,
            cwd=self._settings.workspace,
        )
        self._fs: FileSystem = self._bash.fs()

        self._ensure_workspace()

    # --------------------------------------------------------------------------
    # 内部方法
    # --------------------------------------------------------------------------

    def _ensure_workspace(self) -> None:
        """确保工作目录存在。

        为什么需要主动建：bashkit 的 `fs.write_file` **不会**自动创建父目录，
        实测报 `RuntimeError: io error: parent directory not found`。
        所以在会话创建时就先把工作目录建好，避免第一次写入就失败。

        关于 try/except：实测已确认 `fs.mkdir(recursive=True)` 对**已存在**目录
        不报错（见 `docs/设计决策记录.md` EXP-08 实验 2）。
        保留 try/except 是防御性设计——本方法在 `__init__` 和 `reset()` 两处被调用，
        而 `reset()` 会连工作目录一起清掉，调用时序若有变化就可能撞上异常。
        目录建不出来时**不抛异常**：记 debug 日志后继续，
        真正的失败会在第一次 read/write 时以明确的 SandboxError 暴露出来，
        比在构造会话时抛一个含义模糊的异常更容易定位。
        """
        try:
            self._fs.mkdir(self._settings.workspace, recursive=True)
        except Exception as exc:  # noqa: BLE001 - 已存在等"非致命"情况都要放行
            self._log.debug(
                "workspace_mkdir_skipped",
                workspace=self._settings.workspace,
                reason=str(exc),
            )

    def _resolve(self, path: str) -> str:
        """把模型给的路径规范化，并限制在沙箱工作目录内。

        为什么必须做这件事：工具参数由**模型生成**，它可能给出
        `../../etc/passwd`、`/tmp/x` 这类路径。虽然 VFS 本身与宿主机完全隔离
        （碰不到真实文件系统），但越出工作目录会让**同一会话内多个任务的文件互相污染**，
        也会让"沙箱边界"这个概念在语义上失效。

        **为什么必须用 posixpath.normpath 规范化，而不能只做字符串前缀检查**：
        这是本模块真实踩过的漏洞。曾经写成：

            joined = path if path.startswith("/") else f"{workspace}/{path}"
            if not joined.startswith(f"{workspace}/"):   # ← 这里判断为"在界内"
                raise SandboxError(...)

        但 `"/workspace/../outside.txt".startswith("/workspace/")` 是 **True**，
        于是 `../outside.txt` 一路穿出了工作目录，日志里留下
        `path=/workspace/../outside.txt` 这样的记录——**拦不住，且不报错**。

        根本原因：**字符串前缀检查和路径语义是两回事**。
        `..` 在字符串里只是两个字符，在路径语义里是"上一层目录"。
        必须先 `normpath` 把 `.` 和 `..` 解析掉，**再**做边界判断。

        实测的规范化结果（POSIX 语义，VFS 用 `/` 分隔所以用 posixpath 而非 os.path）：

            'a.txt'                      -> '/workspace/a.txt'        放行
            './a.txt'                    -> '/workspace/a.txt'        放行
            'd//x.txt'                   -> '/workspace/d/x.txt'      放行（多余斜杠归一）
            '.'                          -> '/workspace'              放行（工作目录自身）
            '../outside.txt'             -> '/outside.txt'            **拒绝**
            'a/../../up.txt'             -> '/up.txt'                 **拒绝**
            '/workspace/../outside.txt'  -> '/outside.txt'            **拒绝**
            '/workspaceX/a.txt'          -> '/workspaceX/a.txt'       **拒绝**（前缀相似但不能放行）
            '/etc/passwd'                -> '/etc/passwd'             **拒绝**

        Args:
            path: 模型给出的路径，可以是相对路径或 VFS 绝对路径。

        Returns:
            VFS 内的规范化绝对路径。相对路径被拼到工作目录下，
            `.` / `..` / 重复斜杠都已被解析。

        Raises:
            SandboxError: 路径为空，或规范化后落在工作目录之外时。
        """
        cleaned = (path or "").strip()
        if not cleaned:
            raise SandboxError("路径不能为空")

        workspace = posixpath.normpath(self._settings.workspace)
        # 相对路径拼到工作目录下；绝对路径原样使用（下面的边界检查会拦住越界）
        joined = cleaned if cleaned.startswith("/") else posixpath.join(workspace, cleaned)
        # 关键：先规范化，再做边界判断。顺序反了就等于没拦。
        normalized = posixpath.normpath(joined)

        # 允许路径等于工作目录本身，或以 "工作目录/" 开头
        if normalized != workspace and not normalized.startswith(f"{workspace}/"):
            raise SandboxError(
                f"路径 {path!r} 越出沙箱工作目录 {workspace}。\n"
                f"（规范化后的路径是 {normalized!r}）\n"
                "沙箱与宿主机是隔离的，但这不代表可以越出工作目录——"
                "越界会让同一会话内不同任务的文件互相污染。"
            )
        return normalized

    # --------------------------------------------------------------------------
    # 会话元信息
    # --------------------------------------------------------------------------

    @property
    def workspace(self) -> str:
        """沙箱工作目录（VFS 内路径）。"""
        return self._settings.workspace

    @property
    def limits(self) -> dict[str, Any]:
        """当前生效的资源上限。

        为什么要把这个暴露出来：调试"为什么这个命令被拒绝了"时，
        第一件需要知道的事就是当前的上限是多少。让排查信息一条命令就能拿到，
        而不是去翻 .env。
        """
        return {
            "workspace": self._settings.workspace,
            "max_commands": self._settings.max_commands,
            "max_loop_iterations": self._settings.max_loop_iterations,
            "timeout_seconds": self._settings.timeout_seconds,
            "max_read_bytes": self._settings.max_read_bytes,
            "max_write_bytes": self._settings.max_write_bytes,
        }

    # --------------------------------------------------------------------------
    # 文件系统操作
    # --------------------------------------------------------------------------

    def exists(self, path: str) -> bool:
        """判断路径是否存在（相对路径按工作目录解析）。"""
        return bool(self._fs.exists(self._resolve(path)))

    def stat(self, path: str) -> FileStat:
        """取文件元信息。

        Raises:
            SandboxError: 路径越界，或无法解析 bashkit 的返回结构时。
        """
        resolved = self._resolve(path)
        return FileStat.from_raw(self._fs.stat(resolved), resolved)

    async def read(
        self,
        path: str,
        *,
        offset: int = 0,
        limit: int | None = None,
        with_line_numbers: bool = True,
    ) -> str:
        """读取文件内容（按行分块，可选行号）。

        为什么读文件要带 offset/limit：模型可能试图读一个大文件，
        全量塞进上下文会迅速耗尽 token 预算。分块读取让模型自己决定要不要继续读。

        为什么用 `asyncio.to_thread` 包住同步的 `fs` 调用：
        `fs()` 系列是同步阻塞的，直接 await 里调用会**阻塞整个事件循环**
        （实测 2MB 写入让 ticker 从 30 降到 21）。AGENTS.md 明确禁止
        「在异步节点中执行阻塞 I/O」。注意 to_thread 并不能让 VFS 变成并发安全，
        它只是把阻塞调用移出事件循环。

        Args:
            path: 文件路径（相对路径按工作目录解析）。
            offset: 起始行号（0 基，即 offset=0 表示从第 1 行开始）。
            limit: 最多返回多少行。None 表示到文件末尾。
            with_line_numbers: 是否加上行号前缀。

        Returns:
            格式化的文本。首行是说明（文件共几行、当前显示哪一段、
            还有多少行未显示），后面是内容本身。

        Raises:
            SandboxError: 路径越界、文件超过读上限，或路径是目录（此时返回目录列表）。
        """
        resolved = self._resolve(path)
        meta = await asyncio.to_thread(self.stat, resolved)

        # 目录：返回条目列表。让模型能"看一眼"目录内容，
        # 而不是收到一个无法理解的错误。
        #
        # 实测（EXP-08）：`fs.read_file()` 读目录会抛
        # RuntimeError('io error: is a directory')，不会优雅返回。
        # 所以这个分支必须存在——否则模型说错一次路径，工具就崩了。
        if meta.is_dir:
            entries = await asyncio.to_thread(self._fs.read_dir, resolved)
            names = FileStat.names_from_entries(entries, resolved)
            listing = "\n".join(f"  {name}" for name in names)
            return f"{resolved} 是目录（{len(names)} 项）：\n{listing}"

        if meta.size > self._settings.max_read_bytes:
            raise SandboxError(
                f"文件 {resolved} 大小 {meta.size} 字节，超过读上限 "
                f"{self._settings.max_read_bytes} 字节。\n"
                f"请用 offset/limit 分段读取（文件是纯文本，共约 "
                f"{meta.size} 字节）。"
            )

        try:
            raw: bytes = await asyncio.to_thread(self._fs.read_file, resolved)
        except RuntimeError as exc:
            # bashkit 的 fs 系列在底层 IO 失败时抛原生 RuntimeError
            #（实测：读目录 -> 'io error: is a directory'；
            #  父目录不存在 -> 'io error: parent directory not found'）。
            # 统一包成 SandboxError，让上层只需要捕获一种基础设施异常。
            raise SandboxError(
                f"读取 {resolved} 失败（bashkit 原生错误）：{exc}"
            ) from exc

        # errors="replace" 而不是抛异常：模型可能读到混有二进制内容的文件，
        # 此时给出"能看的文本"比报一个编码错误更有用。
        text = raw.decode("utf-8", errors="replace")

        lines = text.splitlines()
        start = max(0, offset)
        end = len(lines) if limit is None else min(len(lines), start + max(0, limit))
        window = lines[start:end]

        if with_line_numbers:
            body = "\n".join(
                f"{start + index + 1:5d} | {line}" for index, line in enumerate(window)
            )
        else:
            body = "\n".join(window)

        header = f"# {resolved} 共 {len(lines)} 行，显示 {start + 1}-{end}"
        if end < len(lines):
            header += f"（还有 {len(lines) - end} 行未显示，可加大 limit 或 offset）"
        return f"{header}\n{body}"

    async def write(self, path: str, content: str) -> str:
        """写入文件（覆盖模式），自动创建父目录。

        为什么是覆盖而不是追加：Agent 场景下"改写文件"比"追加"更常见，
        且追加语义容易产生重复内容。需要追加时模型可以用 bash 的 `>>`。

        Args:
            path: 文件路径（相对路径按工作目录解析）。
            content: 要写入的文本内容。

        Returns:
            给模型看的成功说明（含落盘字节数）。

        Raises:
            SandboxError: 路径越界，或内容超过写上限时。
        """
        resolved = self._resolve(path)
        payload = content.encode("utf-8")

        if len(payload) > self._settings.max_write_bytes:
            raise SandboxError(
                f"待写入内容 {len(payload)} 字节，超过写上限 "
                f"{self._settings.max_write_bytes} 字节。\n"
                "请拆分内容分多次写入，或改用 bash 命令生成大文件。"
            )

        # 父目录必须先建：bashkit 的 write_file 不会自动创建
        # （实测报 RuntimeError: io error: parent directory not found）
        parent = resolved.rsplit("/", 1)[0]
        if parent:
            await asyncio.to_thread(self._fs.mkdir, parent, True)

        try:
            await asyncio.to_thread(self._fs.write_file, resolved, payload)
        except RuntimeError as exc:
            # 同 read：bashkit 底层 IO 失败抛原生 RuntimeError，统一包成 SandboxError
            raise SandboxError(
                f"写入 {resolved} 失败（bashkit 原生错误）：{exc}"
            ) from exc

        self._log.info("sandbox_write", path=resolved, bytes=len(payload))
        return f"已写入 {resolved}（{len(payload)} 字节）"

    async def bash(self, commands: str) -> tuple[int, str, str]:
        """在沙箱中执行 bash 命令。

        **本方法不抛异常、不判成败**，把三样原始信息都交回上层。
        理由：AGENTS.md 的全局降级路径要求
        「工具调用超时 → 返回错误信息给模型，由模型决定换工具或放弃」。
        如果在沙箱层就抛异常，模型拿不到失败详情，就无法做出有依据的决策。

        实测的失败形态（EXP-08）：
            命令本身失败（如 cat 不存在的文件）
                → error=None, exit_code=1, stderr='file not found'
            资源超限（如超过 max_commands）
                → 抛 BashError('resource limit exceeded: ...')

        Args:
            commands: 要执行的 shell 命令（可以多行、多条）。

        Returns:
            (exit_code, stdout, stderr) 三元组。

            **基础设施故障也被归一成同一个表示**（exit_code=1，原始错误写在 stderr），
            实测返回例如：
                (1, '', 'resource limit exceeded: maximum command count exceeded (2)')

            为什么 stderr 里**不加**自己的包装前缀（如 'sandbox error: '）：
            上层统一用 `exit_code != 0` 判断失败，包装前缀不提供额外信息，
            反而会遮蔽 bashkit 的原始错误文本，增加排查难度。
            保留原文，让"到底哪里出错"一眼可见。
        """
        try:
            result = await self._bash.execute(commands)
        except BashError as exc:
            # 只有资源超限/超时这类基础设施故障才会走到这里
            self._log.warning(
                "sandbox_bash_error",
                commands=TruncatedRepr(commands),
                reason=str(exc),
            )
            return 1, "", f"sandbox error: {exc}"
        return int(result.exit_code), result.stdout or "", result.stderr or ""

    def reset(self) -> None:
        """清空沙箱内所有文件（包括工作目录），然后重建工作目录。

        用途：
            1. 单元测试之间互相隔离——上一个测试写的文件不该影响下一个
            2. 会话级别的资源回收

        实测语义（EXP-08 实验 3）：
            reset 前  exists('/workspace/tmp.txt') = True
            reset 后  exists('/workspace/tmp.txt') = False
            reset 后  exists('/workspace')         = False   ← 工作目录也被清掉
            reset 后  exists('.')                  = True    ← 但 VFS 根还在

        所以 reset() 之后**必须**调用 `_ensure_workspace()` 重建工作目录，
        否则沙箱处于"根存在但工作目录不存在"的半可用状态，
        第一次 write 的 mkdir 虽然能建回来，但第一次 read 会失败。
        """
        self._bash.reset()
        self._ensure_workspace()


# ==============================================================================
# 进程内单例
# ==============================================================================
#
# 为什么必须是单例：VFS 一致性要求 read / write / bash 操作**同一个实例**。
# 如果每次调用都 new 一个 SandboxSession，write 写的文件 bash 就看不见了
# （而这是 EXP-08 实测确认的行为）。
#
# 为什么用「模块级变量 + get_session()」而不是直接 `_session = SandboxSession()`：
# 模块级直接构造会在 import 时就创建沙箱（消耗资源、且测试无法重置）。
# 惰性构造 + reset_session() 让测试可以拿到干净环境。
#
# 注意：这里不用 lru_cache（config.py 用的是 lru_cache），
# 因为沙箱是有状态资源，需要显式的 reset 语义，而不是 cache_clear。

_session: SandboxSession | None = None


def get_session() -> SandboxSession:
    """获取进程内唯一的沙箱会话（首次调用时创建）。

    Returns:
        SandboxSession 实例。
    """
    global _session
    if _session is None:
        _session = SandboxSession()
    return _session


def reset_session() -> None:
    """销毁单例，下次 `get_session()` 会创建全新的沙箱。

    为什么需要"销毁"而不是"清空"：
    测试可能需要不同配置的沙箱（例如 max_commands=2），
    配置是构造时传给 BashTool 的，无法在运行中修改，只能重建。
    """
    global _session
    _session = None