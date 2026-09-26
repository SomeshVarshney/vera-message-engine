"""Output checks that run on every message before it leaves the process.

These map onto the penalties in examples/api-call-examples.md:
  - Example F.4: a URL in the body is "hard fail for that action - Meta would
    reject. Penalty: -3 per URL". challenge-brief.md 5.4 says the opposite
    ("URLs allowed when they add clear value"), so the two documents disagree.
    We follow the one that states a concrete penalty, because the risk is
    asymmetric: no writer here needs a URL to make its point, so stripping
    costs nothing, while emitting one costs -3 per action if F.4 is enforced.
    This guard is mainly a net under the optional LLM rewrite pass
  - category taboo vocabulary is a category-fit penalty
  - the same body twice in a conversation is -2
  - a missing required field scores the action 0
"""

import re
from typing import Dict, Set

from .facts import FactSheet
from .voice import strip_taboo
from .writers import Draft

URL_RE = re.compile(r"(https?://\S+|www\.\S+|\b[a-z0-9-]+\.(?:com|in|org|net|io)\b/\S*)", re.I)

# Words that read as internal tooling to a merchant. The rubric docks a point
# for exposing jargon, and these all appear in the context objects.
JARGON = {
    "suppression_key": "",
    "trigger_kind": "",
    "merchant_id": "",
    "context_id": "",
    "lapsed_soft": "lapsed",
    "lapsed_hard": "lapsed",
    "customer_aggregate": "your customer list",
    "peer_stats": "category median",
    "ctr_below_peer_median": "below the category median",
    "factsheet": "",
}

# body -> conversations that have already seen it
_sent_bodies: Dict[str, Set[str]] = {}


def clean(draft: Draft, fs: FactSheet) -> Draft:
    body = draft.body

    body = URL_RE.sub("", body)
    body = strip_taboo(body, fs.vocab_taboo)

    for term, replacement in JARGON.items():
        if term in body:
            body = body.replace(term, replacement)

    # tidy the seams left by removals
    body = re.sub(r"[ \t]{2,}", " ", body)
    body = re.sub(r"\s+([,.;:])", r"\1", body)
    body = re.sub(r"([,.;:]){2,}", r"\1", body)
    body = re.sub(r"\n{3,}", "\n\n", body)

    draft.body = body.strip()
    return draft


def is_sendable(draft: Draft, fs: FactSheet) -> bool:
    if not draft.body or len(draft.body) < 40:
        return False
    if not draft.cta:
        return False
    if URL_RE.search(draft.body):
        return False
    # a body that still carries an unfilled placeholder means a writer read a
    # field that wasn't pushed
    if "{" in draft.body or "None" in draft.body.split():
        return False
    return True


def remember(conversation_id: str, body: str) -> None:
    _sent_bodies.setdefault(_key(body), set()).add(conversation_id)


def already_sent(conversation_id: str, body: str) -> bool:
    return conversation_id in _sent_bodies.get(_key(body), set())


def _key(body: str) -> str:
    return re.sub(r"\s+", " ", body.strip().lower())


def reset() -> None:
    _sent_bodies.clear()
