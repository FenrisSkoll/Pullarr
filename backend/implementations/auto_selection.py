"""Select from existing receipts; no parsing, scoring, I/O or grab execution."""

from backend.base.auto_selection import (AutoSelectionPolicy,
                                         SelectionDecision, SelectionReason)
from backend.base.release_evaluation import Compatibility, CoverageBand
from backend.base.release_search import SearchState
from backend.implementations.release_scoring import (rank_evaluations,
                                                     ranking_key)


def select_automatically(evaluations, *, search_state: SearchState,
                         unavailable=frozenset(), policy=AutoSelectionPolicy()):
    ranked = rank_evaluations(evaluations)
    eligible = tuple(e for e in ranked if e.state == Compatibility.COMPATIBLE
                     and e.band in (CoverageBand.EXACT, CoverageBand.CONTAINING)
                     and e.candidate.candidate_id and e.candidate.acquisition.key)
    available = tuple(e for e in eligible if e.candidate.candidate_id not in unavailable)
    selected = None
    if search_state != SearchState.COMPLETE:
        reason = SelectionReason.SEARCH_INCOMPLETE
    elif not eligible:
        reason = SelectionReason.NO_ACCEPTABLE_RELEASE
    elif not available:
        reason = SelectionReason.OPERATIONALLY_BLOCKED
    elif len(available) > 1 and ranking_key(available[0])[:5] == ranking_key(available[1])[:5]:
        # Exactly the preference-bearing prefix of Phase 5B's rank. The remaining
        # source/identity/semantic keys provide presentation determinism only.
        reason = SelectionReason.QUALITY_TIE
    else:
        reason, selected = SelectionReason.UNIQUE_BEST, available[0]
    return SelectionDecision(reason, selected,
        tuple(e.candidate.candidate_id for e in available[:8]), policy.fingerprint)
