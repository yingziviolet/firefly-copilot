# Financial Agent Demo UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a lightweight `/agent` browser page and launcher entry that demonstrate the existing backend Agent through visual answers and tool steps.

**Architecture:** Keep the completed Agent loop and JSON API unchanged. Extend the Agent route module with a self-contained HTML/CSS/JavaScript page and cookie-based browser authentication, then point the existing Windows launcher at that page. The browser remains a thin renderer; all planning, tools, validation, calculations, limits, and audits stay in Python.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, native HTML/CSS/JavaScript, PowerShell WinForms, pytest.

---

## File map

**Modify**

- `app/api/routes_agent.py` — browser authentication, standalone page, and existing JSON API.
- `app/main.py` — register the page router.
- `tests/test_agent.py` — page, cookie bootstrap, and API-cookie tests.
- `scripts/launcher.ps1` — authenticated Agent URL and primary Agent button.
- `README.md` — document the visual demo entry.

**Do not create**

- Frontend packages, templates, static bundles, migrations, or database models.

### Task 1: Standalone Agent page and browser authentication

**Files:**

- Modify: `tests/test_agent.py`
- Modify: `app/api/routes_agent.py`
- Modify: `app/main.py`

- [ ] **Step 1: Add failing page and cookie tests**

Append these tests to `tests/test_agent.py`:

```python
def test_agent_page_renders_visual_client(client):
    response = client.get("/agent")

    assert response.status_code == 200
    assert "财务调查 Agent" in response.text
    assert 'id="agent-form"' in response.text
    assert 'id="messages"' in response.text
    assert 'id="steps"' in response.text
    assert "fetch('/api/agent/query'" in response.text
    assert ".textContent" in response.text
    assert ".innerHTML" not in response.text


def test_agent_page_bootstraps_httponly_cookie(client, monkeypatch):
    monkeypatch.setattr(
        routes_agent,
        "get_settings",
        lambda: SimpleNamespace(console_token="secret"),
    )

    unauthorized = client.get("/agent")
    assert unauthorized.status_code == 401

    response = client.get("/agent?token=secret", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/agent"
    assert "HttpOnly" in response.headers["set-cookie"]

    page = client.get("/agent")
    assert page.status_code == 200


def test_agent_endpoint_accepts_console_cookie(client, monkeypatch):
    expected = AgentResponse(
        trace_id="trace-agent-cookie",
        answer="未发现重复扣费。",
        stopped_reason="finished",
        steps=[],
    )
    monkeypatch.setattr(
        routes_agent,
        "get_settings",
        lambda: SimpleNamespace(console_token="secret"),
    )
    monkeypatch.setattr(routes_agent, "run_agent", lambda question, session: expected)
    client.cookies.set("console_token", "secret")

    response = client.post("/api/agent/query", json={"question": "检查重复扣费"})

    assert response.status_code == 200
    assert response.json()["trace_id"] == "trace-agent-cookie"
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
& "E:\后端+agent\.venv\Scripts\python.exe" -m pytest tests/test_agent.py -q
```

Expected: the page tests fail because `GET /agent` returns `404`, and cookie-only API
authentication returns `401`.

- [ ] **Step 3: Add the page router and shared browser authentication**

Update imports and authentication in `app/api/routes_agent.py`:

```python
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

_COOKIE_NAME = "console_token"


def _token_equal(supplied: str, expected: str) -> bool:
    return secrets.compare_digest(supplied.encode(), expected.encode())


def _authorized(request: Request, supplied: str | None = None) -> bool:
    expected = get_settings().console_token
    if not expected:
        return True
    return bool(
        (supplied and _token_equal(supplied, expected))
        or (
            request.cookies.get(_COOKIE_NAME)
            and _token_equal(request.cookies[_COOKIE_NAME], expected)
        )
    )


def require_agent_auth(
    request: Request,
    supplied: Annotated[str | None, Header(alias="X-Console-Token")] = None,
) -> None:
    if not _authorized(request, supplied):
        raise HTTPException(status_code=401, detail="缺少或错误的访问令牌")
```

Keep the existing API router and add:

```python
page_router = APIRouter(tags=["agent-ui"])


@page_router.get("/agent", response_class=HTMLResponse)
def agent_page(request: Request) -> Response:
    expected = get_settings().console_token
    supplied = request.query_params.get("token")
    if expected and supplied and _token_equal(supplied, expected):
        response = RedirectResponse("/agent", status_code=303)
        response.set_cookie(
            _COOKIE_NAME,
            expected,
            httponly=True,
            samesite="strict",
        )
        return response
    if not _authorized(request):
        return HTMLResponse("401: 缺少或错误的访问令牌", status_code=401)
    return HTMLResponse(_AGENT_PAGE)
```

- [ ] **Step 4: Add the self-contained visual page**

Add `_AGENT_PAGE` above `agent_page` in `app/api/routes_agent.py`. Use this complete page;
all response-derived values are assigned with `textContent`:

```python
_AGENT_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>财务调查 Agent</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#f4f7fb;color:#172033;font-family:"Microsoft YaHei UI",system-ui,sans-serif}
header{background:#14213d;color:#fff;padding:20px max(24px,calc((100% - 1100px)/2))}
header div{display:flex;align-items:center;justify-content:space-between;gap:16px}
header h1{font-size:22px;margin:0}header span{font-size:13px;color:#c9d5eb}
main{max-width:1100px;margin:24px auto;padding:0 20px}
.workspace{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(260px,.7fr);gap:18px}
.panel{background:#fff;border:1px solid #dce4ef;border-radius:14px;box-shadow:0 8px 24px #26375710}
#messages{min-height:390px;padding:18px;display:flex;flex-direction:column;gap:12px}
.message{max-width:84%;padding:12px 14px;border-radius:13px;line-height:1.65;white-space:pre-wrap}
.user{align-self:flex-end;background:#2864d7;color:#fff}
.assistant{align-self:flex-start;background:#f5f7fb;border:1px solid #dce4ef}
.error{align-self:flex-start;background:#fff1f1;color:#a32828}
#agent-form{display:flex;gap:10px;padding:14px;border-top:1px solid #dce4ef}
#question{flex:1;resize:vertical;min-height:48px;max-height:120px;padding:12px;border:1px solid #c8d2e1;border-radius:9px;font:inherit}
button{border:0;border-radius:9px;padding:0 20px;background:#2864d7;color:#fff;font-weight:700;cursor:pointer}
button:disabled{opacity:.55;cursor:wait}
.side{padding:16px}.side h2{font-size:16px;margin:0 0 12px}
.step{border-left:3px solid #2a8a64;background:#f1faf7;padding:10px;margin:9px 0;border-radius:0 8px 8px 0}
.step strong,.step small{display:block}.step small{margin-top:5px;color:#627086;white-space:pre-wrap}
#trace{margin-top:14px;color:#68758a;font-size:12px;word-break:break-all}
.empty{color:#7d899b;font-size:13px}
@media(max-width:760px){.workspace{grid-template-columns:1fr}#messages{min-height:300px}}
</style>
</head>
<body>
<header><div><h1>财务调查 Agent</h1><span>只读模式 · 最多 3 步工具调用</span></div></header>
<main>
  <div class="workspace">
    <section class="panel">
      <div id="messages">
        <div class="message assistant">你好，我可以调查支出增长、交易明细、订阅和重复扣费。</div>
      </div>
      <form id="agent-form">
        <textarea id="question" maxlength="500" required
          placeholder="例如：调查本月支出增加的原因，并检查重复扣费"></textarea>
        <button id="submit" type="submit">开始调查</button>
      </form>
    </section>
    <aside class="panel side">
      <h2>Agent 调查过程</h2>
      <div id="steps"><p class="empty">提交问题后，这里会显示工具步骤。</p></div>
      <div id="trace"></div>
    </aside>
  </div>
</main>
<script>
const form=document.querySelector('#agent-form');
const question=document.querySelector('#question');
const submit=document.querySelector('#submit');
const messages=document.querySelector('#messages');
const steps=document.querySelector('#steps');
const trace=document.querySelector('#trace');
const toolNames={
  summarize_spending:'汇总支出',
  search_transactions:'查询交易',
  detect_subscriptions:'检查订阅',
  find_duplicate_charges:'检查重复扣费'
};
function addMessage(kind,text){
  const node=document.createElement('div');
  node.className='message '+kind;
  node.textContent=text;
  messages.append(node);
}
function renderSteps(data){
  steps.replaceChildren();
  if(!data.steps.length){
    const empty=document.createElement('p');
    empty.className='empty';
    empty.textContent='Agent 直接完成，没有调用工具。';
    steps.append(empty);
  }
  data.steps.forEach((item,index)=>{
    const node=document.createElement('div');
    const title=document.createElement('strong');
    const detail=document.createElement('small');
    node.className='step';
    title.textContent=(index+1)+'. '+(toolNames[item.tool]||item.tool)+' · '+item.status;
    detail.textContent=item.observation_summary;
    node.append(title,detail);
    steps.append(node);
  });
  trace.textContent='trace_id: '+data.trace_id+' · '+data.stopped_reason;
}
form.addEventListener('submit',async(event)=>{
  event.preventDefault();
  const text=question.value.trim();
  if(!text)return;
  addMessage('user',text);
  submit.disabled=true;
  submit.textContent='调查中…';
  try{
    const response=await fetch('/api/agent/query',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      credentials:'same-origin',
      body:JSON.stringify({question:text})
    });
    if(!response.ok)throw new Error('request failed');
    const data=await response.json();
    addMessage('assistant',data.answer);
    renderSteps(data);
    question.value='';
  }catch(error){
    addMessage('error','调查失败，请检查 LLM 和 Firefly 配置后重试。');
  }finally{
    submit.disabled=false;
    submit.textContent='开始调查';
  }
});
</script>
</body>
</html>"""
```

- [ ] **Step 5: Register the page router**

In `app/main.py`, import:

```python
from app.api.routes_agent import page_router as agent_page_router
```

Register it without the `/api` prefix:

```python
app.include_router(agent_page_router)
```

- [ ] **Step 6: Run focused tests and verify GREEN**

Run:

```powershell
& "E:\后端+agent\.venv\Scripts\python.exe" -m pytest tests/test_agent.py tests/test_web_console.py -q
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit Task 1**

```powershell
git add app/api/routes_agent.py app/main.py tests/test_agent.py
git commit -m "feat: add visual financial agent page"
```

### Task 2: One-click Agent launcher

**Files:**

- Modify: `scripts/launcher.ps1`

- [ ] **Step 1: Extend the launcher self-test first**

In the `SelfTest` branch of `scripts/launcher.ps1`, replace calls to `Get-ReviewUrl` with
`Get-ConsoleUrl` and add:

```powershell
$actual = Get-ConsoleUrl -Path "/agent" -EnvPath $fixture
$expected = "http://127.0.0.1:8000/agent?token=abc%20123%26x"
if ($actual -ne $expected) {
    throw "Agent URL test failed: expected '$expected', got '$actual'"
}

$form = New-LauncherForm
try {
    $start = $form.Controls.Find("startButton", $true)
    if ($start.Count -ne 1 -or $start[0].Text -ne "启动并打开财务 Agent") {
        throw "Missing Agent launcher button"
    }
}
finally {
    $form.Dispose()
}
```

- [ ] **Step 2: Run the launcher self-test and verify RED**

Run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/launcher.ps1 -Action SelfTest
```

Expected: FAIL because `Get-ConsoleUrl` is not defined and the primary button still opens the
review console.

- [ ] **Step 3: Generalize the URL helper and update the primary action**

Replace `Get-ReviewUrl` with:

```powershell
function Get-ConsoleUrl {
    param(
        [string]$Path = "/review",
        [string]$EnvPath = (Join-Path $ProjectRoot ".env")
    )

    $token = Get-DotEnvValue -Path $EnvPath -Name "CONSOLE_TOKEN"
    $url = "http://127.0.0.1:8000$Path"
    if (-not $token) { return $url }
    return "${url}?token=$([Uri]::EscapeDataString($token))"
}
```

Update the buttons:

```powershell
$startButton = & $addButton "startButton" "启动并打开财务 Agent" 28 112 270
$reviewButton = & $addButton "reviewButton" "打开记账复核台" 306 112 270
```

Update their actions:

```powershell
$startButton.Add_Click({
    & $runAction (Get-GuiChildAction -Button "start") "后台启动中，完成后自动打开财务 Agent"
}.GetNewClosure())
$reviewButton.Add_Click({ Start-Process (Get-ConsoleUrl -Path "/review") }.GetNewClosure())
```

Update `StartAndOpen`:

```powershell
Start-ProjectServices
Start-Process (Get-ConsoleUrl -Path "/agent")
```

- [ ] **Step 4: Run the launcher self-test and verify GREEN**

Run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/launcher.ps1 -Action SelfTest
```

Expected:

```text
[OK] launcher self-test passed
```

- [ ] **Step 5: Commit Task 2**

```powershell
git add scripts/launcher.ps1
git commit -m "feat: open financial agent from launcher"
```

### Task 3: Documentation and final verification

**Files:**

- Modify: `README.md`

- [ ] **Step 1: Document the lightweight visual entry**

In the Agent section of `README.md`, add:

```markdown
### 可视化演示

Windows 用户双击 `启动记账系统.cmd`，点击“启动并打开财务 Agent”即可进入
`http://localhost:8000/agent`。页面支持直接提问，并展示最终回答、工具调用步骤、
状态和 `trace_id`。

该页面只是演示层；Agent 决策、工具执行、金额计算、参数校验和审计仍全部位于
Python 后端。
```

Update the daily-use table with:

```markdown
| 多步财务调查 | 双击启动器并打开财务 Agent，输入问题后查看回答和右侧工具步骤 |
```

- [ ] **Step 2: Run static checks**

Run:

```powershell
& "E:\后端+agent\.venv\Scripts\python.exe" -m ruff check .
```

Expected:

```text
All checks passed!
```

- [ ] **Step 3: Run the complete test suite**

Run:

```powershell
& "E:\后端+agent\.venv\Scripts\python.exe" -m pytest -q
```

Expected: all tests pass; the count is greater than the current 217.

- [ ] **Step 4: Run the launcher self-test**

Run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/launcher.ps1 -Action SelfTest
```

Expected:

```text
[OK] launcher self-test passed
```

- [ ] **Step 5: Inspect scope and whitespace**

Run:

```powershell
git diff --check
git status --short
git diff --stat
```

Expected: only the Agent route, main router registration, launcher, README, and focused tests are
changed. No dependency, migration, model, or frontend-build file is added.

- [ ] **Step 6: Commit documentation**

```powershell
git add README.md
git commit -m "docs: explain visual agent demo"
```

- [ ] **Step 7: Record final evidence**

Run:

```powershell
git status --short
git log --oneline -8
```

Expected: a clean worktree with focused UI, launcher, and documentation commits.
