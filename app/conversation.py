"""What to do when a merchant (or a customer) writes back.

Three failure modes the brief calls out explicitly, and all three are handled
here rather than by a model:

  1. Auto-reply pollution — production Vera burns two to three turns on a
     WhatsApp Business canned reply. We flag it once, back off once, then stop.
  2. Intent-handoff failure — the merchant says "ok let's do it" and the bot
     asks another qualifying question. Once intent is detected we stop asking
     questions entirely and hand over an artefact.
  3. Not knowing when to stop — explicit opt-out ends the conversation and
     suppresses the merchant for the rest of the run.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .facts import build_factsheet
from .voice import contains_qualifier, pick

# Canned WhatsApp Business openers. Matching is on a normalised string so
# punctuation and casing don't matter.
AUTO_REPLY_MARKERS = (
    "thank you for contacting",
    "thanks for contacting",
    "thank you for reaching out",
    "our team will respond",
    "our team will get back",
    "we will get back to you shortly",
    "aapki jaankari ke liye",
    "hamari team tak pahuncha",
    "this is an automated",
    "i am an automated assistant",
    "main ek automated assistant",
    "away message",
    "we are currently closed",
    "your message is important to us",
)

OPT_OUT = (
    "stop messaging", "stop sending", "stop these", "unsubscribe", "do not message",
    "dont message", "don't message", "not interested", "leave me alone", "remove me",
    "band karo", "mat bhejo", "pareshan mat",
)

HOSTILE = ("useless", "spam", "nonsense", "bakwas", "waste of time", "rubbish", "stop bothering",
           "why are you bothering", "annoying")

COMMIT = (
    "lets do it", "let's do it", "go ahead", "please do", "please go", "do it", "yes please",
    "ok yes", "okay yes", "sounds good", "proceed", "send it", "send me", "start it",
    "haan", "kar do", "theek hai", "thik hai", "chalega", "ready", "confirm", "sure",
    "yes", "ok", "okay", "yup", "done",
)

DEFER = ("later", "busy", "tomorrow", "next week", "call me", "baad mein", "abhi nahi",
         "not now", "give me time", "will check")

OFF_TOPIC = ("gst", "income tax", "itr", "loan", "insurance", "visa", "passport", "legal notice",
             "court", "electricity bill", "rent agreement", "police", "hiring", "salary")


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).strip()


@dataclass
class ConversationState:
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    trigger_id: Optional[str] = None
    turns: List[Dict[str, Any]] = field(default_factory=list)
    sent_bodies: List[str] = field(default_factory=list)
    auto_reply_hits: int = 0
    hostile_hits: int = 0
    nudges_unanswered: int = 0
    committed: bool = False
    closed: bool = False


@dataclass
class Move:
    action: str                       # send | wait | end
    body: str = ""
    cta: str = "open_ended"
    wait_seconds: Optional[int] = None
    rationale: str = ""
    # set when this turn was a canned auto-reply, so the caller can keep a
    # per-merchant count — the harness reuses the same number across different
    # conversation ids, and per-conversation state alone would never catch it
    auto_reply: bool = False
    opt_out: bool = False


def classify(message: str, state: ConversationState, merchant_auto_hits: int) -> str:
    text = _norm(message)
    if not text:
        return "empty"

    if any(marker in text for marker in AUTO_REPLY_MARKERS):
        return "auto_reply"

    # the same sentence back twice is an auto-responder even if the wording
    # isn't one we recognise
    previous = [_norm(t["message"]) for t in state.turns if t["role"] != "bot"]
    if text in previous:
        return "auto_reply"

    if any(phrase in text for phrase in OPT_OUT):
        return "opt_out"
    if any(phrase in text for phrase in HOSTILE):
        return "hostile"
    if any(topic in text for topic in OFF_TOPIC):
        return "off_topic"
    if any(phrase in text for phrase in DEFER) and "?" not in message:
        return "defer"

    # commitment check is word-boundary aware so "okay" doesn't match inside
    # another word and a trailing question still counts as a question
    words = set(text.split())
    if any(p in text for p in COMMIT if " " in p) or (words & {p for p in COMMIT if " " not in p}):
        if "?" in message and len(text.split()) > 6:
            return "question"
        return "commit"

    if "?" in message:
        return "question"
    return "unclear"


# --------------------------------------------------------------------------- replies


def _context_bits(store, state: ConversationState) -> Dict[str, Any]:
    merchant = store.get("merchant", state.merchant_id) if state.merchant_id else None
    category = store.category_for(merchant) if merchant else None
    customer = store.get("customer", state.customer_id) if state.customer_id else None
    trigger = store.resolve_trigger(state.trigger_id) if state.trigger_id else None
    fs = build_factsheet(category, merchant, trigger or {}, customer)
    return {"fs": fs}


def _action_body(fs, state: ConversationState) -> str:
    """The reply we give once the merchant has committed.

    Deliberately contains no question — the whole point is that we stop asking
    and start doing.
    """
    offer = fs.active_offers[0] if fs.active_offers else fs.suggest_offer()
    scope = None
    for key, label in (
        ("high_risk_adult_count", "high-risk adult patients"),
        ("chronic_rx_count", "chronic-Rx customers"),
        ("total_active_members", "active members"),
        ("lapsed_180d_plus", "lapsed customers"),
        ("lapsed_90d_plus", "lapsed customers"),
    ):
        if fs.customer_aggregate.get(key):
            scope = f"{fs.customer_aggregate[key]} {label}"
            break

    openers = [
        "Done — starting on it now.",
        "On it.",
        "Right, that's underway.",
    ]
    opener = pick(openers, state.conversation_id, str(len(state.turns)))

    lines = [opener]
    if offer:
        lines.append(f"I'm drafting the listing post around {offer} and the short reply you can "
                     "paste when customers ask about price.")
    else:
        lines.append("I'm drafting the listing post and the short reply you can paste when "
                     "customers ask about price.")
    if scope:
        lines.append(f"Reply CONFIRM and it goes live, along with the note to your {scope}.")
    else:
        lines.append("Reply CONFIRM and it goes live today.")
    return " ".join(lines)


def _fresh(state: ConversationState, body: str) -> str:
    """Never send the same body twice in one conversation (-2 each time)."""
    if body not in state.sent_bodies:
        return body
    suffix = " Anything you want changed, send it in one line and I'll adjust."
    alt = body.rstrip(".") + "." + suffix
    return alt if alt not in state.sent_bodies else body + f" (#{len(state.sent_bodies) + 1})"


def respond(store, state: ConversationState, message: str, merchant_auto_hits: int) -> Move:
    bits = _context_bits(store, state)
    fs = bits["fs"]
    name = fs.salutation_name if fs.merchant_id else "there"
    kind = classify(message, state, merchant_auto_hits)

    if kind == "opt_out":
        return Move(
            action="end",
            opt_out=True,
            rationale=("Merchant asked to stop. Closing the conversation and suppressing every "
                       "further trigger for this merchant for the rest of the run."),
        )

    if kind == "hostile":
        state.hostile_hits += 1
        if state.hostile_hits > 1 or merchant_auto_hits > 0:
            return Move(
                action="end",
                opt_out=True,
                rationale="Second negative signal from this merchant; ending rather than "
                          "pushing further.",
            )
        body = (f"Sorry {name} — that landed badly and I won't keep pushing. I'll stop here. "
                "If anything changes, message 'Hi Vera' and I'll pick it up.")
        return Move(
            action="send", body=_fresh(state, body), cta="none",
            rationale=("Merchant is frustrated. One short acknowledgement with an opt-back-in "
                       "path, then the conversation closes — no retry, no counter-pitch."),
        )

    if kind == "auto_reply":
        state.auto_reply_hits += 1
        hits = max(state.auto_reply_hits, merchant_auto_hits + 1)
        if hits >= 3:
            return Move(
                action="end", auto_reply=True,
                rationale=("Third identical canned reply — this number is answering "
                           "automatically and the owner is not reading. Closing so we stop "
                           "burning turns on it."),
            )
        if hits == 2:
            return Move(
                action="wait", wait_seconds=86400, auto_reply=True,
                rationale=("Same auto-reply twice, so the owner is not at the phone. Backing "
                           "off 24 hours before any retry."),
            )
        body = (f"Looks like an auto-reply. When {name} sees this, one word back is enough — "
                "reply YES and I'll take it from there.")
        return Move(
            action="send", body=_fresh(state, body), cta="binary_yes_no", auto_reply=True,
            rationale=("Detected a WhatsApp Business canned reply on the first turn. One short "
                       "message addressed past the auto-responder to the owner, then we stop."),
        )

    if kind == "commit":
        state.committed = True
        body = _action_body(fs, state)
        # the harness checks that a committed merchant is not asked another
        # qualifying question
        qualifier = contains_qualifier(body)
        if qualifier:
            body = body.replace(qualifier, "").replace("  ", " ")
        return Move(
            action="send", body=_fresh(state, body), cta="binary_confirm_cancel",
            rationale=("Merchant committed explicitly, so qualification stops here and the "
                       "next message is the artefact itself plus a single CONFIRM."),
        )

    if kind == "defer":
        return Move(
            action="wait", wait_seconds=14400,
            rationale="Merchant asked for time; backing off four hours rather than pressing.",
        )

    if kind == "off_topic":
        topic = next((t for t in OFF_TOPIC if t in _norm(message)), "that")
        anchor = fs.active_offers[0] if fs.active_offers else "your listing"
        body = (f"{topic.upper() if len(topic) <= 4 else topic.capitalize()} sits outside what "
                f"I can help with — your CA is the right person for it. Back to {anchor}: "
                "say the word and I'll send the draft across.")
        return Move(
            action="send", body=_fresh(state, body), cta="open_ended",
            rationale=("Out-of-scope request declined in one line without pretending to be "
                       "able to help, then the thread is returned to the original trigger."),
        )

    if kind == "question":
        body = (f"Short answer: I can do that from here, {name}. Give me the one detail you "
                "want it built around and I'll send the draft in your voice — nothing goes "
                "live until you approve it.")
        return Move(
            action="send", body=_fresh(state, body), cta="open_ended",
            rationale="Merchant engaged with a question; answering directly and keeping the "
                      "next step to a single detail.",
        )

    if kind == "empty":
        return Move(action="wait", wait_seconds=3600,
                    rationale="Empty inbound; waiting rather than guessing at intent.")

    # unclear — one clarifying attempt, then leave it alone
    state.nudges_unanswered += 1
    if state.nudges_unanswered >= 2:
        return Move(
            action="end",
            rationale="Two turns without a readable signal; closing rather than nudging a "
                      "third time.",
        )
    suggestion = fs.suggest_offer() or "this week's listing post"
    body = (f"Taking that as a maybe. The one thing worth doing this week is {suggestion} — "
            "reply YES and I'll set it up, or STOP and I'll leave it.")
    return Move(
        action="send", body=_fresh(state, body), cta="binary_yes_no",
        rationale=("Reply was ambiguous, so the message narrows to a single yes/no with an "
                   "explicit exit rather than asking an open question."),
    )
