# tripwire — Test Report (Smoke + Regression)

**Subject:** Miniclip technical assignment — `tripwire`
**Environment:** Python 3.11, Linux, stdlib-only (no external deps)
**Method:** Run 1 = smoke test (does it work at all?). Run 2 = regression test
(does it work *robustly* — deterministic, no flakiness, stable under adversarial
conditions?).

> **Status:** The one defect found (`--fail-on none`) has been **fixed** and is
> now covered by a regression test. See [CHANGES.md](CHANGES.md). This document
> records both the original findings and the post-fix state.

---

## 1. Summary verdict

| Aspect | Result |
|--------|--------|
| Unit test suite | ✅ PASS (10 original → **18** after fix), 0 flakes across repeated runs |
| Documented CLI workflows (README) | ✅ All run as advertised |
| Determinism / flakiness | ✅ Byte-identical output across runs **and** under `PYTHONHASHSEED` randomization |
| Exit-code contract | 🐞 → ✅ `--fail-on none` was inverted; **fixed** + regression-tested |
| Input robustness (missing/malformed) | ✅ Degrades gracefully, never crashes |

**Bottom line:** the tool is genuinely robust — the statistical core is verified,
output is fully deterministic, and bad input is handled as findings rather than
stack traces. The single functional defect (`--fail-on none`) has been corrected.

---

## 2. Run 1 — Smoke Test

### 2.1 Unit suite — `python3 -m tests.test_checks`

| Expected | Actual |
|----------|--------|
| All tests pass | ✅ `OK` |

Covers: chi-square p-values vs textbook critical values (3.841/df1≈0.05,
10.828/df1≈0.001, …), every seeded issue caught by its named check, broken
configs trip static checks, injected drift detected, clean stream *not*
false-flagged.

### 2.2 Documented CLI workflows

| Command | Expected | Actual |
|---------|----------|--------|
| `check` clean config + seeded events | dynamic+forensic findings, exit 1 | ✅ 6 error / 4 warn / 1 info |
| `check` broken_weights_config | static errors | ✅ 4 error / 4 warn / 1 info |
| `check` broken_economy_config | static errors | ✅ 3 error / 3 warn / 1 info |
| `gen … --inject drift,sticky` | writes large stream | ✅ 7,563 events / 3,000 users |
| `check` generated stream | drift + sticky caught | ✅ drift p≈0, 79 sticky users, 1717 negative balances |
| `--json` | valid machine-readable JSON | ✅ parses |
| `--help` | usage text | ✅ shows `check` / `gen` |

The "config as contract / events as evidence" design holds up — the cross-layer
issues (sticky, drift, economy mismatch, negative balance) are exactly what gets
surfaced.

---

## 3. Run 2 — Regression / Robustness Test

### 3.1 Stability (no flakiness)

| Test | Expected | Actual |
|------|----------|--------|
| Unit suite ×3 | identical PASS | ✅ |
| Same-seed generator twice | byte-identical files | ✅ `diff` clean |
| `check --json` ×3 | identical report | ✅ |

### 3.2 Hash-order robustness (the real flakiness test)

Python set/dict iteration order is randomized per-process by `PYTHONHASHSEED`.
Tools that leak set-iteration order into output are *latently flaky* (pass
locally, fail in CI). Same commands under 4 hash seeds, comparing MD5 of full
output:

| Input | HASHSEED 0 / 1 / 42 / 1000 |
|-------|----------------------------|
| example + events | identical ✅ |
| broken_weights | identical ✅ |
| broken_economy | identical ✅ |

**The strongest robustness result.** The author was disciplined about `sorted(…)`
wherever set/dict order could leak. No hidden nondeterminism.

### 3.3 Input robustness

| Test | Expected | Actual |
|------|----------|--------|
| Missing config file | exit 2 | ✅ |
| Malformed JSON config | exit 2, reported as finding | ✅ |
| Empty events file | exit 0 | ✅ |
| `--only static\|dynamic\|forensic` | filters sections | ✅ |

A check that throws is caught by `run_all` and converted into an ERROR finding —
one bad check can't take the tool down.

---

## 4. The defect found (now fixed): `--fail-on none`

**Expected (per `cli.py` docstring):** *"exit 0 = no findings at or above the
`--fail-on` threshold."* With threshold `none`, nothing is at/above it, so a
config full of errors should still exit **0** (the "report but never block CI"
mode).

**Actual (before fix):**

```
$ python3 -m tripwire check --config examples/broken_economy_config.json --fail-on none
$ echo $?
1          # ← expected 0
```

**Root cause** — `cli.py`:

```python
threshold = {"error": _RANK[ERROR], "warn": _RANK[WARN], "none": 99}[args.fail_on]
worst = min((_RANK[f.severity] for f in findings), default=99)
return 1 if worst <= threshold else 0
```

`_RANK = {ERROR:0, WARN:1, INFO:2}`. The test is `worst <= threshold`. For `none`
the sentinel was `99`, but every severity rank (0,1,2) is `<= 99`, so *every*
finding tripped the failure exit — `none` behaved like "fail on everything."

**Fix:** the `none` sentinel must be a rank nothing can reach → `-1`. Now no rank
is `<= -1`, so `none` exits 0 regardless of findings; `error`/`warn` unaffected.
Verified + locked in by the new `TestExitCodes` class.

**Why it slipped through:** the suite tested *findings* thoroughly but never
asserted the *exit codes* that are the entire CI contract. That gap is now closed.

---

## 5. Refactor applied (see CHANGES.md for the full list)

- Fixed `--fail-on none`; added an 8-case exit-code/CLI contract test class.
- `--only` now validates its categories (unknown → exit 2) and the report's
  "checks run: N" reflects the filtered count, not always 24.
- `Context` memoises first-exposure-per-user once (O(events) instead of
  O(events × checks)); suite runtime dropped ~3×.
- Replaced an O(n²) per-user membership scan in the balance check with a set.

All existing detection behaviour is unchanged — the documented workflows produce
byte-identical reports before and after, and determinism under `PYTHONHASHSEED`
is preserved.

---

## 6. Further optimization ideas (not yet applied — judgment calls / larger scope)

- **Lift hard-coded thresholds** (2%/10% missing-analytics, ≥200 drift sample,
  p<0.001) into one `THRESHOLDS` table or per-check config, so teams tune noise
  without editing logic. The README already flags this as a day-2 item.
- **Cache the base economy item table** in `effective_economy_for_group` (rebuilt
  per call today) if a check ever iterates groups × items on large configs.
- **Unify finding ordering**: text output sorts category-first, JSON
  severity-first. Intentional, but worth a single documented ordering contract so
  the two can't silently diverge.
- **CI**: a GitHub Actions workflow running the suite + the two broken configs
  would make the exit-code contract a required status check (the FINDINGS.md
  pre-merge gate, realised).
