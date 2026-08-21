"""One text-completion call, two providers.

`ollama` keeps everything on the machine — the default, and the reason this project
exists. `openai` targets any OpenAI-compatible /chat/completions endpoint: LM Studio,
llama.cpp's server, vLLM, OpenRouter, Groq, or OpenAI itself. Choosing a hosted one
sends transcript text to that provider; the console says so plainly rather than
letting you forget.
"""

from __future__ import annotations

import re

import httpx

from .config import Config

# Reasoning models emit <think> blocks whether or not you asked.
_THINK = re.compile(r"<think>.*?</think>", re.S)


class LLMError(RuntimeError):
    pass


def is_local(cfg: Config) -> bool:
    """True when no transcript text leaves this machine."""
    if cfg.llm_provider == "ollama":
        return "127.0.0.1" in cfg.ollama_host or "localhost" in cfg.ollama_host
    host = cfg.openai_base_url
    return "127.0.0.1" in host or "localhost" in host


def available(cfg: Config) -> tuple[bool, str]:
    """(reachable, human-readable reason). Never raises."""
    try:
        if cfg.llm_provider == "ollama":
            r = httpx.get(f"{cfg.ollama_host}/api/tags", timeout=5)
            if not r.is_success:
                return False, f"Ollama returned HTTP {r.status_code}"
            models = [m.get("name") for m in r.json().get("models", [])]
            if cfg.ollama_model not in models:
                return False, (
                    f"model {cfg.ollama_model!r} not installed. "
                    f"Run: ollama pull {cfg.ollama_model.split(':')[0]}"
                )
            return True, "ok"
        if not cfg.openai_api_key() and "127.0.0.1" not in cfg.openai_base_url:
            return False, f"${cfg.openai_api_key_env} is not set"
        r = httpx.get(
            f"{cfg.openai_base_url.rstrip('/')}/models",
            headers=_auth_headers(cfg),
            timeout=8,
        )
        return (True, "ok") if r.is_success else (False, f"HTTP {r.status_code}")
    except Exception as exc:  # noqa: BLE001
        target = cfg.ollama_host if cfg.llm_provider == "ollama" else cfg.openai_base_url
        return False, f"cannot reach {target}: {exc}"


def _auth_headers(cfg: Config) -> dict[str, str]:
    key = cfg.openai_api_key()
    return {"Authorization": f"Bearer {key}"} if key else {}


def generate(cfg: Config, prompt: str, *, temperature: float = 0.2, timeout: float = 900) -> str:
    if cfg.llm_provider == "ollama":
        resp = httpx.post(
            f"{cfg.ollama_host}/api/generate",
            json={
                "model": cfg.ollama_model,
                "prompt": prompt,
                "stream": False,
                "think": False,
                "options": {"temperature": temperature, "num_ctx": 8192},
            },
            timeout=timeout,
        )
        if not resp.is_success:
            raise LLMError(f"Ollama HTTP {resp.status_code}: {resp.text[:200]}")
        out = resp.json().get("response", "")
    else:
        resp = httpx.post(
            f"{cfg.openai_base_url.rstrip('/')}/chat/completions",
            headers={"Content-Type": "application/json", **_auth_headers(cfg)},
            json={
                "model": cfg.openai_model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature,
            },
            timeout=timeout,
        )
        if not resp.is_success:
            raise LLMError(f"{cfg.openai_base_url} HTTP {resp.status_code}: {resp.text[:200]}")
        choices = resp.json().get("choices") or []
        if not choices:
            raise LLMError("provider returned no choices")
        out = choices[0].get("message", {}).get("content", "")

    return _THINK.sub("", out or "").strip()
