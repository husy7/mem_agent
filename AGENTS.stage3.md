
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