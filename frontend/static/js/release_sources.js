// Configuration only. No matching, searching, ranking or download actions.
function renderReleaseSources(container, sources, edit) {
    container.replaceChildren();
    for (const source of sources) {
        const button = container.ownerDocument.createElement('button');
        button.type = 'button';
        button.textContent = `${source.name} (${source.mode}; ${source.enabled ? 'enabled' : 'disabled'})`;
        button.onclick = () => edit(source);
        container.appendChild(button);
    }
}

function releaseSourceCategories(value) {
    if (!value.trim()) return [];
    const parts = value.split(',').map(v => v.trim());
    if (parts.length > 32 || parts.some(v => !/^[1-9][0-9]{0,7}$/.test(v)))
        throw new Error('Enter comma-separated positive category IDs.');
    return [...new Set(parts.map(Number))].sort((a, b) => a - b);
}

function setupReleaseSources(apiKey) {
    const field = name => document.querySelector(`#release-source-${name}`);
    const status = message => { field('status').textContent = message; };
    function edit(source) {
        field('id').value = source.id || '';
        for (const name of ['name', 'url', 'mode', 'priority']) field(name).value = source[name];
        field('enabled').checked = source.enabled;
        field('categories').value = source.categories.join(',');
        field('key').value = '';
    }
    function fresh() { edit({name: '', url: '', mode: 'prowlarr', priority: 0, enabled: true, categories: []}); }
    async function load() {
        const json = await fetchAPI('/release-sources', apiKey);
        renderReleaseSources(field('list'), json.result, edit);
    }
    async function action(method, suffix, body) {
        try {
            const response = await sendAPI(method, `/release-sources${suffix}`, apiKey, {}, body);
            const json = await response.json();
            if (json.error) { status(`Source action failed: ${json.result?.code || 'configuration'}`); return null; }
            return json.result;
        } catch (_) { status('Source action failed. Check configuration or source availability.'); return null; }
    }
    field('form').onsubmit = async event => {
        event.preventDefault();
        let categories;
        try { categories = releaseSourceCategories(field('categories').value); }
        catch (_) { status('Enter comma-separated positive category IDs.'); return; }
        const id = field('id').value;
        const result = await action(id ? 'PUT' : 'POST', id ? `/${id}` : '', {
            name: field('name').value, mode: field('mode').value, url: field('url').value,
            api_key: field('key').value, enabled: field('enabled').checked,
            priority: Number(field('priority').value), categories
        });
        field('key').value = '';
        if (result) { edit(result); status('Saved. No search or download was started.'); await load(); }
    };
    field('new').onclick = fresh;
    field('test').onclick = async () => {
        const id = field('id').value;
        if (!id) { status('Save a source first.'); return; }
        status('Checking saved capabilities…');
        const result = await action('POST', `/${id}/test`, {});
        if (result) status(`Capabilities: ${result.usable ? 'usable' : 'unavailable or partial'}. ` +
            result.sources.map(s => `${s.source}: ${s.error || 'supported'}`).join('; ') +
            '. Direct-source capabilities may not verify search authentication.');
    };
    field('delete').onclick = async () => {
        const id = field('id').value;
        if (!id) return;
        const result = await action('DELETE', `/${id}`, {});
        if (result) { fresh(); status('Source configuration deleted. Library unchanged.'); await load(); }
    };
    load().catch(() => status('Could not load source configuration.'));
}

if (typeof document !== 'undefined' && document.querySelector('#release-source-form'))
    usingApiKey().then(setupReleaseSources);
