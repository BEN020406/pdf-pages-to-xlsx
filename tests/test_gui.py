# -*- coding: utf-8 -*-
"""GUI 回归测试：用 offscreen 平台，验证不崩溃、不阻塞。

重点钉住一个实测崩溃：
    config 里 llm_enabled=True 但 privacy_ack=False 时，
    SettingsPage._load_into_ui() 的 setChecked() 会触发 checkedChanged，
    进而调用 _on_toggle → _confirm_privacy → 模态 QMessageBox，
    在 MainWindow 构造阶段就阻塞，窗口起不来（看起来像"点设置崩溃"）。
"""
import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

pytest.importorskip("PyQt5")
pytest.importorskip("qfluentwidgets")

from PyQt5.QtWidgets import QApplication, QMessageBox  # noqa: E402


@pytest.fixture(autouse=True)
def never_touch_real_config(monkeypatch):
    """测试绝不允许写真实配置文件。

    坑：`_on_save` 只调 `save_config`，而 fixture 只替换了 `load_config`，
    一旦哪个测试点到「保存」，就会把占位符 key 写进用户真实的
    `%APPDATA%\\pdf2xlsx\\config.json`（实测发生过：真 key 被覆盖成 `sk-test`）。
    """
    from gui import settings_page as m

    saved = []
    monkeypatch.setattr(m, "save_config", lambda cfg: saved.append(cfg) or [])
    return saved


@pytest.fixture
def no_modal(monkeypatch):
    """禁止任何模态弹框：构造期弹框会让测试卡死，必须显式拦掉。"""
    from gui import settings_page as m

    called = {"confirm": 0}

    def fake_confirm(self):
        called["confirm"] += 1
        return False

    monkeypatch.setattr(m.SettingsPage, "_confirm_privacy", fake_confirm)
    return called


@pytest.fixture
def cfg_factory(monkeypatch):
    """注入指定的 AppConfig，模拟磁盘上的 config.json。"""
    from core.secure_store import AppConfig
    from gui import settings_page as m

    def make(**kw):
        cfg = AppConfig(api_key="sk-test", **kw)

        def fake_load():
            return AppConfig(
                base_url=cfg.base_url, model=cfg.model, detail=cfg.detail,
                llm_enabled=cfg.llm_enabled, max_cost_cny=cfg.max_cost_cny,
                api_key=cfg.api_key, api_key_enc="", privacy_ack=cfg.privacy_ack,
            )

        monkeypatch.setattr(m, "load_config", fake_load)
        return cfg

    return make


class TestConstructionNoModal:
    """构造阶段绝不能弹模态框 —— 这是崩溃根因。"""

    def test_enabled_without_ack_does_not_prompt(self, app, no_modal, cfg_factory):
        """实测崩溃场景：llm_enabled=True 且 privacy_ack=False。"""
        cfg_factory(llm_enabled=True, privacy_ack=False)
        from gui.settings_page import SettingsPage
        page = SettingsPage()
        assert page is not None
        assert no_modal["confirm"] == 0, "构造期间弹了模态框，会阻塞主窗口"

    def test_enabled_with_key_no_prompt(self, app, no_modal, cfg_factory):
        cfg_factory(llm_enabled=True, privacy_ack=True)
        from gui.settings_page import SettingsPage
        SettingsPage()
        assert no_modal["confirm"] == 0

    def test_disabled_clean(self, app, no_modal, cfg_factory):
        cfg_factory(llm_enabled=False, privacy_ack=False)
        from gui.settings_page import SettingsPage
        page = SettingsPage()
        assert page.sw_enable.isChecked() is False


class TestToggleStillGuards:
    """修复不能把隐私保护弄丢：用户手动开开关时仍要确认。"""

    def test_user_toggle_prompts_when_not_acked(self, app, no_modal, cfg_factory):
        cfg_factory(llm_enabled=False, privacy_ack=False)
        from gui.settings_page import SettingsPage
        page = SettingsPage()
        page.ed_key.setText("sk-test")
        page.sw_enable.setChecked(True)     # 模拟用户点击
        assert no_modal["confirm"] == 1, "用户手动开启时必须弹确认"

    def test_decline_rolls_back(self, app, no_modal, cfg_factory):
        cfg_factory(llm_enabled=False, privacy_ack=False)
        from gui.settings_page import SettingsPage
        page = SettingsPage()
        page.ed_key.setText("sk-test")
        page.sw_enable.setChecked(True)     # no_modal 返回 False
        assert page.sw_enable.isChecked() is False, "用户取消后开关必须回弹"

    def test_ack_allows_toggle(self, app, monkeypatch, cfg_factory):
        from gui import settings_page as m
        monkeypatch.setattr(m.SettingsPage, "_confirm_privacy", lambda self: True)
        cfg_factory(llm_enabled=False, privacy_ack=False)
        from gui.settings_page import SettingsPage
        page = SettingsPage()
        page.ed_key.setText("sk-test")
        page.sw_enable.setChecked(True)
        assert page.sw_enable.isChecked() is True
        assert page.cfg.privacy_ack is True


class TestKeySanity:
    """占位符 key 必须当「没配」，不能让用户以为大模型开着、实际一路回退本地 OCR。"""

    def test_placeholder_key_is_rejected(self, app, no_modal, cfg_factory):
        cfg_factory(llm_enabled=True, privacy_ack=True)   # fixture 里 key 是 "sk-test"
        from gui.settings_page import SettingsPage
        page = SettingsPage()
        assert page.to_llm_config() is None
        assert "占位符" in page.llm_block_reason()

    def test_full_length_key_passes(self, app, no_modal, cfg_factory):
        cfg_factory(llm_enabled=True, privacy_ack=True)
        from gui.settings_page import SettingsPage
        page = SettingsPage()
        page.ed_key.setText("sk-" + "a" * 40)
        assert page.to_llm_config() is not None
        assert page.llm_block_reason() == ""

    def test_url_in_key_field_is_rejected(self, app, no_modal, cfg_factory):
        """实测踩过：把网址粘进 Key 栏，界面看不出来，一跑就是 401。"""
        cfg_factory(llm_enabled=True, privacy_ack=True)
        from gui.settings_page import SettingsPage
        page = SettingsPage()
        page.ed_key.setText("https://steamcommunity.com/profiles/76561198")
        assert page.to_llm_config() is None
        assert "网址" in page.llm_block_reason()

    def test_reason_when_switch_off(self, app, no_modal, cfg_factory):
        cfg_factory(llm_enabled=False, privacy_ack=False)
        from gui.settings_page import SettingsPage
        page = SettingsPage()
        assert "开关" in page.llm_block_reason()


class TestMainWindow:
    def test_window_builds_with_enabled_config(self, app, no_modal, cfg_factory):
        """完整主窗口：这才是用户实际启动的路径。"""
        cfg_factory(llm_enabled=True, privacy_ack=False)
        from gui.app import MainWindow
        w = MainWindow()
        assert w.stackedWidget.count() == 4
        assert no_modal["confirm"] == 0

    def test_switch_to_every_page(self, app, no_modal, cfg_factory):
        cfg_factory(llm_enabled=True, privacy_ack=False)
        from gui.app import MainWindow
        w = MainWindow()
        w.resize(1180, 780)
        for page in (w.import_page, w.review_page, w.export_page, w.settings_page):
            w.switchTo(page)
            app.processEvents()
            assert w.stackedWidget.currentWidget() is page

    def test_settings_page_renders(self, app, no_modal, cfg_factory):
        cfg_factory(llm_enabled=True, privacy_ack=False)
        from gui.app import MainWindow
        w = MainWindow()
        w.resize(1180, 780)
        w.show()
        w.switchTo(w.settings_page)
        app.processEvents()
        assert w.settings_page.width() > 0
        w.close()
