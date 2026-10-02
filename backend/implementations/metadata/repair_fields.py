"""Pure allowlisted field review; never refresh, identity repair or persistence.

Canonical facts are one atomic field group including legacy projections and
variant relationship. A partial selection cannot leave rich facts inconsistent.
"""

from urllib.parse import urlsplit

from backend.base.metadata_repair import (Field, FieldSelection,
                                          RepairError, RepairField)
from backend.base.switch_review import FrozenReviewData

VOLUME_FIELDS = (Field.TITLE, Field.YEAR, Field.PUBLISHER, Field.DESCRIPTION,
                 Field.SITE_URL, Field.VOLUME_NUMBER, Field.ALT_TITLE)
ISSUE_FIELDS = (Field.TITLE, Field.DESCRIPTION, Field.FACTS)


def field_review(local_data, target):
    local, remote = local_data.view(), target.data.view()
    volume, selected = local['volume'], local['selected']
    if (selected['provider'], selected['provider_id']) != (target.reference.provider, target.reference.provider_id):
        raise RepairError('repair_requires_same_authority')
    if volume['metadata_provider'] != selected['provider']:
        raise RepairError('selected_authority_conflict')
    if any(row['volume_id'] != volume['id'] for row in local['volume_owners']):
        raise RepairError('volume_identity_conflict')
    provider = selected['provider']
    refs = {(r['issue_id'], r['provider']): r['provider_id'] for r in local['issue_refs']}
    owners = {r['provider_id']: r['issue_id'] for r in local['issue_owners'] if r['provider'] == provider}
    remote_issues = {r['provider_id']: r for r in remote['issues']}
    mapping = {}
    for issue in local['issues']:
        identity = refs.get((issue['id'], provider))
        if identity is None or identity not in remote_issues or owners.get(identity) != issue['id']:
            raise RepairError('issue_identity_requires_separate_review')
        mapping[identity] = issue['id']
    # Assertions are evidence only. Never insert references as an incidental repair.
    volume_refs = {r['provider']: r['provider_id'] for r in local['external']}
    assertion_owners = {(r['provider'], r['provider_id']): r['issue_id'] for r in local['issue_owners']}
    for assertion in remote['assertions']:
        owner = mapping.get(assertion['owner_id'])
        if assertion['entity'] == 'volume':
            old = volume_refs.get(assertion['provider'])
        else:
            old = refs.get((owner, assertion['provider']))
            if owner is not None and assertion_owners.get((assertion['provider'], assertion['provider_id']), owner) != owner:
                raise RepairError('asserted_identity_conflict')
        if old is not None and old != assertion['provider_id']:
            raise RepairError('asserted_identity_conflict')

    result = []
    def add(scope, identity, key, before, after, supported, classification=False, reason=''):
        values = FrozenReviewData.create(dict(before=before, after=after,
            provider=provider, ownership='selected_provider' if supported else 'not_owned_by_selected_provider'))
        change = ('omitted_by_provider' if not supported else 'unchanged' if before == after
                  else 'add' if before is None else 'replace')
        result.append(RepairField(FieldSelection(scope, identity, key), values,
                                  'supported' if supported else 'unsupported', change, reason,
                                  classification_input=classification))

    values = dict(remote['volume'], alt_title=(remote['volume']['aliases'] or [None])[0])
    for key in VOLUME_FIELDS:
        supported = key.value in remote['application_fields']['volume']
        proposed = values[key.value] if supported else volume[key.value]
        if key == Field.SITE_URL and volume[key.value]:
            try:
                existing_url = urlsplit(volume[key.value])
                unsafe = (existing_url.scheme not in ('http', 'https') or not existing_url.hostname
                          or existing_url.username or existing_url.password or existing_url.query)
            except ValueError:
                unsafe = True
            if unsafe:
                # Do not copy an old credential-bearing URL into field review or
                # durable before-value history. No generic URL sanitization write.
                raise RepairError('unsafe_existing_site_url_requires_separate_review')
        # Admission suppresses unsafe URLs. That is not evidence for erasure.
        if key == Field.SITE_URL and proposed is None:
            supported, proposed = False, volume[key.value]
        add('volume', volume['id'], key, volume[key.value], proposed, supported,
            key in (Field.TITLE, Field.DESCRIPTION),
            'current_refresh_replaces_including_null' if supported else 'preserve_unowned_or_unavailable')

    records = {r['id']: r for r in local['facts']}
    affected = {e[k] for e in local['coverage'] for k in ('target_issue_id', 'source_issue_id')}
    claimed = {(c[k + '_provider'], c[k + '_provider_id']) for c in local['claims']
               if c['retired_at'] is None for k in ('target', 'source')}
    for issue in local['issues']:
        iid = issue['id']
        identity = refs[(iid, provider)]
        row, record = remote_issues[identity], records[iid]
        for key in (Field.TITLE, Field.DESCRIPTION):
            supported = key.value in remote['application_fields']['issue']
            add('issue', iid, key, issue[key.value], row[key.value] if supported else issue[key.value],
                supported, key == Field.TITLE)
        before = dict(issue_number=issue['issue_number'], calculated_issue_number=issue['calculated_issue_number'],
                      date=issue['date'], facts=record['facts'], variant_of=record['variant_of'])
        after = {k: row[k] for k in before}
        add('issue', iid, Field.FACTS, before, after, True, True,
            'coupled_number_dates_projections_variant')
        if before['variant_of'] != after['variant_of'] and (iid in affected or (provider, identity) in claimed):
            old = result.pop()
            result.append(RepairField(old.selection, old.values, 'blocked', 'blocked',
                'variant_change_with_content_claim_requires_domain_review', classification_input=True))
    bibliography = dict(publication=remote['bibliography'],
        issues=[dict(provider_id=r['provider_id'], bibliography=r['bibliography'])
                for r in remote['issues'] if r['provider_id'] in mapping])
    if remote['bibliography'] is not None or any(r['bibliography'] is not None for r in remote['issues']):
        add('volume', volume['id'], Field.BIBLIOGRAPHY, local['bibliography'], bibliography,
            True, reason='supplied_scalars_and_retained_story_observation_generations')
    return tuple(result), mapping
