const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
class Node {
    constructor(tag) { this.tag = tag; this.children = []; this.value = ''; this.dataset = {}; }
    replaceChildren() { this.children = []; }
    appendChild(child) { this.children.push(child); }
    setAttribute() {}
    set innerHTML(_) { throw Error('HTML forbidden'); }
    set src(_) { throw Error('Remote fetch forbidden'); }
}
const context = {document: {createElement: tag => new Node(tag), createTextNode: text => ({textContent:text})}};
vm.createContext(context);
vm.runInContext(fs.readFileSync('frontend/static/js/collected_contents.js','utf8'), context);
const hostile = '<img onerror=alert(1)>雪';
const target = {provider:'gcd',provider_id:'8',series_name:hostile,number:'Annual'};
const source = {provider:'gcd',provider_id:'1',series_name:hostile,number:'[nn]',local_issue_id:1,edge_count:4};
let confirms = 0, previews = 0;
const root = new Node('div');
const actions = {preview: async (kind, body) => {
    previews++;
    assert.equal(kind,'claim'); assert.equal(body.kind,'complete_issue_containment');
    return {target,source,kind:body.kind,warning:'Operator decision',evidence_outcome:'multiple_reprint_edges',evidence:[],preview_token:'exact'};
}, confirm: async (kind, body) => { confirms++; assert.equal(body.preview_token,'exact'); }, load() {}, retire() {}, history() {}};
const data = {target,candidates:[source],claims:[{id:'c',source_provider:'gcd',source_provider_id:'1',source_series:hostile,
    source_number:'[nn]',source_local_id:1,retired_at:null,kind:'complete_issue_containment',applied_files:0,evidence_count:0}],
    coverage:[],files:[{id:1,filepath:hostile}],offset:0,next_offset:null};
context.renderCollectedContents(root,data,actions);
const all = node => [node,...node.children?.flatMap(all) || []];
assert.equal(confirms,0); assert.equal(previews,0);
assert.ok(all(root).some(n=>n.textContent?.includes(hostile)));
assert.ok(all(root).filter(n=>n.type==='checkbox').every(n=>n.checked===false));
assert.ok(all(root).some(n=>n.textContent?.includes('does not prove completeness')));
(async () => {
    await all(root).find(n=>n.textContent==='Review complete claim').onclick();
    assert.equal(previews,1); assert.equal(confirms,0);
    await all(root).find(n=>n.textContent==='Confirm this exact claim').onclick();
    assert.equal(confirms,1);
    // Exercise production setup with the real sendAPI Response/json contract.
    const panel = new Node('details'), content = new Node('div'), ownership = new Node('p');
    context.document.querySelector = selector => ({'#issue-contents':panel,'#issue-contents-content':content,'#issue-ownership':ownership})[selector];
    context.volume_id = 1;
    context.ViewEls = {issues_list:{querySelector:()=>null}};
    context.fetchAPI = async path => ({result:path.endsWith('/ownership') ?
        {issues:[{issue_id:8,state:'direct',owned:true,collected_coverage:[]}]} : data});
    const sent = [];
    context.sendAPI = async (method, path, key, params, body) => {
        sent.push({method,path,body});
        return {json: async () => ({result:path.endsWith('claim-preview') ?
            {target,source,kind:body.kind,prior_coverage_retired:0,warning:'Operator confirmation',
             evidence_outcome:'material_from_issue',evidence:[],preview_token:'response-token'} : {claim_id:'new'}})};
    };
    context.setupCollectedContents(8,'fixture-key');
    panel.open = true; await panel.ontoggle();
    await all(content).find(n=>n.textContent==='Review complete claim').onclick();
    assert.equal(sent.length,1); assert.ok(sent[0].path.endsWith('claim-preview'));
    await all(content).find(n=>n.textContent==='Confirm this exact claim').onclick();
    assert.equal(sent.length,2); assert.equal(sent[1].body.preview_token,'response-token');
    console.log('Collected contents: text-safe exact-ID preview, separate confirmation, no preselected complete coverage passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
