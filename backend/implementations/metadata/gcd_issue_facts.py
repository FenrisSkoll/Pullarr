"""Pure GCD staging-to-canonical representation, without refresh admission.

Shared by offline fixtures and the REST snapshot adapter. Transport, registry
activation and complete-snapshot admission remain separate responsibilities.
"""

from backend.base.issue_facts import (BibliographicDate, DateKind, IssueFacts,
                                      IssueNumberFacts, VariantOf)
from backend.implementations.metadata.enrichment import ProviderIssueFacts
from backend.implementations.metadata.gcd_staging import GcdIssueSnapshot


def canonical_issue(snapshot: GcdIssueSnapshot, selected_date_field: str) -> ProviderIssueFacts:
    if selected_date_field not in ('key_date', 'on_sale_date'):
        raise ValueError('Explicit GCD date selection required')
    if snapshot.variant is not None and (snapshot.variant.issue_id != snapshot.provider_id
                                         or snapshot.variant.base_issue_id == snapshot.provider_id):
        raise ValueError('Variant relationship owner/base conflict')
    # A variant remains a distinct owner. The staging relationship is neither
    # discarded nor converted to an IdentityAssertion for its base issue.
    return ProviderIssueFacts('gcd', snapshot.provider_id, snapshot.parent_series_id,
        IssueFacts(IssueNumberFacts.interpret(snapshot.number.raw, 'gcd_staging_field', 'number'),
            (BibliographicDate.interpret(snapshot.key_date.raw, DateKind.PUBLICATION,
                'gcd_staging_field', 'key_date', zero_placeholders=True),
             BibliographicDate.interpret(snapshot.on_sale_date.raw, DateKind.ON_SALE,
                'gcd_staging_field', 'on_sale_date', zero_placeholders=True)), selected_date_field),
        None if snapshot.variant is None else VariantOf('gcd', snapshot.variant.base_issue_id, snapshot.variant.provenance))
