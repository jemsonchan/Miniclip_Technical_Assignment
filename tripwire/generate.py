"""Synthetic event-stream generator.

The provided example stream is ~25 events -- too small for statistical signals
like assignment drift to fire. The brief explicitly asks us to "construct your
own larger synthetic event streams to stress-test your tool." This does that: it
produces a realistic stream that honours a config, and can deliberately inject
specific failures so each dynamic/forensic check has something to catch.

Injectable faults (--inject):
  drift     - assign players ignoring the configured weights (50/50 regardless)
  sticky    - reassign a slice of users to a different group on a later exposure
  negative  - let some users overspend into a negative balance
  mismatch  - charge the wrong group's price for an item
  missing   - drop a required analytics property on some events
  orphan    - emit transactions for users that were never exposed
"""

import argparse
import json
import random
from typing import List


def _pick_group(weights: dict, rng: random.Random) -> str:
    groups = list(weights.keys())
    w = [max(0.0, weights[g]) for g in groups]
    total = sum(w) or 1.0
    r = rng.random() * total
    upto = 0.0
    for g, wi in zip(groups, w):
        upto += wi
        if r <= upto:
            return g
    return groups[-1]


def generate(config: dict, n_users: int, seed: int, inject: List[str]) -> List[dict]:
    rng = random.Random(seed)
    inject = set(inject or [])
    exp = config.get("experiment", {}) or {}
    eco = config.get("economy", {}) or {}
    weights = {g["id"]: g.get("weight", 0) for g in exp.get("groups", []) if isinstance(g, dict) and g.get("id")}
    group_ids = list(weights.keys()) or ["control"]
    items = [it for it in eco.get("items", []) or [] if isinstance(it, dict) and it.get("cost")]
    currencies = eco.get("currencies", ["coins"]) or ["coins"]
    start = eco.get("starting_balance", {}) or {c: 100 for c in currencies}

    events: List[dict] = []
    ts = 1_700_000_000
    eid = 0

    def next_id():
        nonlocal eid
        eid += 1
        return f"e{eid:06d}"

    def cost_for(group, item):
        overrides = (eco.get("per_group_overrides", {}) or {}).get(group, {}) or {}
        ov_item = (overrides.get("items", {}) or {}).get(item.get("id"), {})
        cost = dict(item.get("cost", {}))
        cost.update(ov_item.get("cost", {}))
        return cost

    for u in range(n_users):
        uid = f"u{u:05d}"
        ts += rng.randint(1, 50)

        # --- assignment ---
        if "drift" in inject:
            group = group_ids[u % 2] if len(group_ids) >= 2 else group_ids[0]
        else:
            group = _pick_group(weights, rng)

        level = rng.randint(1, 30)
        country = rng.choice(["US", "CA", "GB", "DE", "BR", "JP"])
        is_orphan = "orphan" in inject and rng.random() < 0.05

        if not is_orphan:
            exp_ev = {
                "event": "exposure", "event_id": next_id(), "user_id": uid,
                "experiment_id": exp.get("id", "exp"), "group": group,
                "ts": ts, "country": country, "level": level, "platform": "ios",
            }
            if "missing" in inject and rng.random() < 0.15:
                exp_ev.pop("group", None)  # drop a required property
            events.append(exp_ev)

            # sticky violation: a later, different exposure for some users
            if "sticky" in inject and rng.random() < 0.03 and len(group_ids) >= 2:
                other = next(g for g in group_ids if g != group)
                events.append({
                    "event": "exposure", "event_id": next_id(), "user_id": uid,
                    "experiment_id": exp.get("id", "exp"), "group": other,
                    "ts": ts + rng.randint(100, 999), "country": country, "level": level, "platform": "ios",
                })

        # --- transactions ---
        balance = dict(start)
        n_txn = rng.randint(0, 3)
        for _ in range(n_txn):
            if not items:
                break
            item = rng.choice(items)
            cost = cost_for(group, item)
            cur = next(iter(cost), currencies[0])
            amt = cost.get(cur, 10)
            ts += rng.randint(1, 30)

            if "mismatch" in inject and rng.random() < 0.2:
                amt = amt + 7  # wrong price
            charge = -amt
            if "negative" in inject and rng.random() < 0.1:
                charge = -(balance.get(cur, 0) + amt + 5)  # overspend
            balance[cur] = balance.get(cur, 0) + charge

            txn = {
                "event": "economy_transaction", "event_id": next_id(), "user_id": uid,
                "experiment_id": exp.get("id", "exp"), "group": group, "ts": ts,
                "item_id": item.get("id"), "currency": cur, "amount": charge,
                "balance_after": balance[cur], "kind": "purchase",
            }
            events.append(txn)
    return events


def main(argv=None):
    p = argparse.ArgumentParser(description="Generate a synthetic event stream from a config.")
    p.add_argument("--config", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--n", type=int, default=1000, help="number of users")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--inject", default="", help="comma-separated faults: drift,sticky,negative,mismatch,missing,orphan")
    args = p.parse_args(argv)

    with open(args.config) as fh:
        config = json.load(fh)
    faults = [x.strip() for x in args.inject.split(",") if x.strip()]
    events = generate(config, args.n, args.seed, faults)
    with open(args.out, "w") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")
    print(f"Wrote {len(events)} events for {args.n} users to {args.out}"
          + (f" (injected: {', '.join(faults)})" if faults else " (clean)"))


if __name__ == "__main__":
    main()
