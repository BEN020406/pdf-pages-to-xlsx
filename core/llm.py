# -*- coding: utf-8 -*-
"""core.llm —— 视觉大模型客户端（阿里云百炼 DashScope / 任何 OpenAI 兼容端点）。

全部结论来自 2026-10-23 对真实 key 的实测，不是文档推测：

1. **端点**：`https://dashscope.aliyuncs.com/compatible-mode/v1`，OpenAI 兼容；
2. **`detail` 参数被静默忽略**：low / high / 不传，image_tokens 都是 2147
   —— 所以清晰度只能靠我们自己渲染的 DPI 控制，不能指望 detail；
3. **图片只能放在 user 消息**：放 system 会返回
   `400 InternalError.Algo.InvalidParameter: An incorrect modal image was entered ... wrong position (e.g., in system/ass...`；
4. **`response_format={"type":"json_object"}` 可用**，且能显著减少 markdown 代码围栏；
5. **模型实测结果**（同一份"倾斜+模糊+噪点+低对比+JPEG"的损益表扫描件，150dpi）：

   | 模型 | 数字命中 | 耗时 | 备注 |
   | --- | --- | --- | --- |
   | qwen3-vl-plus | **24/24** | 11.3s | 手写件也 18/18（硬样本 65.6s） |
   | qwen3.6-flash | 24/24 | 8.9s | 手写硬样本 17/18（把" — "读成" -- "） |
   | qwen3.8-flash | 24/24 | 16~35s | 慢，思考模式 |
   | qwen3.7-plus | 24/24 | 17~41s | 慢 |
   | qwen3.5-plus | 24/24 | 58s | 很慢 |
   | qwen3.5-ocr / qwen-vl-ocr / qwen3.5-ocr | 输出结构不同 | 3~10s | 不是 rows 数组，需单独适配 |

   默认选 **qwen3-vl-plus**：准确率满分，速度可接受，官方标注支持结构化输出。
"""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    OpenAI,
    RateLimitError,
)

# 阿里云百炼价格（元 / 百万 token），2026-10 官方页核对，华北2（北京）
# 输入<=32k 档。输出侧按最贵档保守计，真实账单只会更低。
PRICE = {
    "qwen3-vl-plus": {"in": 1.0, "out": 10.0},
    "qwen3.6-flash": {"in": 1.2, "out": 7.2},
    "qwen3.7-plus": {"in": 1.6, "out": 16.0},
    "qwen3.8-flash": {"in": 1.2, "out": 7.2},
    "qwen3.5-flash": {"in": 0.8, "out": 4.8},
}
DEFAULT_PRICE = {"in": 1.0, "out": 10.0}


class LLMError(RuntimeError):
    """带中文可读信息的调用错误。"""

    def __init__(self, message: str, *, retryable: bool = False, code: str = ""):
        super().__init__(message)
        self.retryable = retryable
        self.code = code


@dataclass
class LLMConfig:
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model: str = "qwen3-vl-plus"
    api_key: str = ""
    # detail 在 Qwen 上是空参数（实测无效果），保留是为了兼容其它端点
    detail: str = ""
    max_retries: int = 3
    timeout: float = 180.0
    temperature: float = 0.0
    max_cost_cny: float = 5.0
    json_mode: bool = True

    def price(self) -> dict:
        return PRICE.get(self.model, DEFAULT_PRICE)


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    cost_cny: float = 0.0

    def add(self, p: int, c: int, price: dict) -> None:
        self.prompt_tokens += p
        self.completion_tokens += c
        self.calls += 1
        self.cost_cny += (p / 1e6) * price["in"] + (c / 1e6) * price["out"]


def estimate_max_tokens(rows: int) -> int:
    """按行数估输出上限，防止 JSON 被截断。"""
    return max(2000, int(rows) * 90)


def encode_image(png_bytes: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")


def _strip_fence(text: str) -> str:
    """模型常把 JSON 包在 ```json ... ``` 里，剥掉。"""
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t


# ---------------------------------------------------------------- prompts

STRUCTURE_PROMPT = """你是财务报表版面分析器。只做结构判断，**不要抄写任何数字**。

我会给你一张财务表格页面图，以及本地 OCR 已识别出的文本行（含坐标）。

请输出 json，格式：
{
  "header_rows": [0, 1],
  "data_start_row": 2,
  "columns": [
    {"name": "项目", "kind": "text"},
    {"name": "本期金额", "kind": "number"}
  ],
  "merged_cells": [],
  "skip_rows": [0],
  "notes": ["第 0 行是标题，不是表头"]
}

要求：
- header_rows 用 0 基行号；
- columns 的 kind 只能是 text / number；
- 不确定的字段给空数组，不要编造；
- notes 用中文一句话说明判断依据。"""

DIGIT_PROMPT = """这是一个财务表格单元格的局部截图。请只读出其中的数值。

只输出 json：{"value": "1234.56", "confident": true}
规则：
- 括号表示负数，如 (1,234.56) → -1234.56
- 破折号或横线 → 0
- 看不清就把 confident 设为 false，value 留空字符串
- 不要猜，不要补零"""

TABLE_PROMPT = """把这张财务表格还原成 json：{"rows":[["项目","本期金额","上期金额"],["营业收入","1234.56",""]]}

规则：
- 保持原始行列顺序，不要合并行、不要拆分单元格；
- 空单元格用 ""；
- 数字保留千分位和括号负号的原样写法（如 (12,345.67)），不要重算、不要补零；
- 横线、破折号原样保留为 "—"；
- 不要编造任何数字。"""


class LLMClient:
    def __init__(self, cfg: LLMConfig, *, client: Any = None):
        self.cfg = cfg
        self.usage = Usage()
        self._client = client or OpenAI(
            api_key=cfg.api_key or "sk-placeholder",
            base_url=cfg.base_url,
            timeout=cfg.timeout,
            max_retries=0,          # 重试自己控制，便于计费和取消
        )

    # ---------------------------------------------------------- 底层
    def _image_part(self, png: bytes) -> dict:
        img: dict[str, Any] = {"url": encode_image(png)}
        if self.cfg.detail:
            img["detail"] = self.cfg.detail
        return {"type": "image_url", "image_url": img}

    def _chat(self, messages: list[dict], max_tokens: int, *, json_mode: bool | None = None) -> dict:
        if json_mode is None:
            json_mode = self.cfg.json_mode
        kwargs: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": self.cfg.temperature,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        resp = None
        for attempt in range(self.cfg.max_retries):
            try:
                resp = self._client.chat.completions.create(**kwargs)
                break
            except AuthenticationError as e:
                raise LLMError("API Key 无效或已过期（401），请检查设置。", code="401") from e
            except APIStatusError as e:
                status = getattr(e, "status_code", 0)
                body = str(getattr(e, "body", "") or e)
                if status == 402 or "Arrearage" in body or "余额" in body:
                    raise LLMError("账户余额不足或已欠费（402），请充值后再试。", code="402") from e
                if status == 429:
                    if attempt == self.cfg.max_retries - 1:
                        raise LLMError("触发限流（429），已重试多次仍失败，请降低并发或稍后再试。",
                                       retryable=True, code="429") from e
                    time.sleep(2 ** attempt)
                    continue
                if status in (500, 503):
                    if attempt == self.cfg.max_retries - 1:
                        raise LLMError(f"服务端错误（{status}），已重试多次仍失败。",
                                       retryable=True, code=str(status)) from e
                    time.sleep(2 ** attempt)
                    continue
                if status == 400:
                    if "wrong position" in body or "system/ass" in body:
                        raise LLMError(
                            "图片被放在了 system 消息里，接口只接受 user 消息中的图片（400）。",
                            code="400_image_position") from e
                    if "data_inspection" in body or "inappropriate" in body.lower():
                        raise LLMError("内容审核未通过（400），该页可能触发了敏感内容过滤。",
                                       code="400_moderation") from e
                    raise LLMError(f"请求格式错误（400）：{body[:200]}", code="400") from e
                raise LLMError(f"接口返回 {status}：{body[:200]}", code=str(status)) from e
            except (APITimeoutError, APIConnectionError) as e:
                if attempt == self.cfg.max_retries - 1:
                    raise LLMError(f"网络超时或无法连接：{type(e).__name__}", retryable=True,
                                   code="network") from e
                time.sleep(2 ** attempt)
                continue
            except BadRequestError as e:
                raise LLMError(f"请求被拒绝（400）：{e}", code="400") from e

        if resp is None:
            raise LLMError("调用未产生响应（内部错误）", code="no_response")

        content = (resp.choices[0].message.content or "").strip()
        u = getattr(resp, "usage", None)
        if u is not None:
            self.usage.add(int(getattr(u, "prompt_tokens", 0) or 0),
                           int(getattr(u, "completion_tokens", 0) or 0),
                           self.cfg.price())

        if not content:
            raise LLMError("模型返回了空内容，可重试。", retryable=True, code="empty")

        content = _strip_fence(content)
        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            raise LLMError(f"模型返回的不是合法 JSON，可能被截断：{content[:120]!r}",
                           retryable=True, code="bad_json") from e

    # ---------------------------------------------------------- 上层
    def read_structure(self, png: bytes, ocr_lines: list[dict]) -> dict:
        """A 模式：只标注结构，不复述数字。"""
        lines_txt = "\n".join(
            f"y={int(l['y0'])} x={int(l['x0'])}-{int(l['x1'])}: {l['text']}"
            for l in ocr_lines[:200]
        )
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": STRUCTURE_PROMPT + "\n\n本地 OCR 文本行：\n" + lines_txt},
                self._image_part(png),
            ],
        }]
        return self._chat(messages, max_tokens=estimate_max_tokens(len(ocr_lines)))

    def read_digit(self, cell_png: bytes) -> dict:
        """B 模式：只读一个单元格的数值。"""
        messages = [{
            "role": "user",
            "content": [{"type": "text", "text": DIGIT_PROMPT}, self._image_part(cell_png)],
        }]
        return self._chat(messages, max_tokens=200)

    def read_table(self, png: bytes, rows_hint: int = 30) -> dict:
        """C 模式：整表兜底。"""
        messages = [{
            "role": "user",
            "content": [{"type": "text", "text": TABLE_PROMPT}, self._image_part(png)],
        }]
        return self._chat(messages, max_tokens=estimate_max_tokens(rows_hint))

    # ---------------------------------------------------------- 测试连接
    def test_connection(self) -> tuple[bool, str]:
        """发一个最小纯文本请求，验证 key / 余额 / 网络，不消耗图片 token。"""
        try:
            resp = self._client.chat.completions.create(
                model=self.cfg.model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=5,
                temperature=0,
            )
            u = getattr(resp, "usage", None)
            extra = f"，消耗 {getattr(u, 'prompt_tokens', 0)} 输入 token" if u else ""
            return True, f"连接正常（模型 {self.cfg.model}）{extra}"
        except AuthenticationError:
            return False, "API Key 无效或已过期（401）"
        except APIStatusError as e:
            status = getattr(e, "status_code", 0)
            body = str(getattr(e, "body", "") or e)
            if status == 402 or "Arrearage" in body:
                return False, "账户余额不足或已欠费（402）"
            if status == 404 or "Model" in body:
                return False, f"模型 {self.cfg.model} 不可用（{status}），请检查模型名或开通权限"
            return False, f"接口返回 {status}：{body[:120]}"
        except Exception as e:  # noqa: BLE001
            return False, f"连接失败：{type(e).__name__}: {e}"
