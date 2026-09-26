#!/usr/bin/env python3
"""Offline contract check — no LLM key needed.

Runs the same sequence the judge harness does (warmup, context push,
idempotency, ticks, the three replay scenarios) and prints every composed
message so the copy can be read before anything is submitted.

    python tests/selftest.py [http://localhost:8080]
"""

import json
import re
import sys
from pathlib import Path
from urllib import error as urlerror, request as urlrequest

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080").rstrip("/")
ROOT = Path(__file__).resolve().parents[1]
# dataset/ ships with the challenge pack, so it may sit beside this project
# rather than inside it
DATASET = next((p for p in (ROOT / "dataset", ROOT.parent / "dataset") if p.exists()),
               ROOT / "dataset")

PASS, FAIL = [], []


def call(method, path, body=None, timeout=20):
    data = json.dumps(body).encode() if body is not None else None
    req = urlrequest.Request(f"{BASE}{path}", data=data, method=method,
                             headers={"Content-Type": "application/json"})
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urlerror.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


def check(label, ok, detail=""):
    (PASS if ok else FAIL).append(label)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))


def load():
    categories = {}
    for path in sorted((DATASET / "categories").glob("*.json")):
        item = json.load(open(path, encoding="utf-8"))
        categories[item["slug"]] = item
    merchants = {m["merchant_id"]: m
                 for m in json.load(open(DATASET / "merchants_seed.json", encoding="utf-8"))["merchants"]}
    customers = {c["customer_id"]: c
                 for c in json.load(open(DATASET / "customers_seed.json", encoding="utf-8"))["customers"]}
    triggers = {t["id"]: t
                for t in json.load(open(DATASET / "triggers_seed.json", encoding="utf-8"))["triggers"]}
    return categories, merchants, customers, triggers


def main():
    categories, merchants, customers, triggers = load()

    print("\n== warmup ==")
    status, health = call("GET", "/v1/healthz")
    check("healthz 200", status == 200 and health.get("status") == "ok")
    check("contexts empty at start", health.get("contexts_loaded", {}).get("merchant") == 0,
          json.dumps(health.get("contexts_loaded")))

    status, meta = call("GET", "/v1/metadata")
    check("metadata 200", status == 200 and bool(meta.get("team_name")))

    print("\n== context push ==")
    for slug, payload in categories.items():
        status, data = call("POST", "/v1/context",
                            {"scope": "category", "context_id": slug, "version": 1,
                             "payload": payload, "delivered_at": "2026-09-26T00:00:00Z"})
        if not data.get("accepted"):
            check(f"category/{slug}", False, json.dumps(data))
    for mid, payload in merchants.items():
        call("POST", "/v1/context", {"scope": "merchant", "context_id": mid,
                                     "version": 1, "payload": payload})
    for cid, payload in customers.items():
        call("POST", "/v1/context", {"scope": "customer", "context_id": cid,
                                     "version": 1, "payload": payload})
    for tid, payload in triggers.items():
        call("POST", "/v1/context", {"scope": "trigger", "context_id": tid,
                                     "version": 1, "payload": payload})

    status, health = call("GET", "/v1/healthz")
    loaded = health.get("contexts_loaded", {})
    check("all contexts stored",
          loaded == {"category": len(categories), "merchant": len(merchants),
                     "customer": len(customers), "trigger": len(triggers)},
          json.dumps(loaded))

    mid = list(merchants)[0]
    status, data = call("POST", "/v1/context", {"scope": "merchant", "context_id": mid,
                                                "version": 1, "payload": merchants[mid]})
    check("same version rejected", status == 409 and data.get("accepted") is False,
          f"status={status}")

    bumped = json.loads(json.dumps(merchants[mid]))
    bumped["performance"]["views"] = 2580
    status, data = call("POST", "/v1/context", {"scope": "merchant", "context_id": mid,
                                                "version": 2, "payload": bumped})
    check("higher version accepted", status == 200 and data.get("accepted") is True)

    status, data = call("POST", "/v1/context", {"scope": "nonsense", "context_id": "x",
                                                "version": 1, "payload": {}})
    check("bad scope rejected", status == 400)

    print("\n== ticks ==")
    required = {"conversation_id", "merchant_id", "send_as", "trigger_id", "template_name",
                "template_params", "body", "cta", "suppression_key", "rationale"}
    all_ids = list(triggers)
    seen, bodies = 0, []
    for i in range(0, len(all_ids), 5):
        batch = all_ids[i:i + 5]
        status, data = call("POST", "/v1/tick",
                            {"now": "2026-09-26T10:00:00Z", "available_triggers": batch})
        if status != 200:
            check(f"tick batch {i//5+1}", False, f"status={status}")
            continue
        for action in data.get("actions", []):
            seen += 1
            missing = required - set(action)
            if missing:
                check("action shape", False, f"missing {missing}")
            if re.search(r"https?://|www\.", action["body"]):
                check("no urls", False, action["body"][:60])
            bodies.append(action)

    check("bot sent something", seen > 0, f"{seen} actions")
    check("no duplicate bodies", len({b["body"] for b in bodies}) == len(bodies))

    print("\n== composed messages ==")
    for action in bodies:
        print(f"\n--- {action['trigger_id']}  [{action['send_as']} / {action['cta']}]")
        print(action["body"])
        print(f"    rationale: {action['rationale'][:150]}")

    print("\n== replay: auto-reply hell ==")
    canned = "Thank you for contacting us! Our team will respond shortly."
    ended = False
    for turn in range(1, 5):
        status, data = call("POST", "/v1/reply",
                            {"conversation_id": f"conv_auto_{turn}", "merchant_id": mid,
                             "customer_id": None, "from_role": "merchant", "message": canned,
                             "received_at": "2026-09-26T10:00:00Z", "turn_number": turn + 1})
        print(f"  turn {turn}: {data.get('action')} — {str(data.get('body', ''))[:70]}")
        if data.get("action") == "end":
            ended = True
            break
    check("ends on repeated auto-reply", ended)

    print("\n== replay: intent transition ==")
    status, data = call("POST", "/v1/reply",
                        {"conversation_id": "conv_intent_1", "merchant_id": mid,
                         "from_role": "merchant", "message": "Ok lets do it. Whats next?",
                         "received_at": "2026-09-26T10:00:00Z", "turn_number": 2})
    body = (data.get("body") or "").lower()
    print(f"  {data.get('action')}: {data.get('body')}")
    qualifying = ["would you", "do you", "can you tell", "what if", "how about"]
    actioning = ["done", "sending", "draft", "here", "confirm", "proceed", "next"]
    check("switches to action mode",
          any(w in body for w in actioning) and not any(w in body for w in qualifying))

    print("\n== replay: hostile ==")
    status, data = call("POST", "/v1/reply",
                        {"conversation_id": "conv_hostile", "merchant_id": mid,
                         "from_role": "merchant",
                         "message": "Stop messaging me. This is useless spam.",
                         "received_at": "2026-09-26T10:00:00Z", "turn_number": 2})
    print(f"  {data.get('action')}: {data.get('rationale')}")
    check("handles hostility",
          data.get("action") == "end"
          or any(w in (data.get("body") or "").lower() for w in ("sorry", "apolog", "won't")))

    print("\n== replay: off-topic ==")
    status, data = call("POST", "/v1/reply",
                        {"conversation_id": "conv_offtopic", "merchant_id": list(merchants)[2],
                         "from_role": "merchant",
                         "message": "Btw can you also help me with my GST filing this month?",
                         "received_at": "2026-09-26T10:00:00Z", "turn_number": 2})
    print(f"  {data.get('action')}: {data.get('body')}")
    check("stays on mission", data.get("action") == "send")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for label in FAIL:
            print(f"  failed: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
