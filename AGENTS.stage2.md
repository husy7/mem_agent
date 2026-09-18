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
