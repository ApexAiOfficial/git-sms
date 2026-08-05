"""Lazy LLM provider selection for Git SMS."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any


class LLMNotConfiguredError(RuntimeError):
    """Raised when no supported LLM credential is configured."""


@lru_cache(maxsize=1)
def get_llm_client() -> tuple[str, Any]:
    github_models_key = os.getenv("GITHUB_OPENAI_API_KEY")
    if github_models_key:
        from azure.ai.inference import ChatCompletionsClient
        from azure.core.credentials import AzureKeyCredential

        return "github-models", ChatCompletionsClient(
            endpoint="https://models.github.ai/inference",
            credential=AzureKeyCredential(github_models_key),
        )

    openai_key = os.getenv("OPENAI_API_KEY")
    if openai_key:
        from openai import OpenAI

        return "openai", OpenAI(api_key=openai_key)

    raise LLMNotConfiguredError("Set GITHUB_OPENAI_API_KEY or OPENAI_API_KEY to enable AI features.")


def complete_text(*, system_prompt: str, user_prompt: str, temperature: float, max_tokens: int) -> str:
    provider, client = get_llm_client()
    if provider == "github-models":
        from azure.ai.inference.models import SystemMessage, UserMessage

        response = client.complete(
            model=os.getenv("GITHUB_MODELS_MODEL", "openai/gpt-4o"),
            messages=[SystemMessage(content=system_prompt), UserMessage(content=user_prompt)],
            temperature=temperature,
            max_tokens=max_tokens,
        )
    else:
        response = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o"),
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            temperature=temperature,
            max_tokens=max_tokens,
        )
    if not response or not response.choices or not response.choices[0].message.content:
        raise RuntimeError("The configured LLM returned no content.")
    return str(response.choices[0].message.content).strip()
