"""Output-budget policy for reasoning models.

Measured 2026-11-11 against the real upstream: ``kimi-k2.6`` answered a normal
question with ``finish_reason=length`` and **empty** content, having spent all
1,200 output tokens on reasoning (4,783 characters of thinking) and none on the
answer. ``glm-5.3`` and ``kimi-k2.7-code`` did the same at other budgets. The
models were fine; the budget was not.

Retrying at the same budget reproduces it exactly — the budget is the constraint,
not the prompt. The operator's rule is the correct one: **give thinking as much
room as it needs**. Reasoning tokens are not an answer, so a budget that only
pays for thinking has not delivered anything, and the request is not done.

The asymmetry that makes this safe: a caller's ``max_tokens`` is a statement
about *how long the answer may be*, not about how long the model may think. When
a provider counts both against one number, a reasoning model can starve on a
budget that was perfectly reasonable for its answer. So the proxy escalates the
budget on that specific failure, repeatedly, without an artificial ceiling.

Two things stay bounded, because they are about not wasting money rather than
about limiting thought:

* **Rounds.** Each retry is a paid call and re-pays for reasoning. Escalation
  stops after ``token_escalation_rounds`` attempts (default 4) and surfaces the
  diagnostic instead of looping forever.
* **Growth.** Each round multiplies the budget, so the loop terminates in a few
  steps rather than inching upward.

A ceiling remains configurable (``token_escalation``) for operators who want one.
Zero means no ceiling, which is the default.
"""
from __future__ import annotations

# Budget a caller is assumed to want when they named none and the provider's own
# default proved too small. Also the floor for the first escalation, so a caller
# who asked for a tiny answer still gives the model room to think.
DEFAULT_STARTING_BUDGET = 4096

# Multiplier per escalation round.
GROWTH = 4

# Smallest caller budget treated as "I want a short answer" rather than as a
# budget that is worth escalating. Below this the request looks deliberately
# tiny, but a reasoning model still needs thinking room, so it is escalated from
# DEFAULT_STARTING_BUDGET rather than doubled.
SMALL_BUDGET = 512


def escalated_budget(current: object, ceiling: int = 0, round: int = 1) -> int | None:
    """Return a larger output budget for escalation round ``round`` (1-based).

    ``ceiling=0`` means no ceiling. ``None`` is returned only when the input is
    unusable (a value from a client request must never crash the request path);
    escalation itself is bounded by rounds, not by this function.
    """
    try:
        requested = int(current)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        requested = 0
    if requested < 0:
        requested = 0

    if requested < SMALL_BUDGET:
        budget = DEFAULT_STARTING_BUDGET
    else:
        budget = requested
    for _ in range(max(1, round)):
        budget *= GROWTH

    if ceiling and ceiling > 0:
        return min(budget, ceiling)
    return budget


def is_budget_starvation(classification: str, deliverable: bool, reason: str) -> bool:
    """True when a reply was empty because the model ran out of output budget.

    Distinct from two neighbours that must NOT escalate:

    * partial content (``truncated:<score reason>``) — the caller got most of an
      answer; re-running would pay for the same fragment;
    * any other terminal outcome — a provider error comes from the same broken
      route no matter how large the budget is.
    """
    return (classification == "truncated" and not deliverable
            and reason == "output-budget-exhausted")
