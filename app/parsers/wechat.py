"""微信支付账单解析器:同时支持 CSV 与 XLSX(新版「用于个人对账」导出)。

导出路径:微信 -> 我 -> 服务 -> 钱包 -> 账单 -> 常见问题 -> 下载账单。
格式识别:字节流以 PK\x03\x04(zip 魔数)开头视为 xlsx,否则按文本 csv 处理。
典型表头列:交易时间, 交易类型, 交易对方, 商品, 收/支, 金额(元), 支付方式,
当前状态, 交易单号, 商户单号, 备注
映射:
- source=WECHAT, source_ref=交易单号
- 收/支: 支出->EXPENSE, 收入->INCOME, "/"(中性,如零钱提现)->跳过
- 金额(元) 带 ¥ 前缀需去除
- category_hint=交易类型, account_hint=支付方式, counterparty=交易对方
"""

import csv
import io
from datetime import datetime
from typing import Any

import openpyxl
from pydantic import ValidationError

from app.logger import get_logger
from app.parsers.base import ParseError, ParseResult
from app.schemas.transaction import CanonicalTransaction, TxnDirection, TxnSource

logger = get_logger(__name__)

# xlsx(zip)文件魔数
_XLSX_MAGIC = b"PK\x03\x04"

# 依次尝试的编码:UTF-8(兼容 BOM)优先,失败再按国标 GB18030(GBK 超集)
_ENCODINGS = ("utf-8-sig", "gb18030")

# 收/支 -> 方向;"/" 等中性取值不在映射内,按跳过处理
_DIRECTION_MAP = {
    "支出": TxnDirection.EXPENSE,
    "收入": TxnDirection.INCOME,
}

# 表头行特征列:同时出现才认定为真正表头(preamble 说明行不会同时含这两个)
_HEADER_MARKERS = ("交易时间", "收/支")


def _decode(raw: bytes) -> str:
    for encoding in _ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ParseError("无法识别账单编码(已尝试 utf-8-sig / gb18030)")


def _locate_header(rows: list[list[str]]) -> int:
    for idx, row in enumerate(rows):
        line = ",".join(row)
        if all(marker in line for marker in _HEADER_MARKERS):
            return idx
    raise ParseError("未找到微信账单表头行")


def _find_col(header: list[str], name: str, *, prefix: bool = False) -> int | None:
    """按列名(或前缀,兼容"金额(元)"全/半角括号差异)定位列下标。"""
    for idx, cell in enumerate(header):
        if cell == name or (prefix and cell.startswith(name)):
            return idx
    return None


def _parse_time(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return datetime.fromisoformat(value)


def _cell_to_str(value: Any) -> str:
    """xlsx 单元格值统一转 str:None->空串,datetime->标准格式,数字->str。"""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def _rows_from_csv(raw: bytes) -> list[list[str]]:
    text = _decode(raw)
    return list(csv.reader(io.StringIO(text)))


def _rows_from_xlsx(raw: bytes) -> list[list[str]]:
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        try:
            sheet = workbook.worksheets[0]
            return [
                [_cell_to_str(value) for value in row]
                for row in sheet.iter_rows(values_only=True)
            ]
        finally:
            workbook.close()
    except Exception as exc:
        raise ParseError(f"无法解析微信 xlsx 账单:{exc}") from exc


def _convert_rows(rows: list[list[str]]) -> ParseResult:
    """csv/xlsx 共享逻辑:定位表头 -> 列映射 -> 逐行转 CanonicalTransaction。"""
    start = _locate_header(rows)
    header = [cell.strip() for cell in rows[start]]

    cols = {
        "time": _find_col(header, "交易时间"),
        "category": _find_col(header, "交易类型"),
        "counterparty": _find_col(header, "交易对方"),
        "goods": _find_col(header, "商品"),
        "direction": _find_col(header, "收/支"),
        "amount": _find_col(header, "金额", prefix=True),
        "account": _find_col(header, "支付方式"),
        "ref": _find_col(header, "交易单号"),
    }
    if cols["time"] is None or cols["direction"] is None or cols["amount"] is None:
        raise ParseError("微信账单表头缺少必需列(交易时间/收/支/金额)")

    def cell(row: list[str], key: str) -> str:
        idx = cols[key]
        if idx is None or idx >= len(row):
            return ""
        return row[idx].strip()

    results: list[CanonicalTransaction] = []
    skipped = 0

    for row in rows[start + 1 :]:
        # 纯空行(尾部空行等)不计入 skipped
        if not any(c.strip() for c in row):
            continue

        direction = _DIRECTION_MAP.get(cell(row, "direction"))
        if direction is None:
            # "/" 中性流水(如零钱提现)或未知取值:跳过
            skipped += 1
            continue

        try:
            txn = CanonicalTransaction(
                source=TxnSource.WECHAT,
                direction=direction,
                occurred_at=_parse_time(cell(row, "time")),
                # 金额字符串的 ¥ 前缀/千分位由模型 validator 统一清洗
                amount=cell(row, "amount"),
                counterparty=cell(row, "counterparty"),
                description=cell(row, "goods"),
                source_ref=cell(row, "ref") or None,
                category_hint=cell(row, "category") or None,
                account_hint=cell(row, "account") or None,
                raw=dict(zip(header, row, strict=False)),
            )
        except (ValidationError, ValueError) as exc:
            skipped += 1
            logger.warning("wechat_row_skipped", error=str(exc))
            continue
        results.append(txn)

    return results, skipped


def parse_wechat_csv(raw: bytes) -> ParseResult:
    """解析微信账单字节流,自动识别 csv 或 xlsx 格式(zip 魔数判定)。"""
    if raw[:4] == _XLSX_MAGIC:
        rows = _rows_from_xlsx(raw)
    else:
        rows = _rows_from_csv(raw)
    return _convert_rows(rows)
