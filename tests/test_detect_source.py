"""detect_source 测试:csv/xlsx x alipay/wechat 四象限 + 各类识别失败。"""

import io

import pytest
from openpyxl import Workbook

from app.parsers.base import ParseError, detect_source

_ALIPAY_HEAD = (
    "支付宝交易流水明细------------------------------------\n"
    "账号:[steve@example.com]\n"
    "---------------------------交易记录明细列表----------------------------\n"
    "交易时间,交易分类,交易对方,对方账号,商品说明,收/支,金额,收/付款方式,"
    "交易状态,交易订单号,商家订单号,备注\n"
)

_WECHAT_HEAD = (
    "微信支付账单明细,,,,,,,,,,\n"
    "微信昵称:[老王],,,,,,,,,,\n"
    "----------------------微信支付账单明细列表--------------------,,,,,,,,,,\n"
    "交易时间,交易类型,交易对方,商品,收/支,金额(元),支付方式,当前状态,交易单号,商户单号,备注\n"
)


def _xlsx_bytes(rows: list[list]) -> bytes:
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------- 四象限 ----------


def test_detect_alipay_csv():
    assert detect_source(_ALIPAY_HEAD.encode("utf-8")) == "alipay"
    # gb18030 编码同样可识别
    assert detect_source(_ALIPAY_HEAD.encode("gb18030")) == "alipay"


def test_detect_wechat_csv():
    assert detect_source(_WECHAT_HEAD.encode("utf-8-sig")) == "wechat"
    assert detect_source(_WECHAT_HEAD.encode("gb18030")) == "wechat"


def test_detect_alipay_xlsx():
    raw = _xlsx_bytes([["支付宝交易流水明细"], ["账号:[steve@example.com]"]])
    assert detect_source(raw) == "alipay"


def test_detect_wechat_xlsx():
    raw = _xlsx_bytes([["微信支付账单明细"], ["微信昵称:[老王]"]])
    assert detect_source(raw) == "wechat"


# ---------- 识别失败 ----------


def test_detect_no_marker_raises():
    with pytest.raises(ParseError, match="无法自动识别账单渠道"):
        detect_source("交易时间,收/支,金额\n2026-07-01 12:00:00,支出,1.00\n".encode())


def test_detect_both_markers_raises():
    with pytest.raises(ParseError, match="无法自动识别账单渠道"):
        detect_source("微信支付 与 支付宝 都出现\n".encode())


def test_detect_undecodable_bytes_raises():
    with pytest.raises(ParseError, match="无法自动识别账单渠道"):
        detect_source(b"\xff\xfe\xff\xff\x81\xff")


def test_detect_corrupt_xlsx_raises():
    with pytest.raises(ParseError, match="无法自动识别账单渠道"):
        detect_source(b"PK\x03\x04garbage-not-a-real-xlsx")


def test_detect_marker_beyond_head_lines_not_seen():
    """特征出现在头部 30 行之外:不参与识别,视为识别失败。"""
    raw = ("普通行\n" * 40 + "支付宝\n").encode()
    with pytest.raises(ParseError, match="无法自动识别账单渠道"):
        detect_source(raw)
