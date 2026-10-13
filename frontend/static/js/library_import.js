/* Local discovery and explicit one-step import. No draft writes during scans. */
const importRows = new Map(), importCatalogues = new Map();
let importKey, importOffset, importEditing, importGeneration = 0;
const imp = id => document.getElementById(id);
const importError = message => { imp('import-error').textContent = message; };
const identityKey = row => `${row.identity.provider}:${row.identity.id}`;
function importReason(reason) {
    if (reason?.startsWith('target_exists: ')) return `Target already exists: ${reason.slice(15)}`;
    return ({source_unavailable:'Source file is missing', source_unreadable:'Source file is unreadable',
        source_outside_library_roots:'Source is outside configured library roots',
        issue_not_in_publication:'Selected issue is not in this publication',
        file_owned_by_other_publication:'File belongs to another publication',
        file_busy_or_recovery_ambiguous:'An earlier file operation may still be active; source and target were left untouched',
        seeding_source_use_keep_in_place:'This is a seeding source; choose Keep in place',
        invalid_target_filename:'Choose a filename with the same archive extension',
        copy_verification_failed:'Copy could not be verified; source was kept',
        source_changed_before_import:'Source changed during import; retry after it is stable',
        source_changed_during_copy:'Source changed during copying; source was kept',
        OSError:'Filesystem operation failed; source was kept'}[reason] || reason || 'Import failed');
}

function filterImport() {
    const query = imp('import-filter').value.toLowerCase();
    for (const row of importRows.values())
        row.element.hidden = !!query && !`${row.data.filepath} ${row.catalogue?.title || ''} ${row.status || ''}`.toLowerCase().includes(query);
}
function setImportStatus(row, status) {
    row.status = status;
    row.element.querySelector('.import-status').textContent = status;
    filterImport();
}
function showIssueChoice(row) {
    const cell = row.element.querySelector('.issue-column'); cell.replaceChildren();
    const issues = (row.catalogue?.issues || []).filter(issue => row.issueIds.includes(issue.id));
    const label = document.createElement('span');
    label.textContent = issues.length ? issues.map(i => `${i.number}${i.title ? ' · ' + i.title : ''}`).join(', ') : 'Choose issue';
    const button = document.createElement('button'); button.type = 'button'; button.textContent = 'Edit issues';
    button.onclick = () => editIssues(row);
    cell.append(label, button);
}
async function loadCatalogue(row) {
    if (!row.identity) throw new Error('Choose a publication first');
    const key = identityKey(row);
    if (!importCatalogues.has(key)) {
        const group = [...importRows.values()].filter(item => item.identity && identityKey(item) === key).slice(0, 100);
        const request = sendAPI('POST', '/libraryimport/catalog', importKey, {}, {
            provider:row.identity.provider, provider_id:row.identity.id,
            files:group.map(item => item.data.filepath)
        }).then(response => response.json()).then(json => {
            for (const item of group) {
                item.proposal = json.result.proposals?.[item.data.filepath];
                item.catalogue = json.result;
                if (!item.manual && !item.issueIds.length && item.proposal?.issue_ids?.length)
                    item.issueIds = item.proposal.issue_ids;
                showIssueChoice(item);
            }
            return json.result;
        }).catch(error => { importCatalogues.delete(key); throw error; });
        importCatalogues.set(key, request);
    }
    row.catalogue = await importCatalogues.get(key);
    if (!row.manual && !row.issueIds.length && row.proposal?.issue_ids?.length)
        row.issueIds = row.proposal.issue_ids;
    showIssueChoice(row);
}
async function editIssues(row) {
    try { await loadCatalogue(row); }
    catch (error) { setImportStatus(row, error.message); return; }
    showIssueChoice(row);
    const panel = document.createElement('section'); panel.className = 'import-issue-editor';
    const heading = document.createElement('p');
    heading.textContent = `${row.catalogue.title} (${row.catalogue.year || 'year unknown'}) · ${identityKey(row)} · ${row.catalogue.loaded_issue_count} loaded / ${row.catalogue.reported_issue_count ?? 'unknown'} reported · ${row.catalogue.completeness}`;
    const search = document.createElement('input'); search.type = 'search';
    search.placeholder = 'Search number or title'; search.setAttribute('aria-label', 'Find issue');
    const list = document.createElement('div'); list.className = 'import-issue-list';
    const chosen = new Set(row.issueIds);
    function render() {
        list.replaceChildren();
        const matches = row.catalogue.issues.filter(i => `${i.number} ${i.title || ''}`.toLowerCase().includes(search.value.toLowerCase()));
        for (const issue of matches.slice(0, 100)) {
            const label = document.createElement('label'), input = document.createElement('input');
            input.type = 'checkbox'; input.checked = chosen.has(issue.id);
            input.onchange = () => input.checked ? chosen.add(issue.id) : chosen.delete(issue.id);
            label.append(input, document.createTextNode(` ${issue.number}${issue.title ? ' · ' + issue.title : ''}${issue.date ? ' · ' + issue.date : ''}`));
            list.appendChild(label);
        }
        if (matches.length > 100) list.append(document.createTextNode('Narrow the search for more issues.'));
    }
    search.oninput = render; render();
    const apply = document.createElement('button'); apply.type = 'button'; apply.textContent = 'Use selected issues';
    apply.onclick = () => { row.issueIds = [...chosen]; row.manual = true; showIssueChoice(row); };
    panel.append(heading, search, list, apply); row.element.querySelector('.issue-column').append(panel); search.focus();
}
function choosePublication(row, identity, title, url, load=true) {
    row.identity = identity; row.catalogue = null; row.issueIds = []; row.manual = false;
    const link = row.element.querySelector('a'); link.href = url || '#';
    link.textContent = `${title} · ${identity.provider}:${identity.id}`;
    link.title = link.textContent;
    showIssueChoice(row);
    if (load) loadCatalogue(row).catch(() => setImportStatus(row, 'Catalogue unavailable; retry Edit issues'));
}
function addImportRow(data) {
    const element = document.querySelector('.pre-build-els .li-result').cloneNode(true);
    const row = {data, element, identity:null, catalogue:null, issueIds:[], manual:false, status:''};
    importRows.set(data.row_id, row);
    element.dataset.rowid = data.row_id;
    const file = element.querySelector('.file-column'); file.textContent = data.file_title; file.title = data.filepath;
    const details = document.createElement('details'), summary = document.createElement('summary');
    summary.textContent = 'Path and evidence';
    const evidence = document.createElement('p');
    evidence.textContent = `${data.filepath} · ${data.size} bytes${data.embedded ? ' · ComicInfo: ' + data.embedded.series + ' #' + data.embedded.number : ''}`;
    details.append(summary, evidence); file.append(details);
    element.querySelector('a').textContent = 'Choose publication';
    element.querySelector('button').onclick = () => {
        importEditing = row;
        showWindow('cv-window'); imp('search-input').focus();
    };
    if (data.metadata_source) choosePublication(row, data.metadata_source, data.embedded?.series || data.file_title, '');
    else showIssueChoice(row);
    return element;
}
async function scanImport(reset=true) {
    const generation = reset ? ++importGeneration : importGeneration;
    if (reset) {
        importRows.clear(); importOffset = 0;
        document.querySelector('.proposal-list').replaceChildren();
        imp('list-window').classList.add('hidden'); imp('loading-window').classList.remove('hidden');
        imp('start-window').classList.add('hidden');
    }
    const params = {offset:importOffset, page_size:100};
    const folder = imp('folder-filter-input').value.trim();
    if (imp('target-input').value === 'false' && folder) params.folder_filter = encodeURIComponent(folder);
    try {
        const page = (await fetchAPI('/libraryimport/files', importKey, params)).result;
        if (generation !== importGeneration) return;
        importOffset = page.next_offset;
        imp('scan-summary').textContent = `${page.counts.scanned_folders} folders scanned · ${page.counts.discovered} unregistered archives · ${page.counts.already_managed} already in library`;
        const body = document.querySelector('.proposal-list');
        for (const item of page.items) body.appendChild(addImportRow(item));
        imp('import-next-page').classList.toggle('hidden', importOffset === null);
        imp('list-window').classList.remove('hidden'); imp('loading-window').classList.add('hidden');
        imp('no-result-window').classList.toggle('hidden', importRows.size > 0);
        if (reset && imp('auto-match-input').checked) suggestImportPublications();
    } catch (_) {
        imp('loading-window').classList.add('hidden'); imp('start-window').classList.remove('hidden');
        importError('Local scan failed. Check the selected folder and retry.');
    }
}
async function suggestImportPublications() {
    try {
        const results = (await fetchAPI('/libraryimport', importKey, {limit:20, auto_match:true})).result;
        const selected = new Map();
        for (const suggestion of results) {
            if (!suggestion.cv?.id) continue;
            const row = [...importRows.values()].find(item => item.data.filepath === suggestion.filepath);
            if (row && !row.identity) {
                choosePublication(row, {provider:'comicvine', id:String(suggestion.cv.id)},
                    suggestion.cv.title, suggestion.cv.link, false);
                selected.set(identityKey(row), row);
            }
        }
        for (const row of selected.values()) loadCatalogue(row).catch(() =>
            setImportStatus(row, 'Catalogue unavailable; retry Edit issues'));
    } catch (_) { imp('rate-limit-banner').classList.remove('hidden'); }
}
async function searchImport(event) {
    event.preventDefault();
    const query = imp('search-input').value.trim(); if (!query) return;
    const results = document.querySelector('.search-results'); results.replaceChildren();
    try {
        const response = await fetchAPI('/volumes/search', importKey, {query:encodeURIComponent(query),
            provider:imp('import-metadata-provider').value, presentation:'v2', artwork:'false', expand_relations:'false'});
        const groups = MetadataSearchPresentation.groups(response.result);
        const entries = groups ? groups.flatMap(group => group.results) : response.result;
        for (const entry of entries) {
            const identity = entry.metadata_source || {provider:'comicvine', id:String(entry.comicvine_id)};
            const tr = document.createElement('tr'), title = document.createElement('td'), action = document.createElement('td');
            title.textContent = `${entry.title} (${entry.year || 'year unknown'}) · ${identity.provider}:${identity.id} · ${entry.issue_count ?? 'unknown'} issues`;
            const button = document.createElement('button'); button.type = 'button'; button.textContent = 'Select';
            button.onclick = () => { if (importEditing) choosePublication(importEditing, identity, entry.title, entry.site_url); closeWindow(); };
            action.append(button); tr.append(title, action); results.append(tr);
            const bulk = document.createElement('button'); bulk.type = 'button';
            bulk.textContent = 'Apply to checked files';
            bulk.onclick = () => {
                const selected = [...importRows.values()].filter(row => row.element.querySelector('.import-file-select').checked);
                for (const row of selected) choosePublication(row, identity, entry.title, entry.site_url, false);
                if (selected.length) loadCatalogue(selected[0]).catch(() =>
                    selected.forEach(row => setImportStatus(row, 'Catalogue unavailable; retry Edit issues')));
                closeWindow();
            };
            action.append(bulk);
        }
        document.querySelector('.search-results-container').classList.remove('hidden');
    } catch (_) { imp('match-error').textContent = 'Publication search unavailable. Retry or choose another provider.'; }
}
async function submitImport(only=null) {
    const selected = (only || [...importRows.values()]).filter(row =>
        row.element.querySelector('.import-file-select').checked &&
        !row.status.startsWith('Imported') && row.status !== 'Already in library');
    if (!only) for (const row of importRows.values())
        if (!row.element.querySelector('.import-file-select').checked && !row.status)
            setImportStatus(row, 'Skipped');
    if (!selected.length) { importError('Select at least one unimported file.'); return; }
    importError(''); imp('import-button').disabled = true;
    const mode = imp('import-mode').value;
    try {
        for (let start = 0; start < selected.length; start += 100) {
            const batch = selected.slice(start, start + 100);
            const ready = batch.filter(row => row.identity && row.issueIds.length);
            for (const row of batch) if (!ready.includes(row)) setImportStatus(row, 'Failed: choose a publication and issue');
            if (!ready.length) continue;
            const response = await sendAPI('POST', '/libraryimport/submit', importKey, {}, {files:ready.map(row => ({
                source:row.data.filepath, provider:row.identity.provider, provider_id:row.identity.id,
                issue_ids:row.issueIds, mode, filename:row.filename || undefined
            }))});
            if (!response.ok) throw new Error('Import request failed');
            (await response.json()).result.results.forEach((result, index) => {
                const row = ready[index];
                setImportStatus(row, result.status === 'imported' ?
                    `Imported${result.warning ? ' · ' + result.warning : ''}` :
                    result.status === 'already_in_library' ? 'Already in library' : `Failed: ${importReason(result.reason)}`);
                if (result.reason?.startsWith('target_exists: ')) {
                    const name = document.createElement('input'); name.type = 'text';
                    name.placeholder = 'Another filename (same extension)';
                    name.setAttribute('aria-label', 'Alternative filename for ' + row.data.file_title);
                    name.onchange = () => { row.filename = name.value.trim(); };
                    row.element.querySelector('.import-status').append(name, document.createTextNode(' Or choose Keep in place and retry.'));
                }
                if (result.status === 'failed') {
                    const retry = document.createElement('button'); retry.type = 'button'; retry.textContent = 'Retry';
                    retry.onclick = () => submitImport([row]);
                    row.element.querySelector('.import-status').append(retry);
                }
            });
        }
    } catch (error) { importError(error.message); }
    finally { imp('import-button').disabled = false; }
}
usingApiKey().then(async key => {
    importKey = key;
    try { imp('import-mode').value = localStorage.getItem('pullarr.importMode') || 'keep'; } catch (_) { /* private mode */ }
    imp('import-mode').onchange = () => { try { localStorage.setItem('pullarr.importMode', imp('import-mode').value); } catch (_) { /* private mode */ } };
    imp('run-import-button').onclick = () => scanImport();
    imp('import-next-page').onclick = () => scanImport(false);
    imp('import-button').onclick = () => submitImport();
    imp('import-filter').oninput = filterImport;
    imp('selectall-input').onchange = event => {
        for (const row of importRows.values()) row.element.querySelector('.import-file-select').checked = event.target.checked;
    };
    document.querySelector('.search-bar').onsubmit = searchImport;
    document.querySelectorAll('.cancel-button').forEach(button => button.onclick = () => {
        imp('list-window').classList.add('hidden'); imp('no-result-window').classList.add('hidden');
        imp('start-window').classList.remove('hidden');
    });
    try {
        const settings = (await fetchAPI('/settings', key)).result;
        imp('import-metadata-provider').querySelector('option[value="metron"]').disabled = !settings.metron_api_token;
        imp('import-metadata-provider').querySelector('option[value="gcd"]').disabled = !settings.gcd_enabled;
    } catch (_) { /* ComicVine remains available. */ }
});
