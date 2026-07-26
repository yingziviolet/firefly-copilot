"""支付宝账单 CSV 解析器。

导出路径:支付宝 App -> 我的 -> 账单 -> ... -> 开具交易流水证明(csv)。
典型表头列:交易时间, 交易分类, 交易对方, 对方账号, 商品说明, 收/支, 金额, 收/付款方式,
交易状态, 交易订单号, 商家订单号, 备注
映射:
- source=ALIPAY, source_ref=交易订单号
- 收/支: 支出->EXPENSE, 收入->INCOME, 不计收支->跳过(计入 skipped)
- 交易状态含 "退款"/"关闭" 的行跳过
- category_hint=交易分类, account_hint=收/付款方式, counterparty=交易对方
"""

import csv
import io
from datetime import datetime

from pydantic import ValidationError

from app.logger import get_logger
from app.parsers.base import ParseError, ParseResult
from app.schemas.transaction import CanonicalTransaction, TxnDirection, TxnSource

logger = get_logger("parsers.alipay")

# 依次尝试的编码,全部失败视为整体乱码
_ENCODINGS = ("utf-8-sig", "gb18030")
# 表头行必须同时出现的关键列名,用于跳过 preamble 说明行
_HEADER_MARKERS = ("交易时间", "收/支", "金额")
# 收/支 -> 方向;不在映射内(如"不计收支")的行跳过
_DIRECTION_MAP = {"支出": TxnDirection.EXPENSE, "收入": TxnDirection.INCOME}
# 交易状态含以下关键字的行跳过
_SKIP_STATUS = ("退款", "关闭")
# 支付宝常见时间格式
_DT_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
)


def _decode(raw: bytes) -> str:
    """自动探测编码:utf-8-sig -> gb18030,都失败抛 ParseError。"""
    for enc in _ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ParseError("支付宝账单编码无法识别(非 UTF-8 / GB18030)")


def _parse_datetime(value: str) -> datetime:
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    # 兜底:ISO 格式;失败抛 ValueError,由调用方计入 skipped
    return datetime.fromisoformat(value)


def _cell(row: list[str], idx: int | None) -> str:
    """安全取列:列缺失或行过短返回空串。"""
    if idx is None or idx >= len(row):
        return ""
    return row[idx].strip()


def parse_alipay_csv(raw: bytes) -> ParseResult:
    text = _decode(raw)
    lines = text.splitlines()

    # 扫描定位真正的表头行,跳过前面的说明文字
    header_line = None
    for i, line in enumerate(lines):
        if all(marker in line for marker in _HEADER_MARKERS):
            header_line = i
            break
    if header_line is None:
        raise ParseError("未找到支付宝账单表头行")

    reader = csv.reader(io.StringIO("\n".join(lines[header_line:])))
    header = [c.strip() for c in next(reader)]
    col_idx = {name: i for i, name in enumerate(header) if name}

    def _resolve(name: str) -> int | None:
        # 精确匹配优先,其次前缀匹配以兼容 "金额(元)" 之类变体
        if name in col_idx:
            return col_idx[name]
        for h, j in col_idx.items():
            if h.startswith(name):
                return j
        return None

    needed = (
        "交易时间",
        "交易分类",
        "交易对方",
        "商品说明",
        "收/支",
        "金额",
        "收/付款方式",
        "交易状态",
        "交易订单号",
        "备注",
    )
    idx = {name: _resolve(name) for name in needed}

    txns: list[CanonicalTransaction] = []
    skipped = 0
    for row in reader:
        # 空行与 "----" 分隔/页脚装饰行直接忽略,不计入 skipped
        if not row or all(not c.strip() for c in row):
            continue
        if row[0].strip().startswith("---"):
            continue
        try:
            status = _cell(row, idx["交易状态"])
            if any(k in status for k in _SKIP_STATUS):
                skipped += 1
                continue
            direction = _DIRECTION_MAP.get(_cell(row, idx["收/支"]))
            if direction is None:
                # 不计收支 / 未知方向
                skipped += 1
                continue
            txn = CanonicalTransaction(
                source=TxnSource.ALIPAY,
                direction=direction,
                occurred_at=_parse_datetime(_cell(row, idx["交易时间"])),
                amount=_cell(row, idx["金额"]),  # ¥/逗号由 schema 校验器清洗
                counterparty=_cell(row, idx["交易对方"]),
                description=_cell(row, idx["商品说明"]) or _cell(row, idx["备注"]),
                source_ref=_cell(row, idx["交易订单号"]) or None,
                category_hint=_cell(row, idx["交易分类"]) or None,
                account_hint=_cell(row, idx["收/付款方式"]) or None,
                raw={h: _cell(row, j) for h, j in col_idx.items()},
            )
        except (ValidationError, ValueError) as exc:
            # 单行失败不中断整体
            skipped += 1
            logger.debug("alipay_row_skipped", error=str(exc))
            continue
        txns.append(txn)

    logger.info("alipay_csv_parsed", parsed=len(txns), skipped=skipped)
    return txns, skipped
