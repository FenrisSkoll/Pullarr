// Production bibliography renderer: all remotely supplied content remains text.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
class Node {
    constructor(tag) { this.tag = tag; this.children = []; this.attributes = {}; }
    replaceChildren() { this.children = []; }
    appendChild(child) { this.children.push(child); }
    setAttribute(key, value) { this.attributes[key] = value; }
    set innerHTML(_) { throw Error('HTML interpretation forbidden'); }
    set href(_) { throw Error('Remote links not required'); }
    set src(_) { throw Error('Remote image fetch forbidden'); }
}
const context = {document: {createElement: tag => new Node(tag)}};
vm.createContext(context);
vm.runInContext(fs.readFileSync('frontend/static/js/bibliography.js', 'utf8'), context);
const root = new Node('div');
const hostile = '<img src=x onerror=alert(1)>雪';
const data = {available: true, edition: {provider: 'gcd', isbn: hostile, barcode: hostile},
    publication: {binding: 'hardcover'}, variant_of: {base_provider: 'gcd', base_provider_id: '1'},
    dates: [{source_field: 'key_date', raw_value: '2021-12-00', precision: 'month'}],
    retained_sets: [{id: 2, observation_count: 1}, {id: 1, observation_count: 1}],
    story_set: {id: 2}, stories: [{sequence: '1', title: hostile, characters: hostile,
        genre: hostile, credits: [{role: 'script', text: hostile}, {role: 'pencils', text: 'Other'}]}],
    diagnostics: [hostile], publication_diagnostics: []};
let selected;
context.renderBibliography(root, data, value => { selected = value; });
const nodes = node => [node, ...node.children.flatMap(nodes)];
const texts = nodes(root).map(n => n.textContent || '').join('\n');
assert.ok(texts.includes(`title: ${hostile}`));
assert.ok(texts.includes(`script: ${hostile}`));
assert.ok(texts.includes('pencils: Other'));
assert.ok(texts.includes('2021-12-00 (month)'));
assert.ok(texts.includes('active completeness is unproven'));
assert.ok(texts.includes('Variant of: gcd:1'));
const select = nodes(root).find(n => n.tag === 'select');
select.value = '1'; select.onchange();
assert.equal(selected, '1');
context.renderBibliography(root, {available: false}, () => {});
assert.ok(nodes(root).some(n => n.textContent?.includes('Not yet supplied')));
const view = fs.readFileSync('frontend/static/js/view_volume.js', 'utf8');
assert.ok(view.includes('bibliography.ontoggle'));
assert.ok(view.includes('/bibliography'));
console.log('Bibliography: lazy opt-in, literal hostile text, credits, history and no remote fetch passed');
