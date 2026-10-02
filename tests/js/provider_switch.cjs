const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
    constructor(tag='div') { this.tag=tag; this.children=[]; this.nodes=new Map(); this.dataset={volumeId:'1'}; this.hidden=false; this.value=''; }
    appendChild(child) { this.children.push(child); return child; }
    replaceChildren() { this.children=[]; }
    querySelector(id) { if (!this.nodes.has(id)) this.nodes.set(id,new Element()); return this.nodes.get(id); }
    showModal() { this.open=true; }
    close() { this.open=false; }
    set innerHTML(_) { throw Error('Untrusted HTML'); }
}
const context = vm.createContext({module:{exports:{}}, document:{createElement:tag=>new Element(tag)}, URL, url_base:'', console});
vm.runInContext(fs.readFileSync('frontend/static/js/metadata_search.js','utf8'),context);
vm.runInContext(fs.readFileSync('frontend/static/js/provider_switch.js','utf8'),context);
const {Controller,text,messages}=context.module.exports;
const hostile='<img onerror="alert(1)"> 雪 "';
const review=()=>({session_id:'s'.repeat(43),revision:2,expires_in:800,overrides:[],
    source_authority:{volume_id:1,provider:'comicvine',provider_id:'9',generation:0},
    target_issues:[{provider:'metron',provider_id:'A',issue_number:'1A',title:hostile,date:null}],
    preview:{source:{provider:'comicvine',provider_id:'9'},target:{provider:'metron',provider_id:'X'},
        apply_available:false,blockers:['incomplete_correspondence'],mapping_digest:'a'.repeat(64),
        volume_deltas:{title:{before:'Old',after:hostile}},target_volume:{title:hostile},application_fields:{},
        issues:[{local:{id:1,title:hostile},canonical:{},external_ids:[],direct_files:[1],target:null,
            correspondence:{source:{provider:'comicvine',provider_id:'90'},target:null,kind:'unresolved',blockers:[],evidence:[]}}],
        target_only:[],classification:{current:{stored:{value:'hc',locked:true},provenance:{status:'not_recorded'}},
            target_unlocked_evaluation:{value:'tpb',reason:'aged_single_issue_tpb'}},
        bibliography:{old_evidence_becomes_historical:true},graph:{},content:{claims:[],ownership:[]}}});
const descend = root => [root,...root.children.flatMap(descend)];
const find = (root,label) => descend(root).find(node=>node.tag==='button' && node.textContent===label);
const deferred = () => { let resolve,reject; const promise=new Promise((a,b)=>{resolve=a;reject=b;}); return {promise,resolve,reject}; };
const storage=new Map(); storage.setItem=storage.set; storage.getItem=storage.get; storage.removeItem=storage.delete;
(async()=>{
    const root=new Element(), requests=[];
    let response=review();
    const controller=new Controller(root, async(...args)=>{requests.push(args);return response;},storage);
    controller.review=review(); controller.render();
    assert.ok(find(controller.el('review'),'Review final confirmation').disabled);
    assert.ok(descend(controller.el('review')).some(node=>String(node.textContent).includes(hostile)));
    assert.ok(descend(controller.el('review')).some(node=>node.textContent==='incomplete correspondence'));
    controller.review.preview.apply_available=true; controller.review.preview.blockers=[]; controller.render();
    find(controller.el('review'),'Review final confirmation').onclick();
    assert.ok(controller.el('confirm').open);
    response=review(); response.revision=3;
    await controller.revise(1,'A',controller.review);
    assert.equal(requests[0][0],'PUT');
    assert.equal(requests[0][2].revision,2);
    assert.equal(requests[0][2].mappings[0].target_provider_id,'A');
    assert.equal(controller.review.revision,3);
    assert.equal(controller.el('confirm').open,false);
    // A late target A cannot replace target B; abandoned session is cancelled.
    const first=deferred(),second=deferred(); let calls=0;
    controller.api=(method)=>method==='DELETE' ? Promise.resolve({}) : (++calls===1 ? first.promise : second.promise);
    const a=controller.create({provider:'metron',id:'A'}), b=controller.create({provider:'gcd',id:'B'});
    const newer=review(); newer.session_id='b'.repeat(43); second.resolve(newer); await b;
    first.resolve(review()); await a;
    assert.equal(controller.review.session_id,newer.session_id);
    // One pending exact apply, even on repeated button events. Keep retry inputs.
    controller.review.preview.apply_available=true;
    const apply=deferred(); let submits=0;
    controller.api=()=>{submits++;return apply.promise;};
    const pending=controller.apply(); await controller.apply(); assert.equal(submits,1);
    apply.reject({reason:'stale_review'}); await pending;
    assert.equal(controller.el('message').textContent,messages.stale_review);
    assert.equal(JSON.parse(storage.getItem(controller.retryKey)).mapping_digest,'a'.repeat(64));
    assert.equal(controller.el('retry').hidden,false);
    controller.error({reason:'review_unavailable'}); assert.match(controller.el('message').textContent,/restart/);
    const receipt={id:'r',source_provider:'comicvine',source_provider_id:'9',target_provider:'metron',target_provider_id:'X',
        applied_at:'UTC',mapped_count:1,added_count:1,claim_count:1,coverage_count:1,classification_action:'preserved_locked',already_applied:true};
    controller.success(receipt);
    assert.ok(descend(controller.el('success')).some(node=>String(node.textContent).includes('durable receipt recovered')));
    controller.api=async(method,path,body,params)=>{requests.push([method,path,body,params]);return {...receipt,issues:[{local_issue_id:99,target_provider:'metron',target_provider_id:'A'}],claims:[],coverage:[]};};
    await controller.receipt('r',100);
    assert.equal(requests.at(-1)[3].offset,100);
    assert.ok(find(controller.el('receipt'),'Previous receipt details'));
    const parent=new Element();text(parent,'p',hostile);assert.equal(parent.children[0].textContent,hostile);
    console.log('Provider switch UI: text safety, backend applicability, exact revisions, confirmation, async suppression, double apply, durable retry, expiry and receipt pagination OK');
})().catch(error=>{console.error(error);process.exitCode=1;});
