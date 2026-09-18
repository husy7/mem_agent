# 混合模式 AGENTS 文件 Review

在生成四份文件之前，先明确几个关键设计决策，避免四份文件之间出现重复或矛盾。


## 一、四份文件的职责边界

| 文件 | 包含 | 不包含 |
|---|---|---|
| `AGENTS.md` | 项目概述、技术栈、架构总览、全局目录树、编码规范、全局降级路径、全局禁止事项、参考项目 | 任何阶段专属的范围、目录、验证标准 |
| `AGENTS.stage1.md` | 阶段范围（做/不做）、前置依赖、阶段专属目录、ReAct 图结构、工具系统设计、消息转换基础版、阶段验证标准、阶段专属禁止 | 编码规范、技术栈全表（引用全局） |
| `AGENTS.stage2.md` | 阶段范围、前置依赖、阶段专属目录、三层记忆设计、治理逻辑、注入策略、阶段验证标准、阶段专属禁止 | 同上 |
| `AGENTS.stage3.md` | 阶段范围、前置依赖、阶段专属目录、CRAG 设计、预算闸门、评估体系、Trace 层级、阶段验证标准、阶段专属禁止 | 同上 |


## 二、需要消除的重复项

| 内容 | 处理方式 |
|---|---|
| 技术栈 | 只在全局定义一次，阶段文件不重复列表 |
| 编码规范 | 只在全局定义一次 |
| 全局禁止事项 | 只在全局定义一次（如禁止硬编码 API Key） |
| 阶段专属禁止事项 | 放在各阶段文件（如阶段二禁止同步执行记忆写入） |
| 降级路径 | 全局定义通用降级（工具超时、LLM 失败），阶段定义专属降级（如 PostgreSQL 不可用） |
| 目录结构 | 全局给完整树，阶段只列本阶段新增或重点关注的子目录 |
| 验证标准 | 只放在阶段文件，全局不重复 |


## 三、潜在矛盾点

1. **技术栈的阶段性差异**：全局技术栈列出所有技术，但阶段一不会用到 PostgreSQL/Qdrant。需要在全局技术栈表中标注“引入阶段”。
2. **目录树的演进**：全局目录树包含所有阶段的目录，但阶段一开发时只有部分目录存在。需要在阶段文件中明确“本阶段创建哪些目录”。
3. **禁止事项的层级**：全局禁止事项是“永远不能违反”的，阶段禁止事项是“本阶段特别需要注意”的。两者不冲突，但需要在阶段文件中明确写“除全局禁止事项外，本阶段额外禁止……”。


## 四、生成规则

基于以上，生成四份文件：

- `AGENTS.md`：只写不变的东西。
- `AGENTS.stageN.md`：只写变的东西，引用全局文件。
- 阶段文件开头明确写“本文档是 AGENTS.md 的阶段补充，需与 AGENTS.md 一起阅读”。


---

# AGENTS.md

本文件是 **MemAgent** 项目的全局主文件，定义跨阶段不变的架构、技术栈、编码规范和禁止事项。各阶段的补充约束见 `AGENTS.stage1.md` / `AGENTS.stage2.md` / `AGENTS.stage3.md`。

**AI 助手使用规则**：始终先阅读本文件，再阅读当前阶段的补充文件。


## 一、项目概述

**MemAgent** 是一个有状态长期记忆智能体，核心能力：

- 跨会话记住用户偏好、事实与历史交互
- 通过工具调用完成文件读写、命令执行、网页搜索、知识库检索
- 基于 LangGraph 的 ReAct + 自校正循环
- 三层记忆架构（短期 / 工作 / 长期）+ 记忆治理
- Agentic RAG（CRAG 三分类 + 查询重写 + 三条预算闸门）

**一句话**：让 Agent 越用越懂你，同时能干活、能查资料、能记住。

**MVP 模型基线**：DeepSeek V4 Flash（统一使用，`web_search` 由 DeepSeek 服务端原生执行）。


## 二、技术栈

| 层 | 技术 | 引入阶段 |
|---|---|---|
| 编排 | LangGraph | 阶段一 |
| LLM | DeepSeek V4 Flash（Responses API） | 阶段一 |
| 工具沙箱 | bashkit（VFS + LangChain 适配） | 阶段一 |
| 后端 | FastAPI | 阶段一 |
| 日志 | structlog | 阶段一 |
| 短期记忆 | PostgreSQL Checkpointer（PostgresSaver） | 阶段二 |
| 长期记忆 | PostgreSQL Store + Qdrant | 阶段二 |
| 缓存 | Redis | 阶段二 |
| 可观测 | Langfuse | 阶段三 |
| 评估 | RAGAS + 自定义 scorer | 阶段三 |
| 前端 Demo | Streamlit | 阶段三 |
| 部署 | Docker Compose | 阶段三 |


## 三、架构总览

```
用户消息
  ↓
接入层（FastAPI）
  ↓
LangGraph Agent 编排
  ├─ understand（槽位抽取）
  ├─ recall_memory（长期记忆召回 + 前置注入）
  ├─ rewrite_for_context（多轮上下文补全）
  ├─ call_llm（模型推理，绑定工具）
  ├─ call_tool（工具执行）
  ├─ rewrite_for_retrieval（检索失败后改写）
  ├─ update_memory（异步后台任务）
  └─ generate（回复生成）
  ↓
工具注册中心
  ├─ Client-side：read / write / bash / knowledge_search / memory_search
  └─ Server-side：web_search（DeepSeek 内置）
  ↓
三层记忆
  ├─ 短期：PostgresSaver（thread_id 隔离）
  ├─ 工作：State 中的 JSON 槽位
  └─ 长期：PostgreSQL 权威源 + Qdrant 派生索引
```


## 四、全局目录结构

```
memagent/
├── AGENTS.md                  # 本文件
├── AGENTS.stage1.md
├── AGENTS.stage2.md
├── AGENTS.stage3.md
├── README.md
├── docker-compose.yml
├── pyproject.toml
├── .env.example
├── src/
│   ├── agent/
│   │   ├── graph.py
│   │   ├── state.py
│   │   └── nodes/
│   ├── tools/
│   │   ├── registry.py
│   │   ├── fs.py
│   │   ├── bash.py
│   │   ├── knowledge.py
│   │   └── memory.py
│   ├── memory/
│   │   ├── short_term.py
│   │   ├── working.py
│   │   ├── long_term.py
│   │   ├── governance.py
│   │   ├── injection.py
│   │   └── schema.py
│   ├── llm/
│   │   ├── client.py
│   │   └── converter.py
│   ├── rag/
│   │   ├── retriever.py
│   │   ├── grader.py
│   │   ├── rewriter.py
│   │   └── tool.py
│   └── observability/
│       ├── trace.py
│       └── langfuse.py
├── tests/
│   ├── unit/
│   ├── integration/
│   └── evaluation/
└── scripts/
    ├── seed_data.py
    └── run_eval.py
```


## 五、核心设计原则（跨阶段不变）

1. **统一 bashkit VFS**：`read` / `write` / `bash` 三个工具操作同一个 bashkit 实例的虚拟文件系统，禁止使用宿主机文件系统。
2. **工具 Schema 自动生成**：所有工具基于 Pydantic v2 模型自动生成 Function Calling Schema。
3. **异步优先**：I/O 操作使用 `async/await`，禁止在异步节点中执行阻塞 I/O。
4. **结构化日志**：使用 `structlog`，禁止 `print`。
5. **配置外置**：所有配置通过环境变量注入，提供 `.env.example`。
6. **降级不阻塞**：任何外部依赖故障时，对话主流程必须继续。
7. **状态切换而非物理删除**：记忆、工具、审计记录均不物理删除。
8. **可观测优先**：关键路径必须有日志或 Trace，便于定位问题。


## 六、编码规范

- **Python 3.12+**，使用 `pyproject.toml` 管理依赖。
- **类型注解**：所有函数必须有类型注解，使用 Pydantic v2 定义数据模型。
- **错误处理**：工具调用、LLM 调用、数据库操作必须有降级路径，不允许裸 `except`。
- **日志**：`structlog`，JSON 格式，包含 `trace_id` / `user_id` / `node` 字段。
- **测试**：每个模块必须有单元测试，集成测试覆盖主链路，评估测试独立目录。
- **命名**：模块用 snake_case，类用 PascalCase，常量用 UPPER_SNAKE_CASE。
- **注释**：只写“为什么”，不写“是什么”。复杂决策必须有注释说明理由。


## 七、全局降级路径

| 故障 | 降级行为 |
|---|---|
| 工具调用超时 | 返回错误信息给模型，由模型决定换工具或放弃 |
| LLM 调用失败 | 重试 2 次后返回友好错误 |
| 消息转换失败 | 回退到全量历史回传，跳过压缩 |
| 配置缺失 | 启动时快速失败，不允许运行时静默降级 |
| 预算闸门触发 | 返回当前最佳结果，标注“信息可能不完整” |


## 八、全局禁止事项

以下事项在任何阶段都**不允许**：

- 硬编码 API Key、数据库连接串、模型名称。
- 使用 `print` 调试。
- 在异步节点中执行阻塞 I/O。
- 物理删除任何数据（用状态切换替代）。
- 跳过测试或删除测试用例。
- 在未更新 `AGENTS.md` 的情况下改变架构决策。
- 将敏感信息（PII、Key）写入日志。


## 九、参考项目

| 项目 | 参考点 |
|---|---|
| Mem0 | 提取-合并-检索流水线、混合检索 |
| Letta / MemGPT | 分层记忆、sleep-time 异步整合 |
| Zep | 时间知识图谱、事实失效而非删除 |
| A-Mem | 记忆演化、新记忆触发旧记忆更新 |
| MemOS | 异步摄入、自然语言反馈修正 |
| bashkit | VFS 沙箱、LangChain 集成 |
| CRAG | 三分类相关性评分 |
| RAGAS | Agent 评估 scorer |
| Langfuse | Trace 层级设计 |


## 十、更新记录

- 2026-09：初始版本，全局主文件。


---

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

# AGENTS.stage2.md

本文档是 `AGENTS.md` 的阶段二补充，需与 `AGENTS.md` 一起阅读。仅定义阶段二专属的范围、目录、设计、验证标准和禁止事项。


## 一、阶段范围

**做：**
- PostgresSaver 短期记忆
- 工作记忆 JSON 槽位
- PostgresStore + Qdrant 长期记忆
- 记忆抽取独立节点
- 记忆治理（冲突消解、评分、状态切换、遗忘）
- 差异化检索
- 记忆操作工具化
- 混合注入策略
- 消息格式转换层完整版

**不做：**
- Agentic RAG（阶段三）
- 评估体系（阶段三）
- 多用户/多租户隔离（命名空间规范就位，实现留待迭代）


## 二、前置依赖

阶段一完成，Agent 主链路跑通。


## 三、阶段专属目录

本阶段新增以下目录和文件：

```
src/
├── agent/
│   ├── state.py               # 扩展 State（工作记忆槽位）
│   └── nodes/
│       ├── understand.py      # 槽位抽取
│       ├── recall_memory.py   # 长期记忆召回 + 前置注入
│       ├── update_memory.py   # 异步后台任务
│       └── generate.py
├── memory/
│   ├── short_term.py          # Checkpointer
│   ├── working.py             # 工作记忆槽位
│   ├── long_term.py           # Store + Qdrant
│   ├── governance.py          # 冲突消解、评分、遗忘
│   ├── injection.py           # 记忆注入
│   └── schema.py              # PostgreSQL 表定义
├── tools/
│   └── memory.py              # memory_search
└── llm/
    └── converter.py           # 完整版转换层（含摘要压缩）
tests/
├── unit/
│   ├── test_governance.py
│   ├── test_scoring.py
│   └── test_converter.py
└── integration/
    ├── test_cross_session.py
    └── test_conflict.py
```


## 四、核心设计

### 4.1 三层记忆架构

**短期记忆**：PostgresSaver，通过 `thread_id` 隔离。使用连接池，轮次上限默认 50 轮。仅父图配置 Checkpointer，子图不单独配置。

**工作记忆**：State 中的 JSON 槽位（任务描述、实体、计划、步数、工具结果、重试计数）。在 `understand` 节点抽取。

**长期记忆**：PostgreSQL 权威源 + Qdrant 派生索引。

- 命名空间：`("memories", user_id, memory_type)`
- 记忆类型：preference / fact / event / procedure
- 存储粒度：一条记忆 = 一个独立事实单元
- 对 `content` 做 embedding（不是 `structured_data`）

**关键区分**：
- `understand` 节点：只做槽位抽取，每轮都做。
- `update_memory` 节点：从完整对话抽取长期记忆，ReAct 循环结束后执行一次。

### 4.2 PostgreSQL 表结构

**memories 表**：

| 字段 | 说明 |
|---|---|
| `memory_id` | 主键 |
| `user_id` | 用户标识 |
| `memory_type` | preference / fact / event / procedure |
| `content` | 自然语言描述（用于 embedding） |
| `structured_data` | JSON，如 `{"key": "回答风格", "value": "简洁"}` |
| `score` | 综合评分 S(m) |
| `state` | active / historical / archived |
| `valid_from` | 生效时间 |
| `valid_to` | 失效时间（null 表示仍有效） |
| `created_at` | 创建时间 |
| `last_accessed_at` | 最近访问时间 |
| `access_count` | 访问次数 |

**memory_relations 表**：

| 字段 | 说明 |
|---|---|
| `memory_id_a` | 记忆 A |
| `memory_id_b` | 记忆 B |
| `relation_type` | 关联类型 |
| `strength` | 关联强度 |

**Qdrant payload**：`memory_id` + `user_id` + `memory_type` + `state`。

### 4.3 记忆治理

**评分公式**：

```
S(m) = α·I + β·C + γ·R + δ·F
```

默认权重：`α=0.25, β=0.25, γ=0.25, δ=0.25`。

| 因子 | 含义 | 取值范围 | 计算方式 |
|---|---|---|---|
| I | 重要性 | 0-1 | LLM 抽取时给出 |
| C | 置信度 | 0-1 | LLM 抽取时给出 |
| R | 时近性 | 0-1 | `exp(-λ·days_since_last_access)`，λ=0.01 |
| F | 使用频率 | 0-1 | `min(access_count / 10, 1.0)` |

**冲突检测**：基于 `structured_data.key`。新记忆写入前查同一 `user_id` + `memory_type` + `key` 的 active 记忆，值不同则触发冲突仲裁。反义对作为补充规则。

**三条更新路径**：

- 新记忆显著更优 → 旧记忆 `archived`，新记忆 `active`
- 两者接近 → 旧记忆 `historical`，新记忆 `active`
- 旧记忆更优 → 新记忆 `historical`

**时间区间**：冲突消解时旧记忆 `valid_to = now()`，新记忆 `valid_from = now()`。

**差异化检索**：

| 状态 | 检索行为 |
|---|---|
| `active` | 正常召回，不衰减 |
| `historical` | 正常召回，评分 × 0.5 |
| `archived` | 硬过滤，不参与召回 |

**异步更新**：`update_memory` 作为后台任务异步执行，不阻塞用户回复。用 flash 模型做抽取。

### 4.4 记忆注入

- 偏好记忆 + 程序记忆：始终前置注入（≤ 4 条）
- 事实记忆 + 事件记忆：按需通过 `memory_search` 检索
- 总注入上限 ≤ 8 条
- 格式：System Prompt 末尾 `<memory>` 块，每条带 `memory_id`
- 标注“以下是历史记忆，可能与当前对话冲突，以用户最新表达为准”

### 4.5 记忆操作工具化

- `memory_search`：注册为 client-side function tool，模型可主动调用。
- `memory_store`：**不注册为工具**，作为 `update_memory` 节点的内部函数调用。

### 4.6 消息格式转换层（完整版）

在阶段一基础上增加：

- Checkpointer 恢复的消息列表经转换后作为 `input` 数组传入。
- 支持摘要压缩：超过 N 轮后，将早期消息压缩为摘要，只回传摘要 + 最近 N 轮。

### 4.7 图结构

```
START → understand → recall_memory → call_llm → should_tool?
                                      ↑            ↓ 有工具      ↓ 无工具
                                   call_tool    update_memory（异步）
                                                   ↓
                                                generate → END
```


## 五、阶段专属降级路径

| 故障 | 降级行为 |
|---|---|
| PostgreSQL 不可用 | 跳过记忆读写，对话继续，记录告警 |
| Qdrant 不可用 | 降级为 PostgreSQL 按 key 精确查询 |
| 记忆抽取失败 | 跳过本轮更新，不写入空记忆 |
| 冲突检测异常 | 新记忆以 `historical` 写入，不覆盖旧记忆 |
| 记忆写入失败 | 记录日志，不影响本轮回复 |


## 六、验证标准

**功能验证**：

1. 跨会话召回：关闭会话后重新开始，Agent 能召回之前的用户偏好。
2. 冲突消解：新偏好与旧偏好冲突时，按评分进入正确的状态切换路径。
3. 遗忘策略生效：低分记忆不参与检索，高分记忆正常召回。
4. 混合注入生效：前置注入和工具调用两种路径都能召回记忆。
5. Checkpointer 恢复：重启服务后，同一 `thread_id` 的会话能继续。
6. 记忆状态可观测。

**边界与异常场景**：

7. 重复偏好合并：连续三次表达同一偏好，记忆合并而非重复写入。
8. 反转偏好处理：与三个月前完全相反的偏好，旧记忆正确归档。
9. 空结果不写入。
10. 相似但不同主题：高相似度但不同主题的两条记忆，不被误判为冲突。

**性能验证**：

11. 记忆检索 P95 < 200ms。
12. 记忆写入 P95 < 300ms。
13. 整体对话 P95 延迟增加 ≤ 500ms。


## 七、阶段专属禁止事项

除 `AGENTS.md` 的全局禁止事项外，本阶段额外禁止：

- 将 `memory_store` 暴露给模型直接调用。
- 在 `understand` 节点中做长期记忆抽取。
- 在同步流程中执行记忆写入。
- 硬编码评分权重（通过配置注入）。
- 在 `update_memory` 中阻塞用户回复。


## 八、更新记录

- 2026-09：初始版本，对应阶段二。


---

# AGENTS.stage3.md

本文档是 `AGENTS.md` 的阶段三补充，需与 `AGENTS.md` 一起阅读。仅定义阶段三专属的范围、目录、设计、验证标准和禁止事项。


## 一、阶段范围

**做：**
- Agentic RAG 工具封装（`knowledge_search`）
- CRAG 三分类评分与自校正循环
- 三条预算闸门
- 查询重写两个独立节点
- 100+ 测试集（含 `expected_trajectory`）
- Langfuse 全链路 Trace
- Docker Compose 部署
- Streamlit 可视化

**不做：**
- 多 Agent 协作（单 Agent + 一次自检已足够）
- 生产级高可用部署（Demo 级别即可）
- 大规模性能优化（记录瓶颈即可）
- 自建 web_search 实现


## 二、前置依赖

第一、二阶段完成，工具调用和记忆系统跑通。


## 三、阶段专属目录

本阶段新增以下目录和文件：

```
src/
├── rag/
│   ├── retriever.py           # 混合检索
│   ├── grader.py              # CRAG 三分类评分
│   ├── rewriter.py            # 查询重写（两个节点）
│   └── tool.py                # knowledge_search 工具封装
├── tools/
│   └── knowledge.py           # 注册到 ToolRegistry
├── agent/
│   └── nodes/
│       ├── rewrite_for_context.py
│       └── rewrite_for_retrieval.py
└── observability/
    └── langfuse.py            # Trace 层级
tests/
└── evaluation/
    ├── dataset/               # 100+ 测试用例
    ├── scorers/               # RAGAS + 自定义 scorer
    └── run_eval.py
scripts/
└── seed_data.py
docker-compose.yml
```


## 四、核心设计

### 4.1 单 Agent + 一次自检

**架构决策**：2026 年工程共识表明，单 Agent + 工具调用 + 一次自检在 70% 场景下优于多角色 Multi-Agent。因此 Agentic RAG 采用单 Agent + CRAG 自校正循环，**不做**多 Agent Critic-Planner 循环。

### 4.2 CRAG 三分类

| 评分 | 判定 | 动作 |
|---|---|---|
| Relevant | ≥ 0.7 | 进入生成 |
| Ambiguous | 0.4–0.7 | 查询重写 + 重新检索 |
| Irrelevant | < 0.4 | 回退到 web_search 或返回信息不足 |

**参数**：

- `QUALITY_GATE_THRESHOLD = 0.6`
- `MAX_BACKTRACK_COUNT = 2`
- 三分类阈值：Relevant ≥ 0.7，Ambiguous 0.4–0.7，Irrelevant < 0.4

### 4.3 三条预算闸门

任一触发立刻终止：

| 闸门 | 默认值 |
|---|---|
| 最大检索轮数 | 5 |
| 最大 Token | 50k input + 10k output |
| 最大耗时 | 30 秒 |

### 4.4 查询重写两个独立节点

- **`rewrite_for_context`** ：在 `recall_memory` 之后、第一次检索之前执行。负责多轮对话中的上下文补全。
- **`rewrite_for_retrieval`** ：在检索后判定为 Ambiguous/Irrelevant 时执行。负责改写查询以提高召回。

两种重写用的 prompt 和策略不同，**不允许**混在一个节点里。

### 4.5 knowledge_search 工具

- 封装为 client-side function tool，注册到 ToolRegistry。
- 与 `read` / `write` / `bash` / `memory_search` 并列。
- 检索结果作为工具观察返回给模型，**不直接拼接到 prompt**。
- 内部实现混合检索（BM25 + 向量 + RRF）+ reranker。

### 4.6 web_search 集成

- MVP 统一使用 DeepSeek V4 Flash，`web_search` 服务端原生执行。
- 在 `tools` 数组中声明 `{"type": "web_search"}`。
- 消息转换层保留 `web_search_call` 项。
- **不需要**模型路由，**不需要**自建搜索实现。

### 4.7 评估体系

**三个评估层面**：

| 层面 | 评估内容 | 方法 |
|---|---|---|
| 最终响应 | 回答是否正确、完整 | 人工标注 + LLM-as-Judge + RAGAS |
| 轨迹 | 是否走了预期路径 | 对比理想工具调用序列 |
| 单步 | 每步工具选择是否合理 | 逐步检查工具名和参数 |

**核心指标与计算方式**：

| 指标 | Scorer | 计算方式 |
|---|---|---|
| 任务完成率 | AgentGoalAccuracy | 对比 golden reference |
| 工具选择准确率 | ToolCallAccuracy | 对比理想工具序列 |
| 参数正确率 | 自定义 | 对比函数调用参数 |
| 步数比 | 自定义 | 实际步数 / 理想步数 |
| 记忆召回率 | 自定义 | 相关记忆是否出现在召回结果 |
| RAG 相关性 | Faithfulness + ContextRelevance | RAGAS 内置 scorer |

**测试集设计**：100+ 条，覆盖单轮（15）、多轮（15）、工具调用（20）、记忆召回（15）、冲突消解（10）、RAG 检索（10）、多跳推理（5-10）、无答案/越权（10）。

**每个用例必须含 `expected_trajectory` 字段**。

### 4.8 Langfuse Trace 层级

```
trace（一次完整用户请求）
  └── agent（Agent 的一次推理循环）
        ├── generation（每次 LLM 调用：model、token、cost）
        ├── tool（每次工具调用：工具名、参数、结果）
        ├── memory（记忆读写事件）
        └── checkpoint（Checkpointer 状态变更）
```

**先设计 Trace 数据契约，再插桩**。

**评估结果写入 Trace**：评估跑完后，把每个用例的 scorer 结果写入对应 trace 的 score 字段。

### 4.9 部署

**Docker Compose 编排**：FastAPI + PostgreSQL + Qdrant + Redis + Langfuse + Streamlit。

**Streamlit 可视化**：
- 对话界面
- 记忆状态查看（按类型、评分、状态筛选）
- Trace 查看（工具调用、记忆读写、Token 消耗）
- 评估结果展示


## 五、阶段专属降级路径

| 故障 | 降级行为 |
|---|---|
| 检索不相关 | 触发查询重写；重写失败则回退 web_search |
| 预算闸门触发 | 返回当前最佳结果，标注“信息可能不完整” |
| Langfuse 不可用 | 降级为本地日志，不阻塞对话 |
| Qdrant 不可用 | 降级为 PostgreSQL 按 key 精确查询 |
| 评估 scorer 异常 | 记录失败用例，不中断整体评估 |


## 六、验证标准

**功能验证**：

1. Agentic RAG 生效：检索不相关时触发查询重写，循环直到合格或预算耗尽。
2. 三分类路由正确。
3. 混合工具使用：Agent 能在一次对话中混合使用记忆检索、知识库检索和网页搜索。
4. web_search 正常触发。
5. Langfuse Trace 完整。

**评估验证**：

6. 测试集通过率：100+ 测试用例通过率达到可接受水平。
7. 评估指标可量化。
8. 轨迹评估生效：能对比实际工具序列与 `expected_trajectory`。

**性能验证**：

9. 步数效率：多跳场景下实际步数 / 理想步数 ≤ 1.5。
10. 预算闸门生效。

**部署验证**：

11. Docker Compose 一键启动，Streamlit 界面可操作。


## 七、阶段专属禁止事项

除 `AGENTS.md` 的全局禁止事项外，本阶段额外禁止：

- 将 Agentic RAG 做成多 Agent Critic 循环。
- 硬编码步数上限（必须用三条闸门）。
- 把检索结果直接拼接到 prompt（必须作为工具观察返回）。
- 在未标注 `expected_trajectory` 的情况下新增测试用例。


## 八、更新记录

- 2026-09：初始版本，对应阶段三。


## 四份文件的使用方式

| 场景 | 读取 |
|---|---|
| AI 助手首次接入项目 | `AGENTS.md` |
| 开发阶段一 | `AGENTS.md` + `AGENTS.stage1.md` |
| 开发阶段二 | `AGENTS.md` + `AGENTS.stage2.md` |
| 开发阶段三 | `AGENTS.md` + `AGENTS.stage3.md` |
| 架构调整 | 只改 `AGENTS.md` |
| 阶段范围调整 | 只改对应 `AGENTS.stageN.md` |