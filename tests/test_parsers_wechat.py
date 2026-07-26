"""微信账单 CSV 解析器测试:编码探测、方向映射、金额清洗、中性行/坏行跳过、整体乱码。"""

from datetime import datetime
from decimal import Decimal

import pytest

from app.parsers.base import ParseError
from app.parsers.wechat import parse_wechat_csv
from app.schemas.transaction import TxnDirection, TxnSource

# 真实感微信账单:表头前有多行 preamble 说明文字
_PREAMBLE = (
    "微信支付账单明细,,,,,,,,,,\n"
    "微信昵称:[老王],,,,,,,,,,\n"
    "起始时间:[2026-06-01 00:00:00] 终止时间:[2026-06-30 23:59:59],,,,,,,,,,\n"
    "导出类型:[全部],,,,,,,,,,\n"
    "导出时间:[2026-07-01 10:00:00],,,,,,,,,,\n"
    ",,,,,,,,,,\n"
    "共3笔记录,,,,,,,,,,\n"
    "收入:1笔 100.00元,,,,,,,,,,\n"
    "支出:1笔 23.50元,,,,,,,,,,\n"
    "----------------------微信支付账单明细列表--------------------,,,,,,,,,,\n"
)

_HEADER = (
    "交易时间,交易类型,交易对方,商品,收/支,金额(元),支付方式,当前状态,"
    "交易单号,商户单号,备注\n"
)

_ROWS = (
    '2026-06-05 12:30:11,商户消费,肯德基(北京店),"汉堡套餐",支出,"¥23.50",零钱,支付成功,'
    "4200001234202606051234567890,1000010001,/\n"
    "2026-06-08 09:00:00,转账,张三,/,收入,¥100.00,/,已存入零钱,"
    "1000039901202606080987654321,/,备注A\n"
    # 收/支 为 "/" 的中性行(零钱提现),应跳过并计入 skipped
    "2026-06-10 20:15:00,零钱提现,工商银行,/,/,¥50.00,零钱,提现已到账,"
    "1000050001202606100000000001,/,/\n"
)

_BILL_TEXT = _PREAMBLE + _HEADER + _ROWS


def test_parse_utf8_sig_bytes():
    """UTF-8 带 BOM 编码可解析,BOM 不污染首列。"""
    txns, skipped = parse_wechat_csv(_BILL_TEXT.encode("utf-8-sig"))
    assert len(txns) == 2
    assert skipped == 1  # 中性 "/" 行


def test_parse_gb18030_bytes():
    """GB18030(GBK 超集)编码可解析,中文字段不乱码。"""
    txns, skipped = parse_wechat_csv(_BILL_TEXT.encode("gb18030"))
    assert len(txns) == 2
    assert skipped == 1
    assert txns[0].counterparty == "肯德基(北京店)"
    assert txns[1].counterparty == "张三"


def test_direction_mapping_and_fields():
    """支出/收入映射到 Firefly 方向值;各列正确落位。"""
    txns, _ = parse_wechat_csv(_BILL_TEXT.encode("utf-8"))

    expense = txns[0]
    assert expense.source is TxnSource.WECHAT
    assert expense.direction is TxnDirection.EXPENSE
    assert expense.direction.value == "withdrawal"
    assert expense.occurred_at == datetime(2026, 6, 5, 12, 30, 11)
    assert expense.category_hint == "商户消费"
    assert expense.account_hint == "零钱"
    assert expense.description == "汉堡套餐"
    assert expense.currency == "CNY"

    income = txns[1]
    assert income.direction is TxnDirection.INCOME
    assert income.direction.value == "deposit"
    assert income.amount == Decimal("100.00")


def test_source_ref_is_transaction_id():
    """source_ref 取"交易单号"列,而不是商户单号。"""
    txns, _ = parse_wechat_csv(_BILL_TEXT.encode("utf-8"))
    assert txns[0].source_ref == "4200001234202606051234567890"
    assert txns[1].source_ref == "1000039901202606080987654321"


def test_amount_yen_prefix_and_thousand_sep_cleaned():
    """¥ 前缀与千分位逗号被清洗,金额恒为正 Decimal。"""
    bill = (
        _PREAMBLE
        + _HEADER
        + '2026-06-12 08:00:00,商户消费,苹果专卖店,iPhone,支出,"¥1,234.56",招商银行信用卡,'
        "支付成功,4200009999202606120000000001,/,/\n"
    )
    txns, skipped = parse_wechat_csv(bill.encode("utf-8"))
    assert skipped == 0
    assert txns[0].amount == Decimal("1234.56")
    assert txns[0].amount > 0


def test_bad_row_counted_as_skipped_without_aborting():
    """单行解析失败(金额/时间非法)计入 skipped,不中断整体。"""
    bill = (
        _PREAMBLE
        + _HEADER
        + "2026-06-05 12:30:11,商户消费,肯德基,/,支出,¥23.50,零钱,支付成功,4200001,/,/\n"
        # 金额非法
        "2026-06-06 10:00:00,商户消费,坏行商户,/,支出,¥abc,零钱,支付成功,4200002,/,/\n"
        # 时间非法
        "不是时间,商户消费,另一坏行,/,收入,¥1.00,零钱,支付成功,4200003,/,/\n"
        "2026-06-07 11:00:00,转账,李四,/,收入,¥8.00,/,已存入零钱,4200004,/,/\n"
    )
    txns, skipped = parse_wechat_csv(bill.encode("utf-8"))
    assert len(txns) == 2
    assert skipped == 2
    assert [t.source_ref for t in txns] == ["4200001", "4200004"]


def test_garbage_bytes_raise_parse_error():
    """utf-8-sig 与 gb18030 都无法解码的字节流:整体抛 ParseError。"""
    with pytest.raises(ParseError):
        parse_wechat_csv(b"\xff\xff\xfe\xff\xff\x81\xff\xff")


def test_decodable_but_no_header_raises_parse_error():
    """能解码但找不到微信表头的内容同样视为不可解析。"""
    with pytest.raises(ParseError):
        parse_wechat_csv("这只是一段普通文本\n没有账单表头\n".encode())
