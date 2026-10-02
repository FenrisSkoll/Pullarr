// Execute production renderers with hostile catalog text and precise edge semantics.
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
class Node {
    constructor(tag) { this.tag = tag; this.children = []; }
    replaceChildren() { this.children = []; }
    appendChild(child) { this.children.push(child); }
    set innerHTML(_) { throw Error('HTML forbidden'); }
    set src(_) { throw Error('Remote fetch forbidden'); }
    set href(value) { assert.match(value, /^\/volumes\/\d+#issue-\d+$/); this.link = value; }
}
const context = {document: {createElement: tag => new Node(tag)}, url_base: ''};
vm.createContext(context);
vm.runInContext(fs.readFileSync('frontend/static/js/reprint_graph.js', 'utf8'), context);
const root = new Node('div'), hostile = '<img onerror=alert(1)>雪';
const shapes = [[null,null],['100',null],[null,'200'],['100','200']];
const data = {available:true, snapshot:{observed_at:1,fingerprint:'receipt'}, offset:0,next_offset:100,
    issues:[{provider_id:'1',series_name:hostile,number:'[nn]',local_issue_id:1,volume_id:1},
        {provider_id:'2',series_name:'External',number:'1A',local_issue_id:null}],
    stories:[{provider_id:'100',title:hostile},{provider_id:'200',title:'Target'}],
    credits:[{story_id:'100',role:'script',provider_id:'50',creator_id:'9',name_id:'10',creator_name:hostile,
        name_text:hostile,credited_as:hostile,uncertain:1}], incoming:[],
    outgoing:shapes.map(([origin_story,target_story],i)=>({provider_id:String(i),origin_issue:'1',target_issue:'2',
        origin_story,target_story,notes:hostile,shape:`${origin_story?'story':'issue'}_to_${target_story?'story':'issue'}`,active:true}))};
let selected;
context.renderReprints(root,data,value=>selected=value);
const texts = () => root.children.map(n=>n.textContent||'').join('\n');
assert.ok(texts().includes(hostile));
assert.equal((texts().match(/is reprinted in/g)||[]).length,4);
assert.ok(!/contains|collects|includes complete/i.test(texts()));
assert.ok(texts().includes('External-only GCD issue 2'));
assert.ok(texts().includes('Separate from REST story observations'));
root.children.find(n=>n.textContent==='Next page').onclick(); assert.equal(selected,100);
context.renderCatalogStories(root,{available:true,stories:[{provider_id:'100',issue_id:'1',sequence:1,title:hostile}],
    credits:[],next_offset:null},()=>{},()=>{});
assert.ok(texts().includes('GCD story 100, parent issue 1'));
assert.ok(texts().includes(hostile));
context.renderReprints(root,{available:false},()=>{});
assert.ok(texts().includes('not yet synced'));
console.log('Reprint graph: four material-edge shapes, stable identities, hostile text, local-only links and pagination passed');
