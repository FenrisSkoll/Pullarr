const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('frontend/static/js/view_volume.js', 'utf8');
const part = source.slice(source.indexOf('function setCoverSource('), source.indexOf('function fillPage('));
assert.ok(part.includes('function setupManualCover('));

const control = () => ({textContent:'', hidden:false, disabled:false, focused:false,
    focus() {this.focused = true;}});
const cover = {source:control(), upload:control(), remove:control(), note:control(),
    file:{files:[{name:'fixture.png'}], value:'fixture.png', click() {this.clicked = true;}},
    feedback:control()};
const image = {dataset:{fallback:'true'}, src:'original'};
const calls = [];
class FormData {
    values = [];
    append(...args) {this.values.push(args);}
}
const context = vm.createContext({
    ViewEls:{cover, vol_data:{cover:image}}, volume_id:7, url_base:'', FormData,
    sendAPI:async (...args) => {
        calls.push(args);
        return {json:async () => ({result:args[0] === 'POST'
            ? {cover_source:'manual', cover_revision:'digest', provider_cover_available:false}
            : {cover_source:'provider', provider_cover_available:true}})};
    }
});
vm.runInContext(part, context);

(async () => {
    context.setCoverSource('missing');
    assert.equal(cover.source.textContent, 'No cover available');
    assert.equal(cover.remove.hidden, true);
    context.setupManualCover('fixture-key');
    cover.upload.onclick();
    assert.equal(cover.file.clicked, true);
    await cover.file.onchange();
    assert.equal(calls[0][0], 'POST');
    assert.equal(calls[0][1], '/volumes/7/manual-cover');
    assert.ok(calls[0][4] instanceof FormData);
    assert.equal(cover.source.textContent, 'Manual cover');
    assert.equal(cover.upload.textContent, 'Replace cover');
    assert.equal(cover.remove.hidden, false);
    assert.equal(cover.note.textContent, 'Removing this cover will show the no-cover placeholder.');
    assert.ok(image.src.endsWith('revision=digest'));
    assert.equal(cover.file.value, '');
    await cover.remove.onclick();
    assert.equal(calls[1][0], 'DELETE');
    assert.equal(cover.source.textContent, 'Provider cover');
    assert.equal(cover.remove.hidden, true);
    assert.equal(cover.upload.focused, true);
    assert.equal(cover.feedback.textContent, 'Manual cover removed.');
    const previous = image.src;
    cover.file.files = [{name:'bad.webp'}];
    context.sendAPI = async () => {throw {json:async () => ({result:{code:'invalid_cover_image'}})};};
    await cover.file.onchange();
    assert.equal(image.src, previous);
    assert.equal(cover.source.textContent, 'Provider cover');
    assert.equal(cover.feedback.textContent, 'The image could not be read.');
    assert.equal(cover.upload.disabled, false);
    console.log('Manual volume cover: upload, source, removal and focus PASS');
})().catch(error => {console.error(error); process.exitCode = 1;});
