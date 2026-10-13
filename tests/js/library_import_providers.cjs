const assert = require('node:assert/strict');
const harness = require('./library_import_contract.cjs');

harness.finished.then(() => {
    const files = harness.submitted().body.files;
    assert.deepEqual(JSON.parse(JSON.stringify(files.map(file => [file.provider, file.provider_id]))),
        [['comicvine','2127'], ['metron','2127']]);
    assert.ok(files.every(file => !('comicvine_id' in file) && !('id' in file)));
    assert.equal(harness.second.identity.provider, 'metron');
    console.log('Library Import keeps ComicVine and Metron identities separate');
}).catch(error => { console.error(error); process.exitCode = 1; });
