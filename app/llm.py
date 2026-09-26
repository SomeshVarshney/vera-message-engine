"""Optional rewrite pass over an already-composed message.

Off by default, and the bot is designed to score without it. When it is turned
on (VERA_LLM_ENABLED=1 plus a key) the model is allowed to do exactly one
thing: rephrase a message we have already grounded. It cannot introduce a fact,
because the output is rejected if it contains any number the deterministic
draft didn't already have.

Rationale for keeping this optional rather than central:
  - /v1/tick is capped at 30s and can carry up to 20 actions; a per-action
    model call is the easiest way to blow that budget
  - a timeout or a rate limit mid-test costs operational penalties
  - determinism is an explicit requirement, and temperature=0 is a weaker
    guarantee than not calling a model at all
"""

import json
import os
import re
from typing import List, Optional
from urllib import request as urlrequest

from .facts import FactSheet
from .writers import Draft

ENABLED = os.getenv("VERA_LLM_ENABLED", "").strip() in ("1", "true", "yes")
PROVIDER = os.getenv("VERA_LLM_PROVIDER", "openai").strip().lower()
API_KEY = os.getenv("VERA_LLM_API_KEY", "").strip()
MODEL = os.getenv("VERA_LLM_MODEL", "").strip()
TIMEOUT = float(os.getenv("VERA_LLM_TIMEOUT", "8"))

SYSTEM = """You rewrite WhatsApp messages sent by a merchant-growth assistant in India.

Rules, all hard:
- Keep every number, price, date, name and source citation exactly as given.
- Do not add any fact, number, statistic or claim that is not already present.
- Keep exactly one call to action, and keep it as the last sentence.
- No URLs. No emoji unless the input already has one.
- Keep it tight. Shorter is better if nothing is lost.
Return only the rewritten message."""

_NUM_RE = re.compile(r"\d[\d,]*\.?\d*")


def _numbers(text: str) -> List[str]:
    return [n.replace(",", "").rstrip(".") for n in _NUM_RE.findall(text)]


def _call(prompt: str) -> Optional[str]:
    if PROVIDER == "anthropic":
        url = "https://api.anthropic.com/v1/messages"
        payload = {
            "model": MODEL or "claude-sonnet-4-5",
            "max_tokens": 600,
            "temperature": 0,
            "system": SYSTEM,
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {
            "x-api-key": API_KEY,
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        extract = lambda d: d["content"][0]["text"]
    else:
        url = "https://api.openai.com/v1/chat/completions"
        payload = {
            "model": MODEL or "gpt-4o-mini",
            "temperature": 0,
            "max_tokens": 600,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt},
            ],
        }
        headers = {
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        }
        extract = lambda d: d["choices"][0]["message"]["content"]

    req = urlrequest.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
    with urlrequest.urlopen(req, timeout=TIMEOUT) as resp:
        return extract(json.loads(resp.read().decode("utf-8")))


def refine(draft: Draft, fs: FactSheet) -> Draft:
    if not (ENABLED and API_KEY):
        return draft

    prompt = (
        f"Business type: {fs.category_slug}. Tone required: {fs.tone or 'peer'}.\n"
        f"Audience: {'the merchant' if draft.send_as == 'vera' else 'the merchant customer'}.\n"
        f"Never use: {', '.join(fs.vocab_taboo) or 'n/a'}.\n\n"
        f"Message:\n{draft.body}"
    )

    try:
        rewritten = (_call(prompt) or "").strip()
    except Exception:
        # any failure at all — network, quota, parse — keeps the grounded draft
        return draft

    if not rewritten or len(rewritten) < 40:
        return draft

    # reject anything that smuggled in a new figure
    allowed = set(_numbers(draft.body))
    if any(n not in allowed for n in _numbers(rewritten)):
        return draft

    draft.body = rewritten
    return draft
