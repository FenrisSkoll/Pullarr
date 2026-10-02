const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
    constructor(tag, doc) { this.tag = tag; this.ownerDocument = doc; this.children = []; this.textContent = ''; }
    set innerHTML(_) { throw new Error('HTML interpretation forbidden'); }
    appendChild(node) { this.children.push(node); }
    replaceChildren(...nodes) { this.children = nodes; }
    setAttribute() {}
    querySelectorAll(tag) { return this.children.flatMap(n => [...(n.tag === tag ? [n] : []), ...n.querySelectorAll(tag)]); }
}
const doc = {createElement(tag) { return new Element(tag, doc); }};
const context = {window: {confirm: () => true}};
vm.createContext(context);
for (const file of ['release_explanation', 'manual_ddl']) vm.runInContext(fs.readFileSync(`frontend/static/js/${file}.js`, 'utf8'), context);
const hostile = '<img src=x onerror=alert(1)>';
const dto = {explanation_policy: 'kapowarr-release-explanation/v1', scoring_policy: 'kapowarr-release-scoring/v1',
    state: 'compatible', headline: 'Compatible', coverage: 'Exact', score: 280,
    candidate: {raw_title: hostile, source: {name: hostile}, size_bytes: 100, observations: []},
    target: {series: 'Batman', issues: [{label: '1A'}]}, concise_entries: [], entries: []};
function text(node) { return node.textContent + node.children.map(text).join(''); }
(async () => {
    const table = doc.createElement('tbody');
    const calls = [];
    const batch = {search_id: 'operation', state: 'partial', errors: [{source: hostile, code: 'timeout'}], expires_in: 900,
        results: [{selection_id: 'selection', explanation: dto, blocked: false, download_eligible: true, force_eligible: true}]};
    context.renderManualDDL(table, batch, async (...args) => {
        calls.push(args);
        return calls.length === 1 ? {state: 'offering_selection_required', offerings: [{offering_id: 'exact-offering', explanation: dto}]} : {state: 'dispatched'};
    });
    assert.equal(calls.length, 0); // Display/rank never resolves or downloads.
    assert.ok(text(table).includes(hostile));
    await table.querySelectorAll('button').find(b => b.textContent === 'Download').onclick();
    assert.equal(calls[0][0], 'operation');
    assert.equal(calls[0][1], 'selection');
    assert.equal(calls[0][2].force, false);
    assert.ok(!JSON.stringify(calls).includes('url'));
    await table.querySelectorAll('button').find(b => b.textContent === 'Download this offering').onclick();
    assert.equal(calls[1][2].offering_id, 'exact-offering');
    assert.ok(text(table).includes('Dispatched'));
    context.renderManualDDL(table, {...batch, results: [{...batch.results[0], blocked: true, download_eligible: false,
        explanation: {...dto, state: 'rejected', score: null}}]}, async () => ({}));
    assert.equal(table.querySelectorAll('button').find(b => b.textContent === 'Download').disabled, true);
    assert.ok(!text(table).includes('Score:'));
    // Exercise the real production entry point with sendAPI's Response contract.
    const view = fs.readFileSync('frontend/static/js/view_volume.js', 'utf8');
    const start = view.indexOf('function showManualSearch(');
    const end = view.indexOf('// Renaming', start);
    const message = doc.createElement('p');
    const viewTable = {querySelector: () => table};
    context.document = {querySelector: selector => selector === '#searching-message' ? message : viewTable};
    context.hide = () => {};
    context.showWindow = () => {};
    context.volume_id = 1;
    const requests = [];
    context.sendAPI = async (...args) => { requests.push(args); return {json: async () => ({result: batch})}; };
    vm.runInContext(view.slice(start, end), context);
    context.showManualSearch('fixture-key', 5);
    await new Promise(setImmediate);
    assert.equal(requests.length, 1);
    assert.equal(requests[0][0], 'POST');
    assert.equal(requests[0][1], '/issues/5/release-search');
    assert.ok(text(table).includes(hostile));
    console.log('Manual DDL: hostile text, explicit actions, secondary offering selection, typed eligibility passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
