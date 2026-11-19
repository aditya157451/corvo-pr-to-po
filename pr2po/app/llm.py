"""Thin LLM wrapper (Anthropic). Everything that uses it has a deterministic fallback, so the app
works (with reduced convenience) when no key is configured or the call fails."""
from __future__ import annotations

import json
import os
import re

MODEL = os.getenv("LLM_MODEL", "claude-sonnet-5-5")


def available() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def extract_json(text: str) -> dict:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in model output")
    return json.loads(text[start:end + 1])


def complete_json(system: str, user: str, max_tokens: int = 4000, retries: int = 1) -> dict:
    """Ask the model for a single JSON object. Raises on failure (callers fall back)."""
    import anthropic  # lazy: not needed when no key is set

    client = anthropic.Anthropic(timeout=60.0)
    msgs = [{"role": "user", "content": user}]
    last_err = None
    for _ in range(retries + 1):
        resp = client.messages.create(model=MODEL, max_tokens=max_tokens, system=system, messages=msgs)
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        try:
            return extract_json(text)
        except Exception as e:  # ask again once with the parse error
            last_err = e
            msgs = msgs + [{"role": "assistant", "content": text}, {"role": "user", "content": f"That was not valid JSON ({e}). Reply with ONLY the JSON object."}]
    raise ValueError(f"model did not return valid JSON: {last_err}")
