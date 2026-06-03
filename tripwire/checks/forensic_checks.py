"""Forensic / "hacker hat" checks: what's weird that no rule predicted?

These don't map to a single config constraint. They're the questions an
experienced QA asks when staring at a raw stream: does the ordering make sense?
do the running balances reconcile? is anything firing twice? is there a group in
the data that the config has never heard of? This is where you catch the bugs
nobody wrote a spec for.
"""

from typing import List

from ..model import Context, Finding, ERROR, WARN, INFO, CATEGORY_FORENSIC
from . import register

C = CATEGORY_FORENSIC


@register("forensic.unknown_group_in_stream", C)
def unknown_group_in_stream(ctx: Context) -> List[Finding]:
    """A group id appears in events that the config never defines. Classic
    config-drift symptom: the live config rolled ahead of (or behind) this one."""
    known = set(ctx.group_ids)
    if not known:
        return []
    seen = {}
    for ev in ctx.events:
        g = ev.get("group")
        if g is not None and g not in known:
            seen.setdefault(g, 0)
            seen[g] += 1
    if seen:
        return [
            Finding(
                "forensic.unknown_group_in_stream", C, ERROR,
                "Event stream contains groups the config doesn't define",
                f"Groups {sorted(seen)} appear in {sum(seen.values())} event(s) but are absent "
                f"from the config. The running config and the one under review disagree -- "
                f"textbook configuration drift.",
                evidence=[seen],
                count=sum(seen.values()),
            )
        ]
    return []


@register("forensic.duplicate_exposure", C)
def duplicate_exposure(ctx: Context) -> List[Finding]:
    """Analytics firing twice: the same user with multiple exposure events for
    the same group. Inflates exposure counts and double-weights users."""
    offenders = []
    for uid, evs in ctx.by_user.items():
        exps = [e for e in evs if e.get("event") == "exposure"]
        if len(exps) > 1:
            groups = {e.get("group") for e in exps}
            if len(groups) == 1:  # multi-group is the sticky check's job
                offenders.append({"user_id": uid, "exposure_count": len(exps), "group": next(iter(groups))})
    if offenders:
        return [
            Finding(
                "forensic.duplicate_exposure", C, WARN,
                "Exposure analytics fired more than once per user",
                f"{len(offenders)} user(s) have repeated exposure events for the same group. "
                f"Exposure counts and any per-exposure metric are inflated. Check for a missing "
                f"de-dupe / 'fire once' guard on the analytics call.",
                evidence=offenders,
                count=len(offenders),
            )
        ]
    return []


@register("forensic.duplicate_event_id", C)
def duplicate_event_id(ctx: Context) -> List[Finding]:
    seen, dupes = set(), {}
    for ev in ctx.events:
        eid = ev.get("event_id")
        if eid is None:
            continue
        if eid in seen:
            dupes[eid] = dupes.get(eid, 1) + 1
        seen.add(eid)
    if dupes:
        return [
            Finding(
                "forensic.duplicate_event_id", C, WARN,
                "Duplicate event_id values in the stream",
                f"{len(dupes)} event_id(s) appear more than once. If the pipeline assumes "
                f"event_id is unique for de-duplication, these will be silently dropped or "
                f"double-counted depending on which side wins.",
                evidence=[dupes],
                count=len(dupes),
            )
        ]
    return []


@register("forensic.txn_before_exposure", C)
def txn_before_exposure(ctx: Context) -> List[Finding]:
    """A user's first economy transaction is timestamped before their first
    exposure. The player was spending before they were assigned -- ordering or
    clock bug, and it pollutes attribution."""
    offenders = []
    for uid, evs in ctx.by_user.items():
        first_exp = next((e.get("ts") for e in evs if e.get("event") == "exposure" and e.get("ts") is not None), None)
        first_txn = next((e.get("ts") for e in evs if e.get("event") == "economy_transaction" and e.get("ts") is not None), None)
        if first_exp is not None and first_txn is not None and first_txn < first_exp:
            offenders.append({"user_id": uid, "first_txn_ts": first_txn, "first_exposure_ts": first_exp})
    if offenders:
        return [
            Finding(
                "forensic.txn_before_exposure", C, WARN,
                "Transactions recorded before the user's exposure",
                f"{len(offenders)} user(s) transacted before their first exposure event. Either "
                f"events arrive out of order, clocks disagree, or the feature is live before "
                f"assignment is recorded. Attribution windows can't trust this.",
                evidence=offenders,
                count=len(offenders),
            )
        ]
    return []


@register("forensic.balance_reconciliation", C)
def balance_reconciliation(ctx: Context) -> List[Finding]:
    """Replay each user's transactions from the starting balance and check that
    the reported balance_after matches, and that balances never go negative.

    This is the single most valuable forensic check: it catches client-side
    balance bugs, dropped transactions, AND any exploit that drives a balance
    below zero -- none of which any static check can see.
    """
    start = ctx.economy.get("starting_balance", {}) or {}
    mismatches, negatives = [], []
    negative_uids = set()  # one negative-balance finding per user, O(1) membership
    for uid, evs in ctx.by_user.items():
        balance = dict(start)
        for ev in evs:
            if ev.get("event") != "economy_transaction":
                continue
            cur, amt = ev.get("currency"), ev.get("amount")
            if cur is None or not isinstance(amt, (int, float)):
                continue
            balance[cur] = balance.get(cur, 0) + amt
            if balance[cur] < 0 and uid not in negative_uids:
                negative_uids.add(uid)
                negatives.append({"user_id": uid, "currency": cur, "balance": balance[cur], "after_event": ev.get("event_id")})
            reported = ev.get("balance_after")
            if isinstance(reported, (int, float)) and abs(reported - balance[cur]) > 1e-6:
                mismatches.append({
                    "user_id": uid, "currency": cur,
                    "expected_balance": balance[cur], "reported_balance_after": reported,
                    "event_id": ev.get("event_id"),
                })
                balance[cur] = reported  # trust the client's number going forward to avoid cascade noise
    out = []
    if negatives:
        out.append(
            Finding(
                "forensic.balance_reconciliation", C, ERROR,
                "A balance went negative during replay",
                f"{len(negatives)} user(s) reach a negative balance when their transactions are "
                f"replayed from the starting balance. The economy permits spending currency the "
                f"player doesn't have -- a missing affordability check, and a live exploit.",
                evidence=negatives,
                count=len(negatives),
            )
        )
    if mismatches:
        out.append(
            Finding(
                "forensic.balance_reconciliation", C, WARN,
                "Reported balance_after doesn't reconcile with the transaction history",
                f"{len(mismatches)} transaction(s) report a balance_after that disagrees with the "
                f"running total computed from the stream. Either a transaction is missing from "
                f"analytics or the client's balance math is wrong.",
                evidence=mismatches,
                count=len(mismatches),
            )
        )
    return out


@register("forensic.unknown_event_type", C)
def unknown_event_type(ctx: Context) -> List[Finding]:
    known = {"exposure", "economy_transaction"}
    seen = {}
    for ev in ctx.events:
        t = ev.get("event")
        if t is not None and t not in known:
            seen[t] = seen.get(t, 0) + 1
    if seen:
        return [
            Finding(
                "forensic.unknown_event_type", C, INFO,
                "Stream contains event types the tool doesn't model",
                f"Saw event types {sorted(seen)}. Not necessarily wrong, but the tool's checks "
                f"don't reason about them -- flagging so a human can decide whether they need "
                f"their own checks.",
                evidence=[seen],
            )
        ]
    return []
