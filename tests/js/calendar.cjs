const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
    constructor(tag = 'div') { this.tag = tag; this.children = []; this.nodes = new Map(); this.value = ''; this.dataset = {}; }
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
vm.runInContext(fs.readFileSync('frontend/static/js/calendar.js', 'utf8'), context);
const {Controller, text, dateLabel} = context.module.exports;
const nodes = e => [e, ...e.children.flatMap(nodes)];
const find = (e, title) => nodes(e).find(n => n.tag === 'button' && n.textContent === title);
const deferred = () => { let resolve; const promise = new Promise(r => resolve = r); return {promise, resolve}; };
const item = title => ({id: 'publication:1', publication_title: title, title, effective: {date: '2027-03', precision: 'month', kind: 'on_sale'},
    status: 'external', kind: 'omnibus', monitoring_sources: ['collection'], memberships: [], file_owned: null});
(async () => {
    assert.ok(dateLabel({date: '2027-03', precision: 'month', kind: 'cover'}).includes('2027-03 (month'));
    assert.ok(!dateLabel({date: '2027', precision: 'year', kind: 'publication'}).includes('-01'));
    assert.ok(dateLabel({date: null}).includes('TBA / Date Unknown'));
    const root = new Element(), calls = [], hostile = '<img onerror=alert(1)>';
    const ui = new Controller(root, async (...args) => { calls.push(args); return {items: [item(hostile), item(hostile)], total: 2, has_next: true}; });
    await ui.load(); assert.equal(calls[0][3].limit, 50);
    assert.equal(nodes(ui.el('results')).filter(e => e.tag === 'article').length, 1);
    assert.ok(nodes(ui.el('results')).some(e => e.textContent === hostile));
    await find(ui.el('results'), 'Next releases').onclick(); assert.equal(calls[1][3].offset, 50);
    const a = deferred(), b = deferred(); let n = 0; ui.api = () => ++n === 1 ? a.promise : b.promise;
    const old = ui.load(), fresh = ui.load(); b.resolve({items: [item('new')], total: 1}); await fresh;
    a.resolve({items: [item('old')], total: 1}); await old;
    assert.ok(nodes(ui.el('results')).some(e => e.textContent === 'new'));
    assert.ok(!nodes(ui.el('results')).some(e => e.textContent === 'old'));
    ui.api = async () => ({...item(hostile), evidence: [], date_policy: 'Safe policy', refs: [{provider: 'comicvine'}], publication_id: 1});
    await ui.detail('publication:1'); assert.ok(find(ui.el('dialog-body'), 'Add to Library'));
    assert.ok(ui.el('dialog').open);
    const p = new Element(); text(p, 'p', '<script>alert(1)</script>'); assert.equal(p.children[0].textContent, '<script>alert(1)</script>');
    const source = fs.readFileSync('frontend/static/js/calendar.js', 'utf8');
    assert.ok(!source.includes('innerHTML')); assert.ok(!source.includes('new Date(value.date)')); assert.ok(source.includes('this.pending = true'));
    assert.ok(source.includes('/collections/publications/')); assert.ok(source.includes('tries < 450')); assert.ok(source.includes('2000'));
    console.log('Calendar JS: precision/TBA, bounded paging, stale response suppression, duplicate suppression, literal text, detail, Add routing, polling and pending locks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
