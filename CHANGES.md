# Changes — review, bug fix & refactor

This pass reviewed the submission, fixed one functional bug, hardened the test
suite around it, and applied a few low-risk refactors. **No detection behaviour
changed** — the documented workflows produce identical reports before and after,
and output remains deterministic under `PYTHONHASHSEED` randomization. Full test
narrative in [TEST_RESULTS.md](TEST_RESULTS.md).

## Bug fix

### `--fail-on none` was inverted (`tripwire/cli.py`)
`none` is meant to be "report findings but never fail the build." Because the
failure test is `worst_severity_rank <= threshold` with ranks `ERROR=0, WARN=1,
INFO=2`, the `none` sentinel had to be a rank *nothing* can reach. It was set to
`99`, but every real rank (0/1/2) is `<= 99`, so `none` failed on everything —
the exact opposite of its purpose.

```diff
- threshold = {"error": _RANK[ERROR], "warn": _RANK[WARN], "none": 99}[args.fail_on]
+ threshold = {"error": _RANK[ERROR], "warn": _RANK[WARN], "none": -1}[args.fail_on]
```

Impact: anyone wiring the "in-production: alert, don't block" gate from
`FINDINGS.md` (`--fail-on none`) would have gotten a *blocking* CI step.

## Tests added (`tests/test_checks.py`)

New `TestExitCodes` class (8 cases) drives the real `cli.main` entry point and
asserts the **exit-code contract** that was previously untested — which is how
the `--fail-on none` bug slipped in:

- clean config → 0; errors → 1 (default); `--fail-on none` → 0 even with errors;
  `--fail-on warn` blocks on warnings; `--fail-on error` ignores warn-only;
  missing input → 2; unknown `--only` category → 2; `--only static` runs only
  static checks.

Suite went from 10 → 18 tests, all passing.

## Refactors (low-risk, behaviour-preserving)

| File | Change | Why |
|------|--------|-----|
| `tripwire/cli.py` | `--only` validates categories; unknown value → exit 2 with a clear message | `--fail-on` used `choices=`; `--only` silently ran nothing on a typo |
| `tripwire/cli.py` | "checks run: N" now reflects the filtered count, not always 24 | Previously reported total registered checks even under `--only` |
| `tripwire/model.py` | `Context` computes first-exposure-per-user once; `first_exposure_group` is now a dict lookup | Was re-walking each user's events on every call (several checks call it); suite runtime dropped ~3× |
| `tripwire/checks/forensic_checks.py` | Negative-balance "already seen this user" check uses a `set` instead of an O(n) list scan; removed the now-unused `negatives_has` helper | O(n²) → O(n) on the highest-value forensic check, which matters on the production rolling-window use case |

## Housekeeping

- Added `.gitignore` (ignores `__pycache__/`, `*.pyc`, etc.) and removed committed
  byte-code artifacts.
- Added a minimal `pyproject.toml` so `pip install -e .` and a `tripwire` console
  entry point work (was `python3 -m tripwire` only).

---

# Round 2 — the four larger recommendations

The four items previously left as recommendations are now implemented.
Detection behaviour is unchanged: the example report is byte-identical before
and after (same `--json` MD5), and determinism under `PYTHONHASHSEED` holds.

### 1. Thresholds lifted into config (`tripwire/thresholds.py`, new)
Policy numbers — drift sample size & alpha, missing-analytics WARN/ERROR
fractions — moved out of check bodies into a frozen `Thresholds` dataclass with
documented defaults. `Context` builds it via `Thresholds.from_config`, overlaying
numeric overrides from a top-level `tripwire.thresholds` config key. Unknown or
non-numeric keys are ignored (a typo can't crash a run). `dynamic_checks.py` now
reads `ctx.thresholds.*` instead of module constants.

### 2. Per-group economy table cached (`tripwire/model.py`)
`effective_economy_for_group` memoises its merged base+override table per group
in `Context._economy_cache`. `transaction_mismatch` calls it once per
transaction; it now builds each group's table once. Result is documented
read-only (every caller already treats it so).

### 3. Unified ordering contract (`tripwire/model.py`, `tripwire/cli.py`)
The two finding orderings are now named, documented functions defined once:
`sort_findings` (severity-first, for JSON) and `sort_findings_by_category`
(category-first, for the text report). Both tie-break on `check_id` for
determinism. `cli.py` no longer carries its own duplicated `_RANK`/`_CAT_ORDER`
sort keys — it imports `_SEVERITY_ORDER` and the sorters from `model`, so text
and JSON can't silently diverge. (Minor, intended: JSON's secondary tie-break is
now the logical category order — static→dynamic→forensic — instead of
alphabetical; the example output is unaffected.)

### 4. CI workflow (`.github/workflows/ci.yml`, new)
Runs on push to `main` and on PRs, Python 3.8–3.12: unit suite, `pip install -e .`
+ console-script smoke, the exit-code contract (clean→0, broken→1,
`--fail-on none`→0, bad input→2), and a determinism gate comparing `--json` MD5
across two `PYTHONHASHSEED` values. Dependency-free, so it's cheap enough to be a
required check — the pre-merge gate from `FINDINGS.md`, realised.

### Tests
Added `TestThresholdConfig`, `TestEconomyCache`, `TestOrdering` (6 cases).
Suite 18 → **24**, all green.

### Docs
`README.md` gains a "Tuning & CI" section and marks the per-check-config item
partially done. `TEST_RESULTS.md` §6 updated.
