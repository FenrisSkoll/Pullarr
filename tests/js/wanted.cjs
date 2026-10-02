const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
    constructor() { this.children = []; this.handlers = {}; }
    set innerHTML(_) { throw new Error('Unsafe HTML'); }
    appendChild(node) { this.children.push(node); }
    replaceChildren() { this.children = []; }
    addEventListener(event, handler) { this.handlers[event] = handler; }
    setAttribute(name, value) { this[name] = value; }
}
const document = {createElement() { return new Element(); }};
const context = {document, window: {confirm: () => true}};
vm.createContext(context);
vm.runInContext(fs.readFileSync('frontend/static/js/wanted.js', 'utf8'), context);
const hostile = '<img src=x onerror=alert(1)>';
const parent = new Element();
let selected;
context.renderWanted(parent, [{id: 5, volume_id: 1, title: hostile, issue_number: '1A',
    wanted: true, lifecycle: 'review', error: hostile, decision_id: 'server-decision', decision_state: 'review',
    acquisitions: [{intake_id: hostile}]}], value => selected = value);
assert.equal(parent.children[0].children[0].textContent, `${hostile} #1A`);
parent.children[0].children.at(-1).handlers.click();
assert.equal(selected.decision_id, 'server-decision');
assert.equal(selected.acknowledge_duplicate_risk, true);
assert.equal(selected.action, 'release_review');
console.log('Wanted: text-safe diagnostics, exact server identity, explicit review-release acknowledgement passed');
