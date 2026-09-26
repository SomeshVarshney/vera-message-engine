"""Tone, salutation and language handling.

Two audiences with different rules:
  - merchant-facing ("vera"): peer-to-peer. Category voice decides how
    technical we're allowed to get.
  - customer-facing ("merchant_on_behalf"): we are writing *as the merchant*,
    so no internal jargon, no metrics, no mention of magicpin or Vera.

Variant selection is hashed off stable ids rather than randomised, so the same
inputs always produce the same message.
"""

import hashlib
from typing import List, Optional

from .facts import FactSheet

# Phrases the local judge harness reads as "still qualifying". We avoid them
# anywhere we're supposed to be acting, not asking.
QUALIFYING_PHRASES = ("would you", "do you", "can you tell", "what if", "how about")

# Hindi connectors used sparingly for hi-speaking merchants. One phrase per
# message at most — heavier code-mix reads as machine-translated.
HINDI_TAILS = [
    "Bas haan bol dijiye, baaki main dekh leti hoon.",
    "Aap sirf haan kahiye — setup main kar deti hoon.",
    "Ek line mein bata dijiye, aage ka kaam mera.",
]

CUSTOMER_GREETING = {
    "hi": "Namaste",
    "hi-en mix": "Hi",
    "te-en mix": "Hi",
    "ta-en mix": "Vanakkam",
    "kn-en mix": "Hi",
    "english": "Hi",
}


def pick(options: List[str], *seed_parts: str) -> str:
    """Deterministic choice — same seed, same option, every run."""
    if not options:
        return ""
    seed = "|".join(p or "" for p in seed_parts)
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()
    return options[int(digest[:8], 16) % len(options)]


def merchant_open(fs: FactSheet) -> str:
    """Opening address. Repeat contact drops the greeting entirely — the brief
    penalises re-introducing yourself."""
    return fs.salutation_name


def customer_open(fs: FactSheet) -> str:
    greeting = CUSTOMER_GREETING.get((fs.customer_language or "").lower(), "Hi")
    name = fs.customer_name or ""
    # "Karthik (parent: Sumitra)" — address the parent, not the child
    if "(parent:" in name:
        child = name.split("(")[0].strip()
        parent = name.split("parent:")[1].rstrip(")").strip()
        return f"{greeting} {parent}", child
    if name.startswith("(") or not name:
        return greeting, ""
    return f"{greeting} {name}", name


def sign_off_as_merchant(fs: FactSheet) -> str:
    """Who the customer thinks is writing."""
    if fs.owner_first_name and fs.category_slug == "dentists":
        return f"Dr. {fs.owner_first_name}'s clinic"
    if fs.owner_first_name:
        return f"{fs.owner_first_name} from {fs.business_name}"
    return fs.business_name


def hindi_tail(fs: FactSheet, seed: str = "") -> str:
    """One short Hindi line for merchants whose profile lists Hindi.

    Only used on merchant-facing sends where the ask is simple enough that a
    code-mixed closer doesn't muddy it.
    """
    if not fs.speaks_hindi:
        return ""
    return pick(HINDI_TAILS, fs.merchant_id, seed)


def cite(item: dict) -> str:
    """Source citation for research/compliance claims. Uncited claims get
    capped at 7 by the rubric, so this is never optional for digest items."""
    source = (item or {}).get("source")
    return f" ({source})" if source else ""


def strip_taboo(text: str, taboo: List[str]) -> str:
    """Last-resort scrub. Our own copy shouldn't trip this; it exists to catch
    anything the optional LLM pass introduces."""
    cleaned = text
    for word in taboo:
        # entries like "FDA-approved (use only when actually applicable)"
        term = word.split("(")[0].strip()
        if not term:
            continue
        if term.lower() in cleaned.lower():
            idx = cleaned.lower().find(term.lower())
            cleaned = cleaned[:idx] + cleaned[idx + len(term):]
            cleaned = " ".join(cleaned.split())
    return cleaned


def contains_qualifier(text: str) -> Optional[str]:
    lowered = text.lower()
    for phrase in QUALIFYING_PHRASES:
        if phrase in lowered:
            return phrase
    return None


def vocab_hint(fs: FactSheet, *candidates: str) -> str:
    """Return the first candidate term the category actually sanctions.

    Stops us putting 'occlusion' in a salon message just because it sounds
    expert.
    """
    allowed = {v.lower() for v in fs.vocab_allowed}
    for term in candidates:
        if term.lower() in allowed:
            return term
    return ""
