const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
    constructor(tag = 'div') { this.tag = tag; this.children = []; this.nodes = new Map(); this.value = ''; this.disabled = false; }
    appendChild(child) { this.children.push(child); return child; }
    replaceChildren() { this.children = []; }
    querySelector(key) { if (!this.nodes.has(key)) this.nodes.set(key, new Element()); return this.nodes.get(key); }
    setAttribute(key, value) { this[key] = value; }
    showModal() { this.open = true; }
    close() { this.open = false; }
    focus() { this.focused = true; }
    scrollIntoView() { this.scrolled = true; }
    set innerHTML(_) { throw Error('Untrusted HTML'); }
}
const context = vm.createContext({module: {exports: {}}, document: {createElement: tag => new Element(tag)}, console, setTimeout});
vm.runInContext(fs.readFileSync('frontend/static/js/maintenance.js', 'utf8'), context);
const {Controller, messages, text} = context.module.exports;
const nodes = root => [root, ...root.children.flatMap(nodes)];
const find = (root, value) => nodes(root).find(n => n.tag === 'button' && n.textContent === value);
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return {promise, resolve, reject}; };
const storage = new Map(); storage.setItem = storage.set; storage.getItem = storage.get;
const hostile = '<img src=x onerror="window.hostile=true">';
const summary = {id: 'a'.repeat(32), revision: 1, selected: 1, status: 'reviewable', scan_id: 'c'.repeat(32),
    snapshot_digest: 'd'.repeat(64), source_completeness: 'partial', source_reasons: ['bounded']};
const page = {total: 143, offset: 0, limit: 50, revision: 1, items: [{finding: {id: 'f'.repeat(64), explanation: hostile},
    selected: true, excluded: false, action: 'rename', capability: 'supported_preview', blockers: []}]};
(async () => {
    const root = new Element(); const requests = [];
    const controller = new Controller(root, async (...args) => { requests.push(args); return args[1].endsWith('/items') ? page : summary; }, storage);
    controller.saved.worklist = summary.id;
    await controller.loadWorklist();
    assert.ok(nodes(controller.el('worklist')).some(n => n.textContent === hostile));
    assert.ok(find(controller.el('worklist'), 'Select all 143 findings matching these filters'));
    assert.equal(requests[1][3].limit, 50);
    // Exact revisions/snapshots, not page-local selection or client-created evidence.
    let intent;
    controller.edit = async (operation, body) => { intent = {operation, body}; };
    find(controller.el('worklist'), 'Select all 143 findings matching these filters').onclick();
    assert.equal(intent.body.revision, 1); assert.equal(intent.body.snapshot_digest, summary.snapshot_digest);
    assert.equal(intent.operation, 'select-filtered'); assert.equal(intent.body.report_id, summary.scan_id);
    find(controller.el('worklist'), 'Select intent').onclick();
    assert.equal(intent.body.edits[0].finding_id, 'f'.repeat(64));
    assert.equal(intent.body.edits[0].action, 'rename');
    // Older async page must not replace a newer filter result.
    const first = deferred(), second = deferred(); let calls = 0;
    controller.saved.scan = 'b'.repeat(32);
    controller.api = () => (++calls === 1 ? first.promise : second.promise);
    const a = controller.findings(), b = controller.findings();
    second.resolve({total: 1, offset: 0, limit: 50, findings: [{explanation: 'New filter', severity: 'warning', inspection: 'complete'}]}); await b;
    first.resolve({total: 1, offset: 0, limit: 50, findings: [{explanation: 'Old filter'}]}); await a;
    assert.ok(nodes(controller.el('findings')).some(n => n.textContent === 'New filter'));
    assert.ok(!nodes(controller.el('findings')).some(n => n.textContent === 'Old filter'));
    // Duplicate submit suppression and safe failure; no mutation endpoint exists.
    delete controller.edit;
    const pending = deferred(); calls = 0;
    controller.api = () => { calls++; return pending.promise; };
    const edit = controller.edit('revise', {revision: 1, edits: []});
    await controller.edit('revise', {revision: 1, edits: []}); assert.equal(calls, 1);
    pending.reject({reason: 'revision_conflict'}); await edit;
    assert.equal(controller.el('message').textContent, messages.revision_conflict);
    assert.equal(controller.pending, false);
    controller.error({reason: '<script>secret</script>'});
    assert.ok(!controller.el('message').textContent.includes('secret'));
    controller.error({reason: 'review_expired'}); assert.match(controller.el('message').textContent, /application restarted/);
    // History listing and detail are separate; do not call eligibility on overview.
    requests.length = 0;
    const entry = {id: 'q', domain: 'organization', operation: 'duplicate_quarantine', state: 'complete',
        domain_state: 'completed', source: hostile, internal_storage_hidden: true, inverse_capability: 'unchecked'};
    controller.api = async (...args) => { requests.push(args); return {items: [entry], next_cursor: null}; };
    await controller.history(); assert.equal(requests.length, 1); assert.equal(requests[0][1], '/maintenance/history');
    controller.api = async () => ({entry, detail: {internal_storage_hidden: true}});
    await controller.detail(entry);
    assert.ok(nodes(controller.el('detail')).some(n => String(n.textContent).includes('Files are retained')));
    assert.ok(find(controller.el('detail'), 'Check Revert'));
    assert.ok(!nodes(controller.el('detail')).some(n => n.tag === 'button' && /Undo|Purge|Delete|Restore File/.test(n.textContent)));
    // Exact reviewed identity, preserved-state wording and immediate submit lock.
    const rename = {id: 'r'.repeat(32), revision: 2, digest: 'd'.repeat(64), origin: ['a'.repeat(32), 1, 'd'.repeat(64)],
        selected: ['f'.repeat(64)], items: [], collisions: [], mutation_count: 1,
        total: 0, offset: 0, limit: 50, apply_available: true};
    controller.api = async () => rename;
    await controller.renameReview(rename.id);
    find(controller.el('specialized'), 'Review rename confirmation').onclick();
    assert.ok(controller.el('confirm').open);
    assert.equal(controller.confirmation.body.digest, rename.digest);
    assert.match(controller.el('confirm-description').textContent, /Parent folders, associations, metadata and archive contents/);
    const lost = deferred(); let confirmations = 0;
    controller.action = async () => { confirmations++; return lost.promise; };
    const submitting = controller.submitConfirmation();
    await controller.submitConfirmation(); assert.equal(confirmations, 1);
    assert.ok(controller.el('confirm-submit').disabled);
    lost.reject(new Error('Network lost')); await submitting;
    assert.ok(!controller.el('retry-action').hidden);
    assert.equal(JSON.parse(storage.getItem(controller.storageKey + '-confirmation')).body.digest, rename.digest);
    assert.match(controller.el('message').textContent, /may have committed/);
    const exactRetry = controller.retryConfirmation;
    exactRetry.batch_id = 'maintenance-rename:r:2:' + 'd'.repeat(64);
    let mutations = 0;
    controller.action = async () => { mutations++; throw Error('must not resubmit an existing batch'); };
    controller.api = async method => { assert.equal(method, 'GET'); return {}; };
    controller.batch = async () => {};
    await controller.submitConfirmation(exactRetry);
    assert.equal(mutations, 0);
    assert.match(controller.el('message').textContent, /No mutation was resubmitted/);
    // Automatic reload/uncertain-response discovery performs GET only.
    await controller.discoverConfirmation(exactRetry);
    assert.equal(mutations, 0);
    assert.equal(controller.retryConfirmation, null);
    // Folder preservation is default; canonical intent creates a fresh review.
    const folder = {...rename, batch_id: 'maintenance-folder:fixture', canonical_custom: [],
        items: [{finding_id: 'f'.repeat(64), selected: true, source: hostile, target: hostile,
            custom_before: true, custom_after: true, inventory_complete: true, registered_count: 1,
            direct_count: 1, general_count: 0, ancillary_count: 1, directory_count: 1, blockers: [], state: 'no_changes'}]};
    controller.api = async () => folder;
    await controller.folderReview(folder.id);
    assert.ok(nodes(controller.el('specialized')).some(n => n.textContent === 'Preserve custom folder'));
    let custom;
    controller.createFolder = async (worklist, canonical) => { custom = {worklist, canonical}; };
    find(controller.el('specialized'), 'Review use of canonical folder').onclick();
    assert.equal(custom.canonical[0], 'f'.repeat(64));
    find(controller.el('specialized'), 'Review folder confirmation').onclick();
    assert.equal(controller.confirmation.kind, 'folder');
    assert.match(controller.el('confirm-description').textContent, /within their current roots/);
    // Picker retains selection across pages and never admits typed paths/IDs.
    const picked = {id: 1, title: hostile, metadata_provider: 'comicvine', year: 2020};
    controller.api = async (method, path, body, query) => {
        assert.equal(method, 'GET'); assert.equal(path, '/maintenance/volumes'); assert.equal(query.limit, 50);
        return {items: [picked], next_after: null};
    };
    await controller.volumePicker();
    await find(controller.el('volume-results'), `Select: ${hostile} (2020) · comicvine`).onclick();
    assert.equal(controller.selectedVolumes.get(1), hostile);
    const parent = new Element(); text(parent, 'p', hostile); assert.equal(parent.children[0].textContent, hostile);
    // Saved state contains handles only, not evidence, provider payloads or paths.
    controller.save('scan', 'b'.repeat(32));
    assert.ok(!storage.getItem(controller.storageKey).includes(hostile));
    // Specialized field values are display-only; confirmations carry no DTO/value.
    const repair = {id: 'm'.repeat(32), revision: 3, digest: 'e'.repeat(64), provider: 'comicvine', generation: 4,
        origin: {}, selected_count: 1, mutation_count: 1, apply_available: true, blockers: [],
        classification_action: 'preserve', bibliography_action: 'preserve', total: 1, offset: 0, limit: 50,
        items: [{key: 'volume:1:title', scope: 'volume', local_id: 1, field: 'title', before: 'Old', after: hostile,
            change: 'replace', support: 'supported', selected: true}]};
    controller.api = async () => repair;
    await controller.repairReview('metadata', repair.id);
    assert.ok(nodes(controller.el('specialized')).some(n => n.textContent === hostile));
    find(controller.el('specialized'), 'Review repair confirmation').onclick();
    assert.equal(controller.confirmation.kind, 'metadata');
    assert.deepEqual(Object.keys(controller.confirmation.body).sort(), ['confirmed', 'digest', 'revision']);
    assert.ok(!JSON.stringify(controller.confirmation).includes(hostile));
    assert.match(controller.el('confirm-description').textContent, /Files and folders will not change/);
    const metadataIdentity = controller.confirmation;
    controller.history = async () => {};
    controller.showDomainResult = async result => { assert.equal(result.id, 'receipt'); };
    controller.api = async (method, path) => { assert.equal(method, 'GET'); assert.match(path, /\/result$/); return {found: true, kind: 'metadata_receipt', id: 'receipt'}; };
    await controller.discoverConfirmation(metadataIdentity);
    assert.match(controller.el('message').textContent, /No mutation was resubmitted/);
    // Non-exact groups never receive a quarantine control, even when bytes/size look similar.
    const group = {id: 'g'.repeat(64), kind: 'same_direct_publication', quarantine: []};
    controller.api = async () => ({group, impact: [], total: 1, offset: 0, limit: 50,
        members: [{id: 1, path: hostile, size: 42, hash_verified: false, direct_issue_ids: [1], general_volume_ids: [], coverage_count: 1}]});
    await controller.duplicateGroup({id: 'q'.repeat(32), revision: 0}, group.id);
    assert.ok(!find(controller.el('duplicate-members'), 'Select this copy for quarantine'));
    assert.ok(find(controller.el('duplicate-members'), 'Keep All'));
    // Blocked recovery is not an inverse and has no force/confirm control.
    controller.action = async () => ({preview: {eligible: false, manual_inspection_required: true, reasons: ['reconciliation_conflict']}});
    await controller.historyPreview(entry, 'recovery');
    assert.ok(controller.el('specialized').focused);
    assert.ok(controller.el('specialized').scrolled);
    assert.ok(nodes(controller.el('specialized')).some(n => n.textContent === 'Manual inspection required'));
    assert.ok(!find(controller.el('specialized'), 'Continue Recovery'));
    assert.ok(!find(controller.el('specialized'), 'Force Complete'));
    // A completed creation releases the pending lock before rendering its controls.
    const fresh = new Controller(new Element(), async () => summary, storage);
    fresh.saved.scan = 'b'.repeat(32);
    fresh.loadWorklist = async () => { assert.equal(fresh.pending, false); };
    await fresh.createWorklist();
    console.log('Maintenance UI: read-only and specialized reviews, exact confirmations, durable discovery, safe text, paging, stale responses, locks, expiry and domain-specific recovery OK');
})().catch(error => { console.error(error); process.exitCode = 1; });
