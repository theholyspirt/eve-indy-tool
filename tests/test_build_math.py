"""Tests for _max_runs_for_material — EVE's material-efficiency (ME) math.

WHY THIS FUNCTION FIRST
-----------------------
It's the highest-risk code in the project: it's subtle integer math, it has no
I/O (so it's trivial to test), and if it's wrong nothing tells you. The Build
Readiness page would just quietly report the wrong number of runs.

THE RULE BEING TESTED
---------------------
For N runs of a blueprint at material efficiency `me`, the amount of one
material you need is:

    need(N) = max(N, ceil(N * base_qty * (100 - me) / 100))

Two details that a naive implementation gets wrong, and that the cases below
are specifically designed to catch:

  1. The ceil applies to the WHOLE BATCH, not to each run. Rounding up per-run
     and multiplying overcharges you.
  2. Every run consumes AT LEAST 1 of every material, no matter how high ME is.
     That's the `max(N, ...)` floor.

_max_runs_for_material inverts this: given how much you HAVE, what's the
largest N whose need(N) still fits?
"""
import pytest

from app.routes import _max_runs_for_material


@pytest.mark.parametrize(
    "have, base_qty, me, expected, reason",
    [
        # --- Baseline: ME 0 means no discount at all. ---
        # need(n) = 10n, so 100 units buys exactly 10 runs.
        (100, 10, 0, 10, "ME 0 applies no reduction"),

        # --- Ordinary discount. ---
        # ME 10 -> need(n) = 9n. 9*11 = 99 fits in 100; 9*12 = 108 does not.
        # Note the answer is 11, NOT 10 — the discount buys a whole extra run.
        (100, 10, 10, 11, "ME 10 reduces 10/run to 9/run"),

        # --- THE IMPORTANT ONE: batch rounding, not per-run rounding. ---
        # base 3 at ME 10 -> 2.7 per run.
        #   Correct (ceil the batch): need(10) = ceil(27.0) = 27, fits in 27.
        #   Wrong  (ceil per run):    ceil(2.7) = 3/run -> 3*10 = 30, doesn't fit,
        #                             so a naive version would answer 9.
        # If someone ever "simplifies" this function, this case fails.
        (27, 3, 10, 10, "ceil applies to the batch total, not per run"),

        # --- THE OTHER IMPORTANT ONE: the minimum-1-per-run floor. ---
        # base 1 at ME 90 -> 0.1 per run. Without the max(N, ...) floor the
        # math says 5 units could cover 50 runs. EVE says every run still eats
        # at least 1, so 5 units = 5 runs.
        (5, 1, 90, 5, "each run consumes at least 1 regardless of ME"),

        # --- Guard clauses. ---
        (0, 10, 0, 0, "no stock means no runs"),
        (-5, 10, 0, 0, "negative stock is treated as none"),
        (100, 0, 0, 0, "a material with zero base quantity is not a constraint"),
    ],
)
def test_max_runs_for_material(have, base_qty, me, expected, reason):
    assert _max_runs_for_material(have, base_qty, me) == expected, reason


def test_result_never_exceeds_what_stock_allows():
    """Property check: whatever N comes back, need(N) must actually fit.

    The parametrized cases above pin down specific answers. This one asserts the
    invariant that has to hold for EVERY input — a cheap way to catch an
    off-by-one in the upper-bound guess without hand-computing more examples.
    """
    for have in range(1, 60):
        for base_qty in range(1, 8):
            for me in (0, 2, 4, 6, 8, 10):
                n = _max_runs_for_material(have, base_qty, me)
                if n == 0:
                    continue
                mod = 100 - me
                need = max(n, (n * base_qty * mod + 99) // 100)
                assert need <= have, (
                    f"returned {n} runs for have={have}, base={base_qty}, "
                    f"me={me}, but that needs {need}"
                )
                # ...and it should be the LARGEST such N: one more must not fit.
                nxt = n + 1
                need_next = max(nxt, (nxt * base_qty * mod + 99) // 100)
                assert need_next > have, (
                    f"returned {n} runs for have={have}, base={base_qty}, "
                    f"me={me}, but {nxt} would also have fit"
                )
