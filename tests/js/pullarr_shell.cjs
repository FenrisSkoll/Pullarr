const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
    constructor() { this.attrs = {}; this.listeners = {}; this.inert = false; this.isConnected = true; this.dataset = {}; this.classList = {toggle: (k,v) => {this.attrs[k]=v;}}; }
    setAttribute(k,v) { this.attrs[k]=v; }
    hasAttribute(k) { return k in this.attrs; }
    getClientRects() { return [1]; }
    closest() { return null; }
    addEventListener(k,v) { this.listeners[k]=v; }
    focus() { document.activeElement=this; }
    scrollIntoView() { this.scrolled=true; }
    querySelectorAll() { return this.children || []; }
    querySelector() { return this.current || null; }
}
const main=new Element(), nav=new Element(), toggle=new Element(), backdrop=new Element(), skip=new Element(), overlay=new Element(), header=new Element(), shell=new Element();
const link=new Element();nav.current=link;nav.children=[link];
const document={activeElement:toggle,listeners:{},querySelector:s=>({'main':main,'#nav-bar':nav,'#toggle-nav':toggle,'#nav-backdrop':backdrop,'.skip-link':skip,'.window':overlay,'header':header,'.nav-main':shell}[s] || null),addEventListener(k,v){(this.listeners[k]??=[]).push(v);}};
let observer;const narrow={matches:true,addEventListener(k,v){this.changed=v;}};
const source=fs.readFileSync('frontend/static/js/pullarr.js','utf8');
vm.runInNewContext(source,{document,window:{matchMedia:()=>narrow},MutationObserver:class{constructor(fn){observer=fn;}observe(){}},HTMLImageElement:class{}});
assert.equal(nav.inert,true);
toggle.onclick();assert.equal(toggle.attrs['aria-expanded'],'true');assert.equal(main.inert,true);assert.equal(document.activeElement,link);
for(const fn of document.listeners.keydown)fn({key:'Escape',preventDefault(){}});
assert.equal(toggle.attrs['aria-expanded'],'false');assert.equal(document.activeElement,toggle);
skip.listeners.click({preventDefault(){}});assert.equal(document.activeElement,main);assert.equal(main.scrolled,true);
narrow.matches=false;narrow.changed();assert.equal(nav.inert,false);
const dialog=new Element(), cancel=new Element();dialog.children=[cancel];overlay.current=dialog;overlay.attrs['show-window']='';toggle.focus();observer();
assert.equal(document.activeElement,cancel);assert.equal(shell.inert,true);assert.equal(dialog.attrs.role,'dialog');
delete overlay.attrs['show-window'];observer();assert.equal(document.activeElement,toggle);assert.equal(shell.inert,false);
assert(!source.includes('innerHTML'));assert(!source.includes('fetch('));
console.log('Pullarr shell: drawer, breakpoint, keyboard, skip, dialog focus/return, inert background, no network/HTML passed');
