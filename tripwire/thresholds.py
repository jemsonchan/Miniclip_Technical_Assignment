"""Tunable policy thresholds, in one place.

Every number here is a *policy* call -- how much assignment drift is too much,
what fraction of missing analytics counts as "non-trivial" -- not a fact. Teams
legitimately disagree on these, and the README always intended them to be
configurable rather than buried in check bodies. So they live in one dataclass
with documented defaults, and can be overridden per-run from the config under a
top-level `tripwire.thresholds` key -- no code edit needed to retune noise.

Example config override:

    {
      "experiment": { ... },
      "tripwire": { "thresholds": { "drift_min_sample": 500, "drift_alpha": 0.0001 } }
    }

Unknown or non-numeric keys are ignored, so a typo can never crash a run -- it
just falls back to the default for that field.
"""

from dataclasses import dataclass, fields


@dataclass(frozen=True)
class Thresholds:
    # --- assignment-drift (chi-square goodness-of-fit) check ---
    # Below this many assigned users the test is statistically meaningless, so we
    # report INFO ("too small to judge") instead of crying wolf.
    drift_min_sample: int = 200
    # p-value below which we declare drift. 0.001 keeps false alarms rare on a
    # check meant to run continuously in production.
    drift_alpha: float = 0.001

    # --- missing-analytics-property check ---
    # Fraction of a given event type missing a required property.
    missing_props_warn_frac: float = 0.02   # > 2%  -> WARN
    missing_props_error_frac: float = 0.10  # > 10% -> ERROR

    @classmethod
    def from_config(cls, config: dict) -> "Thresholds":
        """Build defaults, overlaying any numeric overrides under
        config['tripwire']['thresholds']. Unknown/non-numeric keys are ignored."""
        overrides = ((config or {}).get("tripwire", {}) or {}).get("thresholds", {}) or {}
        known = {f.name for f in fields(cls)}
        clean = {
            k: v for k, v in overrides.items()
            if k in known and isinstance(v, (int, float)) and not isinstance(v, bool)
        }
        return cls(**clean)
