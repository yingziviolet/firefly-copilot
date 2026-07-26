"""内置 Web 控制台:服务端渲染 HTML,人工复核与快捷记账入口。

GET  /review                  控制台页面(提示条 + 快捷记账 + CSV/XLSX 上传 + 待复核卡片)
POST /review/quick            快捷记账(parse_quick_expense 文本解析)
POST /review/query            自然语言查账(LLM 只解析受限参数,程序聚合)
POST /review/upload           CSV/XLSX 上传(复用 routes_upload.enqueue_csv)
POST /review/{id}/approve     批准 -> finalize_review.delay
POST /review/{id}/correct     改分类(回流规则库)-> finalize_review.delay
                              -> reclassify_pending.delay(rules_only=True) 联动清同商户
POST /review/{id}/reject      驳回(仅状态流转)
POST /review/approve-all      全部批准 -> 逐条 approve + finalize_review.delay
POST /review/reclassify       触发 reclassify_pending.delay(rules_only=False)

鉴权:settings.console_token 非空时启用;带 ?token=<正确值> 种 httponly cookie 并
303 回 /review,此后凭 cookie 通过;两者都没有/不对返回 401 提示页;token 为空不设防。
所有用户数据经 html.escape 输出,防 XSS。
"""

import html
import secrets
from datetime import date, datetime
from typing import Annotated, Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.api.routes_upload import enqueue_csv
from app.config import get_settings
from app.db import get_session
from app.llm.client import LLMError, get_llm_client
from app.logger import get_logger, new_trace_id
from app.models.review import ReviewItem
from app.parsers.base import ParseError
from app.parsers.quick_text import parse_quick_expense
from app.schemas.classify import DEFAULT_CATEGORIES
from app.services import review
from app.services.finance import aggregate_transactions, format_money
from app.services.firefly_client import FireflyError, get_firefly_client
from app.worker.tasks_ingest import finalize_review, ingest_transaction, reclassify_pending

logger = get_logger(__name__)

_COOKIE_NAME = "console_token"


# ---------------------------------------------------------------------------
# 鉴权:单个依赖函数;需要直接回响应(303 种 cookie / 401 提示页)时抛中断异常
# ---------------------------------------------------------------------------


class ConsoleAuthInterrupt(Exception):
    """鉴权依赖携带响应中断请求;由 main 注册的 handler 原样返回该响应。"""

    def __init__(self, response: Response) -> None:
        self.response = response


def console_auth_interrupt_handler(request: Request, exc: ConsoleAuthInterrupt) -> Response:
    return exc.response


def _token_equal(supplied: str, expected: str) -> bool:
    return secrets.compare_digest(supplied.encode(), expected.encode())


def require_console_auth(request: Request) -> None:
    token = get_settings().console_token
    if not token:
        return  # 本机模式不设防

    supplied = request.query_params.get("token")
    if supplied and _token_equal(supplied, token):
        # 首次凭 ?token= 进入:种 cookie 并 303 回干净的 /review(顺带把 token 从 URL 去掉)
        resp = RedirectResponse(url="/review", status_code=303)
        resp.set_cookie(_COOKIE_NAME, token, httponly=True)
        raise ConsoleAuthInterrupt(resp)

    cookie = request.cookies.get(_COOKIE_NAME, "")
    if cookie and _token_equal(cookie, token):
        return

    page = (
        '<!doctype html><html lang="zh"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>需要访问令牌</title></head><body>"
        "<p>401:缺少或错误的访问令牌,请通过 /review?token=&lt;令牌&gt; 访问。</p>"
        "</body></html>"
    )
    raise ConsoleAuthInterrupt(HTMLResponse(page, status_code=401))


router = APIRouter(tags=["console"], dependencies=[Depends(require_console_auth)])

SessionDep = Annotated[Session, Depends(get_session)]


def _redirect_with_msg(msg: str) -> RedirectResponse:
    return RedirectResponse(url=f"/review?{urlencode({'msg': msg})}", status_code=303)


# ---------------------------------------------------------------------------
# 页面渲染(f-string 拼接,零依赖)
# ---------------------------------------------------------------------------

_STYLE = """
body{font-family:system-ui,sans-serif;margin:0 auto;max-width:640px;padding:16px;
background:#f6f7f9;color:#222}
h1{font-size:1.3rem}h2{font-size:1rem;margin:0 0 8px}
.msg{background:#fff7d6;border:1px solid #e6d489;border-radius:8px;padding:10px;margin:12px 0}
.panel,.card{background:#fff;border:1px solid #e3e5e8;border-radius:10px;padding:12px;margin:12px 0}
.row{display:flex;gap:8px;flex-wrap:wrap}
.row input[type=text]{flex:1;min-width:160px}
input,select,button{font-size:1rem;padding:8px;border-radius:8px;border:1px solid #c9ccd1}
button{background:#2563eb;color:#fff;border:none;cursor:pointer}
button.ok{background:#16a34a}button.danger{background:#dc2626}
.meta{display:flex;justify-content:space-between;align-items:baseline}
.amount{font-weight:600}
.sub{color:#666;font-size:.9rem;margin:6px 0}
.actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.actions form{display:flex;gap:6px;margin:0}
.empty{color:#888;text-align:center;padding:24px 0}
"""


def _format_txn_time(occurred_at: Any, fallback: datetime | None) -> str:
    """展示交易发生时间(ISO 字符串);解析失败回退复核项创建时间。"""
    try:
        return datetime.fromisoformat(str(occurred_at)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return fallback.strftime("%Y-%m-%d %H:%M") if fallback else "-"


def _render_card(item: ReviewItem) -> str:
    payload = item.txn_payload or {}
    merchant = html.escape(str(payload.get("counterparty", "?")))
    amount = html.escape(str(payload.get("amount", "?")))
    currency = html.escape(str(payload.get("currency", "")))
    category = html.escape(item.suggested_category or "?")
    confidence = f"{item.confidence:.2f}" if item.confidence is not None else "-"
    occurred = _format_txn_time(payload.get("occurred_at"), item.created_at)
    options = "".join(
        f'<option value="{html.escape(cat, quote=True)}">{html.escape(cat)}</option>'
        for cat in DEFAULT_CATEGORIES
    )
    return f"""
<article class="card">
  <div class="meta"><strong>{merchant}</strong>
    <span class="amount">{amount} {currency}</span></div>
  <div class="sub">建议分类:{category}(置信度 {confidence})· {occurred}</div>
  <div class="actions">
    <form method="post" action="/review/{item.id}/approve">
      <button type="submit" class="ok">批准</button></form>
    <form method="post" action="/review/{item.id}/correct">
      <select name="category">{options}</select>
      <button type="submit">改分类</button></form>
    <form method="post" action="/review/{item.id}/reject">
      <button type="submit" class="danger">驳回</button></form>
  </div>
</article>"""


def _render_page(msg: str, items: list[ReviewItem]) -> str:
    msg_bar = f'<div class="msg">{html.escape(msg)}</div>' if msg else ""
    if items:
        cards = "".join(_render_card(item) for item in items)
    else:
        cards = '<p class="empty">当前没有待复核项。</p>'
    return f"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>记账复核台</title>
<style>{_STYLE}</style>
</head>
<body>
<h1>记账复核台</h1>
{msg_bar}
<section class="panel">
  <h2>快捷记账</h2>
  <form method="post" action="/review/quick" class="row">
    <input type="text" name="text" placeholder="例如:早餐 15 / 昨天 打车 23.5" required>
    <button type="submit">记一笔</button>
  </form>
</section>
<section class="panel">
  <h2>自然语言查账</h2>
  <form method="post" action="/review/query" class="row">
    <input type="text" name="question"
      placeholder="例如:上月餐饮花了多少 / 今年打车多少笔" required>
    <button type="submit">查账</button>
  </form>
</section>
<section class="panel">
  <h2>上传账单 CSV / XLSX</h2>
  <form method="post" action="/review/upload" enctype="multipart/form-data" class="row">
    <select name="source">
      <option value="auto" selected>自动识别</option>
      <option value="alipay">支付宝</option>
      <option value="wechat">微信</option>
    </select>
    <input type="file" name="file" accept=".csv,.xlsx" required>
    <button type="submit">上传</button>
  </form>
</section>
<section>
  <div class="meta">
    <h2>待复核({len(items)})</h2>
    <div class="actions">
      <form method="post" action="/review/reclassify">
        <button type="submit">🔄 重新分类</button></form>
      <form method="post" action="/review/approve-all">
        <button type="submit" class="ok"
          onclick="return confirm('确认批准全部待复核项?')">✅ 全部批准</button></form>
    </div>
  </div>
  {cards}
</section>
</body>
</html>"""


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


@router.get("/review", response_class=HTMLResponse)
def console_page(request: Request, session: SessionDep) -> HTMLResponse:
    msg = request.query_params.get("msg", "")
    items = review.list_pending(session, limit=50)
    return HTMLResponse(_render_page(msg, items))


@router.post("/review/quick")
def console_quick(text: Annotated[str, Form()] = "") -> RedirectResponse:
    txn = parse_quick_expense(text)
    if txn is None:
        return _redirect_with_msg("没看懂这条记录,格式:描述 金额,例如「早餐 15」")
    trace_id = new_trace_id()
    ingest_transaction.delay(txn.dump_for_queue(), trace_id)
    logger.info(
        "console_quick_expense_enqueued",
        trace_id=trace_id,
        counterparty=txn.counterparty,
        amount=str(txn.amount),
    )
    return _redirect_with_msg(f"已收到:{txn.counterparty} {txn.amount} {txn.currency},正在入账处理")


@router.post("/review/query")
def console_query(question: Annotated[str, Form()] = "") -> RedirectResponse:
    if not question.strip():
        return _redirect_with_msg("请输入查账问题")
    try:
        query = get_llm_client().parse_finance_query(question.strip(), date.today())
        splits = get_firefly_client().list_transactions(
            query.start, query.end, txn_type=query.transaction_type
        )
    except (LLMError, FireflyError, httpx.HTTPError) as exc:
        logger.warning("console_finance_query_failed", error=str(exc))
        return _redirect_with_msg("暂时没法查询，请换一种明确问法后重试")

    result = aggregate_transactions(splits, query)
    direction = "收入" if query.transaction_type == "deposit" else "支出"
    filters = " ".join(value for value in (query.category, query.merchant) if value)
    subject = f"{direction} {filters}".strip()
    if query.metric == "count":
        summary = f"共 {result} 笔"
    else:
        summary = f"合计 {format_money(result)} {get_settings().default_currency}"
    return _redirect_with_msg(
        f"查询结果:{query.start.isoformat()} 至 {query.end.isoformat()}，{subject}，{summary}"
    )


# 渠道 -> 中文名,用于上传成功提示
_SOURCE_NAMES = {"alipay": "支付宝", "wechat": "微信"}


@router.post("/review/upload")
async def console_upload(source: Annotated[str, Form()], file: UploadFile) -> RedirectResponse:
    raw = await file.read()
    try:
        trace_id, enqueued, skipped, resolved = enqueue_csv(source, raw)
    except ParseError as exc:  # ParseError 是 ValueError 子类,必须先捕获
        return _redirect_with_msg(f"账单解析失败:{exc}")
    except ValueError:
        return _redirect_with_msg(f"不支持的账单来源:{source}")
    name = _SOURCE_NAMES.get(resolved, resolved)
    return _redirect_with_msg(
        f"已入队 {enqueued} 笔,跳过 {skipped} 笔(渠道:{name},trace {trace_id})"
    )


@router.post("/review/approve-all")
def console_approve_all(session: SessionDep) -> RedirectResponse:
    items = review.list_pending(session, limit=1000)
    approved: list[int] = []
    for item in items:
        try:
            review.approve(session, item.id)
            approved.append(item.id)
        except ValueError as exc:  # 单条异常跳过,不影响整批
            logger.warning("approve_all_skip", item_id=item.id, error=str(exc))
    session.commit()
    for item_id in approved:
        finalize_review.delay(item_id)
    return _redirect_with_msg(f"已批准 {len(approved)} 笔,正在写入 Firefly")


@router.post("/review/reclassify")
def console_reclassify() -> RedirectResponse:
    reclassify_pending.delay(rules_only=False)
    return _redirect_with_msg("已触发重新分类,任务在后台执行,稍后刷新查看结果")


@router.post("/review/{item_id}/approve")
def console_approve(item_id: int, session: SessionDep) -> RedirectResponse:
    try:
        item = review.approve(session, item_id)
        category = item.suggested_category
        session.commit()
    except ValueError as exc:
        session.rollback()
        return _redirect_with_msg(str(exc))
    finalize_review.delay(item_id)
    return _redirect_with_msg(f"已批准 #{item_id}(分类:{category}),正在写入 Firefly")


@router.post("/review/{item_id}/correct")
def console_correct(
    item_id: int, category: Annotated[str, Form()], session: SessionDep
) -> RedirectResponse:
    try:
        review.correct(session, item_id, category)
        session.commit()
    except ValueError as exc:
        session.rollback()
        return _redirect_with_msg(str(exc))
    finalize_review.delay(item_id)
    # 改正已回流规则库:借规则自动清掉同商户其余 pending(零 LLM 成本)
    reclassify_pending.delay(rules_only=True)
    return _redirect_with_msg(f"已改分类 #{item_id} -> {category},正在写入 Firefly")


@router.post("/review/{item_id}/reject")
def console_reject(item_id: int, session: SessionDep) -> RedirectResponse:
    try:
        review.reject(session, item_id)
        session.commit()
    except ValueError as exc:
        session.rollback()
        return _redirect_with_msg(str(exc))
    return _redirect_with_msg(f"已驳回 #{item_id},不会写入 Firefly")
