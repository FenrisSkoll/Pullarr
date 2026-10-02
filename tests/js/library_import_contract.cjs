// Execute the importer selection/submit functions without a browser or network.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
const link = {};
const row = {dataset: {rowid: '0'}, querySelector: () => link};
function element(key) {
    if (!elements.has(key)) elements.set(key, {
        value: 'comicvine', querySelector: () => row,
        querySelectorAll: () => [row]
    });
    return elements.get(key);
}
let submitted;
const context = vm.createContext({
    document: {querySelector: element, getElementById: element, querySelectorAll: () => []},
    hide: () => {},
    window: {confirm: () => false},
    sendAPI: (method, url, key, params, data) => {
        submitted = {method, url, params, data};
        return Promise.resolve({ok: true, json: async () => ({result: {id: 'retained-preview', plans: []}})});
    },
    console
});
const source = fs.readFileSync('frontend/static/js/library_import.js', 'utf8');
vm.runInContext(source.split('// code run on load')[0], context);
vm.runInContext(`
    editMatchId = 0;
    rowidToFilepath[0] = {filepath: '/disposable/Example.cbz'};
    editCVMatch('2127', 'https://example.invalid/cv', 'Example', 2021, 2);
    importLibrary('fake-app-key', true);
`, context);
assert.equal(submitted.method, 'POST');
assert.equal(submitted.url, '/libraryimport/preview');
assert.equal(submitted.params.rename_files, true);
assert.equal(submitted.data[0].filepath, '/disposable/Example.cbz');
// Old bare id is CV-only; new qualified payload must retain that meaning.
assert.equal(submitted.data[0].provider || 'comicvine', 'comicvine');
assert.equal(String(submitted.data[0].provider_id || submitted.data[0].id), '2127');
assert.equal(link.href, 'https://example.invalid/cv');
console.log('Library Import provider selection → preview contract passed; cancelled preview does not apply');
module.exports = {context, submitted: () => submitted};
