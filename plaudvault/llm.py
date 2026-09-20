"""One text-completion call, two providers.

`ollama` keeps everything on the machine — the default, and the reason this project
exists. `openai` targets any OpenAI-compatible /chat/completions endpoint: LM Studio,
llama.cpp's server, vLLM, OpenRouter, Groq, or OpenAI itself. Choosing a hosted one
sends transcript text to that provider; the console says so plainly rather than
letting you forget.

**Locality is a property of where the model runs, not of which port you dialled.** That
distinction used to be free and is not any more. Ollama's hosted models are addressed
*through the local daemon* — you pull `gpt-oss:120b-cloud`, post it to
`127.0.0.1:11434` exactly like any other model, and the daemon forwards the prompt to
Ollama's servers. An address check alone therefore reports "nothing leaves this
machine" while a therapy session is in flight, and `remote_allowed()` waves every tier
through because it believes the provider is local. That is precisely the failure D27
exists to prevent, arriving through the one door D27 did not watch.

So a cloud-suffixed model name makes the provider remote regardless of the host, and the
tier scope applies to it like any other hosted endpoint. The detection is deliberately
broad: a model wrongly treated as remote costs one line of configuration, and a model
wrongly treated as local costs a conversation you cannot take back.
"""

from __future__ import annotations

import re
from dataclasses import replace

import httpx

from .config import Config

# Reasoning models emit <think> blocks whether or not you asked.
_THINK = re.compile(r"<think>.*?</think>", re.S)


class LLMError(RuntimeError):
    pass


# Ollama names its hosted models with a `cloud` suffix — `gpt-oss:120b-cloud`,
# `deepseek-v3.1:671b-cloud`, `qwen3-coder:480b-cloud`. Anchored to a separator so a
# model that merely contains the word (`cloudburst`) is not caught.
_CLOUD_MODEL = re.compile(r"(?:^|[:\-])cloud$")


def model_is_cloud(model: str | None) -> bool:
    """Is this Ollama model actually executed on Ollama's servers?

    The local daemon proxies these, so nothing about the request's address reveals it.
    The name is the only signal available before the prompt is already gone.
    """
    return bool(_CLOUD_MODEL.search((model or "").strip().lower()))


def _addr_is_local(host: str) -> bool:
    return "127.0.0.1" in host or "localhost" in host or "::1" in host


def is_local(cfg: Config) -> bool:
    """True when no transcript text leaves this machine.

    Both halves matter: a local address running a cloud-proxied model is remote, and so
    is a remote address running anything.
    """
    if cfg.llm_provider == "ollama":
        if model_is_cloud(cfg.ollama_model):
            return False
        return _addr_is_local(cfg.ollama_host)
    return _addr_is_local(cfg.openai_base_url)


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


class RemoteNotPermitted(LLMError):
    """This recording's tier is not allowed to reach a remote model."""


def cloud_tiers(cfg: Config) -> set[str]:
    return {t.strip() for t in (cfg.cloud_tier_scope or "").split(",") if t.strip()}


def remote_allowed(cfg: Config, tier: str | None) -> bool:
    """May a recording of this tier be sent to the configured provider?

    Always true for a local provider — nothing leaves the machine. For a remote one the
    answer comes from `cloud_tier_scope`, which is empty by default: holding an API key
    is not the same as deciding which conversations may leave, and those two things must
    not be one switch. `tier=None` means the caller could not say which recording this
    is, and an unknown tier is refused rather than assumed safe.
    """
    if is_local(cfg):
        return True
    scope = cloud_tiers(cfg)
    if not scope:
        return False
    return (tier or "") in scope or (tier is None and False)


def generate(cfg: Config, prompt: str, *, temperature: float = 0.2, timeout: float = 900,
             tier: str | None = None) -> str:
    if not remote_allowed(cfg, tier):
        scope = cloud_tiers(cfg)
        where = (f"Ollama's cloud ({cfg.ollama_model})"
                 if cfg.llm_provider == "ollama" else cfg.openai_base_url)
        raise RemoteNotPermitted(
            f"tier {tier or 'unknown'!r} may not be sent to {where} — "
            + (f"cloud_tier_scope allows {sorted(scope)}" if scope
               else "cloud_tier_scope is empty, so no recording may leave this machine")
        )
    if cfg.llm_provider == "ollama":
        resp = httpx.post(
            f"{cfg.ollama_host}/api/generate",
            json={
                "model": cfg.ollama_model,
                "prompt": prompt,
                "stream": False,
                "think": False,
                # A hosted model is reached for precisely when 8192 is the
                # constraint, so the window travels with the provider rather than
                # being fixed at what this machine can hold.
                "options": {
                    "temperature": temperature,
                    "num_ctx": int(getattr(cfg, "llm_num_ctx", 8192) or 8192),
                    # Bounded so a repetition loop fails fast instead of holding the
                    # call open until the timeout. See config's llm_max_tokens.
                    "num_predict": int(getattr(cfg, "llm_max_tokens", 4096) or 4096),
                },
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
                "max_tokens": int(getattr(cfg, "llm_max_tokens", 4096) or 4096),
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


def with_cloud(cfg: Config) -> Config:
    """The same config pointed at `cloud_model`, for one step of one run.

    On-demand rather than configured-on, because the two calls worth a large model —
    choosing which commitments survive, and writing a brief an agent will act on — are a
    handful per run, while summarising and extracting are hundreds. Switching the whole
    provider to buy quality on the few would send the many.

    Nothing about safety is special-cased here. The returned config is remote by the
    ordinary rules, so `remote_allowed()` consults `cloud_tier_scope` for every
    recording exactly as it would for any hosted endpoint, and a tier outside that scope
    raises rather than quietly falling back to the local model — a silent downgrade
    would mean two different models wrote the same board with nothing saying which.
    """
    if not cfg.cloud_model:
        raise LLMError(
            "cloud_model is not set. Add it to your config, together with the tiers it "
            "may see:\n"
            '    cloud_model = "gpt-oss:120b-cloud"\n'
            '    cloud_tier_scope = "stack"'
        )
    ctx = cfg.cloud_num_ctx or cfg.llm_num_ctx
    if model_is_cloud(cfg.cloud_model):
        return replace(cfg, llm_provider="ollama", ollama_model=cfg.cloud_model,
                       llm_num_ctx=ctx)
    return replace(cfg, llm_provider="openai", openai_model=cfg.cloud_model,
                   llm_num_ctx=ctx)
