"""Self-contained statistics. No numpy/scipy on purpose.

The only non-trivial math the tool needs is a chi-square p-value for the
group-distribution drift check. Pulling in scipy for a single survival function
would mean every teammate and every CI runner needs a pip install before they
can run a quality gate. That trade isn't worth it, so we implement the
regularised incomplete gamma function (Numerical Recipes "gammp"/"gammq")
ourselves. It is ~40 lines, has no dependencies, and is accurate to ~1e-10 for
the degrees of freedom we ever see here (number of experiment groups).
"""

import math


def _gamma_series(a: float, x: float) -> float:
    """Lower regularised incomplete gamma P(a, x) via series. Good for x < a+1."""
    if x <= 0.0:
        return 0.0
    gln = math.lgamma(a)
    ap = a
    total = 1.0 / a
    delta = total
    for _ in range(1000):
        ap += 1.0
        delta *= x / ap
        total += delta
        if abs(delta) < abs(total) * 1e-14:
            break
    return total * math.exp(-x + a * math.log(x) - gln)


def _gamma_cf(a: float, x: float) -> float:
    """Upper regularised incomplete gamma Q(a, x) via continued fraction. Good for x >= a+1."""
    gln = math.lgamma(a)
    tiny = 1e-300
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-14:
            break
    return math.exp(-x + a * math.log(x) - gln) * h


def chi_square_sf(chi_square: float, df: int) -> float:
    """Survival function P(X > chi_square): the p-value.

    Small p => the observed split is unlikely under the configured weights =>
    meaningful drift.
    """
    if df <= 0:
        raise ValueError("df must be positive")
    if chi_square <= 0.0:
        return 1.0
    a = df / 2.0
    x = chi_square / 2.0
    if x < a + 1.0:
        return 1.0 - _gamma_series(a, x)
    return _gamma_cf(a, x)


def chi_square_gof(observed: dict, expected_weights: dict):
    """Chi-square goodness-of-fit of observed counts vs expected weights.

    observed         : {group: count}
    expected_weights : {group: weight}  (need not sum to 1; we normalise)

    Returns (chi_square, df, p_value, expected_counts). A configured group with
    zero observations still contributes -- that is exactly the "group never
    assigned" failure we want to surface.
    """
    groups = sorted(set(observed) | set(expected_weights))
    total = sum(observed.get(g, 0) for g in groups)
    weight_sum = sum(max(0.0, expected_weights.get(g, 0.0)) for g in groups)
    if total == 0 or weight_sum == 0:
        return 0.0, max(1, len(groups) - 1), 1.0, {}

    expected_counts = {
        g: total * max(0.0, expected_weights.get(g, 0.0)) / weight_sum for g in groups
    }
    chi_square = 0.0
    for g in groups:
        e = expected_counts[g]
        if e <= 0:
            continue  # zero-weight group; reported separately, avoid div-by-zero
        o = observed.get(g, 0)
        chi_square += (o - e) ** 2 / e
    df = max(1, len(groups) - 1)
    return chi_square, df, chi_square_sf(chi_square, df), expected_counts
