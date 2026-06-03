"""Static checks: is the contract internally consistent and satisfiable?

These need only the config. They answer "can this experiment even work?" before
a single player sees it. Cheap to run, so they belong in pre-merge CI.
"""

from typing import List

from ..model import Context, Finding, ERROR, WARN, INFO, CATEGORY_STATIC
from . import register

C = CATEGORY_STATIC


@register("static.weights_sum", C)
def weights_sum(ctx: Context) -> List[Finding]:
    weights = [w for w in ctx.weights.values() if isinstance(w, (int, float))]
    if not weights:
        return []
    total = sum(weights)
    for target in (1.0, 100.0):  # accept fraction or percentage convention
        if abs(total - target) < 1e-6:
            return []
    return [
        Finding(
            "static.weights_sum", C, ERROR,
            "Group weights do not sum to a whole",
            f"Weights sum to {total:g}; expected 1.0 or 100. A bucketing layer that "
            f"normalises silently will hide this, but one that doesn't will leave a "
            f"slice of traffic unassigned or double-counted.",
            evidence=[ctx.weights],
        )
    ]


@register("static.zero_weight_group", C)
def zero_weight_group(ctx: Context) -> List[Finding]:
    out = []
    for gid, w in ctx.weights.items():
        if isinstance(w, (int, float)) and w <= 0:
            out.append(
                Finding(
                    "static.zero_weight_group", C, WARN,
                    "Group can never be assigned",
                    f"Group '{gid}' has weight {w}. It will collect zero traffic, so any "
                    f"variant logic behind it is dead. If that's intentional (kill-switched), "
                    f"say so; if not, it's a typo that silently disables a test arm.",
                    evidence=[{"group": gid, "weight": w}],
                )
            )
    return out


@register("static.segment_unknown_group", C)
def segment_unknown_group(ctx: Context) -> List[Finding]:
    out = []
    known = set(ctx.group_ids)
    for rule in ctx.segment_rules:
        if not isinstance(rule, dict):
            continue
        g = rule.get("group")
        if g is not None and g not in known:
            out.append(
                Finding(
                    "static.segment_unknown_group", C, ERROR,
                    "Segment rule targets a non-existent group",
                    f"Rule targets group '{g}', which is not defined in experiment.groups "
                    f"({sorted(known)}). Users matching this rule have nowhere valid to go.",
                    evidence=[rule],
                )
            )
    return out


@register("static.empty_audience", C)
def empty_audience(ctx: Context) -> List[Finding]:
    """A require-block that no real user can satisfy => a dead test arm."""
    out = []
    for rule in ctx.segment_rules:
        if not isinstance(rule, dict):
            continue
        req = rule.get("require", {}) or {}
        g = rule.get("group")
        reasons = []
        lo, hi = req.get("min_level"), req.get("max_level")
        if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and lo > hi:
            reasons.append(f"min_level {lo} > max_level {hi}")
        for key, val in req.items():
            if isinstance(val, list) and len(val) == 0:
                reasons.append(f"'{key}' is an empty allow-list (matches nobody)")
        excl = rule.get("exclude", {}) or {}
        for key, inc in req.items():
            if isinstance(inc, list) and isinstance(excl.get(key), list):
                if inc and set(inc).issubset(set(excl[key])):
                    reasons.append(f"every allowed '{key}' value is also excluded")
        if reasons:
            out.append(
                Finding(
                    "static.empty_audience", C, ERROR,
                    "Segment rule carves out an empty audience",
                    f"Group '{g}' can match no one: {'; '.join(reasons)}.",
                    evidence=[rule],
                )
            )
    return out


def _rule_is_dead(rule: dict) -> bool:
    """True if this rule's require-block already matches nobody (reported by
    static.empty_audience), so it shouldn't also be paired up in overlap analysis."""
    req = rule.get("require", {}) or {}
    lo, hi = req.get("min_level"), req.get("max_level")
    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and lo > hi:
        return True
    for val in req.values():
        if isinstance(val, list) and len(val) == 0:
            return True
    return False


def _requires_can_overlap(ra: dict, rb: dict) -> bool:
    """Conservative: assume overlap unless we can prove the constraints are
    disjoint on some shared list-valued key."""
    for key in set(ra) & set(rb):
        va, vb = ra[key], rb[key]
        if isinstance(va, list) and isinstance(vb, list):
            if not (set(va) & set(vb)):
                return False  # disjoint allow-lists on this key => cannot both match
    return True


@register("static.segment_overlap", C)
def segment_overlap(ctx: Context) -> List[Finding]:
    """Two live rules that can both match the same user but send them to
    different groups => the assignment is order-dependent and ambiguous."""
    out = []
    known = set(ctx.group_ids)
    rules = [
        r for r in ctx.segment_rules
        if isinstance(r, dict) and r.get("group") in known and not _rule_is_dead(r)
    ]
    for i in range(len(rules)):
        for j in range(i + 1, len(rules)):
            a, b = rules[i], rules[j]
            if a.get("group") == b.get("group"):
                continue
            if _requires_can_overlap(a.get("require", {}) or {}, b.get("require", {}) or {}):
                out.append(
                    Finding(
                        "static.segment_overlap", C, WARN,
                        "Two segment rules can match the same user",
                        f"Rules for '{a.get('group')}' and '{b.get('group')}' have compatible "
                        f"requirements, so a user satisfying both gets an order-dependent "
                        f"assignment. Make the rules mutually exclusive or define explicit priority.",
                        evidence=[a, b],
                    )
                )
    return out


@register("static.economy_unknown_currency", C)
def economy_unknown_currency(ctx: Context) -> List[Finding]:
    out = []
    currencies = set(ctx.economy.get("currencies", []) or [])
    if not currencies:
        return []
    for it in ctx.economy.get("items", []) or []:
        if not isinstance(it, dict):
            continue
        for face in ("cost", "reward"):
            for cur in (it.get(face, {}) or {}):
                if cur not in currencies:
                    out.append(
                        Finding(
                            "static.economy_unknown_currency", C, ERROR,
                            "Economy item uses an undeclared currency",
                            f"Item '{it.get('id')}' has a {face} in '{cur}', which is not in "
                            f"economy.currencies {sorted(currencies)}. The client will either "
                            f"crash or silently drop the transaction.",
                            evidence=[{"item": it.get("id"), "face": face, "currency": cur}],
                        )
                    )
    return out


@register("static.economy_free_or_negative", C)
def economy_free_or_negative(ctx: Context) -> List[Finding]:
    """Non-positive costs and the classic free-loop: an item whose reward in a
    currency meets or exceeds its cost in that same currency => infinite money."""
    out = []
    for it in ctx.economy.get("items", []) or []:
        if not isinstance(it, dict):
            continue
        cost = it.get("cost", {}) or {}
        reward = it.get("reward", {}) or {}
        for cur, amt in cost.items():
            if isinstance(amt, (int, float)) and amt <= 0:
                out.append(
                    Finding(
                        "static.economy_free_or_negative", C, WARN,
                        "Item is free or has a negative cost",
                        f"Item '{it.get('id')}' costs {amt} {cur}. A non-positive cost lets "
                        f"players acquire it for free (or be paid to take it).",
                        evidence=[it],
                    )
                )
        for cur in set(cost) & set(reward):
            c, r = cost.get(cur), reward.get(cur)
            if isinstance(c, (int, float)) and isinstance(r, (int, float)) and r >= c:
                out.append(
                    Finding(
                        "static.economy_free_or_negative", C, ERROR,
                        "Item is a net-positive currency loop",
                        f"Item '{it.get('id')}' costs {c} {cur} but rewards {r} {cur}. "
                        f"Buying it repeatedly mints currency -- a money printer.",
                        evidence=[it],
                    )
                )
    return out


@register("static.conversion_loop", C)
def conversion_loop(ctx: Context) -> List[Finding]:
    """Round-tripping currencies through conversions should never leave you with
    more than you started. Detect any 2-cycle whose product of rates > 1."""
    out = []
    rate = {}
    for c in ctx.economy.get("conversions", []) or []:
        if isinstance(c, dict) and c.get("from") and c.get("to") and isinstance(c.get("rate"), (int, float)):
            rate[(c["from"], c["to"])] = c["rate"]
    reported = set()
    for (a, b), r1 in rate.items():
        if frozenset((a, b)) in reported:
            continue
        r2 = rate.get((b, a))
        if r2 is not None and r1 * r2 > 1.0 + 1e-9:
            reported.add(frozenset((a, b)))
            out.append(
                Finding(
                    "static.conversion_loop", C, ERROR,
                    "Currency conversion round-trip is net-positive",
                    f"{a}->{b} at {r1} and {b}->{a} at {r2} multiply to {r1 * r2:g} > 1. "
                    f"Players can convert back and forth to farm currency.",
                    evidence=[{"cycle": [a, b, a], "product": r1 * r2}],
                )
            )
    return out


@register("static.override_unknown_target", C)
def override_unknown_target(ctx: Context) -> List[Finding]:
    out = []
    known_groups = set(ctx.group_ids)
    known_items = {it.get("id") for it in ctx.economy.get("items", []) or [] if isinstance(it, dict)}
    for gid, ov in (ctx.economy.get("per_group_overrides", {}) or {}).items():
        if gid not in known_groups:
            out.append(
                Finding(
                    "static.override_unknown_target", C, WARN,
                    "Per-group override targets an unknown group",
                    f"economy.per_group_overrides has an entry for '{gid}', which is not an "
                    f"experiment group. This override will never apply -- likely a stale or "
                    f"misspelled group id.",
                    evidence=[{"group": gid}],
                )
            )
            continue
        for item_id in (ov or {}).get("items", {}) or {}:
            if known_items and item_id not in known_items:
                out.append(
                    Finding(
                        "static.override_unknown_target", C, WARN,
                        "Per-group override targets an unknown item",
                        f"Override for group '{gid}' references item '{item_id}', which is not in "
                        f"the base economy. It creates an item that only one group can see -- "
                        f"intentional sometimes, but worth confirming.",
                        evidence=[{"group": gid, "item": item_id}],
                    )
                )
    return out


@register("static.unreachable_progression", C)
def unreachable_progression(ctx: Context) -> List[Finding]:
    """If the starting balance can't afford the cheapest item and nothing grants
    the needed currency, the player is stuck at zero progression."""
    start = ctx.economy.get("starting_balance", {}) or {}
    items = ctx.economy.get("items", []) or []
    if not start or not items:
        return []
    gainable = set()
    for it in items:
        for cur in (it.get("reward", {}) or {}):
            gainable.add(cur)
    for c in ctx.economy.get("conversions", []) or []:
        if isinstance(c, dict) and c.get("to"):
            gainable.add(c["to"])
    affordable = False
    for it in items:
        cost = it.get("cost", {}) or {}
        if cost and all(
            isinstance(start.get(cur, 0), (int, float)) and start.get(cur, 0) >= amt
            for cur, amt in cost.items()
        ):
            affordable = True
            break
    if not affordable and not (gainable - {None}):
        return [
            Finding(
                "static.unreachable_progression", C, WARN,
                "No item is affordable from the starting balance",
                f"Starting balance {start} cannot afford any item and no item/conversion grants "
                f"the currencies needed. New players hit a dead end.",
                evidence=[{"starting_balance": start}],
            )
        ]
    return []


@register("static.analytics_unsatisfiable", C)
def analytics_unsatisfiable(ctx: Context) -> List[Finding]:
    """An analytics requirement asking for a property the event type can never
    carry => every event fails validation."""
    out = []
    reqs = ctx.experiment.get("analytics_requirements", {}) or {}
    producible = {
        "exposure": {"user_id", "group", "experiment_id", "ts", "country", "level", "platform"},
        "economy_transaction": {
            "user_id", "group", "experiment_id", "ts", "item_id",
            "currency", "amount", "balance_after", "kind",
        },
    }
    for etype, spec in reqs.items():
        required = set((spec or {}).get("required_properties", []) or [])
        known = producible.get(etype)
        if known is None:
            continue
        impossible = required - known
        if impossible:
            out.append(
                Finding(
                    "static.analytics_unsatisfiable", C, ERROR,
                    "Analytics requirement can never be satisfied",
                    f"Event '{etype}' is required to carry {sorted(impossible)}, which it never "
                    f"produces. Every event will fail validation -- this requirement is dead on "
                    f"arrival.",
                    evidence=[{"event": etype, "impossible_props": sorted(impossible)}],
                )
            )
    return out


@register("static.fetch_failure_default", C)
def fetch_failure_default(ctx: Context) -> List[Finding]:
    """If the remote config fetch fails on launch, what happens? An undefined or
    dangerous default silently breaks the experience -- called out in the brief."""
    policy = ctx.defaults.get("on_fetch_failure")
    if policy is None:
        return [
            Finding(
                "static.fetch_failure_default", C, WARN,
                "No defined behaviour when remote config fetch fails",
                "defaults.on_fetch_failure is unset. On a flaky network the client has no "
                "documented fallback, so behaviour is whatever the code happens to do -- often "
                "assigning everyone to a default arm and quietly polluting the experiment.",
                evidence=[{"defaults": ctx.defaults}],
            )
        ]
    safe = {"use_control", "block", "use_last_known_good"}
    if policy not in safe:
        return [
            Finding(
                "static.fetch_failure_default", C, WARN,
                "Fetch-failure fallback may distort the experiment",
                f"defaults.on_fetch_failure is '{policy}'. If that routes failures into a variant "
                f"arm, network errors get counted as variant exposures and bias the result. "
                f"Prefer 'use_control', 'use_last_known_good', or 'block'.",
                evidence=[{"on_fetch_failure": policy}],
            )
        ]
    return []
