# -*- coding: utf-8 -*-
"""core.reconcile —— 三方对账：本地 OCR / 规则修复 / 大模型。

审计可用性的前提是「每个数字都能说清来源」。这里把三个来源的比较
结果固化成 5 种状态，导出时按状态着色并写进「OCR校验」页。

状态优先级（从高到低）：
    manual    人工改过（最终以此为准，最高优先）
    agree     三方一致
    precision 本地少读小数位，模型更完整（采信模型）
    local     本地与规则一致，模型不同（默认采信本地）
    model     本地低置信 / 无法解析，模型给出（采信模型）
    conflict  全不一致（必须人工）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .numbers import ParsedNumber, normalize

# 本地 OCR 置信度阈值：低于此值视为「不可信」，允许模型接管
LOW_SCORE = 0.90
# 数值比较容差（绝对）
TOL = 0.005


class Verdict(str, Enum):
    AGREE = "agree"
    LOCAL = "local"
    MODEL = "model"
    CONFLICT = "conflict"
    MANUAL = "manual"
    PRECISION = "precision"     # 本地少读了小数位（不是数值不同，是精度不同）


COLORS = {
    Verdict.AGREE: "C6EFCE",      # 绿
    Verdict.LOCAL: "FFEB9C",      # 黄
    Verdict.MODEL: "BDD7EE",      # 蓝
    Verdict.CONFLICT: "FFC7CE",   # 红
    Verdict.MANUAL: "E4DFEC",     # 紫
    Verdict.PRECISION: "FCE4D6",  # 橙
}


def _is_precision_only(a: float, b: float) -> bool:
    """判断两个数是不是「同一个数、只是小数位不同」。

    实测高频 OCR 失败模式：本地把 275,185.31 读成 275,185.3（丢最后一位）、
    把 274,685.31 读成 274,685（整段小数丢掉），但置信度仍高达 0.97+，
    按普通规则会被误判为「采信本地」。

    这里只认整数部分完全相等、且一方是另一方四舍五入到较少小数位的情形。
    """
    if abs(a - b) < TOL:
        return False
    ia, ib = round(a), round(b)
    if abs(a - ia) < TOL and abs(b - ib) < TOL:
        return False                      # 两个都是整数，不是精度问题
    if int(a) != int(b):
        return False                      # 整数部分不同 -> 真的不一样
    sa, sb = f"{abs(a):.6f}".rstrip("0"), f"{abs(b):.6f}".rstrip("0")
    da = len(sa.split(".")[1]) if "." in sa else 0
    db = len(sb.split(".")[1]) if "." in sb else 0
    if da == db:
        return False
    fewer, more, nd = (a, b, da) if da < db else (b, a, db)
    return abs(fewer - round(more, nd)) < TOL


@dataclass
class CellVerdict:
    raw: str                       # OCR 原文
    local: ParsedNumber            # 本地 OCR + 规则
    model: ParsedNumber | None     # 大模型给出的值（没调就是 None）
    ocr_score: float | None        # 本地 OCR 置信度
    final_value: float | None      # 最终采信值
    verdict: Verdict
    manual_value: float | None = None
    manual_text: str = ""
    note: str = ""
    row: int = -1                  # 在页面表格里的位置，界面回填时要定位
    col: int = -1

    @property
    def color(self) -> str:
        return COLORS[self.verdict]

    @property
    def needs_review(self) -> bool:
        return self.verdict in (Verdict.CONFLICT, Verdict.MODEL, Verdict.PRECISION)


def _same(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= TOL


def reconcile_cell(
    raw: str,
    model_raw: str | None = None,
    ocr_score: float | None = None,
    manual_value: float | None = None,
) -> CellVerdict:
    """比较三个来源，给出最终值和状态。"""
    local = normalize(raw)
    model = normalize(model_raw) if model_raw not in (None, "") else None

    # 人工改过就是最终答案
    if manual_value is not None:
        return CellVerdict(
            raw=raw, local=local, model=model, ocr_score=ocr_score,
            final_value=manual_value, verdict=Verdict.MANUAL,
            manual_value=manual_value, note="人工修改",
        )

    lv = local.value
    mv = model.value if model else None
    has_model = model is not None

    # 本地解析不出来
    if lv is None:
        if has_model and mv is not None:
            return CellVerdict(raw, local, model, ocr_score, mv, Verdict.MODEL,
                               note=f"本地无法解析（{local.rule or 'unparsable'}），采信模型")
        return CellVerdict(raw, local, model, ocr_score, None, Verdict.CONFLICT,
                           note="本地与模型都无法给出数值")

    # 本地能解析，但 OCR 置信度低
    low_conf = ocr_score is not None and ocr_score < LOW_SCORE

    if not has_model:
        v = Verdict.MODEL if low_conf else Verdict.AGREE
        note = f"OCR 置信度 {ocr_score:.2f} 偏低" if low_conf else ""
        return CellVerdict(raw, local, model, ocr_score, lv, v, note=note)

    # 三方都有
    if mv is None:
        return CellVerdict(raw, local, model, ocr_score, lv, Verdict.LOCAL,
                           note="模型未能给出数值，采信本地")

    if _same(lv, mv):
        return CellVerdict(raw, local, model, ocr_score, lv, Verdict.AGREE)

    # 只是小数位不同（本地少读了位）-> 采信模型更精确的那个，单独标色
    if _is_precision_only(lv, mv):
        return CellVerdict(raw, local, model, ocr_score, mv, Verdict.PRECISION,
                           note=f"本地读到 {lv:.2f}（少小数位），模型读到 {mv:.2f}，采信模型")

    if low_conf:
        return CellVerdict(raw, local, model, ocr_score, mv, Verdict.MODEL,
                           note=f"OCR 置信度 {ocr_score:.2f} 偏低，采信模型")

    return CellVerdict(raw, local, model, ocr_score, lv, Verdict.LOCAL,
                       note=f"本地 {lv} ≠ 模型 {mv}，默认采信本地")


@dataclass
class ReconcileReport:
    cells: list[CellVerdict] = field(default_factory=list)

    def add(self, cv: CellVerdict) -> None:
        self.cells.append(cv)

    def find(self, row: int, col: int) -> CellVerdict | None:
        """按表格位置找对账条目（界面人工回填时用）。"""
        for c in self.cells:
            if c.row == row and c.col == col:
                return c
        return None

    def mark_manual(self, row: int, col: int, value: float | None,
                    text: str = "") -> bool:
        """界面手工改了一格：同步进对账报告，让「需复核数」跟着减少。

        实测坑：界面只改了 PageResult.cells 上的 verdict，而 review_count
        读的是对账报告，于是改完红格、汇总里仍显示「需复核 1」，
        「本页无问题」按钮永远不会亮 —— 用户以为工具坏了。
        """
        cv = self.find(row, col)
        if cv is None:
            return False
        cv.manual_value = value
        cv.manual_text = text
        cv.verdict = Verdict.MANUAL
        cv.note = "人工修改"
        return True

    @property
    def counts(self) -> dict[str, int]:
        out = {v.value: 0 for v in Verdict}
        for c in self.cells:
            out[c.verdict.value] += 1
        return out

    @property
    def review_count(self) -> int:
        return sum(1 for c in self.cells if c.needs_review)

    def rows_for_sheet(self) -> list[list[str]]:
        """给「OCR校验」工作表用的行。

        数值用固定小数位格式化，不用 :g —— 实测 :g 会把 275185.31
        显示成 275185，追溯时就分不清到底读到了几位。
        """
        def fmt(v: float | None) -> str:
            if v is None:
                return ""
            if abs(v - round(v)) < TOL:
                return f"{v:.0f}"
            return f"{v:.2f}".rstrip("0").rstrip(".")

        rows = [["原文", "规则", "本地值", "模型值", "最终值", "状态", "置信度", "说明"]]
        for c in self.cells:
            rows.append([
                c.raw,
                c.local.rule,
                fmt(c.local.value),
                fmt(c.model.value) if c.model else "",
                fmt(c.final_value),
                c.verdict.value,
                "" if c.ocr_score is None else f"{c.ocr_score:.3f}",
                c.note,
            ])
        return rows
