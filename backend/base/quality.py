"""Provider/indexer-neutral quality policy. Claims never become pixel facts."""

import re
from dataclasses import asdict, dataclass
from typing import Optional

CLASSES = ('unknown', 'digital', 'hd_digital', 'sd_digital', 'scan', 'upscaled', 'hd_upscaled')
LABELS = dict(zip(CLASSES, ('Unknown', 'Digital', 'HD-Digital', 'SD-Digital', 'Scan', 'Upscaled', 'HD-Upscaled')))
MAX_TITLE = 1000
POLICY_VERSION = 'quality/v1'
ANALYZER_VERSION = 'raster-header/v1'


class QualityError(ValueError):
    """Controlled constant error, never an exception containing source paths."""


def integer(value, minimum=0, maximum=2**31-1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise QualityError('invalid_request')
    return value


@dataclass(frozen=True)
class ClaimedQuality:
    quality_class: str = 'unknown'
    source: str = 'unknown'
    resolution: str = 'unspecified'
    conflict: bool = False
    evidence: tuple[str, ...] = ()
    origin: str = 'release_title'

    def preview(self):
        return asdict(self)


def classify(title: object) -> ClaimedQuality:
    """Linear bounded explicit labels, not arbitrary regex or title keywords."""
    if not isinstance(title, str) or len(title) > MAX_TITLE:
        raise QualityError('release_title_bound')
    found = set()
    # Neither partial words nor nested/unterminated groups establish evidence.
    for match in re.finditer(r'\(([^()\[\]]{1,40})\)|\[([^()\[\]]{1,40})\]', title):
        value = next(v for v in match.groups() if v is not None).strip().casefold()
        value = re.sub(r'[\s_\-]+', '-', value)
        normalized = value.replace('-', '_')
        if normalized in CLASSES[1:]:
            found.add(normalized)
    if len(found) > 1:
        return ClaimedQuality(conflict=True, evidence=tuple(sorted(found)))
    if not found:
        return ClaimedQuality()
    value = found.pop()
    source = 'digital' if 'digital' in value else 'upscaled' if 'upscaled' in value else 'scan'
    resolution = 'hd' if value.startswith('hd_') else 'sd' if value.startswith('sd_') else 'unspecified'
    return ClaimedQuality(value, source, resolution, evidence=(value,))


def validate_policy(policy):
    """Small server-normalized snapshot; group index ascends in preference."""
    if not isinstance(policy, dict) or set(policy) != {'groups', 'cutoff', 'upgrades', 'minimum_p10'}:
        raise QualityError('invalid_policy')
    groups = policy['groups']
    if not isinstance(groups, list) or not 1 <= len(groups) <= len(CLASSES):
        raise QualityError('invalid_groups')
    seen = set()
    normalized = []
    for group in groups:
        if not isinstance(group, dict) or set(group) != {'name', 'classes', 'allowed'}:
            raise QualityError('invalid_groups')
        name, classes, allowed = group['name'], group['classes'], group['allowed']
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or type(allowed) is not bool:
            raise QualityError('invalid_groups')
        if not isinstance(classes, list) or not classes or len(classes) > len(CLASSES):
            raise QualityError('invalid_groups')
        for value in classes:
            if not isinstance(value, str) or value not in CLASSES or value in seen:
                raise QualityError('invalid_groups')
            seen.add(value)
        normalized.append(dict(name=name.strip(), classes=sorted(classes), allowed=allowed))
    if seen != set(CLASSES):
        raise QualityError('invalid_groups')
    cutoff = integer(policy['cutoff'], 0, len(groups)-1)
    if not groups[cutoff]['allowed'] or type(policy['upgrades']) is not bool:
        raise QualityError('invalid_cutoff')
    minimum = integer(policy['minimum_p10'], 0, 20000)
    return dict(groups=normalized, cutoff=cutoff, upgrades=policy['upgrades'], minimum_p10=minimum)


def default_policy():
    return dict(groups=[dict(name='Admitted (no automatic upgrades)', classes=list(CLASSES), allowed=True)],
                cutoff=0, upgrades=False, minimum_p10=0)


def group_for(policy, claims: ClaimedQuality):
    if claims.conflict:
        return None
    return next((i for i, group in enumerate(policy['groups']) if claims.quality_class in group['classes']), None)


def verified_requirements(policy, facts):
    if facts is None:
        return 'verification_unavailable'
    if facts.get('integrity') != 'valid':
        return 'integrity_failed'
    if policy['minimum_p10']:
        dimension = facts.get('short_edge', {}).get('p10')
        if dimension is None:
            return 'verification_unavailable'
        if dimension < policy['minimum_p10']:
            return 'dimension_floor_failed'
    return None


def compare(policy, candidate: ClaimedQuality, *, current: Optional[ClaimedQuality] = None,
            verified=None, post_import=False):
    """Single shared comparison; hard identity gates must run before this."""
    group = group_for(policy, candidate)
    old = group_for(policy, current) if current is not None else None
    reason = ('conflicting_claims' if candidate.conflict else 'disallowed' if group is None
              or not policy['groups'][group]['allowed'] else None)
    if reason is None and post_import:
        reason = verified_requirements(policy, verified)
    if reason:
        result = 'not_allowed'
    elif current is None:
        result = 'accepted' if post_import else 'provisional'
    elif old is None:
        result, reason = 'not_allowed', 'current_quality_unresolved'
    elif group == old:
        result, reason = 'equal', 'same_group'
    elif group is not None and group > old:
        result = 'upgrade' if post_import else 'provisional_upgrade'
    else:
        result, reason = 'downgrade', 'lower_group'
    return dict(result=result, reason=reason, group=group, current_group=old,
                provisional=not post_import, cutoff=policy['cutoff'], claims=candidate.preview())


def cutoff_satisfied(policy, claims, facts):
    group = group_for(policy, claims)
    return (group is not None and policy['groups'][group]['allowed'] and group >= policy['cutoff']
            and verified_requirements(policy, facts) is None)
