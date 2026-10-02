const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
    constructor(tag = 'div') { this.tag = tag; this.children = []; this.nodes = new Map(); this.value = ''; this.dataset = {}; }
    appendChild(e) { this.children.push(e); return e; }
    replaceChildren() { this.children = []; }
    querySelector(k) { if (!this.nodes.has(k)) this.nodes.set(k, new Element()); return this.nodes.get(k); }
    setAttribute(k, v) { this[k] = v; }
    showModal() { this.open = true; }
    close() { this.open = false; }
    focus() { this.focused = true; }
    set innerHTML(_) { throw Error('Unsafe HTML'); }
}
const context = vm.createContext({module: {exports: {}}, document: {createElement: t => new Element(t)}, console, setTimeout,
    sessionStorage: {setItem() {}, getItem() { return null; }}});
vm.runInContext(fs.readFileSync('frontend/static/js/reading_orders.js', 'utf8'), context);
const {Controller, text} = context.module.exports;
const nodes = e => [e, ...e.children.flatMap(nodes)];
const find = (e, title) => nodes(e).find(n => n.tag === 'button' && n.textContent === title);
const deferred = () => { let resolve; const promise = new Promise(r => resolve=r); return {promise, resolve}; };
const order = {id: 1, title: '<script>alert(1)</script>', description: 'literal', revision: 3, source: null};
const row = (id, position) => ({id, position, canonical_id: 1, series: 'Series', number: '1.5', year: 2026,
    status: 'missing', content_elsewhere: true, wanted: false, provenance: {kind: 'cbl_import'}, refs: []});
(async () => {
    const calls = [], ui = new Controller(new Element(), async (...args) => { calls.push(args); return {order, items: [row(1,0), row(2,1)], total: 2, offset: 0, has_next: false}; });
    await ui.order(1); assert.equal(calls[0][3].limit, 50);
    assert.equal(nodes(ui.el('content')).filter(e => e.dataset.entryId).length, 2); // repeated issue, distinct entries
    assert.ok(nodes(ui.el('content')).some(e => e.textContent === order.title));
    assert.ok(nodes(ui.el('content')).some(e => String(e.textContent).includes('not exact issue ownership')));
    ui.api = async (...args) => { calls.push(args); if (args[0] === 'POST') return order; return {order, items: [], total: 0, offset: 0}; };
    await find(ui.el('content'), 'Move Down').onclick();
    assert.equal(calls.find(c => c[1].endsWith('/reorder'))[2].revision, 3);
    const pending = deferred(); let submits=0;
    const action = () => { submits++; return pending.promise; };
    const a = ui.perform(action, true), b = ui.perform(action, true); assert.equal(submits,1); pending.resolve(); await a; await b;
    const old = deferred(), fresh = deferred(); let n=0; ui.api = () => ++n===1 ? old.promise : fresh.promise;
    const p1=ui.order(1), p2=ui.order(2); fresh.resolve({order:{...order,id:2,title:'fresh'},items:[],total:0,offset:0}); await p2;
    old.resolve({order:{...order,title:'old'},items:[],total:0,offset:0}); await p1;
    assert.ok(nodes(ui.el('content')).some(e=>e.textContent==='fresh')); assert.ok(!nodes(ui.el('content')).some(e=>e.textContent==='old'));
    ui.wanted({id:'review',digest:'exact',items:[{entry_id:1,title:'Missing',number:'2',bucket:'ready'}, {entry_id:2,title:'External',bucket:'requires_add'}]});
    assert.equal(nodes(ui.el('dialog-body')).filter(e=>e.type==='checkbox').length,1);
    const parent=new Element(); text(parent,'p','<img onerror=alert(1)>'); assert.equal(parent.children[0].textContent,'<img onerror=alert(1)>');
    const source=fs.readFileSync('frontend/static/js/reading_orders.js','utf8');
    assert.ok(!source.includes('innerHTML')); assert.ok(source.includes('expected_digest: review.digest')); assert.ok(source.includes('/add-publication'));
    assert.ok(source.includes('Review Source Changes')); assert.ok(source.includes('this.selected.size >= 50'));
    console.log('Reading Orders JS: pagination, repeats, reorder revision, exact confirmation, missing/C2, pending lock, stale suppression, safe text, Wanted buckets and external Add routing passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
