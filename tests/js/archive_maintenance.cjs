'use strict';
const assert = require('node:assert/strict');
class Node {
    constructor(tag='div') { this.tag=tag; this.children=[]; this.value=''; this.textContent=''; this.handlers={}; }
    appendChild(node) { this.children.push(node); return node; }
    replaceChildren(...nodes) { this.children=nodes; }
    setAttribute(name,value) { this[name]=value; }
    addEventListener(name,handler) { this.handlers[name]=handler; }
    querySelectorAll(selector) {
        return this.children.flatMap(n=>[...(n.tag==='input' && (selector!=='input:checked'||n.checked)?[n]:[]),...n.querySelectorAll(selector)]);
    }
    focus() { global.focus=this; }
    showModal() { this.open=true; }
    close() { this.open=false; if(this.handlers.close)this.handlers.close(); }
}
global.document={createElement:tag=>new Node(tag), addEventListener(){}};
const {Controller,text}=require('../../frontend/static/js/archive_maintenance.js');
const elements=new Map();
const root={querySelector(id){if(!elements.has(id))elements.set(id,new Node());return elements.get(id);}};
const row={file_id:1,filename:'<img src=x onerror=evil()> café',title:'Fixture',size:123};
const preview={...row,target:'fixture.cbz',pages:3,status:'convertible',shared_source:true,apply_available:true};
let calls=[], pending, blocked=false;
const api=async(method,path,body,params)=>{
    calls.push({method,path,body,params});
    if(blocked && method==='POST') return new Promise(resolve=>{pending=resolve;});
    if(path.endsWith('/archives')) return {items:[row],next_after:null};
    if(method==='POST') return {id:'a'.repeat(32)};
    return {state:'complete',done:1,items:[preview],next_offset:null};
};
(async()=>{
    const ui=new Controller(root,api,async()=>{}); ui.el('filter').value='all';
    await ui.start(); assert.equal(ui.el('files').querySelectorAll('input').length,1);
    assert.equal(ui.el('next').disabled,true);
    const literal=text(new Node(),'p',row.filename);assert.equal(literal.textContent,row.filename);assert.equal(literal.children.length,0);
    ui.el('select').onclick(); await ui.run('preview');
    assert.equal(ui.el('dialog').open,true);assert.equal(global.focus,ui.el('dialog-title'));
    assert.equal(calls.filter(c=>c.path.endsWith('batch-apply')).length,0);
    assert.equal(ui.selected('review')[0],1);
    ui.el('dialog').close();assert.equal(global.focus,ui.el('preview'));
    blocked=true;const first=ui.run('scan');await ui.run('scan');
    assert.equal(calls.filter(c=>c.path.endsWith('/scan')).length,1);
    pending({id:'b'.repeat(32)});await first;blocked=false;
    await ui.apply();assert.equal(calls.filter(c=>c.path.endsWith('batch-apply')).length,1);
    assert.deepEqual(calls.find(c=>c.path.endsWith('batch-apply')).body.selected,[1]);
    let resolveFirst,resolveSecond;
    ui.api=()=>new Promise(resolve=>{if(!resolveFirst)resolveFirst=resolve;else resolveSecond=resolve;});
    const old=ui.load(), fresh=ui.load();
    resolveSecond({items:[],next_after:null});await fresh;
    resolveFirst({items:[row],next_after:null});await old;
    assert.equal(ui.el('files').querySelectorAll('input').length,0);
    ui.api=async()=>{throw {reason:'stale_preview'};};
    await ui.load();assert.match(ui.el('message').textContent,/unavailable/);
    console.log('Archive maintenance: preview/dry-run, shared warning, server handles, submit locks, focus return, pagination, stale response, literal hostile text and safe errors PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
