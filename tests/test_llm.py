# -*- coding: utf-8 -*-
"""core.llm 的请求体与错误处理测试 —— 全部用 mock，不消耗真实 token。

另附一组「联网实测」用例，只在设置了 QWEN_API_KEY 环境变量时运行。
"""
import json
import os
from types import SimpleNamespace

import pytest
from openai import APIConnectionError, APIStatusError, AuthenticationError

from core.llm import (
    DEFAULT_PRICE,
    LLMClient,
    LLMConfig,
    LLMError,
    PRICE,
    Usage,
    _strip_fence,
    encode_image,
    estimate_max_tokens,
)


# ---------------------------------------------------------------- fakes

class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def make_resp(content: str, prompt=100, completion=50):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion),
    )


def make_client(responses, **cfg_kw):
    fake = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions(responses)))
    cfg = LLMConfig(api_key="sk-test", **cfg_kw)
    return LLMClient(cfg, client=fake), fake


def _http(code: int):
    import httpx
    req = httpx.Request("POST", "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions")
    return httpx.Response(code, request=req)


def status_error(code: int, body=None):
    return APIStatusError(f"http {code}", response=_http(code), body=body)


def auth_error():
    return AuthenticationError("bad key", response=_http(401), body=None)


# ---------------------------------------------------------------- config

class TestConfig:
    def test_defaults_target_qwen(self):
        cfg = LLMConfig()
        assert "dashscope" in cfg.base_url
        assert cfg.model == "qwen3-vl-plus"

    def test_default_detail_empty(self):
        """Qwen 忽略 detail，默认不传。"""
        assert LLMConfig().detail == ""

    def test_price_lookup(self):
        assert LLMConfig(model="qwen3-vl-plus").price() == PRICE["qwen3-vl-plus"]

    def test_unknown_model_uses_default_price(self):
        assert LLMConfig(model="some-future-model").price() == DEFAULT_PRICE


# ---------------------------------------------------------------- payload

class TestPayload:
    def test_structure_payload_shape(self):
        client, fake = make_client([make_resp(json.dumps({"header_rows": [0]}))])
        lines = [{"y0": 100, "x0": 80, "x1": 200, "text": "项目"}]
        client.read_structure(b"\x89PNG fake", lines)

        req = fake.chat.completions.calls[0]
        assert req["response_format"] == {"type": "json_object"}
        assert req["temperature"] == 0.0
        msgs = req["messages"]
        # 图片必须在 user 消息里 —— 实测放 system 会 400
        assert len(msgs) == 1 and msgs[0]["role"] == "user"
        parts = msgs[0]["content"]
        img = next(p for p in parts if p["type"] == "image_url")
        assert img["image_url"]["url"].startswith("data:image/png;base64,")

    def test_no_detail_sent_by_default(self):
        """实测 Qwen 对 detail 无反应，所以默认不传，避免误导。"""
        client, fake = make_client([make_resp("{}")])
        client.read_table(b"img")
        img = next(p for p in fake.chat.completions.calls[0]["messages"][0]["content"]
                   if p["type"] == "image_url")
        assert "detail" not in img["image_url"]

    def test_detail_sent_when_explicitly_set(self):
        client, fake = make_client([make_resp("{}")], detail="high")
        client.read_table(b"img")
        img = next(p for p in fake.chat.completions.calls[0]["messages"][0]["content"]
                   if p["type"] == "image_url")
        assert img["image_url"]["detail"] == "high"

    def test_prompt_contains_json_keyword(self):
        client, fake = make_client([make_resp("{}")])
        client.read_table(b"img")
        text = next(p for p in fake.chat.completions.calls[0]["messages"][0]["content"]
                    if p["type"] == "text")["text"]
        assert "json" in text.lower()

    def test_max_tokens_scales_with_rows(self):
        assert estimate_max_tokens(10) == 2000
        assert estimate_max_tokens(50) == 4500
        assert estimate_max_tokens(200) == 18000

    def test_digit_payload_small_max_tokens(self):
        client, fake = make_client([make_resp(json.dumps({"value": "1.00", "confident": True}))])
        client.read_digit(b"cell")
        assert fake.chat.completions.calls[0]["max_tokens"] == 200

    def test_encode_image(self):
        assert encode_image(b"\x89PNG") == "data:image/png;base64,iVBORw=="

    def test_fence_stripped(self):
        assert _strip_fence('```json\n{"a":1}\n```') == '{"a":1}'
        assert _strip_fence('{"a":1}') == '{"a":1}'


# ---------------------------------------------------------------- retries

class TestRetries:
    def test_empty_content_is_retryable_error(self):
        client, _ = make_client([make_resp("")])
        with pytest.raises(LLMError) as ei:
            client.read_structure(b"img", [])
        assert ei.value.code == "empty"
        assert ei.value.retryable is True

    def test_bad_json_is_retryable(self):
        client, _ = make_client([make_resp("{truncated")])
        with pytest.raises(LLMError) as ei:
            client.read_structure(b"img", [])
        assert ei.value.code == "bad_json"

    def test_fenced_json_is_accepted(self):
        """模型经常把 JSON 包在 ```json 里，必须能解析。"""
        client, _ = make_client([make_resp('```json\n{"ok": true}\n```')])
        assert client.read_structure(b"img", [])["ok"] is True

    def test_429_then_success(self, monkeypatch):
        monkeypatch.setattr("core.llm.time.sleep", lambda s: None)
        client, fake = make_client([
            status_error(429),
            make_resp(json.dumps({"ok": True})),
        ], max_retries=3)
        assert client.read_structure(b"img", [])["ok"] is True
        assert len(fake.chat.completions.calls) == 2

    def test_429_exhausted(self, monkeypatch):
        monkeypatch.setattr("core.llm.time.sleep", lambda s: None)
        client, _ = make_client([status_error(429)] * 3, max_retries=3)
        with pytest.raises(LLMError) as ei:
            client.read_structure(b"img", [])
        assert ei.value.code == "429"

    def test_402_does_not_retry(self):
        """余额不足重试也没用，必须立刻报错。"""
        client, fake = make_client([status_error(402)] * 3, max_retries=3)
        with pytest.raises(LLMError) as ei:
            client.read_structure(b"img", [])
        assert ei.value.code == "402"
        assert len(fake.chat.completions.calls) == 1

    def test_arrearage_in_body_treated_as_402(self):
        """阿里云欠费有时不是 402，靠 body 里的 Arrearage 识别。"""
        client, _ = make_client([status_error(400, body="Arrearage: account in arrears")])
        with pytest.raises(LLMError) as ei:
            client.read_structure(b"img", [])
        assert ei.value.code == "402"

    def test_401_clear_message(self):
        client, _ = make_client([auth_error()])
        with pytest.raises(LLMError) as ei:
            client.read_structure(b"img", [])
        assert "API Key" in str(ei.value)
        assert ei.value.code == "401"

    def test_image_in_system_position_error(self):
        """实测 Qwen 返回的原文错误，要给用户看得懂的提示。"""
        client, _ = make_client([
            status_error(400, body="InternalError.Algo.InvalidParameter: "
                                   "An incorrect modal image was entered ... in system/ass")
        ])
        with pytest.raises(LLMError) as ei:
            client.read_structure(b"img", [])
        assert ei.value.code == "400_image_position"

    def test_moderation_error(self):
        client, _ = make_client([status_error(400, body="data_inspection failed")])
        with pytest.raises(LLMError) as ei:
            client.read_structure(b"img", [])
        assert ei.value.code == "400_moderation"

    def test_network_error_retryable(self, monkeypatch):
        monkeypatch.setattr("core.llm.time.sleep", lambda s: None)
        client, _ = make_client([APIConnectionError(request=SimpleNamespace())] * 3, max_retries=3)
        with pytest.raises(LLMError) as ei:
            client.read_structure(b"img", [])
        assert ei.value.code == "network"


# ---------------------------------------------------------------- usage

class TestUsage:
    def test_cost_accumulates(self):
        u = Usage()
        u.add(1_000_000, 1_000_000, {"in": 1.0, "out": 10.0})
        assert u.cost_cny == pytest.approx(11.0)

    def test_usage_recorded_after_call(self):
        client, _ = make_client([make_resp("{}", prompt=500, completion=200)])
        client.read_structure(b"img", [])
        assert client.usage.prompt_tokens == 500
        assert client.usage.completion_tokens == 200
        assert client.usage.calls == 1

    def test_single_page_cost_is_cent_level(self):
        """实测一页约 2200 输入 + 400 输出，成本必须是分级，不能到元级。"""
        u = Usage()
        u.add(2200, 400, PRICE["qwen3-vl-plus"])
        assert 0.001 < u.cost_cny < 0.02


# ---------------------------------------------------------------- 联网实测（可选）

@pytest.mark.skipif(not os.environ.get("QWEN_API_KEY"),
                    reason="未设置 QWEN_API_KEY，跳过联网实测")
class TestLive:
    def test_connection_ok(self):
        cfg = LLMConfig(api_key=os.environ["QWEN_API_KEY"])
        ok, msg = LLMClient(cfg).test_connection()
        assert ok, msg

    def test_table_on_real_image(self):
        """真实调用一次，确认返回结构符合预期。"""
        import fitz
        from pathlib import Path

        pdf = Path(__file__).parent.parent / "demo_pdfs" / "profit_scan_hard.pdf"
        if not pdf.exists():
            pytest.skip("缺少样例 PDF")
        doc = fitz.open(pdf)
        png = doc[0].get_pixmap(dpi=150).tobytes("png")
        cfg = LLMConfig(api_key=os.environ["QWEN_API_KEY"])
        out = LLMClient(cfg).read_table(png)
        assert "rows" in out
        assert len(out["rows"]) >= 8
