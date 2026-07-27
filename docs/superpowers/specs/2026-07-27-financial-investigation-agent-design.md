# 多步财务调查 Agent 设计

日期：2026-07-27  
状态：用户已确认方向，等待规格复核

## 1. 背景

Firefly Copilot 当前是一个带 LLM 分类与自然语言意图解析的确定性财务工作流。
本次改造在不改变现有记账、复核和哨兵流程的前提下，增加一个只读的多步财务调查
Agent，使模型能够根据用户目标动态选择工具、读取工具结果并继续调查。

## 2. 目标

- 新增一个 `POST /api/agent/query` 接口。
- 支持模型在最多三次工具调用内完成财务调查。
- 复用现有 Firefly 查询、金额统计、订阅检测和重复扣费逻辑。
- 所有工具只读；模型不能写账、生成 SQL 或调用白名单外函数。
- 使用 Pydantic 校验模型决策和每个工具的参数。
- 金额计算全部由 Python `Decimal` 完成。
- 每一步调用带 `trace_id` 写入现有 `audit_logs`。
- 返回最终答案和可展示的执行步骤，便于面试演示。

## 3. 非目标

- 不做自动写账、修改分类或删除数据。
- 不做 RAG、向量库、长期记忆和多租户。
- 不做 multi-agent、规划 Agent、反思 Agent 或协调者。
- 不引入 LangChain、LangGraph 或新的 Agent 框架。
- 不做流式输出和新前端；使用 FastAPI Swagger 即可演示。
- 不让模型执行金额计算、SQL、URL 或任意代码。
- 不修复与只读 Agent 无关的现有入账重试问题；该问题单独处理。

## 4. 总体架构

```text
POST /api/agent/query
        |
        v
AgentRunner（最多三次工具调用）
        |
        +--> LLMClient：返回结构化 AgentDecision
        |
        +--> Tool Registry
                +-- summarize_spending
                +-- search_transactions
                +-- detect_subscriptions
                +-- find_duplicate_charges
        |
        +--> observation 追加到本次运行状态
        |
        +--> finish 或达到上限后强制总结
        |
        v
AgentResponse + AuditLog
```

模型只负责选择下一步和生成自然语言结论。工具执行、日期限制、筛选、排序和金额计算
均由应用代码控制。

## 5. Agent 状态与循环

一次运行只在当前 HTTP 请求内保存短期状态，不增加持久化会话记忆。

状态包含：

- 原始问题；
- 当前日期；
- 已执行步骤；
- 每一步的工具参数、状态和 observation；
- 工具调用次数；
- `trace_id`。

循环规则：

1. 把用户问题、当前日期、允许的工具和历史 observation 发送给模型。
2. 模型返回结构化 `AgentDecision`：
   - `action`：四个工具之一或 `finish`；
   - `arguments`：工具参数；
   - `reasoning_summary`：不含完整思维链的简短决策说明；
   - `final_answer`：仅 `finish` 时允许。
3. Runner 检查 action 是否在白名单中，并用对应 Pydantic 输入模型校验参数。
4. 执行工具，把受限长度的结果作为 observation 加入状态。
5. 模型可以继续调用工具或返回 `finish`。
6. 最多允许三次工具调用。第三次 observation 后，Runner 使用只允许输出最终答案的
   `AgentFinal` schema 再调用一次模型；因此单次请求最多四次 LLM 调用。

无论模型如何输出，应用都不会执行未注册函数。

## 6. 数据契约

### 6.1 API 请求

```json
{
  "question": "为什么我这个月比上个月花得多？"
}
```

要求：

- 去除首尾空白后不能为空；
- 最大长度 500 字符。

### 6.2 模型决策

`AgentDecision`：

- `action`：`summarize_spending`、`search_transactions`、
  `detect_subscriptions`、`find_duplicate_charges`、`finish`；
- `arguments`：JSON 对象；
- `reasoning_summary`：最大 200 字符；
- `final_answer`：可空，只有 `finish` 时使用。

`AgentFinal`：

- `answer`：最终中文回答；
- `evidence_summary`：最多五条简短证据。

### 6.3 API 响应

```json
{
  "trace_id": "abc123",
  "answer": "本月支出增长主要来自餐饮和订阅服务。",
  "stopped_reason": "finished",
  "steps": [
    {
      "tool": "summarize_spending",
      "arguments": {
        "start": "2026-07-01",
        "end": "2026-07-27",
        "group_by": "category"
      },
      "status": "success",
      "observation_summary": "总支出 2400.00 CNY，共 42 笔"
    }
  ]
}
```

响应不暴露模型完整思维链，也不默认返回完整交易明细。

## 7. 工具

### 7.1 `summarize_spending`

用途：统计一个日期范围内的支出，并按分类或商户分组。

输入：

- `start`、`end`；
- `group_by`：`category` 或 `merchant`。

限制：

- 日期范围不能超过 366 天；
- 最多返回金额最高的 20 个分组；
- 金额使用 `Decimal`，序列化为两位小数字符串。

### 7.2 `search_transactions`

用途：根据日期、分类或商户查询交易，用于进一步解释统计结果。

输入：

- `start`、`end`；
- 可选 `category`、`merchant`；
- `limit`，默认 10，最大 20。

输出只包含日期、商户、分类、金额和描述摘要，并按金额倒序。

### 7.3 `detect_subscriptions`

用途：检查截至指定日期的持续订阅和最近涨价。

输入：

- `as_of`。

内部固定读取最近 120 天的支出并复用现有 `detect_subscriptions`。

### 7.4 `find_duplicate_charges`

用途：查找指定天数内相同商户、相同金额的疑似重复扣费。

输入：

- `days`，默认 7，范围 1–31。

重复分组逻辑从现有哨兵任务复用；如需移动，只提取现有纯函数，不改检测规则。

## 8. LLM 集成

继续使用现有 Anthropic SDK 和兼容 `base_url`，不新增模型依赖。

在 `LLMClient` 中增加：

- `decide_agent_action(...) -> AgentDecision`；
- `finish_agent_answer(...) -> AgentFinal`。

两者继续使用 `messages.parse(..., output_format=...)`。这是一套自研的 typed tool loop：
模型选择 action，Runner 校验并执行，observation 再交回模型。

系统提示明确：

- 只根据工具 observation 回答；
- 不估算或自行计算金额；
- 不生成 SQL、代码和投资建议；
- 信息不足时优先调用工具；
- 已有证据足够时立即 `finish`；
- 不重复调用相同参数的同一工具。

## 9. 权限与安全

- 工具注册表只包含四个只读函数。
- Agent 模块不导入 Firefly 的写交易方法。
- 每个工具参数单独使用 Pydantic 模型校验。
- 日期查询范围、结果数量和 observation 长度有硬限制。
- API 在 `CONSOLE_TOKEN` 配置时要求 `X-Console-Token` 请求头；本机空配置行为与现有
  单用户模式保持一致。
- 日志和 API 响应不返回密钥、Authorization 头或完整上游错误体。

## 10. 审计事件

复用现有 `AuditLog`，新增事件：

- `agent.started`
- `agent.tool_called`
- `agent.tool_succeeded`
- `agent.tool_failed`
- `agent.finished`
- `agent.failed`

审计 payload 保存工具名、校验后的参数、状态、结果摘要和停止原因，不保存模型完整思维链。

## 11. 错误处理

- 用户问题校验失败：FastAPI 返回 `422`。
- Agent API token 错误：返回 `401`。
- 模型调用失败：记录 `agent.failed`，返回不含敏感细节的 `503`。
- 工具参数非法：作为一次 `validation_error` observation 返回模型，计入三次上限。
- Firefly/tool 调用失败：作为一次 `tool_error` observation 返回模型，允许模型结束或换工具。
- 模型返回未知 action：拒绝执行，按 validation error 处理。
- 达到三次工具上限：强制生成最终答案，`stopped_reason="limit"`。
- 强制总结也失败：记录失败并返回 `503`。

## 12. 文件变更

新增：

- `app/agent/__init__.py`
- `app/agent/runner.py`
- `app/agent/tools.py`
- `app/schemas/agent.py`
- `app/api/routes_agent.py`
- `tests/test_agent.py`

修改：

- `app/llm/client.py`：增加两种 Agent 结构化调用；
- `app/main.py`：注册 Agent 路由；
- `README.md`：增加真实 Agent 能力、接口和边界说明。

不新增第三方依赖，不修改现有数据库迁移。

## 13. 测试

所有测试放在一个 `tests/test_agent.py` 中，外部服务使用现有 fake/monkeypatch 模式。

覆盖：

- 模型选择工具、接收 observation 后选择下一个工具并最终结束；
- 不同问题产生不同工具顺序；
- 提前 `finish`；
- 三次工具上限和强制总结；
- 未知工具和非法参数不会被执行；
- 日期范围、limit 和 observation 长度限制；
- Firefly 工具异常作为 observation 返回；
- LLM 异常返回 `503`；
- API token 校验；
- 审计事件完整；
- 工具金额由 `Decimal` 计算；
- Agent 工具注册表不存在写操作。

完成后运行：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
```

## 14. 验收标准

- `POST /api/agent/query` 能回答至少三类问题：
  - 月度支出差异；
  - 订阅及涨价；
  - 疑似重复扣费。
- 月度差异问题至少完成两次不同参数的工具调用后再给结论。
- 执行路径由模型决策，不由路由按问题类型写死。
- 任意单次请求最多三次工具调用、四次 LLM 调用。
- 任何模型输出都不能触发写账、SQL 或未注册函数。
- 响应包含 `trace_id`、最终答案、停止原因和步骤摘要。
- 全量测试和 Ruff 通过。

