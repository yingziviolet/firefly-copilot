import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.agent.runner import AgentError, run_agent
from app.config import get_settings
from app.db import get_session
from app.schemas.agent import AgentQuery, AgentResponse

router = APIRouter(prefix="/agent", tags=["agent"])
page_router = APIRouter(tags=["agent-ui"])
SessionDep = Annotated[Session, Depends(get_session)]
_COOKIE_NAME = "console_token"

_AGENT_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>财务调查 Agent</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#f4f7fb;color:#172033;
font-family:"Microsoft YaHei UI",system-ui,sans-serif}
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
#question{flex:1;resize:vertical;min-height:48px;max-height:120px;padding:12px;
border:1px solid #c8d2e1;border-radius:9px;font:inherit}
button{border:0;border-radius:9px;padding:0 20px;background:#2864d7;color:#fff;
font-weight:700;cursor:pointer}
button:disabled{opacity:.55;cursor:wait}
.side{padding:16px}.side h2{font-size:16px;margin:0 0 12px}
.step{border-left:3px solid #2a8a64;background:#f1faf7;padding:10px;margin:9px 0;
border-radius:0 8px 8px 0}
.step strong,.step small{display:block}
.step small{margin-top:5px;color:#627086;white-space:pre-wrap}
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


def _token_equal(supplied: str, expected: str) -> bool:
    return secrets.compare_digest(supplied.encode(), expected.encode())


def _authorized(request: Request, supplied: str | None = None) -> bool:
    expected = get_settings().console_token
    if not expected:
        return True
    cookie = request.cookies.get(_COOKIE_NAME)
    return bool(
        (supplied and _token_equal(supplied, expected))
        or (cookie and _token_equal(cookie, expected))
    )


def require_agent_auth(
    request: Request,
    supplied: Annotated[str | None, Header(alias="X-Console-Token")] = None,
) -> None:
    if not _authorized(request, supplied):
        raise HTTPException(status_code=401, detail="缺少或错误的访问令牌")


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


@router.post(
    "/query",
    response_model=AgentResponse,
    dependencies=[Depends(require_agent_auth)],
)
def agent_query(payload: AgentQuery, session: SessionDep) -> AgentResponse:
    try:
        return run_agent(payload.question, session)
    except AgentError as exc:
        raise HTTPException(status_code=503, detail="AI Agent 服务暂时不可用") from exc
