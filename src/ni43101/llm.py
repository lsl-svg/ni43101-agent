"""LLM 客户端：OpenAI 兼容协议，多家 provider 可插拔。

设计取舍：
- 不引入 LangChain 等重框架——本系统只需要"给定 system+user，拿回一个 JSON 对象"，
  自己写 40 行比拉一个框架更好评审、更好排障。
- Extractor / Critic / Reviser 用**不同厂商**的模型（异构评审），降低"同源盲区"：
  同一个模型自己批自己，往往对同一类错误系统性失明。
- 强制 JSON + 失败重试 + 兜底解析（模型偶尔会包 ```json 代码块）。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from .config import ModelCfg

PROVIDERS: dict[str, tuple[str, str]] = {
    # provider -> (env var, default base_url)
    "openai": ("OPENAI_API_KEY", "https://api.openai.com/v1"),
    "deepseek": ("DEEPSEEK_API_KEY", "https://api.deepseek.com/v1"),
    "dashscope": ("DASHSCOPE_API_KEY", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    "zhipu": ("ZHIPU_API_KEY", "https://open.bigmodel.cn/api/paas/v4"),
    "openai_compatible": ("OPENAI_COMPAT_API_KEY", ""),
}


class LLMError(RuntimeError):
    pass


def provider_ready(cfg: ModelCfg) -> bool:
    provider = cfg.provider
    if provider == "mock":
        return True
    if provider not in PROVIDERS:
        return False
    env_name, _ = PROVIDERS[provider]
    return bool(os.getenv(env_name))


def extract_json(text: str) -> dict[str, Any]:
    """从模型输出里抠出第一个完整 JSON 对象（容忍 ```json 包裹与前后废话）。"""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start = text.find("{")
    if start < 0:
        raise LLMError(f"模型输出中未找到 JSON: {text[:200]!r}")
    depth = 0
    in_str = False
    escape = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : idx + 1])
    raise LLMError("JSON 括号不闭合，输出被截断")


class LLMClient:
    def __init__(self, cfg: ModelCfg, json_mode: bool = True, max_retries: int = 3, timeout: float = 180.0):
        self.cfg = cfg
        self.json_mode = json_mode
        self.max_retries = max_retries
        self.timeout = timeout
        self._client = None

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        from openai import OpenAI  # 延迟导入：mock 模式无需装 openai 也能跑

        provider = self.cfg.provider
        if provider not in PROVIDERS:
            raise LLMError(f"未知 provider: {provider}")
        env_name, default_base = PROVIDERS[provider]
        api_key = os.getenv(env_name)
        if not api_key:
            raise LLMError(f"缺少环境变量 {env_name}（provider={provider}）")
        base_url = os.getenv("OPENAI_BASE_URL") or default_base or None
        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=self.timeout)
        return self._client

    def json(self, system: str, user: str) -> dict[str, Any]:
        client = self._ensure_client()
        last_err: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            kwargs: dict[str, Any] = {
                "model": self.cfg.model,
                "temperature": self.cfg.temperature,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
            if self.json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            try:
                resp = client.chat.completions.create(**kwargs)
                return extract_json(resp.choices[0].message.content or "")
            except Exception as exc:  # noqa: BLE001 —— 网络/限流/不支持 json_object 统一兜底
                last_err = exc
                # 有些兼容端点不支持 response_format，第二次起退化为纯 prompt 约束
                self.json_mode = False
                time.sleep(min(2**attempt, 8))
        raise LLMError(f"{self.cfg.model} 调用失败（{self.max_retries} 次重试）: {last_err}")
