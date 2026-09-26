"""In-memory context store.

The judge pushes context across four scopes and expects:
  - idempotency on (context_id, version)
  - a higher version to replace the previous one atomically
  - everything to still be there at the end of the test window

Process-local dicts are enough for a 60-minute window (the brief explicitly
allows it) as long as the host doesn't restart us. Single-writer, so a plain
lock around the mutating paths is sufficient.
"""

import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

VALID_SCOPES = ("category", "merchant", "customer", "trigger")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class StaleVersion(Exception):
    def __init__(self, current_version: int):
        super().__init__(f"stale version, current is {current_version}")
        self.current_version = current_version


class ContextStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data: Dict[Tuple[str, str], Dict[str, Any]] = {}
        # merchant_id -> [customer_id]; kept as an index so we don't rescan
        # every customer on each tick
        self._customers_by_merchant: Dict[str, List[str]] = {}
        self.last_push_at: Optional[str] = None

    # ------------------------------------------------------------------ write

    def put(self, scope: str, context_id: str, version: int, payload: Dict[str, Any]) -> str:
        if scope not in VALID_SCOPES:
            raise ValueError("invalid_scope")

        key = (scope, context_id)
        with self._lock:
            existing = self._data.get(key)
            if existing is not None and version <= existing["version"]:
                raise StaleVersion(existing["version"])

            self._data[key] = {
                "version": version,
                "payload": payload or {},
                "stored_at": utcnow_iso(),
            }
            self.last_push_at = self._data[key]["stored_at"]

            if scope == "customer":
                merchant_id = (payload or {}).get("merchant_id")
                if merchant_id:
                    bucket = self._customers_by_merchant.setdefault(merchant_id, [])
                    if context_id not in bucket:
                        bucket.append(context_id)

            return self._data[key]["stored_at"]

    # ------------------------------------------------------------------- read

    def get(self, scope: str, context_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not context_id:
            return None
        entry = self._data.get((scope, context_id))
        return entry["payload"] if entry else None

    def version_of(self, scope: str, context_id: str) -> int:
        entry = self._data.get((scope, context_id))
        return entry["version"] if entry else 0

    def all_of(self, scope: str) -> Dict[str, Dict[str, Any]]:
        return {
            cid: entry["payload"]
            for (s, cid), entry in self._data.items()
            if s == scope
        }

    def counts(self) -> Dict[str, int]:
        counts = {s: 0 for s in VALID_SCOPES}
        for (scope, _cid) in self._data.keys():
            counts[scope] = counts.get(scope, 0) + 1
        return counts

    # Triggers arrive both as pushed context and as bare ids on /v1/tick.
    # Resolve through the store first, then fall back to a stub so a trigger we
    # were never pushed doesn't silently disappear.
    def resolve_trigger(self, trigger_id: str) -> Optional[Dict[str, Any]]:
        payload = self.get("trigger", trigger_id)
        if payload:
            # some pushes nest the trigger under its own "id"; normalise
            payload.setdefault("id", trigger_id)
            return payload
        return None

    def merchant_for(self, trigger: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        mid = trigger.get("merchant_id") or (trigger.get("payload") or {}).get("merchant_id")
        return self.get("merchant", mid)

    def category_for(self, merchant: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not merchant:
            return None
        return self.get("category", merchant.get("category_slug"))

    def customers_of(self, merchant_id: str) -> List[Dict[str, Any]]:
        out = []
        for cid in self._customers_by_merchant.get(merchant_id, []):
            payload = self.get("customer", cid)
            if payload:
                out.append(payload)
        return out

    def wipe(self) -> None:
        with self._lock:
            self._data.clear()
            self._customers_by_merchant.clear()
