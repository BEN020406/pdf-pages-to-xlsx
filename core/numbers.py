# -*- coding: utf-8 -*-
"""core.numbers —— 会计数值规范化（纯函数，无 IO）。

设计原则：
1. 每条规则独立可测，命中后把规则名写进 Cell.rule，导出时可追溯；
2. 不确定就不转：解析不出来返回 None，交给人工；
3. 绝不"猜"数字——只做格式层面的确定性还原。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# 各种破折号/连字符/斜线，会计上表示"无此项"
DASHES = {"-", "—", "–", "－", "−", "／", "/", "——", "--"}
# 全角数字/符号
FULLWIDTH_MAP = {ord("０") + i: ord("0") + i for i in range(10)}
FULLWIDTH_MAP.update({ord("．"): ord("."), ord("，"): ord(","), ord("（"): ord("("), ord("）"): ord(")")})


@dataclass
class ParsedNumber:
    value: float | None      # 解析出的数值；None 表示不是数字
    rule: str                # 命中的规则名，空串表示无需转换
    text: str                # 规范化后的文本形式（用于显示/回写）


def _strip_ocr_spaces(s: str) -> tuple[str, bool]:
    """修复 OCR 在数字内部插入的空格。

    实测样本：'2 7 5, 1 8 5. 3 1' -> '275,185.31'
              '1 1,0 00. 00'      -> '11,000.00'
              '(1 2, 345. 67)'    -> '(12,345.67)'
    """
    if not re.search(r"\d", s):
        return s, False
    out = s
    # 数字之间的空格
    out = re.sub(r"(?<=\d)\s+(?=\d)", "", out)
    # 数字与千分位/小数点之间
    out = re.sub(r"(?<=\d)\s+(?=[.,])", "", out)
    out = re.sub(r"(?<=[.,])\s+(?=\d)", "", out)
    # 括号内侧
    out = re.sub(r"(?<=\()\s+|\s+(?=\))", "", out)
    return out, out != s


def normalize(raw: str) -> ParsedNumber:
    """把 OCR/文本层拿到的一段文字规范成会计数值。

    返回 ParsedNumber(value=None) 表示"这不是一个数字"。
    """
    if raw is None:
        return ParsedNumber(None, "", "")
    s = str(raw).strip()
    if not s:
        return ParsedNumber(None, "", "")

    rules: list[str] = []

    # 0. 全角转半角
    s2 = s.translate(FULLWIDTH_MAP)
    if s2 != s:
        rules.append("fullwidth")
        s = s2

    # 0b. 去掉货币符号/币种前缀（$ / HK$ / MOP / RMB …）
    #     只有后面确实还有数字才剥 ——「$ ---」这种空值行要保持原样，
    #     不能因为剥了 $ 就把它当成 0（那是猜）。
    m = re.match(r"^(?:US\$|HK\$|MOP|RMB|CNY|\$|¥|€|£)\s*", s, re.I)
    if m and re.search(r"\d", s[m.end():]):
        rules.append("strip_currency")
        s = s[m.end():].strip()

    # 1. 纯破折号 = 无此项（会计惯例记 0，但保留规则名）
    if s in DASHES:
        return ParsedNumber(0.0, "dash_zero", s)

    # 2. 丢掉的小数点先补回来：'274.685 31' → '274.685.31'
    #    必须放在「去 OCR 空格」**之前**，否则空格已被抹平、无从判断。
    #    形状判据（严格，避免猜）：
    #       前面是 1~3 位 + 若干 .3位组，空格后面恰好 1~2 位数字。
    #    实测这条能救回手写件里被模型读成 274,655.31 的那个数。
    if " " in s and re.fullmatch(r"\d{1,3}(\.\d{3})+\s+\d{1,2}", s):
        s = re.sub(r"\s+", ".", s)
        rules.append("dot_restore")

    # 3. 去掉数字内部被 OCR 插的空格
    s, changed = _strip_ocr_spaces(s)
    if changed:
        rules.append("digit_space")

    # 3b. OCR 把千分位逗号读成了点：'1.234.567.89' / '800.000.00'
    #     只在模式完全吻合时才还原 —— 1~3 位整数 + 若干 3 位组 + 最后 1~2 位小数，
    #     这个形状没有歧义，不是猜。
    if s.count(".") >= 2 and re.fullmatch(r"\d{1,3}(\.\d{3})+\.\d{1,2}", s):
        head, _, tail = s.rpartition(".")
        s = head.replace(".", ",") + "." + tail
        rules.append("dot_thousand")

    # 3. 去单位后缀（元/万元/万）
    wan = False
    m = re.search(r"(万元|万|元)$", s)
    if m:
        unit = m.group(1)
        s = s[: m.start()].strip()
        if unit in ("万元", "万"):
            wan = True
            rules.append("wan_scale")
        else:
            rules.append("strip_unit")

    # 4. 百分号
    percent = False
    if s.endswith("%"):
        percent = True
        s = s[:-1].strip()
        rules.append("percent")

    # 5. 括号负数
    negative = False
    if (s.startswith("(") and s.endswith(")")) or (s.startswith("（") and s.endswith("）")):
        negative = True
        s = s[1:-1].strip()
        rules.append("paren_negative")

    # 6. 前导负号
    if s[:1] in ("-", "－", "−"):
        negative = True
        s = s[1:].strip()
        if "paren_negative" not in rules:
            rules.append("minus_normalize")

    # 7. 千分位校验 —— 分组不合法就拒绝，绝不猜
    #    实测危险样本：'1,100,000,00'（末组 2 位，很可能是小数点被 OCR 认成逗号）
    #    猜错会得到一个差 100 倍的数，审计里这是灾难。宁可不转，交给人工。
    if "," in s:
        int_part = s.split(".")[0]
        groups = int_part.split(",")
        if len(groups) > 1 and (len(groups[0]) > 3 or any(len(g) != 3 for g in groups[1:])):
            return ParsedNumber(None, "bad_grouping", raw)
        rules.append("thousand_sep")
    s_clean = s.replace(",", "")

    # 8. 严格校验：只允许数字和一个小数点
    if not re.fullmatch(r"\d+(\.\d+)?|\.\d+", s_clean):
        return ParsedNumber(None, "unparsable", raw)

    # 9. 纯 0 串（'000000' 这种 OCR 噪声）不是合法金额
    if len(s_clean) >= 3 and set(s_clean) == {"0"}:
        return ParsedNumber(None, "all_zero_noise", raw)

    # 10. 多个小数点已被第 8 步拒绝；这里再查小数位数异常
    if "." in s_clean and len(s_clean.split(".")[1]) > 4:
        return ParsedNumber(None, "too_many_decimals", raw)

    value = float(s_clean)
    if wan:
        value *= 10000.0
    if negative:
        value = -value
    if percent:
        value = value / 100.0

    display = f"{value:.10g}"
    return ParsedNumber(value, "|".join(rules), display)


def is_numeric_like(raw: str) -> bool:
    """粗判：这段文字看起来是不是想表达一个数字。

    用于决定哪些单元格值得送去 OCR 修复 / LLM 仲裁。
    """
    if raw is None:
        return False
    s = str(raw).strip()
    if not s or s in DASHES:
        return False
    s = s.translate(FULLWIDTH_MAP)
    s = re.sub(r"\s", "", s)
    if not re.search(r"\d", s):
        return False
    # 允许的字符集
    return all(c in "0123456789.,()%-—–−／/万元" for c in s)


def format_amount(value: float | None, decimals: int = 2, thousands: bool = True) -> str:
    """导出到 Excel 时用；None 返回空串（保持空白，不写 0）。"""
    if value is None:
        return ""
    if thousands:
        return f"{value:,.{decimals}f}"
    return f"{value:.{decimals}f}"
