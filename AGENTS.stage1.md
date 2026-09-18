
# AGENTS.stage1.md

本文档是 `AGENTS.md` 的阶段一补充，需与 `AGENTS.md` 一起阅读。仅定义阶段一专属的范围、目录、设计、验证标准和禁止事项。


## 一、阶段范围

**做：**
- LangGraph 图结构搭建
- DeepSeek Responses API 接入
- 工具注册中心
- `read` / `write` / `bash` 三工具（统一基于 bashkit VFS）
- DeepSeek 内置 `web_search` 接入
- ReAct 循环 + 终止条件
- 消息格式转换层（基础版）

**不做：**
- 记忆系统（阶段二）
- Agentic RAG（阶段三）
- 评估体系（阶段三）
- 前端界面（CLI 或简单 API 验证即可）


## 二、前置依赖

无。


## 三、阶段专属目录

本阶段创建以下目录和文件：

```
src/
├── agent/
│   ├── graph.py               # ReAct 图定义
│   ├── state.py               # MessagesState
│   └── nodes/
│       ├── call_llm.py
│       └── call_tool.py
├── tools/
│   ├── registry.py            # 工具注册中心
│   ├── fs.py                  # read / write（bashkit VFS）
│   └── bash.py                # bash（bashkit 包装）
├── llm/
│   ├── client.py              # DeepSeek Responses API 客户端
│   └── converter.py           # 消息转换（基础版）
└── observability/
    └── trace.py               # 基础审计日志
tests/
└── unit/
    ├── test_converter.py
    ├── test_registry.py
    └── test_tools.py
```


## 四、核心设计

### 4.1 ReAct 图结构

```
START → call_llm → should_tool?
                    ↓ 有工具        ↓ 无工具
                 call_tool          END
                    ↓
                 call_llm（循环）
```

- **`call_llm` 节点**：模型推理，绑定工具列表。
- **`call_tool` 节点**：执行工具调用，返回观察结果。
- **条件路由函数**：判断模型输出中是否包含工具调用请求。

**终止条件**：
- 最大步数：10 步
- 工具重试上限：2 次
- 模型输出无工具调用时终止

### 4.2 工具系统

**统一基于 bashkit VFS**，`read` / `write` / `bash` 操作同一个 bashkit 实例。

| 工具 | 类型 | 执行方 |
|---|---|---|
| `read` | client-side | bashkit VFS |
| `write` | client-side | bashkit VFS |
| `bash` | client-side | bashkit VFS |
| `web_search` | server-side | DeepSeek 服务端 |

**bash 工具**：
- 用 `bashkit.langchain.create_bash_tool()` 包装。
- 沙箱配置：`max_commands=500`，`max_loop_iterations=5000`。
- 返回 `ExecResult`，格式化为字符串回传。

**read 工具**：
- 基于 bashkit VFS API 的 `read_file` / `exists`。
- 包装层增加行号、offset / limit、默认 50KB 截断。
- 路径限制在 VFS 工作目录内。

**write 工具**：
- 基于 `write_file`。
- 自动创建父目录，覆盖写入，默认 1MB 上限。

**web_search**：
- 在 `tools` 数组中声明 `{"type": "web_search"}`。
- 服务端执行，输出 `web_search_call` 事件。
- 消息转换层必须保留 `web_search_call` 项。

### 4.3 工具注册中心

`ToolRegistry` 职责：

- **Schema 自动生成**：基于 Pydantic 模型生成 Function Calling JSON Schema。
- **权限过滤**：按角色过滤可用工具列表。
- **审计记录**：记录工具名、参数、结果、延迟。
- **超时管理**：每个工具配置独立超时时间。

**注意**：`create_bash_tool()` 生成的 Schema 是 `{"commands": "..."}` 单参数形式，System Prompt 中描述工具用法时必须与实际 Schema 一致。

### 4.4 消息格式转换层（基础版）

| LangGraph | Responses API |
|---|---|
| `HumanMessage` | `{"type": "message", "role": "user", ...}` |
| `AIMessage`（含 tool_calls） | `{"type": "function_call", ...}` |
| `ToolMessage` | `{"type": "function_call_output", ...}` |
| `AIMessage`（纯文本） | `{"type": "message", "role": "assistant", ...}` |
| `web_search_call` | 保留，不过滤 |

**无状态处理**：Responses API 无状态，每次请求回传完整历史。阶段一直接全量回传。


## 五、阶段专属降级路径

| 故障 | 降级行为 |
|---|---|
| 工具调用超时 | 返回错误信息给模型 |
| 消息转换失败 | 回退到全量历史回传 |
| bashkit 沙箱异常 | 返回错误，不阻塞对话 |
| 最大步数触发 | 强制终止，返回已收集信息 |


## 六、验证标准

1. Agent 能正确选择工具：需要文件读取时调用 `read` 而非 `bash`。
2. Agent 能根据工具结果继续推理。
3. Agent 能在无工具需求时直接回复。
4. 最大步数生效：超过 10 步强制终止。
5. 工具重试上限生效：失败 2 次后降级。
6. `web_search` 正常触发。
7. 审计日志完整。
8. **VFS 一致性**：`write` 写入的文件能被后续 `bash` 和 `read` 读取。
9. **资源限制生效**：超过 `max_commands` 时返回错误。


## 七、阶段专属禁止事项

除 `AGENTS.md` 的全局禁止事项外，本阶段额外禁止：

- 在 `read` / `write` 中使用宿主机文件系统。
- 在 `bash` 工具中开放写操作和网络请求。
- 跳过消息转换层的单元测试。


## 八、更新记录

- 2026-09：初始版本，对应阶段一。


---