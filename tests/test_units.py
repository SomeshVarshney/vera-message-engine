"""Unit tests for the pieces that don't need a running server.

    python -m pytest tests/test_units.py -q
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import writers  # noqa: E402
from app.composer import compose  # noqa: E402
from app.conversation import ConversationState, classify  # noqa: E402
from app.facts import build_factsheet  # noqa: E402
from app.selector import SendLog, choose  # noqa: E402
from app.store import ContextStore, StaleVersion  # noqa: E402

_ROOT = Path(__file__).resolve().parents[1]
DATA = next((p for p in (_ROOT / "dataset", _ROOT.parent / "dataset") if p.exists()),
            _ROOT / "dataset")


@pytest.fixture(scope="module")
def loaded():
    store = ContextStore()
    for path in (DATA / "categories").glob("*.json"):
        item = json.load(open(path, encoding="utf-8"))
        store.put("category", item["slug"], 1, item)
    for m in json.load(open(DATA / "merchants_seed.json", encoding="utf-8"))["merchants"]:
        store.put("merchant", m["merchant_id"], 1, m)
    for c in json.load(open(DATA / "customers_seed.json", encoding="utf-8"))["customers"]:
        store.put("customer", c["customer_id"], 1, c)
    triggers = json.load(open(DATA / "triggers_seed.json", encoding="utf-8"))["triggers"]
    for t in triggers:
        store.put("trigger", t["id"], 1, t)
    return store, {t["id"]: t for t in triggers}


# ---------------------------------------------------------------------- store


def test_same_version_is_rejected():
    store = ContextStore()
    store.put("merchant", "m_1", 1, {"merchant_id": "m_1"})
    with pytest.raises(StaleVersion):
        store.put("merchant", "m_1", 1, {"merchant_id": "m_1"})


def test_higher_version_replaces():
    store = ContextStore()
    store.put("merchant", "m_1", 1, {"performance": {"views": 10}})
    store.put("merchant", "m_1", 2, {"performance": {"views": 99}})
    assert store.get("merchant", "m_1")["performance"]["views"] == 99


def test_unknown_scope():
    with pytest.raises(ValueError):
        ContextStore().put("banana", "x", 1, {})


# ---------------------------------------------------------------------- facts


def test_peer_gap_is_derived(loaded):
    store, triggers = loaded
    merchant = store.get("merchant", "m_001_drmeera_dentist_delhi")
    fs = build_factsheet(store.category_for(merchant), merchant,
                         triggers["trg_001_research_digest_dentists"])
    # 2.1% against a 3.0% peer median
    assert round(fs.ctr_gap_points, 2) == -0.90
    assert fs.signal_days("stale_posts") == 22
    assert fs.salutation_name == "Dr. Meera"


def test_offer_suggestion_skips_what_is_already_running(loaded):
    store, triggers = loaded
    merchant = store.get("merchant", "m_003_studio11_salon_hyderabad")
    fs = build_factsheet(store.category_for(merchant), merchant, {})
    suggestion = fs.suggest_offer()
    assert suggestion not in fs.active_offers
    assert "@" in suggestion  # service+price beats a bare discount


# ------------------------------------------------------------------ composing


def test_every_seed_trigger_composes_or_declines_cleanly(loaded):
    store, triggers = loaded
    for trigger in triggers.values():
        merchant = store.merchant_for(trigger)
        customer = store.get("customer", trigger.get("customer_id"))
        action = compose(store.category_for(merchant), merchant, trigger, customer)
        if action is None:
            continue
        assert action.body and action.cta and action.rationale
        assert "http" not in action.body
        assert "_id" not in action.body
        if trigger.get("scope") == "customer":
            assert action.send_as == "merchant_on_behalf"


def test_customer_trigger_without_customer_context_is_skipped(loaded):
    store, triggers = loaded
    trigger = triggers["trg_003_recall_due_priya"]
    merchant = store.merchant_for(trigger)
    assert compose(store.category_for(merchant), merchant, trigger, None) is None


def test_taboo_vocabulary_never_appears(loaded):
    store, triggers = loaded
    for trigger in triggers.values():
        merchant = store.merchant_for(trigger)
        category = store.category_for(merchant) or {}
        action = compose(category, merchant, trigger,
                         store.get("customer", trigger.get("customer_id")))
        if not action:
            continue
        for word in (category.get("voice") or {}).get("vocab_taboo", []):
            term = word.split("(")[0].strip().lower()
            assert term not in action.body.lower()


def test_unknown_trigger_kind_still_produces_something(loaded):
    store, triggers = loaded
    trigger = dict(triggers["trg_001_research_digest_dentists"])
    trigger["kind"] = "some_kind_invented_after_submission"
    trigger["payload"] = {"headline_metric": 0.34, "area": "Lajpat Nagar"}
    merchant = store.merchant_for(trigger)
    action = compose(store.category_for(merchant), merchant, trigger, None)
    assert action is not None
    assert "Lajpat Nagar" in action.body or "34%" in action.body


# ------------------------------------------------------------------- selector


def test_one_message_per_recipient_per_tick(loaded):
    store, triggers = loaded
    log = SendLog()
    log.tick_count = 1
    chosen = choose(list(triggers), store, log, "2026-04-26T10:00:00Z")
    recipients = [t["_recipient"] for t in chosen]
    assert len(recipients) == len(set(recipients))
    assert len(chosen) <= 20


def test_suppression_key_blocks_a_repeat(loaded):
    store, triggers = loaded
    log = SendLog()
    log.tick_count = 1
    first = choose(["trg_002_compliance_dci_radiograph"], store, log, "2026-04-26T10:00:00Z")
    assert first
    log.record(first[0]["merchant_id"], first[0]["suppression_key"])
    log.tick_count = 9
    assert choose(["trg_002_compliance_dci_radiograph"], store, log,
                  "2026-04-26T10:45:00Z") == []


def test_opted_out_merchant_gets_nothing(loaded):
    store, triggers = loaded
    log = SendLog()
    log.tick_count = 1
    log.opt_out("m_001_drmeera_dentist_delhi")
    assert choose(["trg_002_compliance_dci_radiograph"], store, log,
                  "2026-04-26T10:00:00Z") == []


def test_far_off_low_urgency_trigger_is_not_worth_sending(loaded):
    store, triggers = loaded
    log = SendLog()
    log.tick_count = 1
    # Diwali, 188 days out, urgency 1
    assert choose(["trg_006_festival_diwali"], store, log, "2026-04-26T10:00:00Z") == []


# --------------------------------------------------------------- conversation


@pytest.mark.parametrize("message,expected", [
    ("Thank you for contacting us! Our team will respond shortly.", "auto_reply"),
    ("Main ek automated assistant hoon", "auto_reply"),
    ("Not interested. Stop messaging me.", "opt_out"),
    ("This is useless spam", "hostile"),
    ("Ok lets do it. Whats next?", "commit"),
    ("haan kar do", "commit"),
    ("Can you also help me with my GST filing?", "off_topic"),
    ("Call me tomorrow, busy right now", "defer"),
    ("What does that cost me exactly and who pays for the photos?", "question"),
])
def test_inbound_classification(message, expected):
    assert classify(message, ConversationState("c"), 0) == expected


def test_identical_reply_twice_reads_as_an_auto_responder():
    state = ConversationState("c")
    state.turns.append({"role": "merchant", "message": "We are away from the desk"})
    assert classify("We are away from the desk", state, 0) == "auto_reply"


# ------------------------------------------------------------------- helpers


def test_date_and_price_helpers():
    assert writers.human_date("2026-05-12") == "12 May"
    assert writers.human_date("2026-11-08T18:00:00+05:30", with_year=True) == "8 Nov 2026"
    assert writers.price_of("Weekday Lunch Thali @ ₹149") == 149
    assert writers.price_of("3 FREE Trial Classes") is None
    assert writers.months_between("2026-05-12", "2026-11-12") == 6
    assert writers._readable_window("skin prep program 30day") == "30-day skin-prep programme"
