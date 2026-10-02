const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
    constructor(tag = 'div') { this.tag = tag; this.children = []; this.nodes = new Map(); this.value = ''; this.disabled = false; }
    appendChild(e) { this.children.push(e); return e; }
    replaceChildren() { this.children = []; }
    querySelector(key) { if (!this.nodes.has(key)) this.nodes.set(key, new Element()); return this.nodes.get(key); }
    setAttribute(k, v) { this[k] = v; }
    showModal() { this.open = true; }
    close() { this.open = false; }
    focus() { this.focused = true; }
    set innerHTML(_) { throw Error('unsafe HTML'); }
}
const context = vm.createContext({module: {exports: {}}, document: {createElement: t => new Element(t)}, console, setTimeout});
vm.runInContext(fs.readFileSync('frontend/static/js/collections.js', 'utf8'), context);
const {Controller, text, messages} = context.module.exports;
const nodes = e => [e, ...e.children.flatMap(nodes)];
const find = (e, title) => nodes(e).find(n => n.tag === 'button' && n.textContent === title);
const deferred = () => { let resolve; const promise = new Promise(r => resolve = r); return {promise, resolve}; };
(async () => {
    const root = new Element(); const calls = [];
    const ui = new Controller(root, async (...args) => { calls.push(args); return {items: [{id: 1, title: '<script>alert(1)</script>'}], next_after: 2}; });
    await ui.list(); assert.equal(calls[0][3].limit, 50); assert.ok(find(ui.el('list'), '<script>alert(1)</script>'));
    await find(ui.el('list'), 'Next Collections page').onclick(); assert.equal(calls[1][3].after, 2);
    const a = deferred(), b = deferred(); let n = 0; ui.api = () => ++n === 1 ? a.promise : b.promise;
    const old = ui.list(), fresh = ui.list(); b.resolve({items: [{title: 'new'}], next_after: null}); await fresh;
    a.resolve({items: [{title: 'old'}], next_after: null}); await old; assert.ok(find(ui.el('list'), 'new')); assert.ok(!find(ui.el('list'), 'old'));
    ui.tree = {id: 1, revision: 4, nodes: [{id: 2, title: 'Root'}]}; ui.node = {id: 2, title: 'Root', path: [2]};
    ui.api = async () => ({items: [{id: 'a'.repeat(64), revision: 7, title: '<img onerror=alert(1)>', provider: 'gcd', provider_id: '900', decision: 'pending',
        evidence: {explanation: 'Search only'}, local_match_ids: [], existing_memberships: []}], has_next: false});
    await ui.suggestions(); let submit; ui.modal = (title, build, action, callback) => { const panel = new Element(); build(panel); submit = callback; };
    find(ui.el('suggestions'), 'Review / Accept').onclick(); let sent; ui.api = async (...args) => { sent = args; }; ui.open = async () => {};
    await submit(); assert.equal(sent[2].revision, 7); assert.equal(sent[2].collection_revision, 4); assert.equal(sent[2].decision, 'accepted');
    assert.deepEqual(Object.keys(sent[2]).sort(), ['collection_revision', 'decision', 'revision']);
    assert.ok(!Object.keys(sent[2]).includes('evidence')); assert.ok(messages.search_expired.includes('saved'));
    const parent = new Element(); text(parent, 'p', '" onclick="alert(1)'); assert.equal(parent.children[0].textContent, '" onclick="alert(1)');
    const source = fs.readFileSync('frontend/static/js/collections.js', 'utf8');
    assert.ok(!source.includes('innerHTML')); assert.ok(!source.includes('auto_search: true')); assert.ok(source.includes('this.pending = true'));
    console.log('Collections JS: pagination, literal text, stale suppression, exact decisions, bounded requests and pending locks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
