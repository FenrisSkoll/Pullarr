// Typed configuration only; no arbitrary client controls or persisted secrets.
function setupManagedClients(apiKey) {
    const field = name => document.querySelector(`#managed-${name}`);
    let selected = null, pending = false, generation = 0;
    const status = text => { field('status').textContent = text; };
    const failureCodes = new Set(['configuration', 'invalid_selection', 'resolver_expired',
        'source_resolution_failed', 'redirect_rejected', 'invalid_nzb', 'authentication',
        'unavailable', 'timeout', 'response_limit', 'invalid_response', 'category_missing',
        'submission_rejected', 'submission_ambiguous', 'remote_missing', 'remote_failed',
        'downloader_configuration_changed', 'busy', 'invalid_request', 'internal_error']);
    function failure(result) {
        const code = result?.error === 'ClientFailure' && result.result?.code;
        if (failureCodes.has(code))
            status(`Action blocked: ${code}. Reload and review current configuration.`);
        else status('Client action unavailable. Saved configuration and acquisitions are retained.');
    }
    const typeChanged = () => { field('torrent').hidden = field('kind').value !== 'qbittorrent'; };
    function edit(value = null) {
        selected = value;
        for (const key of ['name', 'url', 'category', 'kind', 'priority'])
            field(key).value = value?.[key] ?? ({category: 'pullarr', kind: 'nzbget', priority: 0}[key] ?? '');
        field('enabled').checked = value?.enabled ?? false;
        field('username').value = ''; field('password').value = '';
        field('mode').value = value?.retention.mode ?? 'client_managed';
        field('ratio').value = value?.retention.ratio_target ?? '1';
        field('seconds').value = value?.retention.seed_seconds ?? 86400;
        field('respect').checked = value?.retention.respect_minimums ?? true;
        typeChanged();
    }
    async function load() {
        const token = ++generation;
        const response = await fetchAPI('/managed-clients', apiKey);
        if (token !== generation) return;
        field('list').replaceChildren();
        for (const value of response.result) {
            const button = document.createElement('button');
            button.type = 'button';
            button.textContent = `${value.name} · ${value.kind} · ${value.enabled ? 'Enabled' : 'Disabled'} · Mapping ID: ${value.id}`;
            button.onclick = () => { if (!pending) edit(value); };
            field('list').appendChild(button);
        }
        if (!response.result.length) field('list').textContent = 'No additional clients configured.';
    }
    async function action(method, suffix, body, done) {
        if (pending) return;
        pending = true;
        field('form').querySelectorAll('button').forEach(button => { button.disabled = true; });
        try {
            const response = await sendAPI(method, '/managed-clients' + suffix, apiKey, {}, body);
            const result = await response.json();
            if (!response.ok || result.error) { failure(result); return; }
            await done(result.result);
        } catch (response) {
            // sendAPI rejects HTTP failures with the authenticated API Response.
            let result;
            try { result = await response.json(); } catch (_) { /* Transport/unparseable failure. */ }
            failure(result);
        }
        finally {
            field('password').value = ''; pending = false;
            field('form').querySelectorAll('button').forEach(button => { button.disabled = false; });
        }
    }
    field('form').onsubmit = event => {
        event.preventDefault();
        if (!field('respect').checked && !confirm('Ignore source minimum seed requirements? This may violate tracker rules.')) return;
        const configuration = {
            name: field('name').value, kind: field('kind').value, url: field('url').value,
            username: field('username').value, password: field('password').value,
            enabled: field('enabled').checked, category: field('category').value,
            priority: Number(field('priority').value),
            retention: {mode: field('mode').value, ratio_target: field('ratio').value,
                seed_seconds: Number(field('seconds').value), respect_minimums: field('respect').checked}
        };
        action(selected ? 'PUT' : 'POST', selected ? `/${selected.id}` : '',
            selected ? {revision: selected.revision, configuration} : {configuration}, async value => {
                edit(value); status('Saved. No download was started.'); await load();
            });
    };
    field('test').onclick = () => {
        if (!selected) { status('Save a client first.'); return; }
        action('POST', `/${selected.id}/test`, {revision: selected.revision}, value => {
            status(`Connected: ${value.product} ${value.version} · ${value.protocol}`);
        });
    };
    field('delete').onclick = () => {
        if (!selected || !confirm(`Delete configuration for ${selected.name}? Acquisition history remains. Active jobs block deletion.`)) return;
        action('DELETE', `/${selected.id}`, {revision: selected.revision}, async () => {
            edit(); status('Configuration deleted. History retained.'); await load();
        });
    };
    field('new').onclick = () => { if (!pending) edit(); };
    field('kind').onchange = typeChanged;
    edit(); load().catch(() => status('Could not load client configuration.'));
}
if (typeof document !== 'undefined' && document.querySelector('#managed-form'))
    usingApiKey().then(setupManagedClients);
