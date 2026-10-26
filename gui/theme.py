# -*- coding: utf-8 -*-
"""gui.theme —— 设计令牌与样式。

设计定位：审计现场的生产力工具，不是消费级应用。
取舍原则：
    1. 背景冷灰、卡片纯白 —— 财务数据要冷静克制，暖色会显得在修饰什么
    2. 主色深墨蓝，只给主操作 —— Fluent 默认青色太轻浮
    3. 彩色只用于表达数据可信度（状态色），其余全部灰度
    4. 数字一律等宽 + 右对齐 + 千分位 —— 核对时不会看错位
    5. 不用渐变、阴影堆叠、装饰插图
"""
from __future__ import annotations

from PyQt5.QtGui import QColor, QFont, QFontDatabase


class T:
    """设计令牌。T 取自 Token。"""

    # 背景与层级
    BG = "#F7F8FA"            # 页面底色，冷灰
    SURFACE = "#FFFFFF"       # 卡片
    SURFACE_ALT = "#FAFBFC"   # 次级区块
    BORDER = "#E3E6EB"        # 分隔线
    BORDER_STRONG = "#CDD3DB"

    # 文字
    TEXT = "#1B1F24"          # 主文本
    TEXT_SUB = "#5A6472"      # 次要说明
    TEXT_MUTE = "#8A94A3"     # 提示、占位
    TEXT_INVERT = "#FFFFFF"

    # 主色（深墨蓝）
    PRIMARY = "#1A3A5C"
    PRIMARY_HOVER = "#24496F"
    PRIMARY_PRESS = "#122B45"
    PRIMARY_SOFT = "#E8EEF5"  # 主色浅底

    # 状态色（唯一允许彩色的地方）
    OK = "#0F7B4F"
    OK_BG = "#E6F4EC"
    WARN = "#B45309"
    WARN_BG = "#FDF3E3"
    INFO = "#1D5FA8"
    INFO_BG = "#E8F0FA"
    DANGER = "#B42318"
    DANGER_BG = "#FDECEA"
    NEUTRAL = "#6B7280"
    NEUTRAL_BG = "#F0F2F5"

    # 间距节奏（4 的倍数）
    SP_XS = 4
    SP_S = 8
    SP_M = 12
    SP_L = 16
    SP_XL = 24
    SP_XXL = 32

    # 圆角
    R_S = 4
    R_M = 8
    R_L = 12

    # 字号
    FS_TITLE = 20
    FS_SECTION = 14
    FS_BODY = 13
    FS_SMALL = 12
    FS_TINY = 11


def pick_font(*candidates: str) -> str:
    """按优先级挑一个本机存在的字体。"""
    fams = set(QFontDatabase().families())
    for c in candidates:
        if c in fams:
            return c
    return ""


def ui_font(size: int = T.FS_BODY, bold: bool = False) -> QFont:
    fam = pick_font("Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans SC",
                    "Source Han Sans SC", "SimHei")
    f = QFont(fam) if fam else QFont()
    f.setPixelSize(size)
    f.setBold(bold)
    return f


def mono_font(size: int = T.FS_BODY, bold: bool = False) -> QFont:
    """数字用等宽 —— 金额对齐是会计的视觉习惯。"""
    fam = pick_font("Consolas", "Cascadia Mono", "JetBrains Mono",
                    "DejaVu Sans Mono", "Courier New")
    f = QFont(fam) if fam else QFont()
    f.setPixelSize(size)
    f.setBold(bold)
    f.setStyleHint(QFont.Monospace)
    return f


def color(hex_str: str, alpha: int = 255) -> QColor:
    c = QColor(hex_str)
    c.setAlpha(alpha)
    return c


# ---------------------------------------------------------------- QSS

PAGE_QSS = f"""
QWidget#pageBody {{
    background: {T.BG};
}}
QLabel#pageTitle {{
    color: {T.TEXT};
    font-size: {T.FS_TITLE}px;
    font-weight: 600;
}}
QLabel#pageLead {{
    color: {T.TEXT_SUB};
    font-size: {T.FS_BODY}px;
}}
QLabel#sectionTitle {{
    color: {T.TEXT};
    font-size: {T.FS_SECTION}px;
    font-weight: 600;
}}
QLabel#fieldLabel {{
    color: {T.TEXT_SUB};
    font-size: {T.FS_SMALL}px;
}}
QLabel#hint {{
    color: {T.TEXT_MUTE};
    font-size: {T.FS_TINY}px;
}}
QScrollArea {{
    background: {T.BG};
    border: none;
}}
/* 只给滚动区的直接内容容器上底色，不碰卡片 ——
   否则 QScrollArea > QWidget > QWidget 会盖掉 QWidget#card 的白底
   （实测：设置页卡片全变灰） */
QScrollArea > QWidget#qt_scrollarea_viewport {{
    background: {T.BG};
}}
/* 卡片：用 background-color 而不是 background ——
   background 简写在部分 Qt 版本上对 WA_StyledBackground 的控件不生效
   （实测 palette 仍是 #efefef，卡片显示为灰）。 */
QWidget#card {{
    background-color: {T.SURFACE};
    border: 1px solid {T.BORDER};
    border-radius: {T.R_M}px;
}}
QWidget#cardMuted {{
    background-color: {T.SURFACE_ALT};
    border: 1px solid {T.BORDER};
    border-radius: {T.R_M}px;
}}
QWidget#warnCard {{
    background-color: {T.WARN_BG};
    border: 1px solid #F0D9B5;
    border-radius: {T.R_M}px;
}}
QWidget#warnCard QLabel {{
    background-color: transparent;
}}
QWidget#card QLabel, QWidget#cardMuted QLabel {{
    background-color: transparent;
}}
QWidget#divider {{
    background-color: {T.BORDER};
}}
"""

TABLE_QSS = f"""
QTableWidget {{
    background: {T.SURFACE};
    border: 1px solid {T.BORDER};
    border-radius: {T.R_M}px;
    gridline-color: {T.BORDER};
    selection-background-color: {T.PRIMARY_SOFT};
    selection-color: {T.TEXT};
}}
QTableWidget::item {{
    padding: 6px 8px;
    border: none;
}}
QHeaderView::section {{
    background: {T.SURFACE_ALT};
    color: {T.TEXT_SUB};
    font-size: {T.FS_SMALL}px;
    font-weight: 600;
    padding: 8px;
    border: none;
    border-bottom: 1px solid {T.BORDER};
    border-right: 1px solid {T.BORDER};
}}
"""

# 主操作按钮：把 Fluent 默认青色压成深墨蓝
# 只覆盖主按钮，普通按钮保持中性，避免满屏彩色
BUTTON_QSS = f"""
PrimaryPushButton {{
    background: {T.PRIMARY};
    color: {T.TEXT_INVERT};
    border: none;
    border-radius: {T.R_S}px;
    padding: 0 16px;
    font-size: {T.FS_BODY}px;
    font-weight: 600;
}}
PrimaryPushButton:hover {{
    background: {T.PRIMARY_HOVER};
}}
PrimaryPushButton:pressed {{
    background: {T.PRIMARY_PRESS};
}}
PrimaryPushButton:disabled {{
    background: {T.BORDER_STRONG};
    color: {T.TEXT_MUTE};
}}
PushButton {{
    background: {T.SURFACE};
    color: {T.TEXT};
    border: 1px solid {T.BORDER_STRONG};
    border-radius: {T.R_S}px;
    padding: 0 14px;
    font-size: {T.FS_BODY}px;
}}
PushButton:hover {{
    background: {T.SURFACE_ALT};
    border-color: {T.PRIMARY};
}}
PushButton:pressed {{
    background: {T.PRIMARY_SOFT};
}}
PushButton:disabled {{
    color: {T.TEXT_MUTE};
    border-color: {T.BORDER};
}}
LineEdit, PasswordLineEdit {{
    background: {T.SURFACE};
    border: 1px solid {T.BORDER_STRONG};
    border-radius: {T.R_S}px;
    padding: 6px 10px;
    font-size: {T.FS_BODY}px;
    color: {T.TEXT};
}}
LineEdit:focus, PasswordLineEdit:focus {{
    border-color: {T.PRIMARY};
}}
ComboBox {{
    background: {T.SURFACE};
    border: 1px solid {T.BORDER_STRONG};
    border-radius: {T.R_S}px;
    padding: 6px 10px;
    font-size: {T.FS_BODY}px;
    color: {T.TEXT};
}}
ComboBox:hover {{
    border-color: {T.PRIMARY};
}}
"""
