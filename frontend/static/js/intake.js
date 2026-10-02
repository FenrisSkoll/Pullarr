/* Server-owned states and plans; no client matching or path authority. */
function intakeText(parent, tag, value) {
    const node = document.createElement(tag);
    node.textContent = value == null ? '' : String(value);
    parent.appendChild(node);
    return node;
}

function renderIntakes(parent, rows, retry, recover) {
    parent.replaceChildren();
    for (const row of rows) {
        const section = document.createElement('section');
        parent.appendChild(section);
        intakeText(section, 'h2', row.title || row.id);
        intakeText(section, 'p', `${row.kind} / ${row.source} — ${row.state}`);
        intakeText(section, 'p', `Target volume ${row.volume_id}; issues ${(row.issue_ids || []).join(', ')}`);
        if (row.error) intakeText(section, 'p', row.error);
        for (const note of row.policy_notes || []) intakeText(section, 'p', note);
        for (const artifact of row.artifacts) {
            const details = document.createElement('details');
            section.appendChild(details);
            intakeText(details, 'summary', `${artifact.state}: ${artifact.path}`);
            if (artifact.error) intakeText(details, 'p', artifact.error);
            intakeText(details, 'pre', JSON.stringify(artifact.summary, null, 2));
            if (artifact.organization_job_id) {
                intakeText(details, 'p', `Organization job: ${artifact.organization_job_id} (${artifact.job_state}). Existing journal owns recovery; intake retry only reconciles it.`);
                if (recover && artifact.job_state !== 'completed') {
                    const recoverButton = intakeText(details, 'button', 'Inspect / resume existing job');
                    recoverButton.type = 'button';
                    recoverButton.addEventListener('click', () => recover(row.id, artifact.id));
                }
            }
        }
        if (row.state !== 'completed') {
            const button = intakeText(section, 'button', 'Retry observation / reconcile');
            button.type = 'button';
            button.addEventListener('click', () => retry(row.id));
        }
    }
}

async function setupIntake(apiKey) {
    const error = document.getElementById('intake-error');
    let offset = 0;
    async function refresh() {
        try {
            const [intakes, mappings] = await Promise.all([
                fetchAPI('/acquisition-intakes', apiKey, {offset}), fetchAPI('/acquisition-path-mappings', apiKey)
            ]);
            renderIntakes(document.getElementById('intake-results'), intakes.result, async id => {
                try {
                    const response = await sendAPI('POST', `/acquisition-intakes/${encodeURIComponent(id)}/retry`, apiKey);
                    if (!response.ok) throw new Error('retry');
                    await refresh();
                } catch (_) { error.textContent = 'Retry unavailable. Existing artifacts and jobs are retained.'; }
            }, async (id, artifact) => {
                const path = `/acquisition-intakes/${encodeURIComponent(id)}/artifacts/${encodeURIComponent(artifact)}/job`;
                try {
                    const job = await fetchAPI(path, apiKey);
                    if (window.confirm(`Existing organization journal:\n${JSON.stringify(job.result, null, 2)}\n\nResume this exact job? No replacement plan will be created.`)) {
                        const response = await sendAPI('POST', path, apiKey, {}, {action: 'resume'});
                        if (!response.ok) throw new Error('recovery');
                        await refresh();
                    }
                } catch (_) { error.textContent = 'Recovery requires review. The existing journal is retained.'; }
            });
            const target = document.getElementById('intake-mappings');
            document.getElementById('intake-previous').disabled = offset === 0;
            document.getElementById('intake-next').disabled = intakes.result.length < 100;
            target.replaceChildren();
            for (const mapping of mappings.result) {
                intakeText(target, 'pre', JSON.stringify(mapping, null, 2));
            }
            error.textContent = '';
        } catch (_) { error.textContent = 'Unable to load intake status.'; }
    }
    document.getElementById('intake-refresh').addEventListener('click', refresh);
    document.getElementById('intake-previous').addEventListener('click', () => { offset = Math.max(0, offset - 100); refresh(); });
    document.getElementById('intake-next').addEventListener('click', () => { offset += 100; refresh(); });
    document.getElementById('intake-mapping-form').addEventListener('submit', async event => {
        event.preventDefault();
        const data = Object.fromEntries(new FormData(event.target));
        if (!data.local_prefix) delete data.local_prefix;
        data.enabled = true;
        try {
            const response = await sendAPI('POST', '/acquisition-path-mappings', apiKey, {}, data);
            if (!response.ok) throw new Error('mapping');
            await refresh();
        } catch (_) { error.textContent = 'Mapping rejected. Check client identity, path style and local containment.'; }
    });
    await refresh();
}

if (typeof module !== 'undefined') module.exports = {renderIntakes};
if (typeof usingApiKey === 'function') usingApiKey().then(setupIntake);
