# Vera message engine

A stateful HTTP service that decides what Vera should say to a merchant next, and says it.

Five endpoints (`/v1/context`, `/v1/tick`, `/v1/reply`, `/v1/healthz`, `/v1/metadata`), FastAPI,
no database, no model call on the request path.

---

## Approach

**Everything is grounded in a fact sheet.** Each composition starts by flattening the four
contexts into a single `FactSheet` (`app/facts.py`). Writers may read from that object and
nothing else. There is no path by which a message can contain a number the judge did not push,
because the only numbers in scope are the ones that were pushed.

**One writer per trigger kind.** `app/writers.py` holds a writer for each kind in the dataset
plus a grounded fallback for kinds that arrive after submission. Each writer builds the same
shape: *why now, with a real figure → what it means for this business, with one of their own
figures → one low-friction next step*. Category voice decides the vocabulary; a dentist gets
"fluoride varnish" and a source citation, a restaurant owner gets "covers" and a delivery banner.

**A selector decides whether to send at all.** `app/selector.py` scores every live trigger on
kind weight, stated urgency, whether it corroborates a signal already on the account, deadline
proximity and severity. Below threshold, nothing goes out. Each recipient gets at most one
message per tick with a three-tick cooldown, suppression keys are honoured, and the whole tick
is capped at twenty actions. A hundred live triggers is not a hundred messages.

**Conversations run on an explicit state machine,** not a prompt (`app/conversation.py`). It
classifies each inbound turn as auto-reply, opt-out, hostile, commitment, deferral, off-topic,
question or unclear, and routes to `send`, `wait` or `end`. The three failure modes the brief
calls out are handled directly: a canned WhatsApp Business reply gets one message addressed past
the auto-responder, then a 24h back-off, then a close; an explicit commitment switches the bot
out of question-asking and into handing over a draft; an opt-out ends the conversation and
suppresses that merchant for the rest of the run.

**Guards run on every outbound** (`app/guards.py`): URLs stripped, category taboo vocabulary
scrubbed, internal field names rewritten into merchant language, and a per-conversation body
hash so the same text never goes out twice.

---

## Tradeoffs

**No LLM in the send path, on purpose.** The obvious build is a prompt per message. I didn't do
that, for three reasons. A tick can carry twenty actions inside a thirty-second budget, and
twenty sequential model calls is the easiest way to lose that budget; a timeout or a rate limit
mid-window costs operational penalties that no amount of copy quality earns back; and
determinism was an explicit requirement, which `temperature=0` approximates and not calling a
model guarantees. A full tick over a hundred triggers returns in about 7 ms.

The cost is real: the copy is built from templates, so it cannot surprise you. I've spent the
saved budget on grounding instead — peer comparisons, cohort matching, derived gaps and per-kind
judgement calls — which is where the rubric puts the marks anyway.

An optional rewrite pass exists behind `VERA_LLM_ENABLED` (`app/llm.py`). It may only rephrase
an already-grounded draft, and its output is discarded if it contains any figure the draft did
not. It is off by default.

**Expiry is a demotion, not a veto.** `available_triggers` is the judge saying these are live
right now. If our clock disagrees with a stale `expires_at`, we deprioritise rather than go
silent — being quiet for a whole test window is a much worse failure than sending something
slightly late.

**We offer derivations instead of asserting them.** The pharmacy recall message is the clearest
case. Stating "22 of your customers took the recalled batch" would score well and would also be
invented — that count isn't in any pushed context. The message instead says there are 240
chronic-Rx customers on file (which *is* in context) and offers to filter the list. Same
specificity, no fabrication.

**In-memory state, single worker.** The brief permits it and the window is sixty minutes. It
does mean a restart loses everything, so the container runs one worker and the health check is
the only thing keeping it warm.

---

## What extra context would have helped most

1. **Merchant calendar and open slots.** Every customer-facing message wants a real slot to
   offer. Today only `recall_due` carries them in its payload, so the rest have to ask for a time
   instead of proposing one, which measurably lowers reply rates.
2. **Per-customer dispensing and service line items.** The recall case needs to know which
   customers received which batch. With that, the message names a count instead of offering to
   go and find one.
3. **Outcome feedback on past sends.** `conversation_history` records that a merchant replied,
   but not which lever caused it. Reply rates per lever per category would let the selector
   learn its weights instead of using the ones I set by hand.
4. **Language actually used, not just declared.** `identity.languages` lists what the merchant
   can read; the conversation history shows what they write in. The second one is what should
   drive code-mixing, and it's only available for merchants who have already replied.

---

## Running it

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Offline contract check — exercises warmup, idempotency, version bumps, ticks and all three
replay scenarios, and prints every composed message. No API key needed:

```bash
python tests/selftest.py http://localhost:8080
```

Official harness — set `BOT_URL`, `LLM_PROVIDER` and `LLM_API_KEY` at the top of the file:

```bash
python judge_simulator.py
```

Deploy with the included `Dockerfile` (or `render.yaml` on Render). Set `VERA_TEAM_NAME`,
`VERA_TEAM_MEMBERS` and `VERA_CONTACT_EMAIL` so `/v1/metadata` reports correctly.

## Layout

```
app/
  main.py          five endpoints, in-process state
  store.py         versioned context store, idempotent on (id, version)
  facts.py         the four contexts flattened into one grounded fact sheet
  writers.py       one composer per trigger kind + grounded fallback
  selector.py      what is worth sending, and what is not
  conversation.py  auto-reply, intent handoff, hostile, off-topic, exit
  guards.py        URLs, taboo vocabulary, jargon, anti-repetition
  voice.py         salutation, code-mixing, citation, tone helpers
  llm.py           optional rewrite pass, off by default
tests/selftest.py  offline end-to-end check against a running instance
```
