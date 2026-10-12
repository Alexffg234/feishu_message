"""最小 OpenAI 兼容 chat 客户端(用于主题分类)。

只实现 /chat/completions 单轮调用,不引入 openai 官方 SDK。
兼容任何 OpenAI 风格端点(OpenAI / Haier model API / vLLM / Ollama 等)。
"""
from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """LLM 调用失败(网络 / HTTP / 响应解析)。"""


class LLMClient:
    def __init__(self, api_base: str, api_key: str, model: str, timeout: int = 30):
        self._api_base = (api_base or "").rstrip("/")
        self._api_key = api_key or ""
        self._model = model
        self._timeout = timeout

    def chat(self, system: str, user: str) -> str:
        """单轮对话,返回 assistant 文本(已去首尾空白)。"""
        url = f"{self._api_base}/chat/completions"
        payload = {
            "model": self._model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        try:
            resp = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            raise LLMError(f"LLM 请求失败: {e}") from e
        except ValueError as e:  # JSON 解析失败
            raise LLMError(f"LLM 响应非 JSON: {e}") from e

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"LLM 响应结构异常: {str(data)[:200]}") from e
        return (content or "").strip()
