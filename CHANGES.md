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

## Not done (deliberately — larger scope / judgment calls)

Documented as recommendations in `TEST_RESULTS.md` §6 rather than applied:
lifting hard-coded thresholds into config, caching the per-group economy table,
unifying text/JSON ordering, and adding a CI workflow.
