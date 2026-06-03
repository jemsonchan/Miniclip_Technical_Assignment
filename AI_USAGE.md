# AI usage

I used an AI assistant (Claude) throughout this task. I treated it as a fast,
literal pair-programmer: great at scaffolding and recall, in need of supervision
on judgment and anything numeric. Here's the honest breakdown.

## What I used it for

- **Scoping and framing.** I talked through the brief with it before writing
  code, specifically to pressure-test the "config as contract / events as
  evidence" framing and the static/dynamic/forensic split. That reframe is the
  spine of the whole tool and it survived the conversation, so I kept it.
- **Scaffolding the check registry and the 24 checks.** Once the architecture
  was set, generating each `check()` function from a one-line description was
  fast and the AI was good at it.
- **The chi-square implementation, the generator, and the test suite** — all
  drafted with AI assistance, then verified by me (see below).
- **Reviewing my own output** — I had it re-read the report output looking for
  noise and redundant findings, which is how I found two real problems.

## Two or three things I kept

- The incomplete-gamma chi-square p-value. I asked for a stdlib implementation
  specifically to avoid a scipy dependency, and the Numerical-Recipes-style
  series/continued-fraction split it produced was correct and compact. I kept it
  — *after* testing it (below).
- The permissive dict-based loader. The AI's first instinct and mine agreed:
  don't parse the config into strict dataclasses, because that turns the tool
  into the schema validator the brief explicitly doesn't want and crashes on the
  malformed inputs we're supposed to flag.
- The fault-injecting generator design. Being able to say `--inject drift,sticky`
  and get a stream that trips exactly those checks made the whole thing testable;
  that was a good suggestion I ran with.

## Two or three things I rejected or rewrote

- **It reached for `scipy`/`pandas` first.** I pushed back — a quality gate that
  needs a `pip install` to run is a quality gate people skip. We dropped to
  stdlib-only and implemented the one stat we needed by hand.
- **The segment-overlap check was combinatorially noisy.** Its first version
  flagged every pair of rules, including rules that were already dead (unknown
  group, empty audience). On the broken config that produced six near-identical
  warnings. I rewrote it to only compare *live* rules, which dropped it to the
  one real overlap. The brief warns against crying wolf and the first draft was
  doing exactly that.
- **The conversion-loop check reported each cycle twice** (gems→coins→gems and
  coins→gems→coins are the same loop). I added a `frozenset` dedupe. Small, but
  it's the difference between a report a human trusts and one they learn to
  ignore.
- **The balance-reconciliation check cascaded.** Once one transaction's
  `balance_after` disagreed, every later one for that user did too, flooding the
  output. I changed it to trust the reported balance going forward after the
  first mismatch, so you get one finding per break, not a wall.

## How I verified its output when it mattered

- **The chi-square is the one piece of real math, so I didn't trust it — I
  tested it.** The suite asserts the p-value against standard critical values
  (3.841/df=1, 5.991/df=2, etc. → p ≈ 0.05; 10.828/df=1 → p ≈ 0.001). If the
  implementation were subtly wrong, those tests fail.
- **Every seeded issue has a test that names the check that must catch it**, so
  "it looks like it works" became "the suite proves which check fires."
- **I used the generator to check both directions**: an injected-drift stream
  *must* trip the drift check, and a clean large stream must *not* (no false
  positive). Both are in the suite.

## Anything surprising

The most useful catch came from *reading the tool's actual output*, not from
reviewing the code. The conversion-loop double-reporting and the segment-overlap
noise both looked completely fine in the source — clean, reasonable functions —
and only revealed themselves as noise when I ran them against the broken config
and looked at the report as a teammate would. It's a good reminder that with AI
the code reads plausibly almost always; the verification that pays off is
running it and judging the result, not re-reading the function.
