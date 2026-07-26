"""CSV 解析器公共约定与共享适配器。

支付宝/微信导出的 CSV 特点(实现必须处理):
- 编码通常为 GBK/GB18030,也可能是 UTF-8(带或不带 BOM)-> 自动探测:
  依次尝试 utf-8-sig、gb18030,失败抛 ParseError
- 表头前有若干行说明文字(preamble),需要扫描定位真正的表头行
- 金额可能带 ¥ 前缀或逗号分隔符
- "不计收支"/"其他"类的中性流水按 direction 规则处理或跳过

解析函数签名统一:输入原始 bytes,输出 (成功列表, 跳过行数)。
解析失败的单行不中断整体,计入 skipped。

共享适配器:csv/xlsx 字节流统一转成 行列表(list[list[str]]),
各解析器只做表头定位与列映射,与文件格式解耦。
"""

import csv
import io
from datetime import datetime
from typing import Any

import openpyxl

from app.schemas.transaction import CanonicalTransaction


class ParseError(ValueError):
    pass


ParseResult = tuple[list[CanonicalTransaction], int]

# xlsx(zip)文件魔数
XLSX_MAGIC = b"PK\x03\x04"

# 依次尝试的编码:UTF-8(兼容 BOM)优先,失败再按国标 GB18030(GBK 超集)
_ENCODINGS = ("utf-8-sig", "gb18030")

# detect_source 只看文件头部这么多行
_DETECT_LINES = 30

_DETECT_FAIL_MSG = "无法自动识别账单渠道,请手动选择"


def is_xlsx(raw: bytes) -> bool:
    """按 zip 魔数判断是否为 xlsx 字节流。"""
    return raw[:4] == XLSX_MAGIC


def decode_text(raw: bytes, *, error: str) -> str:
    """自动探测编码解码文本;全部失败抛 ParseError(error)。"""
    for encoding in _ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ParseError(error)


def decode_text_rows(raw: bytes, *, decode_error: str) -> list[list[str]]:
    """csv 字节流 -> 行列表;解码失败抛 ParseError(decode_error)。"""
    text = decode_text(raw, error=decode_error)
    return list(csv.reader(io.StringIO(text)))


def _cell_to_str(value: Any) -> str:
    """xlsx 单元格值统一转 str:None->空串,datetime->标准格式,数字->str。"""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def rows_from_xlsx(raw: bytes, *, label: str) -> list[list[str]]:
    """xlsx 字节流 -> 行列表(首个工作表);label 用于错误提示,如"微信"。"""
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
        raise ParseError(f"无法解析{label} xlsx 账单:{exc}") from exc


def detect_source(raw: bytes) -> str:
    """按文件头部文本自动识别账单渠道,返回 "alipay" / "wechat"。

    识别不了(读不出头部 / 特征都命中 / 都不命中)抛 ParseError。
    """
    try:
        if is_xlsx(raw):
            rows = rows_from_xlsx(raw, label="")[:_DETECT_LINES]
            head = "\n".join(",".join(row) for row in rows)
        else:
            text = decode_text(raw, error=_DETECT_FAIL_MSG)
            head = "\n".join(text.splitlines()[:_DETECT_LINES])
    except ParseError:
        raise ParseError(_DETECT_FAIL_MSG) from None

    is_wechat = "微信支付" in head or "微信昵称" in head
    is_alipay = "支付宝" in head
    if is_wechat == is_alipay:
        raise ParseError(_DETECT_FAIL_MSG)
    return "wechat" if is_wechat else "alipay"
