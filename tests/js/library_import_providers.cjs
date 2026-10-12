// Provider identities and bulk publication edits remain scoped to checked files.
const assert = require('node:assert/strict');
const vm = require('node:vm');
const harness = require('./library_import_contract.cjs');

(async () => {
    await harness.finished;
    harness.unselectedBox.checked = true;
    vm.runInContext(`
        rowidToFilepath[1] = {filepath:'/disposable/Metron.cbz',
            identity:{provider:'metron',id:'2127'}, issueIds:['401'],
            manualIssues:true, catalog:{revision:'metron-revision'}};
        rowidToFilepath[0].issueIds=['301'];
        rowidToFilepath[0].manualIssues=true;
        prepareImport('fake-app-key', false);
    `, harness.context);
    await new Promise(setImmediate);
    const values = JSON.parse(JSON.stringify(harness.submitted().data));
    assert.deepEqual(values.map(v => [v.provider, v.provider_id]), [
        ['comicvine', '2127'], ['metron', '2127']
    ]);
    assert.ok(values.every(v => !('id' in v) && !('comicvine_id' in v)));

    harness.unselectedBox.checked = false;
    vm.runInContext(`
        selectedRows.add(0); selectedRows.add(1); editMatchId=0;
        editMetadataMatch({provider:'gcd',id:'2128'},
            'https://example.invalid/gcd', 'A different edition', 2021, 2, true);
    `, harness.context);
    assert.equal(vm.runInContext('rowidToFilepath[1].identity.provider', harness.context), 'metron');
    assert.equal(vm.runInContext('rowidToFilepath[0].identity.provider', harness.context), 'gcd');
    console.log('Library Import provider namespaces and checked-row bulk assignment passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
