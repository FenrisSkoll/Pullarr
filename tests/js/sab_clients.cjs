const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const nodes = {};
class Element {
    constructor() { this.children = []; this.ownerDocument = document; this.value = ''; }
    set innerHTML(_) { throw new Error('Unsafe HTML'); }
    appendChild(c) { this.children.push(c); }
    replaceChildren() { this.children = []; }
}
const document = {createElement() { return new Element(); }, querySelector(s) { return nodes[s] || null; }};
const ctx = {document, Number};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync('frontend/static/js/sab_clients.js', 'utf8'), ctx);
const hostile = '<img src=x onerror=alert(1)>';
const list = new Element();
let selected;
ctx.renderSAB(list, [{name: hostile, enabled: true}], v => selected = v);
assert.ok(list.children[0].textContent.includes(hostile));
list.children[0].onclick();
assert.equal(selected.name, hostile);
ctx.renderSAB(list, [{title: hostile, source: hostile, state: 'failed', category: hostile,
    observation: {status: hostile}, error: hostile}]);
assert.ok(list.children[0].textContent.includes(hostile));
for (const key of ['form', 'id', 'list', 'name', 'url', 'category', 'priority', 'enabled',
    'categories', 'key', 'status', 'new', 'test', 'delete', 'refresh', 'jobs'])
    nodes[`#sab-${key}`] = new Element();
const calls = [];
ctx.fetchAPI = async () => ({result: []});
ctx.sendAPI = async (method, path, api, params, body) => {
    calls.push({method, path, body});
    return {json: async () => ({result: path.endsWith('/test')
        ? {version: '5.1.3', categories: [hostile]}
        : {id: 'sab1', name: hostile, url: 'http://fixture/base', category: '*', priority: -100, enabled: true}})};
};
(async () => {
    ctx.setupSAB('application-fixture-key');
    nodes['#sab-name'].value = hostile;
    nodes['#sab-url'].value = 'http://fixture/base';
    nodes['#sab-category'].value = '*';
    nodes['#sab-priority'].value = '-100';
    nodes['#sab-key'].value = 'fixture-sab-secret';
    await nodes['#sab-form'].onsubmit({preventDefault() {}});
    assert.equal(calls[0].path, '/sab-clients');
    assert.equal(nodes['#sab-key'].value, '');
    assert.equal(nodes['#sab-id'].value, 'sab1');
    assert.ok(nodes['#sab-status'].textContent.includes('No submission'));
    await nodes['#sab-test'].onclick();
    assert.equal(calls[1].method, 'POST');
    assert.equal(calls[1].path, '/sab-clients/sab1/test');
    assert.equal(nodes['#sab-categories'].children[1].textContent, hostile);
    await nodes['#sab-refresh'].onclick();
    assert.equal(calls.length, 2); // Refresh reads persisted observations, no remote action.
    console.log('SAB settings/status: text safety, secret clearing, category discovery, config-only actions passed');
})().catch(e => { console.error(e); process.exitCode = 1; });
