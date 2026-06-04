# tripwire — Verification Report (post-update re-test)

**Subject:** Miniclip technical assignment — `tripwire`
**Commit under test:** `9b5249c` (main)
**Environment:** Python 3.11 (suite also gated 3.8–3.12 in CI), Linux, stdlib-only
**Date:** 2026-06-04

This is the third test pass. It re-runs the same two scenarios used originally —
**Scenario 1 = smoke test**, **Scenario 2 = regression/robustness** — against the
updated code, and compares expected vs actual *now* against the previous rounds.

## Where the previous rounds left things

| Round | What it was | Tests | Result | Open defects |
|-------|-------------|-------|--------|--------------|
| **R1** | Original submission, as received | 10 | All pass; deterministic | 🐞 `--fail-on none` inverted (always blocked) |
| **R2a** | After bug fix + low-risk refactor | 18 | All pass | none |
| **R2b** | After the 4 optimizations (thresholds/cache/ordering/CI) | 24 | All pass | none |
| **R3 (now)** | Re-verification of `9b5249c` | **24** | **All pass** | **none** |

---

## Scenario 1 — Smoke test

| # | Check | Expected | Actual (now) | vs previous |
|---|-------|----------|--------------|-------------|
| S1.1 | Unit suite | all green | ✅ `Ran 24 tests … OK` | R1 had 10; +14 added, all pass |
| S1.2 | clean + seeded events | exit 1, findings | ✅ `6 error, 4 warn, 1 info`, exit 1 | identical to R1/R2 |
| S1.2 | broken_weights | exit 1, static errors | ✅ `4 error, 4 warn, 1 info` | identical |
| S1.2 | broken_economy | exit 1, static errors | ✅ `3 error, 3 warn, 1 info` | identical |
| S1.2 | `gen --inject drift,sticky` (n=3000) | writes stream | ✅ 7,563 events / 3,000 users, exit 0 | identical |
| S1.2 | check generated stream | drift+sticky caught | ✅ `3 error`, exit 1 | identical |
| S1.3 | `--json` | valid JSON | ✅ parses | identical |
| S1.3 | `--fail-on none` | exit 0 | ✅ exit 0 | **was the R1 bug → now correct** |
| S1.3 | `--only bogus` | exit 2 | ✅ exit 2 | new in R2, holds |
| S1.3 | `--only static` count | accurate | ✅ `checks run: 12` | new in R2 (R1 wrongly said 24) |
| S1.3 | `--help` / console script | usage shown | ✅ exit 0 | new in R2, holds |

**Smoke result: PASS.** Every documented workflow behaves as advertised, and the
summaries match the earlier rounds byte-for-byte where they should.

---

## Scenario 2 — Regression / robustness test

| # | Check | Expected | Actual (now) | vs previous |
|---|-------|----------|--------------|-------------|
| S2.1 | Suite ×5 | identical pass each | ✅ run1–5 = OK | stable, as R1/R2 |
| S2.2 | `PYTHONHASHSEED` ∈ {0,1,42,1000,99999} → 1 distinct output | 1 each | ✅ 1 / 1 / 1 | **strongest property, still holds** |
| S2.3 | Same-seed generator (all 6 fault types) | byte-identical | ✅ `diff` clean | as R1/R2 |
| S2.4 | Example report MD5 | `fff64daf…` (R1 baseline) | ✅ `fff64daf…` | **unchanged across all refactors** |
| S2.5 | `--fail-on error/warn/none` on errors | 1 / 1 / 0 | ✅ 1 / 1 / 0 | the R1 regression stays fixed |
| S2.6 | Config threshold override | ERROR → INFO | ✅ ERROR → INFO | new in R2b, verified end-to-end |
| S2.7 | Missing config | exit 2 | ✅ 2 | as R1 |
| S2.7 | Malformed JSON config | exit 2, no crash | ✅ 2 | as R1 |
| S2.7 | Empty events file | exit 0 | ✅ 0 | as R1 |
| S2.7 | Bad event line | exit 2, no crash | ✅ 2 | as R1 |
| S2.7 | Empty config `{}` | no crash | ✅ exit 0 | robust |
| S2.8 | Garbage threshold override | ignored, no crash | ✅ exit 1, no crash | new defensive path holds |
| S2.9 | CI workflow YAML | parses | ✅ valid | new in R2b |
| S2.10 | CI exit-code step (simulated) | all gates pass | ✅ clean/broken/none/missing all correct | new |
| S2.11 | `pip install -e .` + `tripwire` | installs, runs | ✅ install OK, console script works | new in R2 |
| S2.12 | All 6 injected faults (n=5000) | each caught, **no internal exceptions** | ✅ all caught; `internal exceptions: NONE` | robust |
| S2.13 | 20k users / 50k events | completes fast | ✅ **0.95s wall** | confirms cache/memo refactors scale |

**Regression result: PASS.** No flakiness, no nondeterminism, no crashes on
malformed input, no internal check exceptions, and the example output is
byte-identical to the very first run despite three rounds of changes.

---

## Expected vs actual — summary

Across **35 distinct checks** spanning both scenarios, expected == actual in
**every** case. The one defect ever found (`--fail-on none`, R1) is fixed and now
guarded by a regression test plus a CI gate. Nothing new surfaced in this pass.

---

## Is it good enough to submit to Zahra by email?

**Yes — it is in a submittable state.** Reasoning:

**What's strong**
- **Correct & green.** 24/24 tests pass repeatedly; all brief-relevant scenarios
  (static / dynamic / forensic, broken configs, generated streams) behave as
  documented.
- **Robust, not just working.** Byte-identical output under hash-seed
  randomization is the property most submissions get subtly wrong; this one is
  clean. Malformed input degrades to findings/exit-codes, never a stack trace.
- **The one bug is gone and can't silently come back** — it's pinned by a test
  and a CI gate.
- **Engineering maturity on display:** stdlib-only verified chi-square,
  config-tunable thresholds, a dependency-free CI pre-merge gate, deterministic
  CI-friendly exit codes, and clear docs (`README`, `FINDINGS`, `AI_USAGE`,
  `CHANGES`, test reports).
- **Performance headroom:** 50k events in ~1s, so the "production rolling window"
  story in `FINDINGS.md` is credible.

**Honest caveats to mention in the email (not blockers)**
- The CI workflow has not yet executed on GitHub — it's committed but needs
  Actions enabled / a first run (or a PR) to show a green badge. Worth confirming
  before you point Zahra at it.
- A few day-2 items remain *by design* and are documented as such: allow-list /
  suppression config, `--baseline` mode, config-evolution diffing, SRM as a
  continuous alert. Calling these out shows judgment rather than gaps.
- The example config/events are the candidate's own constructions (the canonical
  files weren't attached to the brief) — already documented in the README's
  Assumptions section.

**Suggested framing for the email:** lead with the "config is a contract, events
are evidence" thesis and the three-layer model, note it's stdlib-only and
CI-ready with deterministic exit codes, and link `README.md` + `FINDINGS.md` as
the entry points. Mention the remaining day-2 items briefly to show scope
awareness.

**Bottom line:** functionally complete, robust, well-documented, and defensible
under questioning. Ship it — ideally after one CI run goes green so the
status check is visible.
