const managedRetentionLabels = {
    client_managed: 'Client managed', keep: 'Keep until manually removed',
    after_import: 'Remove after safe import', ratio: 'Ratio target',
    seedtime: 'Seeding-time target', either: 'Ratio OR seeding time',
    both: 'Ratio AND seeding time'
};
function managedRemovalReason(reason) {
    const labels = {
        tracker_requirements: 'Indexer/tracker minimum seed requirements are not satisfied.',
        intake_not_complete: 'Import or file review is not complete.',
        artifact_review_required: 'An imported artifact needs review.',
        fresh_client_observation_required: 'A fresh client observation is required.',
        client_relationship_changed: 'The torrent or its category changed in the client.',
        library_identity_changed: 'The library file changed; removal needs review.',
        download_not_complete: 'The download is not complete.',
        organization_work_pending: 'Library organization or recovery is still pending.',
        cleanup_reconciliation_required: 'A previous removal has an uncertain outcome. Inspect the torrent in its client; automatic removal will not retry.'
    };
    return labels[reason] || reason.replaceAll('_', ' ');
}
function renderManagedDownloads(parent, values, review) {
    parent.replaceChildren();
    const text = (container, tag, value) => {
        const node = document.createElement(tag); node.textContent = value;
        container.appendChild(node); return node;
    };
    if (!values.length) { text(parent, 'p', 'No managed acquisitions on this page.'); return; }
    const table = document.createElement('table'); parent.appendChild(table);
    const header = document.createElement('thead'); table.appendChild(header);
    const headings = document.createElement('tr'); header.appendChild(headings);
    for (const title of ['Release / source', 'Client / protocol', 'Acquisition', 'Torrent retention', 'Actions'])
        text(headings, 'th', title).scope = 'col';
    const body = document.createElement('tbody'); table.appendChild(body);
    for (const value of values) {
        const row = document.createElement('tr'); body.appendChild(row);
        text(row, 'td', `${value.title} · ${value.source}`);
        text(row, 'td', `${value.client} · ${value.protocol === 'nzb' ? 'Usenet' : value.protocol}`);
        const acquisition = text(row, 'td', `${value.acquisition === 'completed' ? 'Imported' : value.acquisition}` +
            (value.error ? ` · ${value.error}` : ''));
        if (typeof value.observation?.progress === 'number')
            text(acquisition, 'p', `Download: ${value.observation.progress.toFixed(1)}%`);
        if (typeof value.torrent?.size === 'number')
            text(acquisition, 'p', `Payload: ${value.torrent.size} bytes`);
        if (typeof value.torrent?.dlspeed === 'number' && typeof value.torrent?.upspeed === 'number')
            text(acquisition, 'p', `Down ${value.torrent.dlspeed} B/s · Up ${value.torrent.upspeed} B/s`);
        const retention = text(row, 'td', value.torrent_state || 'Not applicable');
        if (value.torrent_state) {
            text(retention, 'p', `Ratio: ${value.torrent.ratio ?? 'unknown'} · Seeding: ${value.torrent.seeding_time ?? 'unknown'} seconds`);
            text(retention, 'p', `Policy: ${managedRetentionLabels[value.retention.mode] || 'Review required'}`);
            if (['ratio', 'either', 'both'].includes(value.retention.mode))
                text(retention, 'p', `Ratio target: ${value.retention.ratio_target}`);
            if (['seedtime', 'either', 'both'].includes(value.retention.mode))
                text(retention, 'p', `Seeding-time target: ${value.retention.seed_seconds} seconds`);
            if (Object.keys(value.requirements).length)
                text(retention, 'p', `Source minimums: ${value.requirements.seedtype || 'either'} · ratio ${value.requirements.minimumratio ?? 'not supplied'} · time ${value.requirements.minimumseedtime == null ? 'not supplied' : value.requirements.minimumseedtime + ' seconds'}`);
        }
        const actions = text(row, 'td', '');
        if (value.torrent_state && !value.torrent_state.startsWith('removed_')) {
            for (const [label, removeData] of [['Remove torrent only', false], ['Remove torrent + data', true]]) {
                const button = text(actions, 'button', label); button.type = 'button';
                if (removeData) button.className = 'danger';
                button.onclick = () => review(value, removeData);
            }
        }
    }
}

function setupManagedDownloads(apiKey) {
    const field = name => document.getElementById('managed-download-' + name);
    let offset = 0, generation = 0, pending = false;
    const status = value => { field('status').textContent = value; };
    async function load() {
        const token = ++generation;
        try {
            const result = await fetchAPI('/managed-downloads', apiKey, {offset, limit: 50});
            if (token !== generation) return;
            renderManagedDownloads(field('rows'), result.result, review);
            field('previous').disabled = offset === 0;
            field('next').disabled = result.result.length < 50;
        } catch (_) { if (token === generation) status('Managed status unavailable. Library and acquisition history are retained.'); }
    }
    async function post(path, body) {
        const response = await sendAPI('POST', path, apiKey, {}, body);
        const result = await response.json();
        if (!response.ok || result.error) throw new Error('Controlled client operation failure');
        return result.result;
    }
    async function review(value, deleteData) {
        if (pending) return;
        pending = true;
        try {
            const path = `/managed-downloads/${value.id}`;
            const preview = await post(path + '/cleanup-preview', {delete_data: deleteData});
            if (!preview.eligible) { status('Removal blocked: ' + preview.reasons.map(managedRemovalReason).join(' ')); return; }
            if (!confirm(`${deleteData ? 'Remove torrent AND its source data' : 'Remove torrent, keeping source data'} for “${value.title}”? The verified library copy remains. Seeding stops.`)) return;
            await post(path + '/cleanup', {delete_data: deleteData, confirmation: preview.confirmation});
            status('Reviewed removal completed. Library copy retained.'); await load();
        } catch (_) { status('Removal was not confirmed. Refresh and review current client/import state before retrying.'); }
        finally { pending = false; }
    }
    field('refresh').onclick = load;
    field('previous').onclick = () => { offset = Math.max(0, offset - 50); load(); };
    field('next').onclick = () => { offset += 50; load(); };
    load();
}
if (typeof document !== 'undefined' && document.getElementById('managed-download-rows'))
    usingApiKey().then(setupManagedDownloads);
