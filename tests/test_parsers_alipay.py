"""支付宝 CSV 解析器测试:编码探测、preamble、方向映射、脏金额、跳过计数、乱码。"""

from datetime import datetime
from decimal import Decimal

import pytest

from app.parsers.alipay import parse_alipay_csv
from app.parsers.base import ParseError
from app.schemas.transaction import TxnDirection, TxnSource

HEADER = (
    "交易时间,交易分类,交易对方,对方账号,商品说明,收/支,金额,收/付款方式,"
    "交易状态,交易订单号,商家订单号,备注"
)

PREAMBLE = (
    "支付宝交易流水明细------------------------------------\n"
    "账号:[steve@example.com]\n"
    "起始时间:[2026-07-01 00:00:00]    终止时间:[2026-07-25 23:59:59]\n"
    "---------------------------交易记录明细列表----------------------------\n"
)

# 正常支出(¥ 前缀)/ 正常收入(千分位逗号)/ 不计收支 / 退款 / 脏金额
ROWS = (
    "2026-07-01 12:30:00,餐饮美食,肯德基(北京店),kfc@service.alipay.com,汉堡套餐,"
    "支出,¥32.50,余额宝,交易成功,2026070122001100001,M20260701001,\n"
    '2026-07-02 09:00:00,转账红包,张三,zs***@163.com,转账,收入,"1,200.00",余额,'
    "交易成功,2026070222001100002,,朋友还款\n"
    "2026-07-03 10:00:00,投资理财,余额宝,,余额宝-单次转入,不计收支,100.00,余额,"
    "交易成功,2026070322001100003,,\n"
    "2026-07-04 11:00:00,服饰装扮,某网店,,纯棉T恤,支出,59.00,花呗,退款成功,"
    "2026070422001100004,,\n"
    "2026-07-05 12:00:00,餐饮美食,坏数据商户,,金额损坏行,支出,abc,余额,交易成功,"
    "2026070522001100005,,\n"
)

CSV_TEXT = PREAMBLE + HEADER + "\n" + ROWS


def _utf8_sample() -> bytes:
    # 带 BOM 的 UTF-8 变体
    return CSV_TEXT.encode("utf-8-sig")


def _gbk_sample() -> bytes:
    # GBK/GB18030 变体,并使用全角 ￥ 更贴近真实导出
    return CSV_TEXT.replace("¥", "￥").encode("gb18030")


def test_parse_utf8_bom_sample():
    txns, skipped = parse_alipay_csv(_utf8_sample())
    assert len(txns) == 2
    # 不计收支 + 退款 + 脏金额 = 3
    assert skipped == 3

    expense = txns[0]
    assert expense.source == TxnSource.ALIPAY
    assert expense.direction == TxnDirection.EXPENSE
    assert expense.occurred_at == datetime(2026, 7, 1, 12, 30, 0)
    assert expense.amount == Decimal("32.50")
    assert expense.currency == "CNY"
    assert expense.counterparty == "肯德基(北京店)"
    assert expense.description == "汉堡套餐"

    income = txns[1]
    assert income.direction == TxnDirection.INCOME
    assert income.amount == Decimal("1200.00")
    assert income.counterparty == "张三"


def test_parse_gb18030_sample_matches_utf8():
    utf8_txns, utf8_skipped = parse_alipay_csv(_utf8_sample())
    gbk_txns, gbk_skipped = parse_alipay_csv(_gbk_sample())
    assert gbk_skipped == utf8_skipped == 3
    assert len(gbk_txns) == len(utf8_txns) == 2
    assert [t.amount for t in gbk_txns] == [t.amount for t in utf8_txns]
    assert [t.counterparty for t in gbk_txns] == [t.counterparty for t in utf8_txns]
    assert [t.source_ref for t in gbk_txns] == [t.source_ref for t in utf8_txns]


def test_direction_maps_to_firefly_types():
    txns, _ = parse_alipay_csv(_utf8_sample())
    assert txns[0].direction.value == "withdrawal"
    assert txns[1].direction.value == "deposit"


def test_hint_fields_extracted():
    txns, _ = parse_alipay_csv(_utf8_sample())
    expense, income = txns
    assert expense.source_ref == "2026070122001100001"
    assert expense.category_hint == "餐饮美食"
    assert expense.account_hint == "余额宝"
    assert income.source_ref == "2026070222001100002"
    assert income.category_hint == "转账红包"
    assert income.account_hint == "余额"
    # raw 保留原始行,审计用
    assert expense.raw is not None
    assert expense.raw["交易订单号"] == "2026070122001100001"


@pytest.mark.parametrize(
    "row, reason",
    [
        (
            "2026-07-03 10:00:00,投资理财,余额宝,,转入,不计收支,100.00,余额,"
            "交易成功,2026070322001100003,,",
            "不计收支",
        ),
        (
            "2026-07-04 11:00:00,服饰装扮,某网店,,T恤,支出,59.00,花呗,退款成功,"
            "2026070422001100004,,",
            "退款",
        ),
        (
            "2026-07-06 08:00:00,数码电器,某商家,,耳机,支出,199.00,余额,交易关闭,"
            "2026070622001100006,,",
            "交易关闭",
        ),
        (
            "2026-07-05 12:00:00,餐饮美食,坏商户,,坏行,支出,abc,余额,交易成功,"
            "2026070522001100005,,",
            "脏金额",
        ),
        (
            "不是时间,餐饮美食,商户,,说明,支出,10.00,余额,交易成功,"
            "2026070722001100007,,",
            "坏时间",
        ),
    ],
)
def test_single_bad_row_counted_as_skipped(row: str, reason: str):
    raw = (PREAMBLE + HEADER + "\n" + row + "\n").encode("utf-8")
    txns, skipped = parse_alipay_csv(raw)
    assert txns == [], reason
    assert skipped == 1, reason


def test_blank_and_separator_lines_not_counted():
    raw = (
        PREAMBLE
        + HEADER
        + "\n"
        + "2026-07-01 12:30:00,餐饮美食,肯德基,,套餐,支出,32.50,余额宝,交易成功,"
        "2026070122001100001,,\n"
        + "\n"
        + "------------------------------------------------\n"
    ).encode("utf-8")
    txns, skipped = parse_alipay_csv(raw)
    assert len(txns) == 1
    assert skipped == 0


def test_garbage_bytes_raise_parse_error():
    # 两种编码都无法解码的字节流
    with pytest.raises(ParseError):
        parse_alipay_csv(b"\xff\xfe\xff\xff\xff\x00\xff")


def test_decodable_but_no_header_raises_parse_error():
    with pytest.raises(ParseError):
        parse_alipay_csv("这不是账单文件\n只有说明文字\n".encode("gb18030"))
