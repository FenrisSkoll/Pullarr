"""Complete target acquisition and immutable admission, not library persistence."""

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from hashlib import sha256
from time import time
from urllib.parse import urlsplit

from backend.base.provider_switch import MAX_ISSUES, ProviderReference
from backend.base.switch_review import FrozenReviewData, SwitchReviewError
from backend.implementations.metadata.enrichment import VolumeFetchResult
from backend.implementations.metadata.format_evidence import \
    validate_format_evidence
from backend.implementations.metadata.persistence import fetch_input
from backend.implementations.metadata.publication_evidence import \
    validate_publication_evidence
from backend.implementations.metadata.snapshot import MetadataSnapshotProvider
from backend.internals.issue_facts import mapped_facts


class MetadataReviewProvider(ABC):
    @abstractmethod
    async def fetch_review(self, provider_id: str, issue_limit: int) -> VolumeFetchResult:
        """Complete manual fetch; no library/filesystem mutation or cache writes."""
        ...


@dataclass(frozen=True)
class AdmittedSwitchTarget:
    reference: ProviderReference
    data: FrozenReviewData
    physical: object = None
    publication: object = None


def admit(result, requested):
    """Recheck mutable transport invariants, then own only normalized data."""
    volume = result.metadata
    if (volume.provider, volume.provider_id) != (requested.provider, requested.provider_id):
        raise SwitchReviewError('target_identity_mismatch')
    if type(volume.issue_count) is not int or not 0 <= volume.issue_count <= MAX_ISSUES:
        raise SwitchReviewError('target_issue_limit')
    validate_format_evidence(result.format_evidence, volume.provider, volume.provider_id)
    validate_publication_evidence(result.publication_evidence, volume.provider, volume.provider_id)
    snapshot = result.snapshot
    if snapshot is not None:
        snapshot.__post_init__()  # Existing GCD admission, not a second variant policy.
        if snapshot.volume is not volume:
            raise SwitchReviewError('snapshot_owner_mismatch')
    elif volume.issues is None or len(volume.issues) != volume.issue_count:
        raise SwitchReviewError('incomplete_target')
    data = fetch_input(result, requested.provider)
    issues = data.pop('issues')
    ids = {row['provider_id'] for row in issues}
    if len(ids) != len(issues) or len(ids) != volume.issue_count:
        raise SwitchReviewError('duplicate_or_incomplete_target')
    facts = {item.provider_id: item for item in result.issue_facts}
    if len(facts) != len(result.issue_facts) or not set(facts) <= ids:
        raise SwitchReviewError('invalid_fact_owner')
    rich = {item.provider_id: item for item in snapshot.issues} if snapshot else {}
    for row in issues:
        ProviderReference(row['provider'], row['provider_id'])
        if row['provider'] != requested.provider or row['volume_provider_id'] != requested.provider_id:
            raise SwitchReviewError('wrong_issue_parent')
        item = rich.get(row['provider_id']) or facts.get(row['provider_id'])
        if item is not None and (item.provider != requested.provider or item.parent_id != requested.provider_id):
            raise SwitchReviewError('invalid_fact_owner')
        row['facts'] = item.facts if item else mapped_facts(row['issue_number'], row['date'], requested.provider + '_mapped')
        row['variant_of'] = item.variant_of if item else None
        row['bibliography'] = getattr(item, 'bibliography', None)
    parents = {row['provider_id']: row['variant_of'].provider_id for row in issues if row['variant_of']}
    checked = set()
    for row in issues:
        relation = row['variant_of']
        if relation and (relation.provider != requested.provider or relation.provider_id not in ids):
            raise SwitchReviewError('invalid_variant')
        current, visiting = row['provider_id'], set()
        while current in parents and current not in checked:
            if current in visiting:
                raise SwitchReviewError('cyclic_variant')
            visiting.add(current)
            current = parents[current]
        checked.update(visiting)
    assertions = {}
    for item in result.enrichment:
        ProviderReference(item.provider, item.provider_id)
        if (item.owner_provider != requested.provider or item.provider == requested.provider
                or item.entity not in ('issue', 'volume') or not item.provenance
                or (item.entity == 'volume' and item.owner_id != requested.provider_id)
                or (item.entity == 'issue' and item.owner_id not in ids)):
            raise SwitchReviewError('invalid_assertion_owner')
        key = (item.entity, item.owner_id, item.provider)
        if key in assertions and assertions[key] != item:
            raise SwitchReviewError('contradictory_assertion')
        assertions[key] = item
    cover = data.pop('cover')
    data['cover_available'] = cover is not None
    data['cover_digest'] = sha256(cover).hexdigest() if cover else None
    for field in ('site_url', 'cover_link'):
        url = data[field]
        if url:
            parsed = urlsplit(url)
            if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password or parsed.query:
                # Links are not needed for correspondence and never fetch authority.
                data[field] = None
    payload = dict(volume=data, issues=issues, assertions=tuple(assertions.values()),
        physical=result.format_evidence, publication=result.publication_evidence,
        bibliography=snapshot.publication if snapshot else None,
        receipt=asdict(snapshot.receipt) if snapshot else dict(
            policy='complete-manual-membership/v1', expected_count=volume.issue_count, acquired_at=time()),
        application_fields=dict(volume=(('title', 'year', 'publisher', 'description', 'site_url') if snapshot else
            ('title', 'year', 'publisher', 'description', 'site_url', 'volume_number', 'alt_title')),
            issue=(('issue_number', 'calculated_issue_number', 'title', 'date') if snapshot else
                   ('issue_number', 'calculated_issue_number', 'title', 'date', 'description'))),
        completeness='bounded_provider_membership_not_atomic_remote_snapshot', artwork_action='preserve_local')
    return AdmittedSwitchTarget(requested, FrozenReviewData.create(payload), result.format_evidence, result.publication_evidence)


async def acquire_target(reference):
    from backend.implementations.metadata.registry import get_volume_provider
    provider = get_volume_provider(reference.provider)
    if isinstance(provider, MetadataSnapshotProvider):
        snapshot = await provider.fetch_snapshot(reference.provider_id)
        result = VolumeFetchResult(snapshot.volume, (), snapshot=snapshot)
    elif isinstance(provider, MetadataReviewProvider):
        result = await provider.fetch_review(reference.provider_id, MAX_ISSUES)
    else:
        raise SwitchReviewError('complete_review_capability_unavailable')
    return admit(result, reference)
