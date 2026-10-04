'use strict';
const assert=require('node:assert/strict');
const ui=require('../../frontend/static/js/local_organization.js');
class Element {
    constructor(tag,doc){this.tag=tag;this.ownerDocument=doc;this.children=[];this.textContent='';}
    appendChild(node){this.children.push(node);return node;}
    replaceChildren(){this.children=[];}
    focus(){this.focused=true;}
    scrollIntoView(){this.scrolled=true;}
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
    console.log('Local organization: nonmutating preview, explicit Apply, focus, double-click lock, partial review, safe labels PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
