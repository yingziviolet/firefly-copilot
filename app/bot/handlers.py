"""Telegram Bot(aiogram v3)处理器。

功能(P1):
1. /start:欢迎 + 用法说明;非白名单用户(settings.allowed_user_ids)一律拒绝
2. 文本记账:形如 "早餐 15" / "打车 23.5 昨天" 的消息 -> 解析为 CanonicalTransaction
   (source=TELEGRAM, direction=EXPENSE, counterparty=描述词)-> ingest_transaction.delay
   解析规则:最后一个数字为金额;无法解析时回复用法提示
3. /pending:列出待复核项,每项附 InlineKeyboard:
   [✅ 批准 approve:{id}] [✏️ 改分类 correct:{id}] [❌ 驳回 reject:{id}]
   - 批准 -> review.approve + finalize_review.delay
   - 改分类 -> 给出 DEFAULT_CATEGORIES 分类按钮(correctcat:{id}:{category})
     -> review.correct(回流规则库)+ finalize_review.delay
   - 驳回 -> review.reject
4. 所有 handler 用 db.session_scope() 管理事务

导出 router: aiogram.Router,供 runner 装配。
"""

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.config import get_settings
from app.db import session_scope
from app.logger import get_logger, new_trace_id
from app.models.review import ReviewItem
from app.schemas.classify import DEFAULT_CATEGORIES
from app.schemas.transaction import CanonicalTransaction, TxnDirection, TxnSource
from app.services import review
from app.worker.tasks_ingest import finalize_review, ingest_transaction

logger = get_logger(__name__)

router = Router()

USAGE_TEXT = (
    "记一笔账:直接发「描述 金额」,例如「早餐 15」「打车 23.5」「昨天 午饭 20」\n"
    "查看待复核:/pending"
)

# 金额:整数或小数
_AMOUNT_RE = re.compile(r"\d+(?:\.\d+)?")
# 相对日期词 -> 回溯天数
_DATE_WORDS: dict[str, int] = {"前天": 2, "昨天": 1}
# 金额附带的货币字样,解析后从描述中剔除
_CURRENCY_RE = re.compile(r"[¥￥]|块钱|[元块]")


# ---------------------------------------------------------------------------
# 可测纯逻辑
# ---------------------------------------------------------------------------


def parse_quick_expense(text: str) -> CanonicalTransaction | None:
    """解析快捷记账文本:最后一个数字为金额,其余为描述/商户;无金额返回 None。"""
    text = (text or "").strip()
    if not text:
        return None

    matches = list(_AMOUNT_RE.finditer(text))
    if not matches:
        return None
    last = matches[-1]
    try:
        amount = Decimal(last.group())
    except InvalidOperation:  # pragma: no cover - 正则已保证格式
        return None
    if amount <= 0:
        return None

    # 去掉金额后剩下的部分为描述
    remainder = text[: last.start()] + text[last.end() :]

    # 相对日期词:昨天/前天
    day_offset = 0
    for word, offset in _DATE_WORDS.items():
        if word in remainder:
            remainder = remainder.replace(word, " ")
            day_offset = offset
            break

    remainder = _CURRENCY_RE.sub(" ", remainder)
    description = " ".join(remainder.split())
    counterparty = description or "未知"

    return CanonicalTransaction(
        source=TxnSource.TELEGRAM,
        direction=TxnDirection.EXPENSE,
        occurred_at=datetime.now(UTC) - timedelta(days=day_offset),
        amount=amount,
        counterparty=counterparty,
        description=description,
        raw={"text": text},
    )


def is_allowed_user(user_id: int | None) -> bool:
    """白名单校验:settings.allowed_user_ids 之外一律拒绝。"""
    return user_id is not None and user_id in get_settings().allowed_user_ids


def build_cb(action: str, item_id: int, category: str | None = None) -> str:
    """构造回调数据:approve:{id} / correct:{id} / reject:{id} / correctcat:{id}:{cat}。"""
    if category is not None:
        return f"{action}:{item_id}:{category}"
    return f"{action}:{item_id}"


def parse_cb(data: str) -> tuple[str, int, str | None]:
    """解析回调数据 -> (action, item_id, category|None);格式非法抛 ValueError。"""
    parts = data.split(":", 2)
    if len(parts) < 2 or not parts[0]:
        raise ValueError(f"回调数据格式非法: {data!r}")
    try:
        item_id = int(parts[1])
    except ValueError as exc:
        raise ValueError(f"回调数据 item_id 非法: {data!r}") from exc
    category = parts[2] if len(parts) == 3 else None
    return parts[0], item_id, category


def build_pending_keyboard(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ 批准", callback_data=build_cb("approve", item_id)),
                InlineKeyboardButton(text="✏️ 改分类", callback_data=build_cb("correct", item_id)),
                InlineKeyboardButton(text="❌ 驳回", callback_data=build_cb("reject", item_id)),
            ]
        ]
    )


def build_category_keyboard(item_id: int) -> InlineKeyboardMarkup:
    """DEFAULT_CATEGORIES 按钮,每行 3 个。"""
    buttons = [
        InlineKeyboardButton(text=cat, callback_data=build_cb("correctcat", item_id, cat))
        for cat in DEFAULT_CATEGORIES
    ]
    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_pending_item(item: ReviewItem) -> str:
    payload = item.txn_payload or {}
    confidence = f"{item.confidence:.2f}" if item.confidence is not None else "-"
    return (
        f"#{item.id} {payload.get('counterparty', '?')} "
        f"{payload.get('amount', '?')} {payload.get('currency', '')}\n"
        f"建议分类: {item.suggested_category or '?'}(置信度 {confidence})"
    )


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------


async def _reject_if_not_allowed(message: Message) -> bool:
    """非白名单用户回复拒绝并返回 True。"""
    user_id = message.from_user.id if message.from_user else None
    if is_allowed_user(user_id):
        return False
    logger.warning("bot_user_rejected", user_id=user_id)
    await message.answer("你不在使用白名单内,拒绝服务。")
    return True


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if await _reject_if_not_allowed(message):
        return
    await message.answer(f"欢迎使用记账助手!\n{USAGE_TEXT}")


@router.message(Command("pending"))
async def cmd_pending(message: Message) -> None:
    if await _reject_if_not_allowed(message):
        return
    with session_scope() as session:
        items = review.list_pending(session)
        cards = [(item.id, format_pending_item(item)) for item in items]
    if not cards:
        await message.answer("当前没有待复核项。")
        return
    for item_id, text in cards:
        await message.answer(text, reply_markup=build_pending_keyboard(item_id))


@router.message(F.text)
async def handle_quick_expense(message: Message) -> None:
    if await _reject_if_not_allowed(message):
        return
    txn = parse_quick_expense(message.text or "")
    if txn is None:
        await message.answer(f"没看懂这条记录。\n{USAGE_TEXT}")
        return
    trace_id = new_trace_id()
    ingest_transaction.delay(txn.dump_for_queue(), trace_id)
    logger.info(
        "bot_quick_expense_enqueued",
        trace_id=trace_id,
        counterparty=txn.counterparty,
        amount=str(txn.amount),
    )
    await message.answer(
        f"已收到:{txn.counterparty} {txn.amount} {txn.currency},正在入账处理。"
    )


async def _cb_edit(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None):
    """编辑原消息(不可编辑时忽略)并应答回调。"""
    msg = callback.message
    if msg is not None and hasattr(msg, "edit_text"):
        await msg.edit_text(text, reply_markup=markup)
    await callback.answer()


async def _cb_guard(callback: CallbackQuery) -> bool:
    """回调白名单校验:非白名单弹提示并返回 True。"""
    user_id = callback.from_user.id if callback.from_user else None
    if is_allowed_user(user_id):
        return False
    await callback.answer("你不在使用白名单内。", show_alert=True)
    return True


@router.callback_query(F.data.startswith("approve:"))
async def cb_approve(callback: CallbackQuery) -> None:
    if await _cb_guard(callback):
        return
    _, item_id, _ = parse_cb(callback.data or "")
    try:
        with session_scope() as session:
            item = review.approve(session, item_id)
            category = item.suggested_category
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    finalize_review.delay(item_id)
    await _cb_edit(callback, f"✅ 已批准 #{item_id}(分类:{category}),正在写入 Firefly。")


@router.callback_query(F.data.startswith("correct:"))
async def cb_correct(callback: CallbackQuery) -> None:
    if await _cb_guard(callback):
        return
    _, item_id, _ = parse_cb(callback.data or "")
    await _cb_edit(
        callback,
        f"请为 #{item_id} 选择正确分类:",
        markup=build_category_keyboard(item_id),
    )


@router.callback_query(F.data.startswith("correctcat:"))
async def cb_correct_category(callback: CallbackQuery) -> None:
    if await _cb_guard(callback):
        return
    _, item_id, category = parse_cb(callback.data or "")
    if not category:
        await callback.answer("回调数据缺少分类。", show_alert=True)
        return
    try:
        with session_scope() as session:
            review.correct(session, item_id, category)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    finalize_review.delay(item_id)
    await _cb_edit(callback, f"✏️ 已改分类 #{item_id} -> {category},正在写入 Firefly。")


@router.callback_query(F.data.startswith("reject:"))
async def cb_reject(callback: CallbackQuery) -> None:
    if await _cb_guard(callback):
        return
    _, item_id, _ = parse_cb(callback.data or "")
    try:
        with session_scope() as session:
            review.reject(session, item_id)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await _cb_edit(callback, f"❌ 已驳回 #{item_id},不会写入 Firefly。")
