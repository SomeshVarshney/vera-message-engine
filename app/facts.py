"""Pull every usable fact out of the four contexts, once, up front.

The whole bot runs on one rule: if a number or a claim isn't in here, it does
not go in the message. Writers read from a FactSheet and never from raw dicts,
so there's exactly one place where "did we actually get told this?" is decided.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def pct(value: Optional[float], digits: int = 0) -> Optional[str]:
    """0.38 -> '38%'. Returns None when the input isn't a number."""
    if value is None:
        return None
    return f"{value * 100:.{digits}f}%"


def signed_pct(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    return f"{abs(value) * 100:.0f}%"


def inr(value: Any) -> Optional[str]:
    n = _num(value)
    if n is None:
        # some payloads carry the amount as a string already
        if isinstance(value, str) and value.strip():
            return value if value.strip().startswith("₹") else f"₹{value.strip()}"
        return None
    if n == int(n):
        return f"₹{int(n):,}"
    return f"₹{n:,.2f}"


@dataclass
class FactSheet:
    # identity
    merchant_id: str = ""
    business_name: str = ""
    owner_first_name: str = ""
    city: str = ""
    locality: str = ""
    verified: Optional[bool] = None
    languages: List[str] = field(default_factory=list)
    established_year: Optional[int] = None

    # commercial state
    sub_status: str = ""
    sub_plan: str = ""
    days_remaining: Optional[int] = None
    days_since_expiry: Optional[int] = None

    # performance
    window_days: Optional[int] = None
    views: Optional[int] = None
    calls: Optional[int] = None
    directions: Optional[int] = None
    leads: Optional[int] = None
    ctr: Optional[float] = None
    views_delta_7d: Optional[float] = None
    calls_delta_7d: Optional[float] = None

    # peers
    peer_ctr: Optional[float] = None
    peer_views: Optional[int] = None
    peer_calls: Optional[int] = None
    peer_rating: Optional[float] = None
    peer_reviews: Optional[int] = None
    peer_post_freq_days: Optional[int] = None
    peer_scope: str = ""

    # offers
    active_offers: List[str] = field(default_factory=list)
    expired_offers: List[str] = field(default_factory=list)
    catalog_offers: List[str] = field(default_factory=list)

    # roster
    customer_aggregate: Dict[str, Any] = field(default_factory=dict)
    signals: List[str] = field(default_factory=list)
    signal_values: Dict[str, str] = field(default_factory=dict)
    review_themes: List[Dict[str, Any]] = field(default_factory=list)

    # category knowledge
    category_slug: str = ""
    category_name: str = ""
    digest: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    digest_order: List[str] = field(default_factory=list)
    content_library: List[Dict[str, Any]] = field(default_factory=list)
    seasonal_beats: List[Dict[str, Any]] = field(default_factory=list)
    trend_signals: List[Dict[str, Any]] = field(default_factory=list)
    journals: List[str] = field(default_factory=list)
    authorities: List[str] = field(default_factory=list)
    vocab_allowed: List[str] = field(default_factory=list)
    vocab_taboo: List[str] = field(default_factory=list)
    tone: str = ""
    register: str = ""

    # conversation
    last_merchant_message: str = ""
    last_vera_message: str = ""
    last_engagement: str = ""
    history_len: int = 0

    # customer (optional)
    customer_id: str = ""
    customer_name: str = ""
    customer_language: str = ""
    customer_state: str = ""
    visits_total: Optional[int] = None
    last_visit: str = ""
    first_visit: str = ""
    services_received: List[str] = field(default_factory=list)
    preferred_slots: str = ""
    consent_scope: List[str] = field(default_factory=list)
    customer_extra: Dict[str, Any] = field(default_factory=dict)

    # trigger
    trigger_id: str = ""
    trigger_kind: str = ""
    trigger_scope: str = "merchant"
    trigger_source: str = ""
    urgency: int = 1
    suppression_key: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------ derivations

    @property
    def salutation_name(self) -> str:
        """What we actually open the message with."""
        if self.owner_first_name:
            if self.category_slug == "dentists":
                return f"Dr. {self.owner_first_name}"
            return self.owner_first_name
        return self.business_name or "there"

    @property
    def short_business_name(self) -> str:
        return self.business_name or "your listing"

    @property
    def place(self) -> str:
        if self.locality and self.city:
            return f"{self.locality}, {self.city}"
        return self.locality or self.city or ""

    @property
    def speaks_hindi(self) -> bool:
        return "hi" in [str(x).lower() for x in self.languages]

    @property
    def ctr_gap_points(self) -> Optional[float]:
        """Percentage points between this merchant and the peer median."""
        if self.ctr is None or self.peer_ctr is None:
            return None
        return (self.ctr - self.peer_ctr) * 100

    @property
    def calls_gap(self) -> Optional[int]:
        if self.calls is None or self.peer_calls is None:
            return None
        return int(self.peer_calls - self.calls)

    def signal_days(self, prefix: str) -> Optional[int]:
        """'stale_posts:22d' -> 22"""
        raw = self.signal_values.get(prefix)
        if not raw:
            return None
        digits = "".join(ch for ch in raw if ch.isdigit())
        return int(digits) if digits else None

    def has_signal(self, name: str) -> bool:
        return any(s == name or s.startswith(name + ":") for s in self.signals)

    def digest_item(self, item_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if item_id and item_id in self.digest:
            return self.digest[item_id]
        return None

    def digest_by_kind(self, *kinds: str) -> Optional[Dict[str, Any]]:
        for item_id in self.digest_order:
            if self.digest[item_id].get("kind") in kinds:
                return self.digest[item_id]
        return None

    def newest_digest(self) -> Optional[Dict[str, Any]]:
        """Last pushed item wins — context versions replace wholesale, and the
        judge appends new research to the end of the list."""
        if not self.digest_order:
            return None
        return self.digest[self.digest_order[-1]]

    def suggest_offer(self) -> Optional[str]:
        """Best catalog offer this merchant isn't already running.

        Service+price beats a bare discount (the brief is explicit about this),
        so anything with an '@' in the title sorts first.
        """
        active = {o.lower() for o in self.active_offers}
        pool = [o for o in self.catalog_offers if o.lower() not in active]
        if not pool:
            return None
        priced = [o for o in pool if "@" in o]
        free = [o for o in pool if o.lower().startswith("free")]
        return (priced or free or pool)[0]

    def top_review_theme(self, sentiment: Optional[str] = None) -> Optional[Dict[str, Any]]:
        themes = self.review_themes
        if sentiment:
            themes = [t for t in themes if t.get("sentiment") == sentiment]
        if not themes:
            return None
        return sorted(themes, key=lambda t: t.get("occurrences_30d") or 0, reverse=True)[0]


def build_factsheet(
    category: Optional[Dict[str, Any]],
    merchant: Optional[Dict[str, Any]],
    trigger: Optional[Dict[str, Any]],
    customer: Optional[Dict[str, Any]] = None,
) -> FactSheet:
    category = category or {}
    merchant = merchant or {}
    trigger = trigger or {}

    fs = FactSheet()

    identity = merchant.get("identity") or {}
    fs.merchant_id = merchant.get("merchant_id", "")
    fs.business_name = identity.get("name", "")
    fs.owner_first_name = identity.get("owner_first_name") or identity.get("owner_name") or ""
    fs.city = identity.get("city", "")
    fs.locality = identity.get("locality", "")
    fs.verified = identity.get("verified")
    fs.languages = list(identity.get("languages") or [])
    fs.established_year = identity.get("established_year")

    sub = merchant.get("subscription") or {}
    fs.sub_status = sub.get("status", "")
    fs.sub_plan = sub.get("plan", "")
    fs.days_remaining = sub.get("days_remaining")
    fs.days_since_expiry = sub.get("days_since_expiry")

    perf = merchant.get("performance") or {}
    fs.window_days = perf.get("window_days")
    fs.views = perf.get("views")
    fs.calls = perf.get("calls")
    fs.directions = perf.get("directions")
    fs.leads = perf.get("leads")
    fs.ctr = _num(perf.get("ctr"))
    delta = perf.get("delta_7d") or {}
    fs.views_delta_7d = _num(delta.get("views_pct"))
    fs.calls_delta_7d = _num(delta.get("calls_pct"))

    peer = category.get("peer_stats") or {}
    fs.peer_ctr = _num(peer.get("avg_ctr"))
    fs.peer_views = peer.get("avg_views_30d")
    fs.peer_calls = peer.get("avg_calls_30d")
    fs.peer_rating = peer.get("avg_rating")
    fs.peer_reviews = peer.get("avg_review_count")
    fs.peer_post_freq_days = peer.get("avg_post_freq_days")
    fs.peer_scope = peer.get("scope", "")

    for offer in merchant.get("offers") or []:
        title = offer.get("title")
        if not title:
            continue
        if offer.get("status") == "active":
            fs.active_offers.append(title)
        else:
            fs.expired_offers.append(title)

    fs.catalog_offers = [
        o.get("title") for o in (category.get("offer_catalog") or []) if o.get("title")
    ]

    fs.customer_aggregate = dict(merchant.get("customer_aggregate") or {})
    fs.signals = list(merchant.get("signals") or [])
    for signal in fs.signals:
        if ":" in signal:
            name, _, value = signal.partition(":")
            fs.signal_values[name] = value
    fs.review_themes = list(merchant.get("review_themes") or [])

    fs.category_slug = category.get("slug") or merchant.get("category_slug") or ""
    fs.category_name = category.get("display_name") or fs.category_slug.title()
    for item in category.get("digest") or []:
        item_id = item.get("id") or item.get("title")
        if not item_id:
            continue
        fs.digest[item_id] = item
        fs.digest_order.append(item_id)
    fs.content_library = list(category.get("patient_content_library") or [])
    fs.seasonal_beats = list(category.get("seasonal_beats") or [])
    fs.trend_signals = list(category.get("trend_signals") or [])
    fs.journals = list(category.get("professional_journals") or [])
    fs.authorities = list(category.get("regulatory_authorities") or [])

    voice = category.get("voice") or {}
    fs.vocab_allowed = list(voice.get("vocab_allowed") or [])
    fs.vocab_taboo = list(voice.get("vocab_taboo") or [])
    fs.tone = voice.get("tone", "")
    fs.register = voice.get("register", "")

    history = merchant.get("conversation_history") or []
    fs.history_len = len(history)
    for turn in reversed(history):
        sender = (turn.get("from") or "").lower()
        if sender == "merchant" and not fs.last_merchant_message:
            fs.last_merchant_message = turn.get("body", "")
            fs.last_engagement = turn.get("engagement", "")
        elif sender == "vera" and not fs.last_vera_message:
            fs.last_vera_message = turn.get("body", "")

    if customer:
        cid = customer.get("identity") or {}
        rel = customer.get("relationship") or {}
        prefs = customer.get("preferences") or {}
        fs.customer_id = customer.get("customer_id", "")
        fs.customer_name = cid.get("name", "")
        fs.customer_language = cid.get("language_pref", "")
        fs.customer_state = customer.get("state", "")
        fs.visits_total = rel.get("visits_total")
        fs.last_visit = rel.get("last_visit", "")
        fs.first_visit = rel.get("first_visit", "")
        fs.services_received = list(rel.get("services_received") or [])
        fs.preferred_slots = prefs.get("preferred_slots", "")
        fs.consent_scope = list((customer.get("consent") or {}).get("scope") or [])
        fs.customer_extra = {**rel, **prefs, **cid}

    fs.trigger_id = trigger.get("id", "")
    fs.trigger_kind = trigger.get("kind", "")
    fs.trigger_scope = trigger.get("scope", "merchant")
    fs.trigger_source = trigger.get("source", "")
    fs.urgency = trigger.get("urgency") or 1
    fs.suppression_key = trigger.get("suppression_key") or f"{fs.trigger_kind}:{fs.merchant_id}"
    fs.payload = dict(trigger.get("payload") or {})

    return fs
