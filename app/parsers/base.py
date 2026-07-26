"""CSV 解析器公共约定。

支付宝/微信导出的 CSV 特点(实现必须处理):
- 编码通常为 GBK/GB18030,也可能是 UTF-8(带或不带 BOM)-> 自动探测:
  依次尝试 utf-8-sig、gb18030,失败抛 ParseError
- 表头前有若干行说明文字(preamble),需要扫描定位真正的表头行
- 金额可能带 ¥ 前缀或逗号分隔符
- "不计收支"/"其他"类的中性流水按 direction 规则处理或跳过

解析函数签名统一:输入原始 bytes,输出 (成功列表, 跳过行数)。
解析失败的单行不中断整体,计入 skipped。
"""

from app.schemas.transaction import CanonicalTransaction


class ParseError(ValueError):
    pass


ParseResult = tuple[list[CanonicalTransaction], int]
