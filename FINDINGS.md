# Findings — Monday-morning hand-off

*If I were handing this to the team Monday, here's what I'd say about what this
class of tooling should catch, and how to put it in the workflow.*

## The highest-risk issues are the cross-layer ones

Config-only mistakes (weights that don't sum, a typo'd group) are real but
cheap — they usually fail loudly or get caught in review. **The incidents that
cost money and corrupt results are the ones where the config looks fine and
reality disagrees.** In priority order, this is what I'd make sure we always
catch:

1. **Sticky-assignment violations.** A user who sees `control` on Monday and
   `variant_a` on Wednesday poisons *both* arms — their behaviour is attributed
   to two groups and every downstream metric is quietly wrong. This is the most
   dangerous failure because nothing crashes; the experiment just lies to you.
   In the sample stream the tool catches it on `u1`; in a 3,000-user generated
   stream it flagged 82 affected users from a single injected bug.

2. **Assignment drift / sample-ratio mismatch.** If the bucketer stops honouring
   the configured weights (a hashing change, a stale rollout), your 50/25/25
   quietly becomes 50/50/0. The tool's chi-square check caught an injected
   50/50 split at p ≈ 0 with `variant_b` starved to zero. SRM is the single
   best early-warning signal that an experiment's results are untrustworthy.

3. **Per-group economy mismatches & negative balances.** A transaction that
   charges the wrong group's price means the override isn't being applied — the
   experiment you're measuring isn't the one you configured. And a balance that
   goes negative on replay is both a correctness bug and a live exploit (a
   missing affordability check). Both are invisible to any static check; you can
   only see them by replaying the stream against the config.

4. **Analytics integrity.** Required properties missing on a chunk of events, or
   users transacting with no exposure recorded, or exposures firing twice — each
   one silently biases or inflates a metric someone will later make a decision
   on. Cheap to detect, expensive to discover after the readout.

The pattern across all four: **they're silent.** None throws an error in
production. That's exactly why they need automated cross-referencing — a human
reading the config will never see them.

## How I'd integrate it

I'd run the same engine at three gates, escalating what it can see:

- **Pre-merge (static, blocking).** On any change to a config, run
  `tripwire check --config <file> --fail-on error`. It's dependency-free and
  finishes instantly, so it's a cheap required status check. This stops
  un-shippable configs — empty audiences, money printers, unsatisfiable
  analytics — from ever landing.

- **Pre-release / canary (full, blocking on a canary slice).** Once the
  experiment has run against a small traffic slice, feed the captured events in:
  `tripwire check --config <file> --events <canary.jsonl> --json`. This is where
  sticky, drift, and economy-mismatch bugs surface *before* full rollout. Gate
  the rollout on a clean run.

- **In-production (full, alerting not blocking).** Run on a rolling window of
  live events on a schedule. Drift and analytics-integrity checks are the ones
  that matter here; route `ERROR` findings to the experiment owner. Don't block
  — alert, because by now traffic is live and a human needs to decide.

Practically: the `--json` output and exit codes mean this is a few lines of CI
YAML, not a project. I'd start with the pre-merge gate (highest leverage, zero
risk) and add the canary gate once the team trusts the signal.

## One caution

A clean run is **not** a proof of correctness — it means none of the 24 checks
fired. The forensic layer exists precisely because we don't know everything to
look for. I'd treat new production incidents as the backlog for new checks: every
time something breaks that the tool didn't catch, that's one more `@register`
function. The value compounds.
