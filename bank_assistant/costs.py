"""What each AI call cost: token counts from the provider's reply, priced in euros.

Token counts are exact when the provider returns them (Anthropic and OpenAI both do).
The euro amount is an estimate: list prices per million tokens times the tokens used,
converted from US dollars. The provider's bill is the real number.
"""

from dataclasses import asdict, dataclass
from urllib.parse import urlparse

PRICES_CHECKED = "22 October 2026"
USD_TO_EUR = 0.89          # 1 USD in EUR on 21 Oct 2026; editable in the app

# USD per 1 million tokens: input, cache write, cache read, output.
# Anthropic: platform.claude.com/docs/en/about-claude/pricing (5-minute cache write).
# OpenAI: developers.openai.com/api/docs/pricing (no cache-write surcharge, so write = input).
PRICES = {
    "claude-haiku-5-5": (0.10, 0.125, 0.01, 0.50),
    "claude-sonnet-5-5": (2.00, 2.50, 0.10, 10.00),
    "claude-opus-5-5": (4.00, 5.00, 0.20, 20.00),
    "claude-haiku-4-5": (1.00, 1.25, 0.10, 5.00),
    "claude-sonnet-4-6": (3.00, 3.75, 0.30, 15.00),
    "gpt-6-luna": (0.10, 0.10, 0.01, 0.50),
    "gpt-6.1-sol": (2.00, 2.00, 0.10, 10.00),
    "gpt-6-astra": (10.00, 10.00, 1.00, 50.00),
    "gpt-5-mini": (0.25, 0.25, 0.025, 2.00),     # older model, not on today's price page: check before relying on it
}


@dataclass
class Usage:
    """Tokens used by one model call."""
    model: str
    provider: str
    input: int = 0           # input tokens billed at the normal rate
    output: int = 0
    cache_read: int = 0      # input tokens read from the prompt cache (cheap)
    cache_write: int = 0     # input tokens written to the cache (Anthropic charges a bit extra)
    estimated: bool = False  # no counts from the provider: guessed from text length
    free: bool = False       # local model, no bill
    price_as: str = ""       # model whose prices apply (defaults to `model`)

    @property
    def tokens(self):
        return self.input + self.output + self.cache_read + self.cache_write

    def to_dict(self):
        return asdict(self)


def estimate_tokens(text):
    """Rough count for English text and SQL: about 4 characters per token."""
    return max(1, round(len(text or "") / 4))


def is_local(base_url):
    host = urlparse(base_url or "").hostname or ""
    return host in ("localhost", "127.0.0.1", "0.0.0.0", "::1") or host.endswith(".local")


def from_anthropic(body, model):
    u = body.get("usage") or {}
    return Usage(model, "anthropic", int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0),
                 int(u.get("cache_read_input_tokens") or 0), int(u.get("cache_creation_input_tokens") or 0),
                 estimated=not u)


def from_openai(body, model, provider="openai", free=False):
    """OpenAI's prompt_tokens include the cached ones, so split them out."""
    u = body.get("usage") or {}
    cached = int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    prompt = int(u.get("prompt_tokens") or 0)
    return Usage(model, provider, max(prompt - cached, 0), int(u.get("completion_tokens") or 0), cached, 0,
                 estimated=not u, free=free)


def price_for(model, prices=PRICES):
    """Exact match, else the longest known name the model starts with (dated versions)."""
    if model in prices:
        return prices[model]
    hits = [k for k in prices if model.startswith(k)]
    return prices[max(hits, key=len)] if hits else None


def cost_eur(usage, prices=PRICES, usd_to_eur=USD_TO_EUR):
    """Euros for one call, or None when the model's price is unknown."""
    if usage.free:
        return 0.0
    p = price_for(usage.price_as or usage.model, prices)
    if p is None:
        return None
    inp, write, read, out = p
    usd = (usage.input * inp + usage.cache_write * write + usage.cache_read * read + usage.output * out) / 1e6
    return usd * usd_to_eur


def total(usages, prices=PRICES, usd_to_eur=USD_TO_EUR):
    """Sum of tokens and euros for a list of calls; euros are None if any price is unknown."""
    eur = 0.0
    for u in usages:
        c = cost_eur(u, prices, usd_to_eur)
        if c is None:
            eur = None
            break
        eur += c
    return {"calls": len(usages), "input": sum(u.input for u in usages), "output": sum(u.output for u in usages),
            "cache_read": sum(u.cache_read for u in usages), "cache_write": sum(u.cache_write for u in usages),
            "eur": eur, "estimated": any(u.estimated for u in usages)}


def eur(v):
    """Small amounts need more decimals: €0.0004, €0.012, €1.23."""
    if v is None:
        return "price unknown"
    if v == 0:
        return "€0"
    if abs(v) < 0.0001:
        return "< €0.0001"
    if abs(v) < 0.01:
        return f"€{v:.4f}"
    if abs(v) < 1:
        return f"€{v:.3f}"
    return f"€{v:,.2f}"
