# -*- coding: utf-8 -*-
"""core.secure_store —— API Key 的本机加密存储。

Windows 上用 DPAPI（CryptProtectData）加密：密文只能被「当前 Windows 用户
在本机」解出，复制到别的机器/别的用户账户都解不开。

非 Windows 或 pywin32 不可用时，退化为「不落盘」，只在内存里保留，
并在调用方读取配置时给出提示（见 load_config 的 warning 字段）。
"""
from __future__ import annotations

import base64
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "pdf2xlsx"
CONFIG_PATH = CONFIG_DIR / "config.json"
ENTROPY = b"pdf2xlsx::llm::v1"


def _dpapi_available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import win32crypt  # noqa: F401
    except Exception:
        return False
    return True


def encrypt(plaintext: str) -> str:
    """加密为可存文本的 base64；失败返回空串。"""
    if not plaintext:
        return ""
    if not _dpapi_available():
        return ""
    import win32crypt

    blob = win32crypt.CryptProtectData(
        plaintext.encode("utf-8"), "pdf2xlsx", ENTROPY, None, None, 0
    )
    return base64.b64encode(blob).decode("ascii")


def decrypt(cipher_b64: str) -> str:
    """解密；任何异常都返回空串（宁可让用户重填，也不抛栈到 UI）。"""
    if not cipher_b64:
        return ""
    if not _dpapi_available():
        return ""
    import win32crypt

    try:
        blob = base64.b64decode(cipher_b64)
        _desc, data = win32crypt.CryptUnprotectData(blob, ENTROPY, None, None, 0)
        return data.decode("utf-8")
    except Exception:
        return ""


@dataclass
class AppConfig:
    """落盘配置。api_key 只在内存里是明文，落盘时替换成 api_key_enc。"""
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model: str = "qwen3-vl-plus"
    detail: str = ""                       # Qwen 忽略该参数，留空
    llm_enabled: bool = False
    max_cost_cny: float = 5.0
    api_key: str = ""                      # 内存明文，不写盘
    api_key_enc: str = ""                  # 落盘密文
    privacy_ack: bool = False              # 是否已确认过「数据会外发」
    warnings: list[str] = field(default_factory=list)

    def to_public_dict(self) -> dict:
        return {
            "base_url": self.base_url,
            "model": self.model,
            "detail": self.detail,
            "llm_enabled": self.llm_enabled,
            "max_cost_cny": self.max_cost_cny,
            "privacy_ack": self.privacy_ack,
        }


def save_config(cfg: AppConfig) -> list[str]:
    """保存配置，返回告警列表（比如密钥无法加密）。"""
    warnings: list[str] = []
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    enc = cfg.api_key_enc
    if cfg.api_key:
        enc = encrypt(cfg.api_key)
        if not enc:
            warnings.append("当前环境无法加密 API Key，密钥未写入磁盘（仅本次运行有效）。")
        cfg.api_key_enc = enc

    payload = cfg.to_public_dict()
    payload["api_key_enc"] = enc if enc else ""
    CONFIG_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return warnings


def load_config() -> AppConfig:
    cfg = AppConfig()
    if not CONFIG_PATH.exists():
        return cfg
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        cfg.warnings.append(f"配置文件损坏，已忽略：{e}")
        return cfg

    cfg.base_url = data.get("base_url", cfg.base_url)
    cfg.model = data.get("model", cfg.model)
    cfg.detail = data.get("detail", cfg.detail)
    cfg.llm_enabled = bool(data.get("llm_enabled", False))
    cfg.max_cost_cny = float(data.get("max_cost_cny", cfg.max_cost_cny))
    cfg.privacy_ack = bool(data.get("privacy_ack", False))
    cfg.api_key_enc = data.get("api_key_enc", "")

    if cfg.api_key_enc:
        cfg.api_key = decrypt(cfg.api_key_enc)
        if not cfg.api_key:
            cfg.warnings.append("已保存的 API Key 无法解密（可能换了 Windows 账户或机器），请重新填写。")
    return cfg
