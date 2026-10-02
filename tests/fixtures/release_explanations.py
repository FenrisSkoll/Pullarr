"""Offline Phase 5B receipt characterization; no explanation dependency."""

from hashlib import sha256
from json import dumps

from backend.base.release_candidate import (CoverageKind, PackKind,
                                            ReleaseCoverage)
from backend.implementations.release_scoring import (evaluate_release,
                                                     preview_evaluation,
                                                     rank_evaluations)
from tests.Tbackend.features.release_scoring import (candidate, fact_candidate,
                                                     structured, target)


def evaluations():
    wanted = target()
    return tuple(evaluate_release(wanted, c) for c in (
        candidate(), candidate('Batman #6 (2016)'), candidate('Batman #1-10 (2016)'),
        candidate('Batman #1,3,5 (2016)'), candidate('Batman #5 (2011)'),
        structured(series='Batman'),
        structured(series='Batman', pack=PackKind.SERIES,
                   coverage=ReleaseCoverage(CoverageKind.PACK)),
        fact_candidate('1A'),
    ))


def receipt_digest(values):
    payload = {'evaluations': [preview_evaluation(e) for e in values],
               'ranking': [preview_evaluation(e) for e in rank_evaluations(values)]}
    return sha256(dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
