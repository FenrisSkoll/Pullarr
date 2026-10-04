/* Display authoritative facts only; no scoring, identity matching or URL grabs. */
function wantedText(parent, tag, text) {
    const node = document.createElement(tag);
    node.textContent = text == null ? '' : String(text);
    parent.appendChild(node);
    return node;
}
function renderWanted(parent, rows, act) {
    parent.replaceChildren();
    if (!rows.length) wantedText(parent, 'p', 'No issues in this view. Monitoring and Quality Profiles determine acquisition eligibility.');
    for (const row of rows) {
        const section = wantedText(parent, 'section', '');
        wantedText(section, 'h2', `${row.title} #${row.issue_number}`);
        const state = wantedText(section, 'span', row.lifecycle.replaceAll('_', ' '));
        state.className = 'badge';
        state.setAttribute('data-state', row.lifecycle);
        if (row.error) wantedText(section, 'p', row.error);
        if (row.acquisition_reason) wantedText(section, 'p', row.acquisition_reason === 'upgrade' ? 'Upgrade — already owned, below the effective Quality Profile cutoff.' : 'Missing — ordinary monitored acquisition.');
        if (row.quality?.assignment) {
            const assignment = row.quality.assignment, profile = assignment.profile;
            wantedText(section, 'p', assignment.conflict ? 'Quality Profile conflict — choose an explicit volume profile.' :
                `Quality Profile: ${profile?.name || 'Default'} · Current group: ${profile?.groups?.[row.quality.quality_group]?.name || 'Unknown'} · Cutoff: ${profile?.groups?.[profile.cutoff]?.name || 'Unknown'}`);
            if (row.quality.content_elsewhere) wantedText(section, 'p', 'Content represented elsewhere (C2), separate from direct ownership and quality.');
        }
        wantedText(section, 'p', `Last search: ${row.last_search_outcome || row.last_search || 'never'} ${row.last_search_error || ''}; next: ${row.next_search ? new Date(row.next_search * 1000).toISOString() : 'due'}`);
        for (const acquisition of row.acquisitions || []) {
            wantedText(section, 'p', `Acquisition: ${acquisition.intake_state || 'active'} · ${acquisition.kind || 'existing pipeline'}`);
            const detail = wantedText(section, 'details', '');
            wantedText(detail, 'summary', 'Acquisition identifiers');
            wantedText(detail, 'p', `Download: ${acquisition.acquisition_id || 'Unknown'} · Intake: ${acquisition.intake_id || 'Not observed'}`);
        }
        const link = wantedText(section, 'a', 'Volume / interactive search');
        link.href = `${typeof url_base === 'string' ? url_base : ''}/volumes/${Number(row.volume_id)}`;
        if (row.wanted && !row.decision_id && row.lifecycle !== 'existing_acquisition') {
            const button = wantedText(section, 'button', 'Search and grab now');
            button.type = 'button';
            button.addEventListener('click', () => act({action: 'search', volume_id: row.volume_id, issue_id: row.id}));
        }
        if (row.decision_state === 'review') {
            const button = wantedText(section, 'button', 'Release review hold…');
            button.type = 'button';
            button.addEventListener('click', () => {
                if (window.confirm('This does not cancel a downloader, recover a job or delete an artifact. Another grab may duplicate existing work. Inspect acquisition intake first. Release this hold and accept that risk?'))
                    act({action: 'release_review', decision_id: row.decision_id, acknowledge_duplicate_risk: true});
            });
        }
    }
}
async function setupWanted(key) {
    let offset = 0;
    let configuration;
    const status = document.getElementById('wanted-status');
    const form = document.getElementById('wanted-config');
    async function act(data) {
        const response = await sendAPI('POST', '/wanted', key, {}, data);
        if (!response.ok) { status.textContent = 'Action refused; inspect current reservation and intake.'; return; }
        await refresh();
    }
    async function refresh() {
        try {
            const state = document.getElementById('wanted-filter').value;
            const [rows, config, history, clients] = await Promise.all([
                fetchAPI('/wanted', key, {offset, ...(state ? {state} : {})}),
                fetchAPI('/wanted/configuration', key), fetchAPI('/wanted/history', key), fetchAPI('/sab-clients', key)
            ]);
            configuration = config.result.configuration;
            form.elements.mode.value = configuration.mode;
            form.elements.sab_client_id.replaceChildren();
            wantedText(form.elements.sab_client_id, 'option', 'Use the sole enabled NZB client').value = '';
            for (const client of clients.result) {
                const option = wantedText(form.elements.sab_client_id, 'option', `${client.name}${client.enabled ? '' : ' (disabled)'}`);
                option.value = client.id;
            }
            form.elements.sab_client_id.value = configuration.sab_client_id || '';
            renderWanted(document.getElementById('wanted-results'), rows.result, act);
            const target = document.getElementById('wanted-history');
            target.replaceChildren();
            for (const run of history.result) {
                const receipt = wantedText(target, 'details', '');
                wantedText(receipt, 'summary', `${run.outcome || run.state || 'Search'} · ${run.created_at ? new Date(run.created_at * 1000).toLocaleString() : 'Recorded search'}`);
                for (const [name, value] of Object.entries(run)) {
                    if (value !== null && typeof value !== 'object') wantedText(receipt, 'p', `${name.replaceAll('_', ' ')}: ${value}`);
                }
            }
            status.textContent = `Automation worker: ${config.result.health?.running ? 'Running' : 'Idle'}. ${(config.result.source_backoff || []).length} source cooldowns. ${(config.result.discovery || []).length} discovery observations.`;
            if (config.result.health?.last_error) status.textContent += ` Last worker error: ${config.result.health.last_error}`;
            document.getElementById('wanted-previous').disabled = offset === 0;
            document.getElementById('wanted-next').disabled = rows.result.length < 100;
        } catch (_) { status.textContent = 'Wanted status unavailable.'; }
    }
    form.addEventListener('submit', async event => {
        event.preventDefault();
        const response = await sendAPI('POST', '/wanted/configuration', key, {}, {...configuration,
            mode: form.elements.mode.value, sab_client_id: form.elements.sab_client_id.value || null});
        if (!response.ok) { status.textContent = 'Configuration rejected.'; return; }
        await refresh();
    });
    document.getElementById('wanted-refresh').onclick = refresh;
    document.getElementById('wanted-filter').onchange = () => { offset = 0; refresh(); };
    document.getElementById('wanted-previous').onclick = () => { offset = Math.max(0, offset - 100); refresh(); };
    document.getElementById('wanted-next').onclick = () => { offset += 100; refresh(); };
    await refresh();
}
if (typeof usingApiKey === 'function') usingApiKey().then(setupWanted);
