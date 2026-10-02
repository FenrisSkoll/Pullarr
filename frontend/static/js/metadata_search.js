/* Group presentation only: no ranking, equivalence or automatic selection. */
const MetadataSearchPresentation = (() => {
    function groups(data) { return data?.schema === 'metadata-search/v2' ? data.providers : null; }
    function heading(group, table = false) {
        const el = document.createElement(table ? 'tr' : 'h2');
        el.className = 'metadata-search-group';
        const body = table ? document.createElement('td') : el;
        if (table) { body.colSpan = 2; el.appendChild(body); }
        const statuses = {complete: group.result_count ? `${group.result_count} results` : 'No results',
            limited: `${group.result_count} results — search limit reached`, disabled: 'Disabled',
            auth_required: 'Not configured or credentials rejected', rate_limited: 'Rate limited',
            unavailable: 'Unavailable', failed: 'Search failed'};
        body.textContent = `${group.label} — ${statuses[group.status] || 'Unavailable'}`;
        if (group.reason) body.textContent += ` (${group.reason})`;
        if (group.duplicate_count) body.textContent += ` — ${group.duplicate_count} repeated identities omitted`;
        return el;
    }
    function annotations(result) {
        const refs = result.local_identity_annotations || [];
        const notes = refs.filter(ref => ref.kind !== 'exact_selected_identity').map(ref =>
            `Cross-reference on local volume ${ref.volume_id} (${ref.selected_provider} authority; reported by ${ref.provenance})`);
        if (result.identity_conflict) notes.unshift('Identity conflict: references point to different local volumes. No automatic merge or switch.');
        return notes.join(' · ');
    }
    function safeLink(value) {
        try {
            const url = new URL(value);
            return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : '';
        } catch (_) { return ''; }
    }
    return {groups, heading, annotations, safeLink};
})();
