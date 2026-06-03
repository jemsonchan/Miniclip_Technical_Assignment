"""Data model + loaders + the derived view that checks reason over.

Design note: we deliberately do NOT parse the config into strict dataclasses.
Real configs are "intentionally informal -- fields may be missing, extra,
malformed, or contradictory." If we forced everything through a schema at load
time we'd turn the tool into the JSON-schema validator the brief explicitly does
not want, and we'd crash on exactly the malformed inputs we're meant to flag.

So the config and events stay as plain dicts/lists. Every check accesses them
defensively (.get with defaults) and is responsible for reporting bad shape as a
Finding rather than throwing. The Context class below pre-computes the few
cross-cutting views (per-user timeline, assignment counts) that almost every
dynamic check needs, so we don't re-walk the stream in each check.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# Severity ladder. ERROR is the CI gate -- its presence sets a non-zero exit
# code. WARN is "a human should look before this ships". INFO is context the
# tool surfaces but does not block on (e.g. "sample too small to judge drift").
ERROR = "ERROR"
WARN = "WARN"
INFO = "INFO"

CATEGORY_STATIC = "static"
CATEGORY_DYNAMIC = "dynamic"
CATEGORY_FORENSIC = "forensic"

_SEVERITY_ORDER = {ERROR: 0, WARN: 1, INFO: 2}


@dataclass
class Finding:
    check_id: str
    category: str
    severity: str
    title: str
    detail: str
    evidence: List[Any] = field(default_factory=list)  # small sample, not the firehose
    count: int = 1  # how many times this issue occurred (events / groups / users)

    def to_dict(self) -> dict:
        return {
            "check_id": self.check_id,
            "category": self.category,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "count": self.count,
            "evidence": self.evidence[:5],
        }


@dataclass
class LoadResult:
    data: Any
    errors: List[Finding] = field(default_factory=list)


def load_config(path: str) -> LoadResult:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return LoadResult(json.load(fh))
    except FileNotFoundError:
        return LoadResult({}, [_load_error("config.missing", f"Config file not found: {path}")])
    except json.JSONDecodeError as e:
        return LoadResult({}, [_load_error("config.invalid_json", f"Config is not valid JSON: {e}")])


def load_events(path: str) -> LoadResult:
    """Parse JSONL tolerantly. A malformed line is a finding, not a crash."""
    events: List[dict] = []
    errors: List[Finding] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    events.append(json.loads(raw))
                except json.JSONDecodeError as e:
                    errors.append(
                        _load_error(
                            "events.bad_line",
                            f"Event stream line {lineno} is not valid JSON: {e}",
                        )
                    )
    except FileNotFoundError:
        errors.append(_load_error("events.missing", f"Event file not found: {path}"))
    return LoadResult(events, errors)


def _load_error(check_id: str, msg: str) -> Finding:
    return Finding(check_id, CATEGORY_STATIC, ERROR, "Input could not be loaded", msg)


class Context:
    """Everything a check might need, computed once.

    Checks should treat this as read-only. Naming conventions for events are
    documented in the README; if a field is absent we leave it None and let the
    relevant check decide whether that absence is itself a problem.
    """

    def __init__(self, config: dict, events: List[dict]):
        self.config = config or {}
        self.events = events or []

        self.experiment: dict = self.config.get("experiment", {}) or {}
        self.economy: dict = self.config.get("economy", {}) or {}
        self.defaults: dict = self.config.get("defaults", {}) or {}

        # --- config-derived views ---
        self.groups: List[dict] = self.experiment.get("groups", []) or []
        self.group_ids: List[str] = [g.get("id") for g in self.groups if isinstance(g, dict) and g.get("id")]
        self.weights: Dict[str, float] = {
            g.get("id"): g.get("weight")
            for g in self.groups
            if isinstance(g, dict) and g.get("id") is not None
        }
        self.segment_rules: List[dict] = self.experiment.get("segment_rules", []) or []
        self.sticky: bool = bool(self.experiment.get("sticky", True))

        # --- runtime-derived views ---
        # per user: ordered list of their events, plus the groups they were
        # exposed to and whether they ever transacted.
        self.by_user: Dict[str, List[dict]] = {}
        self.exposures: List[dict] = []
        self.transactions: List[dict] = []
        for ev in self.events:
            if not isinstance(ev, dict):
                continue
            uid = ev.get("user_id")
            if uid is not None:
                self.by_user.setdefault(uid, []).append(ev)
            etype = ev.get("event")
            if etype == "exposure":
                self.exposures.append(ev)
            elif etype == "economy_transaction":
                self.transactions.append(ev)

        # stable ordering by timestamp where present (None sorts last)
        for uid, evs in self.by_user.items():
            evs.sort(key=lambda e: (e.get("ts") is None, e.get("ts")))

        # First-exposure group per user, computed once here rather than by
        # re-walking each user's events on every call. Several checks ask for it
        # (assignment_counts, transaction_mismatch, ...); memoising keeps the
        # work O(events) instead of O(events x checks).
        self._first_group: Dict[str, Optional[str]] = {
            uid: next((e.get("group") for e in evs if e.get("event") == "exposure"), None)
            for uid, evs in self.by_user.items()
        }

    # ---- helpers shared by checks ----

    def effective_economy_for_group(self, group_id: Optional[str]) -> Dict[str, dict]:
        """Item table for a group, applying per-group overrides over the base."""
        base = {it.get("id"): it for it in self.economy.get("items", []) or [] if isinstance(it, dict)}
        merged = {k: dict(v) for k, v in base.items()}
        overrides = (self.economy.get("per_group_overrides", {}) or {}).get(group_id, {}) or {}
        for item_id, ov in (overrides.get("items", {}) or {}).items():
            merged.setdefault(item_id, {"id": item_id})
            for k, v in (ov or {}).items():
                merged[item_id][k] = v
        return merged

    def first_exposure_group(self, uid: str) -> Optional[str]:
        return self._first_group.get(uid)

    def assignment_counts(self) -> Dict[str, int]:
        """One vote per user, using their first exposure (sticky semantics)."""
        counts: Dict[str, int] = {}
        for uid in self.by_user:
            g = self.first_exposure_group(uid)
            if g is not None:
                counts[g] = counts.get(g, 0) + 1
        return counts


def sort_findings(findings: List[Finding]) -> List[Finding]:
    return sorted(
        findings,
        key=lambda f: (_SEVERITY_ORDER.get(f.severity, 9), f.category, f.check_id),
    )
