# -*- coding: utf-8 -*-
"""gui.settings_page —— 设置（大模型配置 + 隐私确认）。"""
from __future__ import annotations

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QHBoxLayout, QVBoxLayout
from qfluentwidgets import (
    ComboBox, InfoBar, InfoBarPosition, LineEdit, PasswordLineEdit,
    PrimaryPushButton, PushButton, SwitchButton,
)

from core.llm import LLMConfig
from core.secure_store import AppConfig, load_config, save_config
from gui.components import Card, Page, field_label, hint, section_title, title
from gui.theme import T
from gui.worker import TestConnectionWorker

MODELS = [
    ("qwen3-vl-plus", "qwen3-vl-plus　准确率最高（实测 24/24）"),
    ("qwen3.6-flash", "qwen3.6-flash　最快（8.9s/页）"),
    ("qwen3.7-plus", "qwen3.7-plus　准确，较慢"),
    ("qwen3.8-flash", "qwen3.8-flash　准确，较慢"),
]


class SettingsPage(Page):
    config_changed = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__("settingsPage", parent, scroll=True)
        self.cfg: AppConfig = load_config()
        self._test_worker: TestConnectionWorker | None = None
        self._needs_privacy_ack = False
        self._build()
        self._load_into_ui()

    # ------------------------------------------------------------ UI
    def _build(self):
        outer = self.vlayout

        outer.addWidget(title(
            "设置", "大模型只用于识别扫描件和手写件；有文本层的页面不会上传。"))

        warn = Card(warn=True)
        w1 = field_label("数据外发提示")
        w1.setStyleSheet(f"color:{T.WARN};font-size:{T.FS_SECTION}px;font-weight:600")
        warn.v.addWidget(w1)
        txt = field_label(
            "启用后，扫描件/手写件的页面图片会上传至阿里云百炼（dashscope.aliyuncs.com）。\n"
            "真实账套、客户资料外发前，请确认符合所内保密要求。")
        txt.setWordWrap(True)
        txt.setStyleSheet(f"color:{T.WARN};font-size:{T.FS_SMALL}px")
        warn.v.addWidget(txt)
        outer.addWidget(warn)

        card = Card()
        row = QHBoxLayout()
        row.setSpacing(T.SP_M)
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(section_title("启用大模型识图"))
        col.addWidget(hint("关闭时只用文本层 + 本地 OCR，全部本机处理。"))
        row.addLayout(col, 1)
        self.sw_enable = SwitchButton()
        self.sw_enable.setOnText("开")
        self.sw_enable.setOffText("关")
        row.addWidget(self.sw_enable, 0, Qt.AlignVCenter)
        card.v.addLayout(row)
        outer.addWidget(card)

        card2 = Card()
        card2.v.addWidget(section_title("接口与密钥"))
        card2.v.addWidget(field_label("接口地址（OpenAI 兼容）"))
        self.ed_base = LineEdit()
        self.ed_base.setPlaceholderText("https://dashscope.aliyuncs.com/compatible-mode/v1")
        card2.v.addWidget(self.ed_base)

        card2.v.addWidget(field_label("模型"))
        self.cb_model = ComboBox()
        for mid, label in MODELS:
            self.cb_model.addItem(label, userData=mid)
        card2.v.addWidget(self.cb_model)

        card2.v.addWidget(field_label("API Key"))
        self.ed_key = PasswordLineEdit()
        self.ed_key.setPlaceholderText("sk-...")
        card2.v.addWidget(self.ed_key)
        card2.v.addWidget(hint(
            "用 Windows DPAPI 加密后存本机；换机器或换 Windows 账户需要重填。"))

        card2.v.addWidget(field_label("本次任务预算上限（元）"))
        self.ed_budget = LineEdit()
        self.ed_budget.setPlaceholderText("5.0")
        self.ed_budget.setMaximumWidth(160)
        card2.v.addWidget(self.ed_budget)
        outer.addWidget(card2)

        row2 = QHBoxLayout()
        row2.setSpacing(T.SP_S)
        self.btn_test = PrimaryPushButton("测试连接")
        self.btn_save = PushButton("保存")
        for b in (self.btn_test, self.btn_save):
            b.setFixedHeight(34)
            row2.addWidget(b)
        row2.addStretch(1)
        outer.addLayout(row2)

        outer.addWidget(hint(
            "成本参考：qwen3-vl-plus 实测约 2200 输入 + 400 输出 token/页 ≈ 0.003 元/页。"
            "有文本层的页面为 0。"))
        outer.addStretch(1)

        self.btn_test.clicked.connect(self._on_test)
        self.btn_save.clicked.connect(self._on_save)
        self.sw_enable.checkedChanged.connect(self._on_toggle)

    # ------------------------------------------------------------ 数据
    def _load_into_ui(self):
        """把配置灌进控件。

        ⚠️ 必须阻断信号：`setChecked()` 会触发 `checkedChanged` →
        `_on_toggle` → `_confirm_privacy` → 模态 QMessageBox。
        而本方法在 `__init__` 里调用，此时主窗口还没显示，
        模态框会直接阻塞构造 —— 实测表现为「点设置就崩/卡死」。

        触发条件：config.json 里 `llm_enabled=true` 且 `privacy_ack=false`
        （上次开了开关但取消了确认框）。
        """
        self.sw_enable.blockSignals(True)
        try:
            self.ed_base.setText(self.cfg.base_url)
            idx = self.cb_model.findData(self.cfg.model)
            if idx >= 0:
                self.cb_model.setCurrentIndex(idx)
            if self.cfg.api_key:
                self.ed_key.setText(self.cfg.api_key)
            self.ed_budget.setText(str(self.cfg.max_cost_cny))
            self.sw_enable.setChecked(self.cfg.llm_enabled)
        finally:
            self.sw_enable.blockSignals(False)

        if self.cfg.llm_enabled and not self.cfg.privacy_ack:
            self.sw_enable.blockSignals(True)
            self.sw_enable.setChecked(False)
            self.sw_enable.blockSignals(False)
            self.cfg.llm_enabled = False
            self._needs_privacy_ack = True
        else:
            self._needs_privacy_ack = False

    def collect(self) -> AppConfig:
        cfg = self.cfg
        cfg.base_url = self.ed_base.text().strip() or cfg.base_url
        cfg.model = self.cb_model.currentData() or cfg.model
        cfg.api_key = self.ed_key.text().strip()
        try:
            cfg.max_cost_cny = max(0.1, float(self.ed_budget.text().strip() or "5"))
        except ValueError:
            cfg.max_cost_cny = 5.0
        cfg.llm_enabled = self.sw_enable.isChecked()
        return cfg

    def to_llm_config(self) -> LLMConfig | None:
        cfg = self.collect()
        if not cfg.llm_enabled or not cfg.api_key or not cfg.privacy_ack:
            return None
        if len(cfg.api_key) < 20:
            # 占位符/没粘全的 key 直接当没配 —— 否则会静默回退本地 OCR，
            # 用户以为开了大模型，实际一页都没调（实测踩过：存的 key 是 "sk-test"）
            return None
        if cfg.api_key.lower().startswith(("http://", "https://")):
            # 实测踩过：把网址粘进了 Key 栏，界面上看不出问题，一跑就是 401
            return None
        return LLMConfig(base_url=cfg.base_url, model=cfg.model,
                         api_key=cfg.api_key, max_cost_cny=cfg.max_cost_cny)

    def llm_block_reason(self) -> str:
        """为什么这次用不上大模型 —— 直接写在界面上，别让用户猜。"""
        cfg = self.collect()
        if not cfg.llm_enabled:
            return "「设置」里的大模型开关是关的"
        if not cfg.api_key:
            return "还没填 API Key"
        if cfg.api_key.lower().startswith(("http://", "https://")):
            return "API Key 那一栏填的是网址（http…），不是 Key"
        if len(cfg.api_key) < 20:
            return f"API Key 看着是占位符（只有 {len(cfg.api_key)} 个字符），请重新粘贴"
        if not cfg.privacy_ack:
            return "还没在「设置」里确认过隐私提示（开关会被自动关掉）"
        return ""

    # ------------------------------------------------------------ 交互
    def _on_toggle(self, on: bool):
        if on:
            if not self.ed_key.text().strip():
                self._warn("还没有填 API Key", "填好 Key 并保存后才能使用大模型。")
                self._rollback()
                return
            if not self.cfg.privacy_ack and not self._confirm_privacy():
                self._rollback()
                return
            self.cfg.privacy_ack = True
            self._needs_privacy_ack = False
        self.cfg.llm_enabled = on
        cfg = self.collect()
        # 打开开关时立刻落盘：只放在内存里的配置，重启就没了，
        # 用户以为「已经开了」其实下次又是未启用（实测踩过）。
        warnings = save_config(cfg)
        self.cfg = cfg
        self.config_changed.emit(cfg)
        if on and not warnings:
            InfoBar.success(
                "已启用并保存", "Key 与开关已写入本机配置，下次启动仍生效。",
                duration=3000, position=InfoBarPosition.TOP_RIGHT,
                parent=self.window())
        elif warnings:
            self._warn("已切换，但保存有告警", "；".join(warnings))

    def _rollback(self):
        self.sw_enable.blockSignals(True)
        self.sw_enable.setChecked(False)
        self.sw_enable.blockSignals(False)
        self.cfg.llm_enabled = False
        self.config_changed.emit(self.collect())

    def _confirm_privacy(self) -> bool:
        from PyQt5.QtWidgets import QMessageBox
        box = QMessageBox(self.window())
        box.setWindowTitle("确认数据外发")
        box.setIcon(QMessageBox.Warning)
        box.setText("启用后，扫描件/手写件的<b>页面图片</b>会上传至：<br>"
                    "<code>dashscope.aliyuncs.com</code>（阿里云百炼）")
        box.setInformativeText(
            "· 只上传需要 OCR 的页面；有文本层的页不上传\n"
            "· 图片在阿里云侧的处理受其服务条款约束\n"
            "· 真实账套/客户资料外发前，请确认符合所内保密要求\n"
            "· 可随时关闭开关停止外发")
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        box.button(QMessageBox.Yes).setText("我已确认，启用")
        box.button(QMessageBox.No).setText("取消")
        return box.exec_() == QMessageBox.Yes

    def _on_test(self):
        cfg = self.collect()
        if not cfg.api_key:
            self._warn("请先填 API Key", "")
            return
        self.btn_test.setEnabled(False)
        self.btn_test.setText("测试中…")
        self._test_worker = TestConnectionWorker(
            LLMConfig(base_url=cfg.base_url, model=cfg.model, api_key=cfg.api_key))
        self._test_worker.done.connect(self._on_test_done)
        self._test_worker.start()

    def _on_test_done(self, ok: bool, msg: str):
        self.btn_test.setEnabled(True)
        self.btn_test.setText("测试连接")
        if ok:
            InfoBar.success("连接正常", msg, duration=4000,
                            position=InfoBarPosition.TOP_RIGHT, parent=self.window())
        else:
            InfoBar.error("连接失败", msg, duration=6000,
                          position=InfoBarPosition.TOP_RIGHT, parent=self.window())

    def _on_save(self):
        cfg = self.collect()
        warnings = save_config(cfg)
        self.cfg = cfg
        self.config_changed.emit(cfg)
        if warnings:
            self._warn("已保存（有告警）", "；".join(warnings))
        else:
            InfoBar.success("已保存", "设置已写入本机配置。", duration=2200,
                            position=InfoBarPosition.TOP_RIGHT, parent=self.window())

    def _warn(self, t: str, c: str):
        InfoBar.warning(t, c, duration=3500,
                        position=InfoBarPosition.TOP_RIGHT, parent=self.window())
