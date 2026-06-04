"""Tests. Run from the repo root:  python -m tests.test_checks   (or pytest)

Two things matter here: (1) the chi-square implementation actually agrees with
textbook critical values -- this is the one piece of real math, so it gets
verified against known answers rather than trusted; (2) every seeded issue in
the example stream is caught by the check that's supposed to catch it.
"""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tripwire.model import Context
from tripwire.checks import run_all
from tripwire.stats import chi_square_sf, chi_square_gof
from tripwire import generate as gen
from tripwire import cli

EX = os.path.join(ROOT, "examples")


def load(path):
    with open(path) as fh:
        return json.load(fh)


def load_events(path):
    out = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def findings_for(config_path, events_path=None):
    cfg = load(config_path)
    evs = load_events(events_path) if events_path else []
    ctx = Context(cfg, evs)
    return {f.check_id for f in run_all(ctx)}, run_all(ctx)


class TestChiSquare(unittest.TestCase):
    """Verify against standard chi-square critical values (alpha = 0.05)."""

    def test_critical_values(self):
        # (chi2, df, expected_p) from standard tables.
        for chi2, df in [(3.841, 1), (5.991, 2), (7.815, 3), (9.488, 4)]:
            p = chi_square_sf(chi2, df)
            self.assertAlmostEqual(p, 0.05, places=2,
                                   msg=f"sf({chi2}, df={df}) = {p}, expected ~0.05")

    def test_p001_values(self):
        # alpha = 0.001 critical values
        for chi2, df in [(10.828, 1), (13.816, 2)]:
            p = chi_square_sf(chi2, df)
            self.assertAlmostEqual(p, 0.001, places=3)

    def test_perfect_fit_is_high_p(self):
        chi, df, p, _ = chi_square_gof({"a": 500, "b": 500}, {"a": 0.5, "b": 0.5})
        self.assertEqual(chi, 0.0)
        self.assertEqual(p, 1.0)

    def test_strong_drift_is_low_p(self):
        chi, df, p, _ = chi_square_gof({"a": 900, "b": 100}, {"a": 0.5, "b": 0.5})
        self.assertLess(p, 1e-6)


class TestExampleStream(unittest.TestCase):
    def setUp(self):
        self.ids, self.findings = findings_for(
            os.path.join(EX, "example_config.json"),
            os.path.join(EX, "example_events.jsonl"),
        )

    def test_each_seeded_issue_is_caught(self):
        expected = {
            "dynamic.sticky_violation",
            "dynamic.segment_violation",
            "dynamic.transaction_mismatch",
            "dynamic.missing_analytics_props",
            "dynamic.transaction_without_exposure",
            "forensic.unknown_group_in_stream",
            "forensic.duplicate_exposure",
            "forensic.txn_before_exposure",
            "forensic.balance_reconciliation",
        }
        missing = expected - self.ids
        self.assertEqual(missing, set(), f"these seeded issues were not caught: {missing}")

    def test_clean_config_has_no_static_errors(self):
        ids, findings = findings_for(os.path.join(EX, "example_config.json"))
        static_errors = [f for f in findings if f.category == "static" and f.severity == "ERROR"]
        self.assertEqual(static_errors, [], f"clean config raised static errors: {static_errors}")


class TestBrokenConfigs(unittest.TestCase):
    def test_broken_weights(self):
        ids, _ = findings_for(os.path.join(EX, "broken_weights_config.json"))
        for expected in ["static.weights_sum", "static.zero_weight_group",
                         "static.segment_unknown_group", "static.empty_audience",
                         "static.analytics_unsatisfiable", "static.fetch_failure_default"]:
            self.assertIn(expected, ids)

    def test_broken_economy(self):
        ids, _ = findings_for(os.path.join(EX, "broken_economy_config.json"))
        for expected in ["static.economy_unknown_currency", "static.economy_free_or_negative",
                         "static.conversion_loop", "static.override_unknown_target"]:
            self.assertIn(expected, ids)


class TestGeneratorDrift(unittest.TestCase):
    """A large generated stream with injected drift must trip the chi-square check."""

    def test_injected_drift_is_detected(self):
        cfg = load(os.path.join(EX, "example_config.json"))
        events = gen.generate(cfg, n_users=3000, seed=1, inject=["drift"])
        ctx = Context(cfg, events)
        ids = {f.check_id for f in run_all(ctx)}
        self.assertIn("dynamic.assignment_drift", ids)

    def test_clean_large_stream_has_no_drift_error(self):
        cfg = load(os.path.join(EX, "example_config.json"))
        events = gen.generate(cfg, n_users=3000, seed=1, inject=[])
        ctx = Context(cfg, events)
        drift = [f for f in run_all(ctx)
                 if f.check_id == "dynamic.assignment_drift" and f.severity == "ERROR"]
        self.assertEqual(drift, [], "clean stream falsely flagged drift")


class TestExitCodes(unittest.TestCase):
    """The exit codes ARE the CI contract -- a check that returns findings must
    not silently exit 0, and `--fail-on none` must never block. These were
    untested, which is how the inverted `--fail-on none` sentinel slipped in.
    Drive the real CLI entry point so the argparse + exit-code wiring is covered.
    """

    CLEAN = os.path.join(EX, "example_config.json")          # no static errors
    BROKEN = os.path.join(EX, "broken_economy_config.json")  # has ERROR findings
    EVENTS = os.path.join(EX, "example_events.jsonl")        # has WARN + ERROR

    def run_cli(self, *argv):
        # cli.main returns the exit code; swallow its stdout so the suite is quiet.
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = cli.main(list(argv))
        return code, buf.getvalue()

    def test_clean_config_exits_zero(self):
        code, _ = self.run_cli("check", "--config", self.CLEAN)
        self.assertEqual(code, 0)

    def test_errors_exit_one_by_default(self):
        code, _ = self.run_cli("check", "--config", self.BROKEN)
        self.assertEqual(code, 1)

    def test_fail_on_none_never_blocks(self):
        # The regression this whole class exists for: errors present, but
        # --fail-on none means "report, don't block" => exit 0.
        code, _ = self.run_cli("check", "--config", self.BROKEN, "--fail-on", "none")
        self.assertEqual(code, 0, "--fail-on none must exit 0 even with ERROR findings")

    def test_fail_on_warn_blocks_on_warnings(self):
        code, _ = self.run_cli("check", "--config", self.CLEAN,
                               "--events", self.EVENTS, "--fail-on", "warn")
        self.assertEqual(code, 1)

    def test_fail_on_error_ignores_warn_only(self):
        # A config whose worst finding is a WARN must pass under --fail-on error.
        code, out = self.run_cli("check", "--config", self.CLEAN, "--fail-on", "error")
        self.assertEqual(code, 0)

    def test_missing_input_exits_two(self):
        code, _ = self.run_cli("check", "--config", "/no/such/file.json")
        self.assertEqual(code, 2)

    def test_unknown_only_category_exits_two(self):
        code, out = self.run_cli("check", "--config", self.CLEAN, "--only", "bogus")
        self.assertEqual(code, 2)
        self.assertIn("unknown categor", out)

    def test_only_filters_to_requested_category(self):
        # --only static must run only static checks (no dynamic/forensic ids).
        code, out = self.run_cli("check", "--config", self.BROKEN, "--only", "static", "--json")
        payload = json.loads(out)
        cats = {f["category"] for f in payload["findings"]}
        self.assertTrue(cats <= {"static"}, f"--only static leaked categories: {cats}")


class TestThresholdConfig(unittest.TestCase):
    """Policy thresholds are tunable from the config, not hard-coded."""

    def _cfg_with_split(self):
        # A config + generated stream whose 50/50 split trips drift at default alpha.
        cfg = load(os.path.join(EX, "example_config.json"))
        events = gen.generate(cfg, n_users=3000, seed=1, inject=["drift"])
        return cfg, events

    def test_defaults_match_documented_values(self):
        from tripwire.thresholds import Thresholds
        t = Thresholds()
        self.assertEqual(t.drift_min_sample, 200)
        self.assertEqual(t.drift_alpha, 0.001)
        self.assertAlmostEqual(t.missing_props_warn_frac, 0.02)
        self.assertAlmostEqual(t.missing_props_error_frac, 0.10)

    def test_raised_min_sample_silences_drift(self):
        # Same drifted stream, but demand an impossibly large sample -> the check
        # downgrades to INFO ("too small to judge") instead of ERROR.
        cfg, events = self._cfg_with_split()
        cfg["tripwire"] = {"thresholds": {"drift_min_sample": 10_000_000}}
        ctx = Context(cfg, events)
        drift = [f for f in run_all(ctx) if f.check_id == "dynamic.assignment_drift"]
        self.assertEqual(len(drift), 1)
        self.assertEqual(drift[0].severity, "INFO")

    def test_unknown_and_bad_override_keys_are_ignored(self):
        from tripwire.thresholds import Thresholds
        t = Thresholds.from_config({"tripwire": {"thresholds": {
            "drift_min_sample": "lots",   # wrong type -> ignored
            "drift_alpha": True,          # bool -> ignored (not a real number here)
            "nonsense_key": 5,            # unknown -> ignored
            "missing_props_warn_frac": 0.5,  # valid -> applied
        }}})
        self.assertEqual(t.drift_min_sample, 200)      # default kept
        self.assertEqual(t.drift_alpha, 0.001)         # default kept
        self.assertAlmostEqual(t.missing_props_warn_frac, 0.5)  # applied


class TestEconomyCache(unittest.TestCase):
    def test_effective_economy_is_memoised_per_group(self):
        cfg = load(os.path.join(EX, "example_config.json"))
        ctx = Context(cfg, [])
        first = ctx.effective_economy_for_group("control")
        second = ctx.effective_economy_for_group("control")
        self.assertIs(first, second, "expected the cached table to be reused")
        # different group key is a different table
        self.assertIsNot(first, ctx.effective_economy_for_group("variant_a"))


class TestOrdering(unittest.TestCase):
    """One canonical ordering contract for each renderer; both deterministic."""

    def test_text_is_category_first_json_is_severity_first(self):
        from tripwire.model import sort_findings, sort_findings_by_category
        ids, findings = findings_for(
            os.path.join(EX, "example_config.json"),
            os.path.join(EX, "example_events.jsonl"),
        )
        cats = [f.category for f in sort_findings_by_category(findings)]
        order = {"static": 0, "dynamic": 1, "forensic": 2}
        self.assertEqual(cats, sorted(cats, key=lambda c: order[c]),
                         "text ordering must be category-first")
        sevs = [f.severity for f in sort_findings(findings)]
        srank = {"ERROR": 0, "WARN": 1, "INFO": 2}
        self.assertEqual(sevs, sorted(sevs, key=lambda s: srank[s]),
                         "json ordering must be severity-first")

    def test_orderings_are_stable_for_equal_keys(self):
        # check_id is the final tie-break, so repeated sorts are identical.
        from tripwire.model import sort_findings
        _, findings = findings_for(
            os.path.join(EX, "example_config.json"),
            os.path.join(EX, "example_events.jsonl"),
        )
        a = [f.check_id for f in sort_findings(findings)]
        b = [f.check_id for f in sort_findings(list(reversed(findings)))]
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main(verbosity=2)
