// Render real Library rows with both ownership and monitored progress contracts.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('frontend/static/js/volumes.js', 'utf8');

function node(kind) {
    const parts = new Map();
    const attributes = new Set();
    const classes = new Set();
    const result = {
        kind, style:{}, dataset:{}, classList:{add(value) { classes.add(value); }},
        classes,
        querySelector(selector) {
            if (!parts.has(selector)) parts.set(selector, {style:{}, setAttribute(){}});
            return parts.get(selector);
        },
        cloneNode() { return node(kind); },
        setAttribute(name) { attributes.add(name); },
        removeAttribute(name) { attributes.delete(name); },
        hasAttribute(name) { return attributes.has(name); }
    };
    return result;
}
const posters = [], table = [];
const context = {
    document: {
        querySelector() { return {}; },
        createDocumentFragment() { return {children:[], appendChild(child) { this.children.push(child); }}; }
    },
    library_els: {views: {
        list:{querySelector(selector) { return posters.find(row => row.classes.has(selector.slice(1))) || null; },
            insertBefore(fragment) { posters.push(...fragment.children); }},
        table:{querySelector(selector) { return table.find(row => row.classes.has(selector.slice(1))) || null; },
            appendChild(fragment) { table.push(...fragment.children); }}
    }},
    pre_build_els: {list_entry:node('poster'), table_entry:node('table')},
    url_base:'', icons:{monitored:'on', unmonitored:'off'},
    setIcon(element, icon, label) { element.label = label; }
};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('class LibraryEntry'),
    source.indexOf('let libraryOffset')), context);
const base = {title:'Fixture', year:2025, volume_number:1, monitored:false};
const fixtures = [
    {id:1, metadata_source:{provider:'local'}, issue_count:31, issues_downloaded:31,
        issue_count_monitored:0, issues_downloaded_monitored:0, expected:'31/31', width:100},
    {id:2, metadata_source:{provider:'local'}, issue_count:13, issues_downloaded:5,
        issue_count_monitored:0, issues_downloaded_monitored:0, expected:'5/13', width:5/13*100},
    {id:3, metadata_source:{provider:'local'}, issue_count:13, issues_downloaded:0,
        issue_count_monitored:0, issues_downloaded_monitored:0, expected:'0/13', width:0},
    ...['comicvine', 'metron', 'gcd'].map((provider,index) => ({id:index+4,
        metadata_source:{provider}, issue_count:13, issues_downloaded:9,
        issue_count_monitored:6, issues_downloaded_monitored:2, expected:'2/6', width:2/6*100}))
];
context.populateLibrary(fixtures.map(value => ({...base, ...value})), 'fixture');
assert.equal(posters.length, fixtures.length);
assert.equal(table.length, fixtures.length);
for (let index=0; index<fixtures.length; index++) {
    const fixture = fixtures[index], poster = posters[index], row = table[index];
    assert.equal(poster.querySelector('.list-prog-num').innerText, fixture.expected);
    assert.equal(row.querySelector('.table-prog-num').innerText, fixture.expected);
    assert.equal(poster.querySelector('.list-prog-bar').style.width, `${fixture.width}%`);
    assert.equal(row.querySelector('.table-prog-bar').style.width, `${fixture.width}%`);
    assert.equal(row.querySelector('.table-monitored').label, 'Unmonitored');
}
assert.equal(posters[0].querySelector('.list-prog-bar').style.backgroundColor,
    'var(--success-color)');
const entry = vm.runInContext("new LibraryEntry(1, 'fixture')", context);
entry.list_entry = posters[0]; entry.table_entry = table[0];
for (const [owned,total] of [[0,0],[NaN,0],[Infinity,13],[-1,13],[3,Infinity],[99,13]]) {
    entry.setProgressBar(owned,total);
    const width = posters[0].querySelector('.list-prog-bar').style.width;
    assert.ok(/^\d+(?:\.\d+)?%$/.test(width), width);
    assert.ok(parseFloat(width) >= 0 && parseFloat(width) <= 100);
}
entry.setProgressBar(99,13);
assert.equal(posters[0].querySelector('.list-prog-num').innerText, '99/13');
assert.equal(posters[0].querySelector('.list-prog-bar').style.width, '100%');
let downloadedStatus;
context.socket = {on(name, callback) { assert.equal(name, 'downloaded_status'); downloadedStatus = callback; }};
context.api_key = 'fixture';
vm.runInContext(source.slice(source.indexOf("socket.on(\n\t\t'downloaded_status'"),
    source.indexOf('\n\t// Socket is init', source.indexOf("socket.on(\n\t\t'downloaded_status'"))), context);
downloadedStatus({volume_id:2, downloaded_issues:[100], not_downloaded_issues:[]});
assert.equal(posters[1].querySelector('.list-prog-num').innerText, '6/13');
downloadedStatus({volume_id:2, downloaded_issues:[], not_downloaded_issues:[100]});
assert.equal(table[1].querySelector('.table-prog-num').innerText, '5/13');
downloadedStatus({volume_id:4, downloaded_issues:[101], not_downloaded_issues:[]});
assert.equal(table[3].querySelector('.table-prog-num').innerText, '3/6');
console.log('Library progress: local ownership, remote monitored counts, finite widths and socket deltas PASS');
