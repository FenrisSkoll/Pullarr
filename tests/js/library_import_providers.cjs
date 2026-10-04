const assert = require('node:assert/strict');
const vm = require('node:vm');
const harness = require('./library_import_contract.cjs');
(async () => {
await new Promise(setImmediate);
await vm.runInContext(`
    rowidToFilepath[1] = {filepath: '/disposable/Metron.cbz', identity: null};
    editMatchId = 1;
    editMetadataMatch({provider: 'metron', id: '2127'},
        'https://metron.cloud/series/example/', 'Metron example', 2021, 2);
    liEls.proposalList.querySelectorAll = () => [
        {dataset: {rowid: '0'}}, {dataset: {rowid: '1'}}
    ];
    importLibrary('fake-app-key');
`, harness.context);
let values = JSON.parse(JSON.stringify(harness.submitted().data));
assert.deepEqual(values.map(v => [v.provider, v.provider_id]), [
    ['comicvine', '2127'], ['metron', '2127']
]);
assert.ok(values.every(v => !('id' in v) && !('comicvine_id' in v)));
await vm.runInContext(`
    selectedRows.add(0); selectedRows.add(1); editMatchId = 0;
    editMetadataMatch({provider: 'metron', id: 'opaque:unchanged'},
        'https://metron.cloud/series/example/', 'Example', null, 2);
    importLibrary('fake-app-key', true);
`, harness.context);
values = JSON.parse(JSON.stringify(harness.submitted().data));
assert.ok(values.every(v => v.provider === 'metron' && v.provider_id === 'opaque:unchanged'));
assert.equal(harness.submitted().params.rename_files, true);
console.log('Library Import mixed-provider selection, namespace and multi-select contracts passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
