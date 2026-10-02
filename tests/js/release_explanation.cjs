// Actual reusable renderer, hostile text, no browser/network or scoring logic.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('frontend/static/js/release_explanation.js', 'utf8');
let markupWrites = 0;
class Element {
    constructor(tag, doc) { this.tag = tag; this.ownerDocument = doc; this.children = []; this.textContent = ''; this.attributes = {}; }
    set innerHTML(_) { markupWrites++; throw new Error('Unsafe markup write'); }
    appendChild(child) { this.children.push(child); }
    replaceChildren(...children) { this.children = children; }
    setAttribute(key, value) { this.attributes[key] = value; }
}
const document = {createElement(tag) { return new Element(tag, document); }};
const context = {};
vm.createContext(context);
vm.runInContext(source, context);
const malicious = '<img src=x onerror=alert(1)>';
const base = {
    explanation_policy: 'kapowarr-release-explanation/v1', scoring_policy: 'kapowarr-release-scoring/v1',
    state: 'compatible', headline: 'Compatible — exact issue coverage', coverage: 'exact issue coverage', score: 321,
    candidate: {raw_title: '<script>alert(1)</script>', source: {name: malicious, via: malicious},
        observations: [{origin: 'structured_source', coverage: {kind: 'single', labels: ['1A']}, pack: 'single_issue'}]},
    target: {series: malicious, series_year: 2016, authority: 'metron', issues: [{label: '1A'}]},
    concise_entries: [0], entries: [{key: 'series_exact', kind: 'positive', points: 321, message: malicious,
        evidence: [{label: 'Structured source metadata', values: [malicious]}]}]
};
function text(node) { return node.textContent + '\n' + node.children.map(text).join('\n'); }
function render(dto) {
    const container = document.createElement('main');
    const root = context.renderReleaseExplanation(container, dto);
    assert.equal(container.children.length, 1);
    assert.equal(root.attributes['aria-label'], 'Release evaluation explanation');
    assert.ok(root.children.some(n => n.tag === 'details'));
    assert.equal(markupWrites, 0);
    return text(root);
}
let rendered = render(base);
assert.ok(rendered.includes(base.candidate.raw_title));
assert.ok(rendered.includes(malicious));
assert.ok(rendered.includes('Score: 321 policy points'));
assert.ok(rendered.includes('+321'));
assert.ok(rendered.includes('1A'));
for (const state of ['rejected', 'review_required', 'undetermined']) {
    rendered = render({...base, state, score: null, entries: [{...base.entries[0], points: 0, kind: 'rejection'}]});
    assert.ok(!rendered.includes('Score:'));
    assert.ok(!rendered.includes('+0'));
}
assert.ok(render({...base, score: 0}).includes('Score: 0 policy points'));
assert.ok(render({...base, entries: [{...base.entries[0], kind: 'penalty', points: -20}]}).includes('Penalty: -20'));
assert.throws(() => render({match: true, match_issue: null}), /unavailable/);
assert.throws(() => render({...base, explanation_policy: 'future'}), /unavailable/);
assert.ok(fs.readFileSync('frontend/templates/view_volume.html', 'utf8').includes('release_explanation.js'));
// Optional real backend DTO stream, never executable source markup.
const input = fs.readFileSync(0, 'utf8').trim();
if (input) {
    const values = JSON.parse(input);
    for (const dto of values) {
        const shown = render(dto);
        assert.ok(shown.includes(dto.headline));
        assert.equal(shown.includes('Score:'), dto.score !== null);
    }
    console.log(`Backend-to-renderer DTO acceptance: ${values.length} receipts passed`);
}
console.log('Release explanation renderer: state, points, expansion, hostile text and legacy isolation passed');
