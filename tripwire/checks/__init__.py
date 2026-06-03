"""Check registry.

Every check is a plain function `check(ctx) -> list[Finding]`, registered with
`@register(id, category)`. Adding a new check is one function + one decorator --
no wiring, no config. That low ceremony is intentional: the brief asks whether a
teammate could "pick up and extend" this, and the honest answer should be "add a
function next to the others and it shows up in the report."
"""

from typing import Callable, List

from ..model import Context, Finding

_REGISTRY: List[dict] = []


def register(check_id: str, category: str):
    def deco(fn: Callable[[Context], List[Finding]]):
        _REGISTRY.append({"id": check_id, "category": category, "fn": fn})
        fn._check_id = check_id  # type: ignore[attr-defined]
        fn._category = category  # type: ignore[attr-defined]
        return fn

    return deco


def all_checks() -> List[dict]:
    return list(_REGISTRY)


def run_all(ctx: Context, categories=None) -> List[Finding]:
    findings: List[Finding] = []
    for entry in _REGISTRY:
        if categories and entry["category"] not in categories:
            continue
        try:
            findings.extend(entry["fn"](ctx) or [])
        except Exception as exc:  # a check must never take the tool down
            from ..model import ERROR
            findings.append(
                Finding(
                    entry["id"],
                    entry["category"],
                    ERROR,
                    "Check raised an exception",
                    f"{type(exc).__name__}: {exc}. This usually means the input is "
                    f"malformed in a way this check did not defend against.",
                )
            )
    return findings


# Import the modules so their @register decorators run.
from . import static_checks   # noqa: E402,F401
from . import dynamic_checks  # noqa: E402,F401
from . import forensic_checks  # noqa: E402,F401
