"""HTTP surface the judge harness talks to.

Five endpoints, all of which have to answer inside the harness timeout even
when there is nothing useful to say. Everything expensive is avoided on the
request path: composition is pure string work over context we already hold, so
a tick with twenty actions still returns in single-digit milliseconds.
"""

import os
import time
from typing import Dict, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import composer, conversation, guards
from .conversation import ConversationState
from .models import ContextPush, ReplyRequest, TickRequest, TickResponse
from .selector import SendLog, choose
from .store import ContextStore, StaleVersion, utcnow_iso

APP_VERSION = "1.0.0"
STARTED_AT = time.time()

app = FastAPI(title="Vera message engine", version=APP_VERSION)

store = ContextStore()
send_log = SendLog()
conversations: Dict[str, ConversationState] = {}
auto_reply_hits_by_merchant: Dict[str, int] = {}

METADATA = {
    "team_name": os.getenv("VERA_TEAM_NAME", "Somesh Varshney"),
    "team_members": [m for m in os.getenv("VERA_TEAM_MEMBERS", "Somesh Varshney").split(",") if m],
    "model": os.getenv("VERA_MODEL_LABEL", "deterministic-composer/1.0 (no LLM in the send path)"),
    "approach": (
        "Rule-based composer grounded in the pushed contexts. Every trigger kind has its own "
        "writer; each writer may only read from a fact sheet built out of the four contexts, so "
        "the bot cannot state a number it was not given. A scoring layer decides which trigger "
        "is worth a message, capped at one per merchant per tick. Conversations run through an "
        "explicit state machine for auto-reply detection, intent handoff and graceful exit. An "
        "optional LLM rewrite pass is available behind an env flag and is rejected if it "
        "introduces any figure the grounded draft did not already contain."
    ),
    "contact_email": os.getenv("VERA_CONTACT_EMAIL", "someshvarshney23@gmail.com"),
    "version": APP_VERSION,
    "submitted_at": os.getenv("VERA_SUBMITTED_AT", "2026-09-26T00:00:00Z"),
}


@app.get("/")
def root():
    return {"service": "vera-message-engine", "version": APP_VERSION, "endpoints": [
        "POST /v1/context", "POST /v1/tick", "POST /v1/reply",
        "GET /v1/healthz", "GET /v1/metadata",
    ]}


@app.get("/v1/healthz")
def healthz():
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - STARTED_AT),
        "contexts_loaded": store.counts(),
    }


@app.get("/v1/metadata")
def metadata():
    return METADATA


@app.post("/v1/context")
def push_context(body: ContextPush):
    try:
        stored_at = store.put(body.scope, body.context_id, body.version, body.payload)
    except ValueError:
        return JSONResponse(
            status_code=400,
            content={"accepted": False, "reason": "invalid_scope",
                     "details": f"unknown scope {body.scope!r}"},
        )
    except StaleVersion as exc:
        return JSONResponse(
            status_code=409,
            content={"accepted": False, "reason": "stale_version",
                     "current_version": exc.current_version},
        )

    return {
        "accepted": True,
        "ack_id": f"ack_{body.context_id}_v{body.version}",
        "stored_at": stored_at,
    }


@app.post("/v1/tick", response_model=TickResponse)
def tick(body: TickRequest):
    send_log.tick_count += 1
    actions = []

    for trigger in choose(body.available_triggers, store, send_log, body.now):
        merchant = store.merchant_for(trigger)
        category = store.category_for(merchant) if merchant else None
        customer = store.get("customer", trigger.get("customer_id"))

        action = composer.compose(category, merchant, trigger, customer)
        if action is None:
            continue
        if guards.already_sent(action.conversation_id, action.body):
            continue

        guards.remember(action.conversation_id, action.body)
        send_log.record(trigger.get("_recipient") or action.merchant_id or "",
                        action.suppression_key)

        state = conversations.setdefault(
            action.conversation_id,
            ConversationState(conversation_id=action.conversation_id),
        )
        state.merchant_id = action.merchant_id
        state.customer_id = action.customer_id
        state.trigger_id = action.trigger_id
        state.sent_bodies.append(action.body)
        state.turns.append({"role": "bot", "message": action.body})

        actions.append(action)

    return TickResponse(actions=actions)


@app.post("/v1/reply")
def reply(body: ReplyRequest):
    state = conversations.get(body.conversation_id)
    if state is None:
        # the harness can open a conversation we never initiated (the replay
        # scenarios do exactly this) — treat it as a live thread, not an error
        state = ConversationState(
            conversation_id=body.conversation_id,
            merchant_id=body.merchant_id,
            customer_id=body.customer_id,
        )
        conversations[body.conversation_id] = state

    if body.merchant_id and not state.merchant_id:
        state.merchant_id = body.merchant_id
    if body.customer_id and not state.customer_id:
        state.customer_id = body.customer_id

    if state.closed:
        return {"action": "end", "rationale": "Conversation already closed on an earlier turn."}

    merchant_hits = auto_reply_hits_by_merchant.get(state.merchant_id or "", 0)
    move = conversation.respond(store, state, body.message, merchant_hits)

    state.turns.append({"role": body.from_role, "message": body.message})

    if move.auto_reply and state.merchant_id:
        auto_reply_hits_by_merchant[state.merchant_id] = merchant_hits + 1
    if move.opt_out:
        send_log.opt_out(state.merchant_id)

    if move.action == "end":
        state.closed = True
        send_log.closed_conversations.add(state.conversation_id)
        return {"action": "end", "rationale": move.rationale}

    if move.action == "wait":
        return {
            "action": "wait",
            "wait_seconds": move.wait_seconds or 3600,
            "rationale": move.rationale,
        }

    state.sent_bodies.append(move.body)
    state.turns.append({"role": "bot", "message": move.body})
    guards.remember(state.conversation_id, move.body)

    return {
        "action": "send",
        "body": move.body,
        "cta": move.cta,
        "rationale": move.rationale,
    }


@app.post("/v1/teardown")
def teardown():
    """Optional endpoint from the brief — wipe everything on request."""
    store.wipe()
    conversations.clear()
    auto_reply_hits_by_merchant.clear()
    guards.reset()
    send_log.used_suppression_keys.clear()
    send_log.last_tick_by_merchant.clear()
    send_log.opted_out_merchants.clear()
    send_log.closed_conversations.clear()
    send_log.tick_count = 0
    return {"wiped": True, "at": utcnow_iso()}
