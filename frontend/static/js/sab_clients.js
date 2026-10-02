// Configuration and persisted observations only. No submit/search controls.
function renderSAB(container, values, edit) {
    container.replaceChildren();
    for (const value of values) {
        const node = container.ownerDocument.createElement(edit ? 'button' : 'p');
        if (edit) {
            node.type = 'button';
            node.textContent = `${value.name} (${value.enabled ? 'enabled' : 'disabled'})`;
            node.onclick = () => edit(value);
        } else {
            node.textContent = `${value.title} — ${value.source} — ${value.state}` +
                ` (${value.observation.status || 'not observed'}); category: ${value.category}` +
                (value.error ? `; ${value.error}` : '');
        }
        container.appendChild(node);
    }
}

function setupSAB(apiKey) {
    const field = name => document.querySelector(`#sab-${name}`);
    const status = text => { field('status').textContent = text; };
    function edit(value) {
        field('id').value = value.id || '';
        for (const name of ['name', 'url', 'category', 'priority']) field(name).value = value[name];
        field('enabled').checked = value.enabled;
        field('key').value = '';
        field('categories').replaceChildren();
    }
    const fresh = () => edit({name: '', url: '', category: '*', priority: -100, enabled: true});
    async function load() { renderSAB(field('list'), (await fetchAPI('/sab-clients', apiKey)).result, edit); }
    async function action(method, suffix, body) {
        try {
            const response = await sendAPI(method, `/sab-clients${suffix}`, apiKey, {}, body);
            const json = await response.json();
            if (json.error) { status(`SAB action failed: ${json.result?.code || 'configuration'}`); return null; }
            return json.result;
        } catch (_) { status('SAB action unavailable.'); return null; }
    }
    field('form').onsubmit = async event => {
        event.preventDefault();
        const id = field('id').value;
        const result = await action(id ? 'PUT' : 'POST', id ? `/${id}` : '', {
            name: field('name').value, url: field('url').value, api_key: field('key').value,
            enabled: field('enabled').checked, category: field('category').value,
            priority: Number(field('priority').value)
        });
        field('key').value = '';
        if (result) { edit(result); status('Saved. No submission was started.'); await load(); }
    };
    field('new').onclick = fresh;
    field('test').onclick = async () => {
        const id = field('id').value;
        if (!id) { status('Save a client first.'); return; }
        const result = await action('POST', `/${id}/test`, {});
        if (result) {
            field('categories').replaceChildren();
            for (const category of ['*', ...result.categories]) {
                const option = document.createElement('option');
                option.value = category;
                option.textContent = category;
                field('categories').appendChild(option);
            }
            status(`SAB ${result.version}: full API access verified. Categories are rechecked at submission.`);
        }
    };
    field('delete').onclick = async () => {
        const id = field('id').value;
        if (id && await action('DELETE', `/${id}`, {})) {
            fresh(); status('Configuration deleted. Download receipts retained.'); await load();
        }
    };
    field('refresh').onclick = async () => {
        try { renderSAB(field('jobs'), (await fetchAPI('/sab-downloads', apiKey)).result); }
        catch (_) { status('Could not load persisted download observations.'); }
    };
    load().catch(() => status('Could not load SAB configuration.'));
}
if (typeof document !== 'undefined' && document.querySelector('#sab-form'))
    usingApiKey().then(setupSAB);
