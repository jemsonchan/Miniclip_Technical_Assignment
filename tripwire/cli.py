"""Command-line interface and report rendering.

Usage:
  python -m tripwire check --config CONFIG.json --events EVENTS.jsonl [--json]
                           [--only static,dynamic,forensic] [--fail-on error|warn|none]
  python -m tripwire gen   --config CONFIG.json --out EVENTS.jsonl --n 2000 [--inject drift,sticky]

Exit codes (so this drops straight into CI):
  0  no findings at or above the --fail-on threshold
  1  findings at/above threshold (default: any ERROR)
  2  could not load inputs
"""

import argparse
import json
import sys

from .model import (
    Context, LoadResult, load_config, load_events, sort_findings,
    ERROR, WARN, INFO,
    CATEGORY_STATIC, CATEGORY_DYNAMIC, CATEGORY_FORENSIC,
)
from .checks import run_all, all_checks
from . import generate as gen

_COLOR = {ERROR: "\033[91m", WARN: "\033[93m", INFO: "\033[96m"}
_RESET = "\033[0m"
_RANK = {ERROR: 0, WARN: 1, INFO: 2}
_CAT_ORDER = {CATEGORY_STATIC: 0, CATEGORY_DYNAMIC: 1, CATEGORY_FORENSIC: 2}
_CATEGORIES = {CATEGORY_STATIC, CATEGORY_DYNAMIC, CATEGORY_FORENSIC}


def _supports_color(stream) -> bool:
    return hasattr(stream, "isatty") and stream.isatty()


def render_text(findings, ctx, use_color: bool, n_checks_run: int = None) -> str:
    if n_checks_run is None:
        n_checks_run = len(all_checks())
    lines = []
    n_users = len(ctx.by_user)
    lines.append("=" * 72)
    lines.append("tripwire -- experiment config + runtime report")
    lines.append(f"groups: {ctx.group_ids or '(none)'}   "
                 f"events: {len(ctx.events)}   users: {n_users}   "
                 f"exposures: {len(ctx.exposures)}   transactions: {len(ctx.transactions)}")
    lines.append("=" * 72)

    counts = {ERROR: 0, WARN: 0, INFO: 0}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1

    if not findings:
        lines.append("\nNo issues found. (That is not the same as 'correct' -- see README on coverage.)")
        return "\n".join(lines)

    ordered = sorted(
        findings,
        key=lambda f: (_CAT_ORDER.get(f.category, 9), _RANK.get(f.severity, 9), f.check_id),
    )
    last_cat = None
    for f in ordered:
        if f.category != last_cat:
            lines.append(f"\n--- {f.category.upper()} ---")
            last_cat = f.category
        tag = f.severity
        if use_color:
            tag = f"{_COLOR.get(f.severity, '')}{f.severity}{_RESET}"
        suffix = f"  (x{f.count})" if f.count > 1 else ""
        lines.append(f"[{tag}] {f.title}{suffix}")
        lines.append(f"       {f.detail}")
        if f.evidence:
            ev = f.evidence[0]
            ev_str = json.dumps(ev) if not isinstance(ev, str) else ev
            if len(ev_str) > 200:
                ev_str = ev_str[:200] + " ..."
            lines.append(f"       e.g. {ev_str}")

    lines.append("\n" + "-" * 72)
    lines.append(f"summary: {counts[ERROR]} error, {counts[WARN]} warn, {counts[INFO]} info  "
                 f"(checks run: {n_checks_run})")
    return "\n".join(lines)


def render_json(findings, ctx) -> str:
    payload = {
        "summary": {
            "groups": ctx.group_ids,
            "events": len(ctx.events),
            "users": len(ctx.by_user),
            "error": sum(1 for f in findings if f.severity == ERROR),
            "warn": sum(1 for f in findings if f.severity == WARN),
            "info": sum(1 for f in findings if f.severity == INFO),
        },
        "findings": [f.to_dict() for f in sort_findings(findings)],
    }
    return json.dumps(payload, indent=2)


def cmd_check(args) -> int:
    cfg = load_config(args.config)
    evs = load_events(args.events) if args.events else LoadResult([], [])
    load_errors = cfg.errors + (evs.errors if args.events else [])

    ctx = Context(cfg.data, evs.data if args.events else [])

    categories = None
    if args.only:
        categories = {c.strip() for c in args.only.split(",") if c.strip()}
        unknown = categories - _CATEGORIES
        if unknown:
            print(f"error: --only got unknown categor{'y' if len(unknown) == 1 else 'ies'} "
                  f"{sorted(unknown)}; valid values are {sorted(_CATEGORIES)}", file=sys.stderr)
            return 2

    findings = load_errors + run_all(ctx, categories)
    n_run = sum(1 for c in all_checks() if categories is None or c["category"] in categories)

    if args.json:
        print(render_json(findings, ctx))
    else:
        print(render_text(findings, ctx, _supports_color(sys.stdout), n_run))

    if load_errors:
        return 2
    # A finding fails the build when its severity rank is <= threshold (lower
    # rank == more severe). "none" must therefore be a rank nothing can reach:
    # -1, NOT a large number -- every real severity (0,1,2) is <= any large
    # sentinel, which would make "none" fail on everything (the opposite of its
    # purpose as the report-but-never-block mode).
    threshold = {"error": _RANK[ERROR], "warn": _RANK[WARN], "none": -1}[args.fail_on]
    worst = min((_RANK[f.severity] for f in findings), default=99)
    return 1 if worst <= threshold else 0


def cmd_gen(args) -> int:
    gen.main([
        "--config", args.config, "--out", args.out,
        "--n", str(args.n), "--seed", str(args.seed), "--inject", args.inject,
    ])
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tripwire", description="Config + runtime checks for live experiments.")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="run checks against a config (+ optional event stream)")
    c.add_argument("--config", required=True)
    c.add_argument("--events", default=None, help="optional JSONL event stream")
    c.add_argument("--only", default=None, help="comma list: static,dynamic,forensic")
    c.add_argument("--json", action="store_true", help="machine-readable output for CI")
    c.add_argument("--fail-on", default="error", choices=["error", "warn", "none"])
    c.set_defaults(func=cmd_check)

    g = sub.add_parser("gen", help="generate a synthetic event stream from a config")
    g.add_argument("--config", required=True)
    g.add_argument("--out", required=True)
    g.add_argument("--n", type=int, default=1000)
    g.add_argument("--seed", type=int, default=7)
    g.add_argument("--inject", default="")
    g.set_defaults(func=cmd_gen)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
