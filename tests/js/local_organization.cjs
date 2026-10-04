'use strict';
const assert=require('node:assert/strict');
const ui=require('../../frontend/static/js/local_organization.js');
class Element {
    constructor(tag,doc){this.tag=tag;this.ownerDocument=doc;this.children=[];this.textContent='';}
    appendChild(node){node.parentNode=this;this.children.push(node);return node;}
    remove(){this.parentNode.children=this.parentNode.children.filter(n=>n!==this);}
    replaceChildren(){this.children=[];}
    focus(){this.focused=true;}
    scrollIntoView(){this.scrolled=true;}
    setAttribute(name,value){this[name]=value;}
    set innerHTML(_){throw Error('Unsafe HTML');}
}
const doc={createElement:tag=>new Element(tag,doc)};
const text=node=>node.textContent+node.children.map(text).join('');
(async()=>{
    const panel=doc.createElement('section'); let calls=0;
    const plans=[{source:'/fixture/Book 1.cbz',status:'ready',issue_labels:['#1 Book 1'],volume_id:1},
        {source:'/fixture/<script>.cbz',status:'review_required',volume_id:1,publication:'Batman',identification_reasons:['issue_coverage_unresolved']}];
    ui.preview(panel,{plans},async()=>{calls++;return {jobs:[{source:plans[0].source,state:'completed'}],review:[plans[1]]};});
    assert.equal(calls,0);
    assert.ok(panel.focused && panel.scrolled);
    assert.ok(text(panel).includes('Book 1.cbz → #1 Book 1'));
    const button=panel.children.find(n=>n.tag==='button');
    const first=button.onclick(); await button.onclick(); await first;
    assert.equal(calls,1);
    assert.ok(text(panel).includes('1 files imported or associated.'));
    assert.ok(text(panel).includes('1 files need issue matching or folder review.'));
    assert.ok(text(panel).includes('<script>.cbz'));
    assert.ok(!text(panel).includes('issue_coverage_unresolved'));
    // A review control must never be an anchor navigating to the volume page.
    const walk=node=>[node,...node.children.filter(c=>!c.hidden).flatMap(walk)];
    const get=(parent,tag,label)=>walk(parent).find(n=>n.tag===tag&&n.textContent===label);
    const reviewed={...plans[1],row_id:'stable-row',review_available:true};
    const detail={filename:'<script>.cbz',publication:'Batman',volume_id:1,
        evidence:[{label:'ComicInfo Number',value:'2'}],reasons:['ComicInfo issue number differs from the filename issue.'],
        existing:[],issues:[{id:1,label:'#1 Book 1'},{id:2,label:'#2 Book 2'}]};
    let loads=0,saves=0,finishLoad,finishSave;
    const api={load:()=>{loads++;return new Promise(resolve=>{finishLoad=resolve;});},
        save:(row,ids)=>{assert.equal(row,'stable-row');assert.deepEqual(ids,[1]);saves++;return new Promise(resolve=>{finishSave=resolve;});}};
    panel.replaceChildren();ui.preview(panel,{plans:[plans[0],reviewed]},async()=>({jobs:[]}),api);
    let review=get(panel,'button','Review issue match');assert.ok(review);assert.ok(!walk(panel).some(n=>n.tag==='a'));
    const opening=review.onclick();await review.onclick();assert.equal(loads,1);
    finishLoad(detail);await opening;
    assert.ok(text(panel).includes('ComicInfo Number: 2'));
    get(panel,'button','Cancel').onclick();assert.ok(review.focused);
    const again=review.onclick();finishLoad(detail);await again;
    walk(panel).find(n=>n.tag==='input'&&n.value==='1').checked=true;
    const save=get(panel,'button','Save association');const saving=save.onclick();await save.onclick();assert.equal(saves,1);
    finishSave({plans:[plans[0],{...reviewed,status:'associated',review_available:false,issue_labels:['#1 Book 1']}]});await saving;
    assert.ok(text(panel).includes('Associated: #1 Book 1'));assert.ok(text(panel).includes('Book 1.cbz'));
    panel.replaceChildren();ui.preview(panel,{plans:[reviewed]},async()=>({jobs:[]}),{load:async()=>{throw {code:'stale_preview'};}});
    await get(panel,'button','Review issue match').onclick();assert.ok(text(panel).includes('This preview is stale.'));
    assert.ok(text(panel).includes('Local files preview'));
    for (const kind of ['existing','embedded']) {
        let overrides=0;
        const conflict={...detail,existing:kind==='existing'?[{issue_id:2,label:'2',forced:false}]:[],embedded_issue_ids:kind==='embedded'?[2]:[]};
        panel.replaceChildren();ui.preview(panel,{plans:[reviewed]},async()=>({jobs:[]}),{
            load:async()=>conflict,
            save:async(row,ids,override)=>{assert.equal(override,true);assert.deepEqual(ids,[1]);overrides++;return {plans:[]};}
        });
        await get(panel,'button','Review issue match').onclick();
        walk(panel).filter(n=>n.tag==='input').forEach(n=>n.checked=false);
        walk(panel).find(n=>n.tag==='input'&&n.value==='1').checked=true;
        const saveOverride=get(panel,'button','Save association');await saveOverride.onclick();
        assert.equal(overrides,0);
        assert.equal(saveOverride.textContent,kind==='existing'?'Replace association':'Override embedded identity');
        await saveOverride.onclick();assert.equal(overrides,1);
    }
    panel.replaceChildren();ui.preview(panel,{plans:[{...plans[0],status:'no_changes'}]},async()=>{throw Error('No mutation');});
    assert.equal(get(panel,'button','Apply ready associations').disabled,true);
    assert.ok(text(panel).includes('Already associated'));
    console.log('Local organization: nonmutating preview, explicit Apply, focus, double-click lock, partial review, safe labels PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
