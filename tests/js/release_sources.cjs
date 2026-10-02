const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('frontend/static/js/release_sources.js', 'utf8');
const nodes = {};
class Element {
    constructor() { this.children = []; this.ownerDocument = document; this.value = ''; }
    set innerHTML(_) { throw new Error('Unsafe HTML'); }
    appendChild(c) { this.children.push(c); }
    replaceChildren() { this.children = []; }
}
const document = { createElement() { return new Element(); }, querySelector(selector) { return nodes[selector] || null; } };
const ctx = {document, Set, Number};
vm.createContext(ctx);
vm.runInContext(source, ctx);
const list = new Element();
let selected;
const hostile = '<img src=x onerror=alert(1)>';
ctx.renderReleaseSources(list, [{name: hostile, mode: 'prowlarr', enabled: true}], s => selected = s);
assert.ok(list.children[0].textContent.includes(hostile));
list.children[0].onclick();
assert.equal(selected.name, hostile);
assert.equal(JSON.stringify(ctx.releaseSourceCategories('7030,7000,7030')), '[7000,7030]');
assert.equal(JSON.stringify(ctx.releaseSourceCategories('')), '[]');
assert.throws(() => ctx.releaseSourceCategories('7030&apikey=bad'));
assert.throws(() => ctx.releaseSourceCategories('0'));
for (const key of ['form', 'id', 'list', 'name', 'url', 'mode', 'priority', 'enabled', 'categories', 'key', 'status', 'new', 'test', 'delete'])
    nodes[`#release-source-${key}`] = new Element();
let calls = [];
ctx.fetchAPI = async () => ({result: []});
ctx.sendAPI = async (method, path, api, params, body) => {
    calls.push({method, path, body});
    return {json: async () => ({result: {id: 'source1', name: hostile, url: 'http://fixture/api', mode: 'newznab', enabled: true, priority: 0, categories: []}})};
};
(async () => {
    ctx.setupReleaseSources('application-fixture-key');
    nodes['#release-source-name'].value = hostile;
    nodes['#release-source-url'].value = 'http://fixture/api';
    nodes['#release-source-mode'].value = 'newznab';
    nodes['#release-source-key'].value = 'fixture-source-secret';
    nodes['#release-source-priority'].value = '0';
    await nodes['#release-source-form'].onsubmit({preventDefault() {}});
    assert.equal(calls.length, 1);
    assert.equal(calls[0].path, '/release-sources');
    assert.equal(nodes['#release-source-key'].value, '');
    assert.equal(nodes['#release-source-id'].value, 'source1');
    assert.ok(nodes['#release-source-status'].textContent.includes('No search or download'));
    console.log('Release sources: safe text, categories, real Response JSON contract, secret clearing and config-only actions passed');
})().catch(e => { console.error(e); process.exitCode = 1; });
