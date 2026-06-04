# tripwire

**Config + runtime checks for live game experiments.** Point it at an
experiment config and a captured event stream; it tells you whether the
experiment is *configured* correctly and whether it's *behaving* correctly.

```
python3 -m tripwire check --config examples/example_config.json \
                          --events examples/example_events.jsonl
```

No dependencies. Python 3.8+. `python3 -m tests.test_checks` runs the suite.

---

## The problem I decided to focus on

The brief is explicit that a JSON-schema validator is the floor, not the goal,
and that the expensive bugs only show up when you *cross-reference config with
reality*. So I built the tool around one idea:

> **The config is a contract. The event stream is evidence. Most real incidents
> are a gap between the two.**

That framing gives three layers, which map onto the brief's three categories:

| Layer | Question | Needs |
|-------|----------|-------|
| **static** | Can this contract even work? | config only |
| **dynamic** | Is reality honouring the contract? | config + events |
| **forensic** | What's weird that no rule predicted? | events, replayed |

A schema validator only ever sees the first column of the first row. Everything
that has actually paged me on a live experiment lives in the other two: a
sticky-bucketing bug that reassigns users mid-test, a per-group price override
the client silently ignores, an economy that lets a balance go negative, an
exposure event that fires twice and doubles every per-user metric. `tripwire`
is built to catch those.

## How it's built

- **A check is one function + one decorator.** `@register("id", category)` over
  a `check(ctx) -> [Finding]`. Adding a check is "write a function next to the
  others and it shows up in the report" — that's the honest answer to *can a
  teammate extend this?*  There are 24 checks across the three layers today.
- **Findings carry severity.** `ERROR` is the CI gate (non-zero exit), `WARN` is
  "a human should look before this ships", `INFO` is context. I spent real
  effort *not* crying wolf — e.g. drift is only judged above a sample size, and
  overlap analysis skips rules that are already flagged dead elsewhere.
- **The one piece of real math is verified, not trusted.** Assignment drift uses
  a chi-square goodness-of-fit test. Rather than pull in scipy (and force a
  `pip install` on every teammate and CI runner for one function), I implemented
  the incomplete-gamma p-value in ~40 lines of stdlib and unit-tested it against
  textbook critical values.
- **CI-ready.** `--json` for machine output, `--fail-on error|warn|none`, and
  exit codes (0 clean / 1 findings / 2 bad input).
- **A generator, because 25 events isn't enough.** `python3 -m tripwire gen`
  produces a realistic stream from a config and can inject specific faults
  (`--inject drift,sticky,negative,mismatch,missing,orphan`) so each dynamic
  check has something to bite on. This is how I stress-test the statistical
  checks the seeded example is deliberately too small to trigger.

## Try it

```
# 1. Clean config, seeded event stream -> dynamic + forensic findings
python3 -m tripwire check --config examples/example_config.json --events examples/example_events.jsonl

# 2. Two deliberately broken configs -> static findings
python3 -m tripwire check --config examples/broken_weights_config.json
python3 -m tripwire check --config examples/broken_economy_config.json

# 3. Generate a large stream with drift + sticky bugs, then catch them
python3 -m tripwire gen --config examples/example_config.json --out /tmp/big.jsonl --n 3000 --inject drift,sticky
python3 -m tripwire check --config examples/example_config.json --events /tmp/big.jsonl
```

## Tuning & CI

**Thresholds are config, not code.** The policy numbers (drift sample size and
alpha, the missing-analytics WARN/ERROR fractions) live in `tripwire/thresholds.py`
with documented defaults, and any of them can be overridden per-run from the
config under a top-level `tripwire.thresholds` key — no code edit to retune noise:

```json
{
  "experiment": { "...": "..." },
  "tripwire": { "thresholds": { "drift_min_sample": 500, "drift_alpha": 0.0001 } }
}
```

Unknown or non-numeric override keys are ignored, so a typo falls back to the
default rather than crashing a run.

**CI.** `.github/workflows/ci.yml` runs the unit suite on Python 3.8–3.12, asserts
the exit-code contract (clean→0, broken→1, `--fail-on none`→0, bad input→2), and
checks that output is byte-identical under two `PYTHONHASHSEED` values so latent
set-ordering flakiness can't slip in. It's the pre-merge gate from `FINDINGS.md`,
realised — and dependency-free, so it stays cheap.

## What I explicitly chose *not* to do

- **No schema/type validator.** The loader stays permissive on purpose — strict
  parsing would crash on exactly the malformed configs we're meant to flag, and
  would re-build the thing the brief says is the floor.
- **No UI, no persistence, no dashboard.** Text + JSON report. The brief says UI
  isn't evaluated; a CI-friendly CLI is worth more.
- **No deep statistical battery.** One well-understood, well-tested drift test
  beats five I'd half-trust. Sequential testing / SRM alerting is a "next day"
  item, noted below.
- **Forensic checks stay 2-cycle / single-pass.** I detect 2-step conversion
  loops, not arbitrary-length arbitrage, and item+conversion arbitrage is out of
  scope. Defensible for a 4-hour build; flagged rather than hidden.

## What I'd build next with another day

1. **Schema-light config evolution diffing** — compare the incoming config to
   the last-known-good and flag *changes* (a weight moved, a price changed, a
   segment narrowed), since drift is usually introduced by a diff, not authored
   from scratch.
2. **Sample-Ratio-Mismatch as a first-class, continuously-running alert** with
   proper multiple-comparison handling, rather than a one-shot chi-square.
3. **A `--baseline` mode** so the tool can run pre-merge (static only) and
   post-release (full) from the same entry point in CI.
4. **Per-check config** (thresholds, allow-lists, suppressions) so teams can
   tune noise without editing code. *(Thresholds are now done — see
   "Tuning & CI" above; allow-lists/suppressions are the remaining piece.)*

## Assumptions (made a call, documented it, moved on)

**On the inputs:** the brief refers to `example_config.json` and
`example_events.jsonl` as provided files, but they weren't attached to the
assignment email (only the brief itself was). Rather than block on a back-and-
forth, I took the brief at its word -- *"You're expected to construct your own
additional configs and event streams... make a call, document the assumption,
and move on"* -- and built representative inputs to the schema below. Everything
runs against them out of the box. If the canonical files surface, re-pointing
the checks is a find-and-replace on field names, not a rewrite.

The rest of the shape I fixed and defended against deviation from:

- **Event shape.** `exposure` and `economy_transaction` events keyed by `event`,
  carrying `user_id`, `group`, `ts`, and (for transactions) `item_id`,
  `currency`, `amount` (negative = spend, positive = grant), `balance_after`.
- **Weights** may be fractions summing to 1.0 or percentages summing to 100;
  both are accepted.
- **Sticky** defaults to true (first exposure wins) unless `experiment.sticky`
  says otherwise.
- **Per-group economy** = base item table with `economy.per_group_overrides`
  merged on top, per group.
- **"Non-trivial fraction"** for missing analytics = >2% (WARN), >10% (ERROR).
- **Drift** needs >=200 assigned users to be judged, tested at p < 0.001.

If a real config used different field names, the checks are small and central
enough that re-pointing them is a find-and-replace, not a rewrite.
