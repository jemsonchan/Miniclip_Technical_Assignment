"""Dynamic checks: is reality honouring the contract?

These cross-reference the config against the captured event stream. This is
where the highest-value issues live -- the brief says so, and it's true: a
config can be perfectly valid and still produce a broken live experiment.
"""

from typing import List

from ..model import Context, Finding, ERROR, WARN, INFO, CATEGORY_DYNAMIC
from ..stats import chi_square_gof
from . import register

C = CATEGORY_DYNAMIC

# Below this many assigned users, distribution drift is statistically
# meaningless -- the brief warns the sample is tiny. We say so rather than cry
# wolf. Use the generator to produce a stream large enough to trip this.
MIN_SAMPLE_FOR_DRIFT = 200
# p-value threshold for declaring drift. 0.001 keeps false alarms rare on a
# check that runs continuously in production.
DRIFT_ALPHA = 0.001


@register("dynamic.assignment_drift", C)
def assignment_drift(ctx: Context) -> List[Finding]:
    if not ctx.weights:
        return []
    observed = ctx.assignment_counts()
    total = sum(observed.values())
    if total < MIN_SAMPLE_FOR_DRIFT:
        return [
            Finding(
                "dynamic.assignment_drift", C, INFO,
                "Not enough exposures to judge assignment drift",
                f"Only {total} assigned users; need >= {MIN_SAMPLE_FOR_DRIFT} for a "
                f"trustworthy chi-square. Observed split so far: {observed}.",
                evidence=[observed],
            )
        ]
    chi, df, p, expected = chi_square_gof(observed, ctx.weights)
    if p < DRIFT_ALPHA:
        return [
            Finding(
                "dynamic.assignment_drift", C, ERROR,
                "Group assignment has drifted from configured weights",
                f"chi-square={chi:.1f}, df={df}, p={p:.2e} (< {DRIFT_ALPHA}). "
                f"Observed {observed} vs expected ~{ {k: round(v, 1) for k, v in expected.items()} }. "
                f"The bucketing layer is not honouring the weights -- a hashing/rounding bug or a "
                f"stale config rollout is the usual cause.",
                evidence=[{"observed": observed, "expected": {k: round(v, 1) for k, v in expected.items()}}],
            )
        ]
    return []


@register("dynamic.segment_violation", C)
def segment_violation(ctx: Context) -> List[Finding]:
    """A user exposed to a group whose require-rules their own context violates."""
    rules_by_group = {}
    for r in ctx.segment_rules:
        if isinstance(r, dict) and r.get("group"):
            rules_by_group.setdefault(r["group"], []).append(r)
    if not rules_by_group:
        return []
    violations = []
    for ev in ctx.exposures:
        g = ev.get("group")
        for rule in rules_by_group.get(g, []):
            bad = _violated_requirements(rule.get("require", {}) or {}, ev)
            if bad:
                violations.append({"user_id": ev.get("user_id"), "group": g, "violated": bad})
    if violations:
        return [
            Finding(
                "dynamic.segment_violation", C, ERROR,
                "Users assigned to groups their segment rules forbid",
                f"{len(violations)} exposure(s) landed in a group whose targeting they don't "
                f"meet (e.g. a level/country gate). The bucketing layer is ignoring segment "
                f"rules, so the variant is leaking to the wrong audience.",
                evidence=violations,
                count=len(violations),
            )
        ]
    return []


def _violated_requirements(req: dict, ev: dict) -> list:
    bad = []
    for key, want in req.items():
        if key == "min_level" and isinstance(ev.get("level"), (int, float)) and ev["level"] < want:
            bad.append(f"level {ev['level']} < min_level {want}")
        elif key == "max_level" and isinstance(ev.get("level"), (int, float)) and ev["level"] > want:
            bad.append(f"level {ev['level']} > max_level {want}")
        elif isinstance(want, list):
            actual = ev.get(key)
            if actual is not None and actual not in want:
                bad.append(f"{key}={actual!r} not in {want}")
    return bad


@register("dynamic.sticky_violation", C)
def sticky_violation(ctx: Context) -> List[Finding]:
    """The same user must always see the same group (when sticky=True)."""
    if not ctx.sticky:
        return []
    offenders = []
    for uid, evs in ctx.by_user.items():
        groups = {e.get("group") for e in evs if e.get("event") == "exposure" and e.get("group") is not None}
        if len(groups) > 1:
            offenders.append({"user_id": uid, "groups_seen": sorted(groups)})
    if offenders:
        return [
            Finding(
                "dynamic.sticky_violation", C, ERROR,
                "Sticky assignment violated: users reassigned over time",
                f"{len(offenders)} user(s) were exposed to more than one group. Sticky "
                f"bucketing is broken -- their analytics straddle arms and their experience "
                f"flickers between variants. This corrupts every downstream metric.",
                evidence=offenders,
                count=len(offenders),
            )
        ]
    return []


@register("dynamic.transaction_mismatch", C)
def transaction_mismatch(ctx: Context) -> List[Finding]:
    """A spend whose amount doesn't match the configured cost for that user's
    group (after per-group overrides)."""
    mismatches = []
    for ev in ctx.transactions:
        item_id = ev.get("item_id")
        cur = ev.get("currency")
        amount = ev.get("amount")
        group = ev.get("group") or ctx.first_exposure_group(ev.get("user_id"))
        if item_id is None or cur is None or not isinstance(amount, (int, float)):
            continue
        table = ctx.effective_economy_for_group(group)
        item = table.get(item_id)
        if not item:
            continue  # unknown item handled by forensic check
        cost = (item.get("cost", {}) or {}).get(cur)
        reward = (item.get("reward", {}) or {}).get(cur)
        # A spend is negative; a reward/grant is positive. Compare magnitudes.
        if amount < 0 and isinstance(cost, (int, float)) and abs(abs(amount) - cost) > 1e-6:
            mismatches.append({
                "user_id": ev.get("user_id"), "group": group, "item": item_id,
                "currency": cur, "charged": abs(amount), "configured_cost": cost,
            })
        elif amount > 0 and isinstance(reward, (int, float)) and abs(amount - reward) > 1e-6:
            mismatches.append({
                "user_id": ev.get("user_id"), "group": group, "item": item_id,
                "currency": cur, "granted": amount, "configured_reward": reward,
            })
    if mismatches:
        return [
            Finding(
                "dynamic.transaction_mismatch", C, ERROR,
                "Transactions don't match the configured economy for the user's group",
                f"{len(mismatches)} transaction(s) charged or granted an amount that differs "
                f"from this group's economy table. Either the client is applying the wrong "
                f"group's prices (override not honoured) or a price changed without the config.",
                evidence=mismatches,
                count=len(mismatches),
            )
        ]
    return []


@register("dynamic.missing_analytics_props", C)
def missing_analytics_props(ctx: Context) -> List[Finding]:
    """Required analytics properties absent in a non-trivial fraction of events."""
    reqs = ctx.experiment.get("analytics_requirements", {}) or {}
    if not reqs:
        return []
    out = []
    THRESHOLD = 0.02  # >2% missing is "non-trivial"
    buckets = {"exposure": ctx.exposures, "economy_transaction": ctx.transactions}
    for etype, evs in buckets.items():
        required = (reqs.get(etype, {}) or {}).get("required_properties", []) or []
        if not required or not evs:
            continue
        for prop in required:
            missing = [e for e in evs if e.get(prop) in (None, "")]
            frac = len(missing) / len(evs)
            if frac > THRESHOLD:
                sev = ERROR if frac > 0.10 else WARN
                out.append(
                    Finding(
                        "dynamic.missing_analytics_props", C, sev,
                        "Required analytics property missing in many events",
                        f"'{prop}' is required on '{etype}' but absent in {len(missing)}/{len(evs)} "
                        f"({frac:.0%}) of them. Anyone slicing the experiment by '{prop}' will get "
                        f"silently incomplete data.",
                        evidence=[{"event": etype, "property": prop, "missing": len(missing), "of": len(evs)}],
                        count=len(missing),
                    )
                )
    return out


@register("dynamic.transaction_without_exposure", C)
def transaction_without_exposure(ctx: Context) -> List[Finding]:
    """Users transacting in the economy but never recorded as exposed. Their
    revenue/behaviour can't be attributed to any arm."""
    offenders = []
    for uid, evs in ctx.by_user.items():
        has_txn = any(e.get("event") == "economy_transaction" for e in evs)
        has_exp = any(e.get("event") == "exposure" for e in evs)
        if has_txn and not has_exp:
            offenders.append(uid)
    if offenders:
        return [
            Finding(
                "dynamic.transaction_without_exposure", C, WARN,
                "Users transacted without ever being exposed",
                f"{len(offenders)} user(s) generated economy transactions but have no exposure "
                f"event. Either exposure analytics is dropping events or these users were placed "
                f"into the feature without assignment -- either way their activity is unattributable.",
                evidence=offenders[:10],
                count=len(offenders),
            )
        ]
    return []
