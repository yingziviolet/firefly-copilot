# P2 Weekly Finance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add one-message weekly finance reports, subscription price-change detection, and restricted natural-language ledger queries.

**Architecture:** Put deterministic finance calculations in one dependency-free service. Celery calls that service for the weekly digest and sends exactly one notification; the review console calls it after an existing structured-output LLM client converts a question into validated query parameters.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, Celery, Firefly III REST API, Anthropic structured output, pytest.

---

### Task 1: Deterministic finance calculations

**Files:**
- Create: `app/schemas/finance.py`
- Create: `app/services/finance.py`
- Create: `tests/test_finance.py`

- [ ] **Step 1: Write failing tests**

Test validated query bounds, sum/count aggregation, monthly subscription recognition, price increases, and exclusion of irregular purchases.

- [ ] **Step 2: Verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_finance.py -q`

Expected: collection failure because `app.services.finance` and `app.schemas.finance` do not exist.

- [ ] **Step 3: Implement minimal schemas and pure functions**

Add:

```python
class FinanceQuery(BaseModel):
    start: date
    end: date
    transaction_type: Literal["withdrawal", "deposit"] = "withdrawal"
    category: str | None = None
    merchant: str | None = None
    metric: Literal["sum", "count"] = "sum"
```

Reject reversed ranges and ranges longer than 366 days. Implement local filtering and aggregation with `Decimal`, plus monthly subscription detection from three or more charges whose consecutive gaps are 25–35 days.

- [ ] **Step 4: Verify GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests/test_finance.py -q`

Expected: all tests pass.

### Task 2: Single-message weekly digest

**Files:**
- Modify: `app/worker/tasks_sentinel.py`
- Modify: `app/worker/celery_app.py`
- Modify: `tests/test_sentinel.py`

- [ ] **Step 1: Write failing tests**

Cover totals, category ranking, subscriptions, duplicate charges, one `notify()` call per run, no notification after Firefly failure, and aggregation of multiple duplicate groups into one existing manual alert.

- [ ] **Step 2: Verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_sentinel.py -q`

Expected: missing `send_weekly_digest` and multiple notification behavior fail.

- [ ] **Step 3: Implement minimal task**

Query 120 days of withdrawals and the completed week of deposits, derive the weekly withdrawal slice locally, build one report, and call `notify()` once. Replace daily beat entry with Monday 09:00 weekly execution. Keep `scan_duplicate_charges()` as a callable compatibility task but combine all duplicate groups into one message.

- [ ] **Step 4: Verify GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests/test_sentinel.py -q`

Expected: all tests pass.

### Task 3: Structured natural-language ledger query

**Files:**
- Modify: `app/llm/client.py`
- Modify: `app/api/routes_review.py`
- Modify: `tests/test_llm_client.py`
- Modify: `tests/test_web_console.py`

- [ ] **Step 1: Write failing tests**

Cover structured parsing into `FinanceQuery`, query form rendering, successful sum/count results, and user-facing errors for invalid LLM output or Firefly failure.

- [ ] **Step 2: Verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_llm_client.py tests/test_web_console.py -q`

Expected: missing query parser and `/review/query` endpoint fail.

- [ ] **Step 3: Implement minimal query path**

Add `LLMClient.parse_finance_query(question, today)` using the existing `messages.parse` client and `FinanceQuery` output format. Add a `/review/query` form and synchronous POST handler that fetches Firefly transactions, runs deterministic aggregation, then redirects to the existing escaped message bar.

- [ ] **Step 4: Verify GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests/test_llm_client.py tests/test_web_console.py -q`

Expected: all tests pass.

### Task 4: Public usage documentation

**Files:**
- Modify: `README.md`
- Modify: `运行与面试演示.md`

- [ ] **Step 1: Update documentation**

Document the Monday 09:00 single-message digest, subscription heuristic, `/review` query examples, supported query limits, and required existing environment variables. Mark the three implemented P2 items and leave email/credit-card work out of the implementation claim.

- [ ] **Step 2: Check secrets and formatting**

Run: `git diff --check`

Expected: no whitespace errors and no credentials in the diff.

### Task 5: Full verification

**Files:** No new files.

- [ ] **Step 1: Run the full test suite**

Run: `.venv\Scripts\python.exe -m pytest -q`

Expected: all tests pass.

- [ ] **Step 2: Run lint**

Run: `.venv\Scripts\python.exe -m ruff check .`

Expected: no lint errors.

- [ ] **Step 3: Inspect final diff**

Run: `git status --short` and `git diff --stat`

Expected: only the planned P2 code, tests, and documentation changed.
