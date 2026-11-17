"""Typed LLM fallback transport. All semantic grading is routed through parallax.semantic."""

from __future__ import annotations

import os
from urllib.parse import urlsplit

from pydantic import Field

from parallax.models import JsonObject, Model


class MissingEvidence(Exception):
    """A semantic judgement cannot be made from available evidence."""


_GATEWAYS = frozenset({"openrouter", "inference_net", "vercel"})  # routing prefixes, not vendors
# Bare model names (no "vendor/" prefix) resolved to their vendor family.
_BARE_FAMILIES = (
    ("claude", "anthropic"),
    ("gpt", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("o4", "openai"),
    ("codex", "openai"),
    ("gemini", "google"),
    ("gemma", "google"),
    ("grok", "x-ai"),
    ("llama", "meta-llama"),
    ("qwen", "qwen"),
    ("deepseek", "deepseek"),
    ("mistral", "mistralai"),
    ("glm", "z-ai"),
    ("kimi", "moonshotai"),
)


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY"
VERCEL_GATEWAY_HOST = "ai-gateway.vercel.sh"
OPENROUTER_HOST = "openrouter.ai"
PROVIDERS_ENV = "PARALLAX_JUDGE_PROVIDERS"
"""Comma-separated upstream providers the fallback may be served by, in preference order. A gateway spreads one
model over many hosts, and not all of them enforce `response_format`: on 2026-11-16 four of the AI Gateway's ten
DeepSeek V4.1 Flash 1017 hosts (boundless, digitalocean, gmicloud, morph) ignored the strict schema on 23 of 23
calls and invented their own keys, while the other six matched it on every call."""
DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash-1017"
"""The fallback panel when none is named: the newest DeepSeek V4.1 Flash release, pinned by its dated id so a
comparison's judge cannot move under it, on OpenRouter with the key Jev already needs."""


class JudgePanel(Model):
    models: tuple[str, ...] = Field(min_length=1)  # from --judge, PARALLAX_JUDGE_MODELS or DEFAULT_MODEL
    # False (`--no-jev`): the panel answers every question and Jev is never asked, for a run whose agent uses
    # Jev itself, where Jev grading it would not be independent.
    jev: bool = True


class JudgeConfig(Model):
    base_url: str
    api_key: str
    providers: tuple[str, ...] = ()  # `PROVIDERS_ENV`; empty lets the gateway route

    def routing(self) -> JsonObject:
        """The request fields that hold the fallback to `providers`, in the gateway's own shape."""
        if not self.providers:
            return {}
        host = urlsplit(self.base_url).hostname
        if host == VERCEL_GATEWAY_HOST:
            return {"providerOptions": {"gateway": {"only": list(self.providers), "order": list(self.providers)}}}
        if host == OPENROUTER_HOST:
            return {"provider": {"only": list(self.providers), "order": list(self.providers), "allow_fallbacks": False}}
        raise JudgeConfigError(f"${PROVIDERS_ENV} is set, but {self.base_url} is not a gateway it can route")


class JudgeConfigError(Exception):
    """The judge endpoint is not configured although a panel is."""


class JudgeItem(Model):
    q: str
    # `why` before `yes`: a strict schema is decoded in field order, so the reason is written before the verdict. With
    # `yes` first, DeepSeek V4.1 Flash sometimes committed to null and then wrote that the evidence was sufficient.
    why: str  # required so the strict response schema below is valid on OpenAI as well
    yes: bool | None


class JudgeReply(Model):
    items: tuple[JudgeItem, ...]


def judge_key() -> str | None:
    """The fallback's key: its own, else the OpenRouter key Jev uses."""
    return os.environ.get("PARALLAX_JUDGE_API_KEY") or os.environ.get(OPENROUTER_KEY_ENV) or None


def panel_from_env() -> JudgePanel | None:
    """`PARALLAX_JUDGE_MODELS`, else `DEFAULT_MODEL` once a key can reach it, else no panel."""
    raw = os.environ.get("PARALLAX_JUDGE_MODELS", "")
    models = tuple(m.strip() for m in raw.split(",") if m.strip())
    if models:
        return JudgePanel(models=models)
    return JudgePanel(models=(DEFAULT_MODEL,)) if judge_key() is not None else None


def config_from_env() -> JudgeConfig:
    api_key = judge_key()
    if api_key is None:
        raise JudgeConfigError(f"a judge panel needs PARALLAX_JUDGE_API_KEY or ${OPENROUTER_KEY_ENV}")
    providers = tuple(p.strip() for p in os.environ.get(PROVIDERS_ENV, "").split(",") if p.strip())
    config = JudgeConfig(
        base_url=os.environ.get("PARALLAX_JUDGE_BASE_URL") or OPENROUTER_BASE_URL, api_key=api_key, providers=providers
    )
    config.routing()  # an unroutable provider list fails here, at configuration, not on every judged trial
    return config


def family(model: str) -> str:
    """Vendor family: the first non-gateway path segment, or inferred from a bare model name."""
    provider, separator, routed = model.lower().partition(":")
    if separator and provider not in _GATEWAYS:
        return provider
    normalized = routed if separator else model.lower()
    parts = [p for p in normalized.split("/") if p]
    while len(parts) > 1 and parts[0] in _GATEWAYS:
        parts = parts[1:]
    if len(parts) > 1:
        return parts[0]
    name = parts[0] if parts else model.lower()
    return next((fam for prefix, fam in _BARE_FAMILIES if name.startswith(prefix)), name)
