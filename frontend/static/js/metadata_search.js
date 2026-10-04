/* Search presentation never changes the selected canonical identity. */
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
    function relationships(result) {
        const labels = {continues_as: 'Continues as', continues_from: 'Continued from', related_series: 'Related series'};
        const notes = (result.relations || []).slice(0, 2).filter(r => labels[r.relation_type]).map(r =>
            `${labels[r.relation_type]} → ${r.target_title || r.target_provider + ':' + r.target_id}`);
        if (result.search_origin === 'relation') notes.unshift('Related continuation · explicit ComicVine relationship');
        return notes.join(' · ');
    }
    function artwork(ticket, apiKey, current) {
        let observer, stopped = false, running = false, used = 0;
        const pending = new Map();
        async function drain() {
            if (running || stopped || !current() || used >= 12 || !pending.size) return;
            running = true;
            const batch = [...pending.entries()].slice(0, Math.min(4, 12 - used));
            batch.forEach(([key]) => pending.delete(key));
            used += batch.length;
            try {
                const response = await sendAPI('POST', '/volumes/search/artwork', apiKey, {},
                    {ticket, identities: batch.map(([key]) => key)});
                const data = await response.json();
                if (stopped || !current()) return;
                for (const row of data.result || []) {
                    const pair = batch.find(([key]) => key === row.result_key);
                    if (pair && row.artwork_state === 'available' && typeof row.image === 'string'
                            && row.image.length <= 87408 && /^data:image\/jpeg;base64,[A-Za-z0-9+/=]+$/.test(row.image)) {
                        pair[1].querySelector('img').src = row.image;
                        pair[1].dataset.cover = row.image;
                    }
                }
            } catch (_) { /* Optional artwork keeps the normal placeholder. */ }
            finally { running = false; if (!stopped && current()) void drain(); }
        }
        if (ticket && typeof IntersectionObserver !== 'undefined') {
            observer = new IntersectionObserver(entries => {
                for (const entry of entries) {
                    if (!entry.isIntersecting) continue;
                    observer.unobserve(entry.target);
                    pending.set(entry.target.dataset.artworkKey, entry.target);
                }
                void drain();
            });
        }
        return {
            observe(entry, result) {
                if (observer && result.artwork_state === 'pending') {
                    entry.dataset.artworkKey = result.result_key;
                    observer.observe(entry);
                }
            },
            stop() { stopped = true; pending.clear(); if (observer) observer.disconnect(); }
        };
    }
    return {groups, heading, annotations, safeLink, relationships, artwork};
})();
