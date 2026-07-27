# Finance Query Intent Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let DeepSeek understand flexible Chinese finance questions, normalize its loose intent locally, query Firefly safely, and return the interpreted filters with the exact result.

**Architecture:** Replace direct strict `FinanceQuery` parsing with a tolerant `RawFinanceIntent` followed by a deterministic local conversion into the existing `FinanceQuery`. Reuse the current Firefly fetch and aggregation path; do not add dependencies, database changes, or a second model call.

**Tech Stack:** Python 3.12, Pydantic, Anthropic-compatible DeepSeek gateway, FastAPI, pytest.

---

### Task 1: Loose intent and deterministic conversion

**Files:**
- Modify: `app/schemas/finance.py`
- Test: `tests/test_finance.py`

- [ ] **Step 1: Write failing conversion tests**

Add tests that construct `RawFinanceIntent` with Chinese values and assert:

```python
raw = RawFinanceIntent(
    start="2026-06-12",
    end="2026-07-27",
    transaction_type="支出",
    category="交通消费",
    metric="金额",
)
query = raw.to_query("1个半月内交通花了多少钱", date(2026, 7, 27))
assert query == FinanceQuery(
    start=date(2026, 6, 12),
    end=date(2026, 7, 27),
    transaction_type="withdrawal",
    category="交通",
    metric="sum",
)
```

Also assert:

```python
raw = RawFinanceIntent(transaction_type="消费", category="餐饮支出", metric="笔数")
query = raw.to_query("这两个月餐饮有多少笔", date(2026, 7, 27))
assert query.start == date(2026, 6, 1)
assert query.end == date(2026, 7, 27)
assert query.category == "餐饮"
assert query.metric == "count"
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_finance.py -q
```

Expected: import failure because `RawFinanceIntent` does not exist.

- [ ] **Step 3: Implement the tolerant schema**

In `app/schemas/finance.py`, add:

```python
class RawFinanceIntent(BaseModel):
    start: str | None = Field(default=None, validation_alias=AliasChoices("start", "start_date"))
    end: str | None = Field(default=None, validation_alias=AliasChoices("end", "end_date"))
    transaction_type: str | None = Field(
        default=None,
        validation_alias=AliasChoices("transaction_type", "type", "direction"),
    )
    category: str | None = None
    merchant: str | None = None
    metric: str | None = None

    def to_query(self, question: str, today: date) -> FinanceQuery:
        start, end = _query_period(question, today, self.start, self.end)
        direction = _normalize_direction(self.transaction_type, question)
        metric = _normalize_metric(self.metric, question)
        return FinanceQuery(
            start=start,
            end=end,
            transaction_type=direction,
            category=_normalize_category(self.category),
            merchant=(self.merchant or "").strip() or None,
            metric=metric,
        )
```

Use standard-library `calendar`, `datetime.timedelta`, and `re` helpers to implement:

- `这两个月`: previous calendar month first day through today.
- `最近两个月`: two rolling months through today.
- `本月`, `上月`, `今年`, explicit Chinese/Arabic month.
- `1个半月`: today minus 45 days.
- no time expression: current month first day through today.
- otherwise parse DeepSeek-provided ISO dates.
- direction and metric aliases from the approved spec.
- remove the suffixes `支出`, `消费`, `花费`, `费用` from categories.

- [ ] **Step 4: Run tests and verify GREEN**

Run the Step 2 command.

Expected: all `tests/test_finance.py` tests pass.

- [ ] **Step 5: Commit**

```powershell
git add app/schemas/finance.py tests/test_finance.py
git commit -m "feat: normalize loose finance query intents"
```

### Task 2: DeepSeek intent extraction and one corrective retry

**Files:**
- Modify: `app/llm/client.py`
- Test: `tests/test_llm_client.py`

- [ ] **Step 1: Write failing DeepSeek intent tests**

Replace the strict finance-query fake output with:

```python
raw = RawFinanceIntent(
    start="2026-06-01",
    end="2026-06-30",
    transaction_type="支出",
    category="餐饮",
    metric="金额",
)
fake = _FakeAnthropic([raw])
result = LLMClient(client=fake).parse_finance_query(
    "六月餐饮花了多少", today=date(2026, 7, 27)
)
assert result.transaction_type == "withdrawal"
assert result.metric == "sum"
assert fake.messages.calls[0]["output_format"] is RawFinanceIntent
```

Add a retry test where the first raw intent contains an invalid date and the second contains valid ISO dates. Assert two model calls and a valid final `FinanceQuery`.

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_llm_client.py -q
```

Expected: FAIL because the client still requests strict `FinanceQuery` output.

- [ ] **Step 3: Replace strict parsing with loose intent parsing**

Update `parse_finance_query` to:

```python
categories = "、".join(DEFAULT_CATEGORIES)
system = (
    f"你是记账查询意图解析器。今天是 {today.isoformat()}。"
    f"可用分类：{categories}。"
    "只提取时间、收入或支出、分类、商户、金额合计或笔数。"
    "日期输出 YYYY-MM-DD；没有时间时使用本月至今。"
    "这两个月表示上一个自然月1日至今天，最近两个月表示滚动两个月。"
    "不得生成 SQL、URL、代码或理财建议。"
)
error = ""
for attempt in range(2):
    content = question if not error else f"{question}\n上次参数无效：{error}\n请修正后重新提取。"
    try:
        response = self._client.messages.parse(
            model=self._settings.llm_model,
            max_tokens=self._settings.llm_max_tokens,
            output_config={"effort": self._settings.llm_effort},
            system=system,
            messages=[{"role": "user", "content": content}],
            output_format=RawFinanceIntent,
        )
        raw = getattr(response, "parsed_output", None)
        if raw is None:
            raise ValueError("模型未返回查账意图")
        return raw.to_query(question, today)
    except Exception as exc:
        error = str(exc)
raise LLMError(f"LLM 查账意图解析失败：{error}")
```

Remove the obsolete direct `FinanceQuery` structured-output path.

- [ ] **Step 4: Run tests and verify GREEN**

Run the Step 2 command.

Expected: all `tests/test_llm_client.py` tests pass.

- [ ] **Step 5: Commit**

```powershell
git add app/llm/client.py tests/test_llm_client.py
git commit -m "feat: let DeepSeek extract loose finance intents"
```

### Task 3: Clear interpreted response and failure stage

**Files:**
- Modify: `app/api/routes_review.py`
- Test: `tests/test_web_console.py`

- [ ] **Step 1: Write failing route tests**

Update the deterministic query assertion to require:

```python
assert "已理解:" in message
assert "2026-06-01 至 2026-06-30" in message
assert "支出" in message
assert "分类「餐饮」" in message
assert "金额合计" in message
assert "查询结果:合计 20.00 CNY" in message
```

Update failure tests so `LLMError` produces `没能识别查询条件`, while a Firefly/network error produces `账目服务暂时不可用`.

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_web_console.py -q
```

Expected: FAIL because the route still uses the generic message and old response format.

- [ ] **Step 3: Implement the response and split errors**

Use separate exception blocks:

```python
except LLMError as exc:
    logger.warning("console_finance_query_parse_failed", error=str(exc))
    return _redirect_with_msg("没能识别查询条件，请尝试：六月餐饮支出多少")
except (FireflyError, httpx.HTTPError) as exc:
    logger.warning("console_finance_query_backend_failed", error=str(exc))
    return _redirect_with_msg("账目服务暂时不可用，请确认 Docker/Firefly 正在运行")
```

Build the result from existing fields:

```python
filters = []
if query.category:
    filters.append(f"分类「{query.category}」")
if query.merchant:
    filters.append(f"商户包含「{query.merchant}」")
metric_label = "交易笔数" if query.metric == "count" else "金额合计"
understood = "，".join(
    [
        f"{query.start.isoformat()} 至 {query.end.isoformat()}",
        direction,
        *filters,
        metric_label,
    ]
)
return _redirect_with_msg(f"已理解:{understood}；查询结果:{summary}")
```

- [ ] **Step 4: Run tests and verify GREEN**

Run the Step 2 command.

Expected: all `tests/test_web_console.py` tests pass.

- [ ] **Step 5: Run the full suite**

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check .
```

Expected: all tests pass and Ruff reports no errors.

- [ ] **Step 6: Commit**

```powershell
git add app/api/routes_review.py tests/test_web_console.py
git commit -m "feat: explain finance query interpretation"
```

### Task 4: Deploy and verify the real DeepSeek path

**Files:**
- No source changes expected.

- [ ] **Step 1: Rebuild current application containers**

Run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1
```

Expected: API, Worker, and Beat start successfully.

- [ ] **Step 2: Run real questions**

Submit through `/review/query`:

```text
这两个月餐饮花了多少
1个半月内交通花了多少钱
六月在美团花了多少
今年打车多少笔
```

Expected: each response starts with `已理解:`, shows dates and filters, and returns an exact amount or count from Firefly.

- [ ] **Step 3: Verify unrelated features**

Run the health endpoint and confirm the review page still renders. Do not send a weekly digest during this verification.

### Task 5: Rollback

**Files:**
- No source changes.

- [ ] **Step 1: Record the recovery point**

The pre-change code remains available at:

```powershell
git switch --detach backup-before-finance-query-intent-20260727
```

Return to the implemented version with `git switch main`. Docker data volumes are unaffected.
