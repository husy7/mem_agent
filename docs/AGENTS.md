
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
- 在异步节点中执行阻塞 I/O。
- 物理删除任何数据（用状态切换替代）。
- 跳过测试或删除测试用例。
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


