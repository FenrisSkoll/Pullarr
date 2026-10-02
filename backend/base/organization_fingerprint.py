"""Canonical facts required by an exact plan, independent of target evaluation."""

from hashlib import sha256
from typing import Iterable, Tuple

from backend.base.naming_policy import NamingSettings
from backend.base.organization_plan import PlanningIssue, PlanningVolume


def database_fingerprint(volume: PlanningVolume, issues: Iterable[PlanningIssue],
                         roots: Iterable[Tuple[int, str]],
                         owners: Iterable[Tuple[int, str]], naming: NamingSettings) -> str:
    facts = (volume, tuple(sorted(issues, key=lambda i: i.identity.id)),
             tuple(sorted(roots)), tuple(sorted(owners)), naming)
    return sha256(repr(facts).encode('utf-8')).hexdigest()
