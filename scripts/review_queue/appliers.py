"""What a decision does, per kind. One table, used by Luna's bridge (the pusher
applies decided items every tick) and the CLI's `apply`, so the two can never
disagree about what approving something means."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from . import actions, captures, pr_reviews, promotions, publish, questions
from . import store as st

Applier = Callable[[st.Item], tuple[bool, str]]

# Kinds whose underlying condition is done once applied: a filed capture has
# left quarantine, an answered question and a pushed or dropped action are
# finished, and so is a merged or closed PR. Alerts resolve only when their
# producer clears them.
RESOLVES_ON_APPLY = frozenset({st.CAPTURE, st.QUESTION, st.ACTION, st.PR_REVIEW, st.PUBLISH})


def defaults(repo: Path) -> dict[str, Applier]:
    return {
        # A capture is either a quarantine file already on main (captures.py)
        # or an article the nightly compile staged privately (promotions.py).
        st.CAPTURE: lambda item: (promotions.apply(item, repo=repo) if item.source == promotions.SOURCE
                                  else captures.apply(item, repo=repo)),
        st.QUESTION: lambda item: questions.apply(item),
        st.ACTION: lambda item: actions.apply(item),
        st.PR_REVIEW: lambda item: pr_reviews.apply(item),
        st.PUBLISH: lambda item: publish.apply(item),
        st.ALERT: lambda item: (True, ""),
        st.PROBE: lambda item: (True, ""),
    }
