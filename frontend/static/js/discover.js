/* Discover is an observation UI. The server owns matching and acquisition. */
const DiscoverUI = (() => {
    function text(parent, tag, value='') {const node=document.createElement(tag);node.textContent=String(value ?? '');parent.appendChild(node);return node;}
    function label(value) {return String(value ?? 'Unavailable').replaceAll('_',' ');}
    class Controller {
        constructor(api, wait=ms=>new Promise(resolve=>setTimeout(resolve,ms))) {this.api=api;this.wait=wait;this.generation=0;this.dialogGeneration=0;this.busy=false;this.offset=0;this.next=null;this.source=null;}
        el(id) {return document.getElementById('d-'+id);}
        message(value) {this.el('message').textContent=value;}
        async run(fn, mutation=false) {if(mutation&&this.busy)return;if(mutation)this.busy=true;try {await fn();}catch(error){this.message(label(error.message||'Request failed'));}finally {if(mutation)this.busy=false;}}
        button(parent,title,fn,mutation=false,disabled=false) {const b=text(parent,'button',title);b.type='button';b.disabled=disabled;b.addEventListener('click',()=>this.run(async()=>{if(mutation)b.disabled=true;try{await fn();}finally{if(mutation)b.disabled=disabled;}},mutation));return b;}
        async load(offset=0) {
            const generation=++this.generation;
            const filters={offset,limit:50,q:this.el('query').value,state:this.el('state').value,category:this.el('category').value,year:this.el('year').value,quality:this.el('quality').value};
            const [page,source]=await Promise.all([this.api('GET','/discover',null,filters),this.api('GET','/discover/status')]);
            if(generation!==this.generation)return;
            this.source=source;this.offset=offset;this.next=page.next_offset;
            this.el('source').textContent=`GetComics · ${source.enabled?'Enabled':'Disabled'} · Automatic polling ${source.automatic?'on':'off'} · ${source.interval_minutes} minutes · Last checked ${source.last_checked?new Date(source.last_checked*1000).toLocaleString():'Never'} · ${source.error?label(source.error):'No source error'}${source.gap?' · Possible discovery gap — bounded catch-up did not prove overlap':''}`;
            const content=this.el('content');content.replaceChildren();
            text(content,'p',`Source window ${page.scanned ? offset+1 : 0}–${offset+page.scanned}. Filters apply to this bounded window; continue to the next window for more. Local status is current; source facts may be stale.`);
            const table=text(content,'table'),head=text(table,'thead'),row=text(head,'tr');
            for(const value of ['Source post','Published / First seen','Local interest / Quality','Actions'])text(row,'th',value);
            const body=text(table,'tbody');
            for(const post of page.items) {
                const tr=text(body,'tr');tr.dataset.postId=String(post.id);
                const title=text(tr,'td');text(title,'strong',post.title);text(title,'p',post.categories.join(' · '));text(title,'p',`Source year: ${post.year_text||'Unknown'} · Size: ${post.size_text||'Unknown'}`);
                text(tr,'td',`${post.published_at||'Unknown source date'} (${label(post.published_precision)}) / ${new Date(post.first_seen*1000).toLocaleString()}`);
                const state=text(tr,'td');text(state,'p',`${label(post.match)} · ${label(post.interest)}`);text(state,'p',`${label(post.claims.quality_class)} — claimed${post.claims.conflict?' (conflicting labels)':''}`);
                if(post.quality)text(state,'p',`${label(post.quality.result)}; verified after download`);
                if(post.local?.content_elsewhere)text(state,'p','Content represented elsewhere (C2), not direct issue ownership');
                if(post.local?.acquisition)text(state,'p',`Acquisition: ${label(post.local.acquisition)}`);
                this.button(text(tr,'td'),'View Discovery',()=>this.detail(post.id));
            }
            if(!page.items.length)text(content,'p','No matching observations in this window. Refresh explicitly or continue paging.');
            this.el('prev').disabled=offset===0;this.el('next').disabled=page.next_offset===null;
        }
        dialog(title) {this.dialogGeneration++;this.el('title').textContent=title;const p=this.el('body');p.replaceChildren();if(!this.el('dialog').open)this.el('dialog').showModal();return p;}
        async task(value) {for(let i=0;i<300;i++){if(!['queued','running'].includes(value.state)){if(value.state!=='complete')throw new Error(value.result?.reason||value.state);return value.result;}this.message(`Discover task: ${value.state}`);await this.wait(1000);value=await this.api('GET','/discover/tasks/'+value.id);}throw new Error('Task still running; check Activity and reload status');}
        async refresh() {this.el('refresh').disabled=true;this.message('Refreshing Discover observations');try{await this.task(await this.api('POST','/discover/refresh',{}));await this.load(0);this.message('Discover observations refreshed. No acquisition was started.');}finally{this.el('refresh').disabled=false;}}
        async detail(id) {
            const p=this.dialog('Discovery Details'),generation=this.dialogGeneration,post=await this.api('GET','/discover/posts/'+id);
            if(generation!==this.dialogGeneration)return;
            text(p,'h3',post.title);text(p,'p',post.summary);text(p,'p',`${label(post.match)} · ${label(post.interest)}`);
            const link=text(p,'a','Open Source Page');link.href=post.url;link.target='_blank';link.rel='noopener noreferrer';
            text(p,'p',`Published: ${post.published_at||'Unknown'} (${label(post.published_precision)}). First seen: ${new Date(post.first_seen*1000).toLocaleString()}. Source revision ${post.revision}.`);
            if(post.local) {
                const volume=text(p,'a','Open Local Volume');volume.href=url_base+'/volumes/'+post.local.volume_id;
                text(p,'p',`Direct ownership: ${post.local.direct_owned?'Owned':'Missing'}. ${post.local.content_elsewhere?'C2 content represented elsewhere; not direct ownership.':''}`);
                this.button(p,'Resolve Current Offerings / Review Acquisition',()=>this.preview(post.id),true,!!post.local.acquisition);
            } else {const add=text(p,'a','Find / Add to Library');add.href=url_base+'/add?q='+encodeURIComponent(post.title.slice(0,200));text(p,'p','Select an exact metadata-provider publication in the normal Add flow. This source post does not establish provider identity.');}
            if(post.detail)text(p,'p',`Cached offering details: ${post.detail.stale?'stale':'observed'} at ${new Date(post.detail.fetched_at*1000).toLocaleString()}. Confirmation always revalidates.`);
        }
        async preview(id) {
            const generation=this.dialogGeneration;
            const result=await this.task(await this.api('POST',`/discover/posts/${id}/acquisition-preview`,{}));
            if(generation!==this.dialogGeneration)return;
            const p=this.dialog('Review Explicit Acquisition');
            text(p,'p','This starts the existing download/import pipeline for the selected local issue. Quality is provisional; upgrades must pass file verification. Polling never performs this action.');
            if(result.reason)text(p,'p',label(result.reason));
            for(const offering of result.offerings){text(p,'h3',offering.title);text(p,'p',`${label(offering.quality?.claims?.quality_class)} — claimed · ${label(offering.quality?.result)} · ${label(offering.quality?.reason)}`);this.button(p,'Confirm Acquire This Offering',async()=>{const receipt=await this.task(await this.api('POST',`/discover/posts/${id}/acquire`,{preview_id:result.preview_id,offering_id:offering.offering_id,confirmed:true}));this.message(`Acquisition: ${label(receipt.state)}`);this.el('dialog').close();await this.load(this.offset);},true,!offering.allowed);}
        }
        settings() {
            const s=this.source,p=this.dialog('GetComics Discovery Settings');
            if(!s)return;
            const field=(title,type,value)=>{const l=text(p,'label',title),input=text(l,'input');input.type=type;if(type==='checkbox')input.checked=value;else input.value=value;return input;};
            const enabled=field('Discovery enabled','checkbox',!!s.enabled),automatic=field('Automatic polling (observation only)','checkbox',!!s.automatic),interval=field('Interval in minutes (30–1440)','number',s.interval_minutes);interval.min='30';interval.max='1440';
            this.button(p,'Save Discovery Settings',async()=>{await this.api('POST','/discover/settings',{revision:s.revision,enabled:enabled.checked,automatic:automatic.checked,interval_minutes:Number(interval.value)});this.el('dialog').close();await this.load(this.offset);},true);
        }
        bind() {
            this.el('filters').addEventListener('submit',event=>{event.preventDefault();this.run(()=>this.load(0));});
            this.el('refresh').addEventListener('click',()=>this.run(()=>this.refresh(),true));
            this.el('settings').addEventListener('click',()=>this.settings());
            this.el('prev').addEventListener('click',()=>this.run(()=>this.load(Math.max(0,this.offset-50))));
            this.el('next').addEventListener('click',()=>this.run(()=>this.load(this.next)));
            this.el('close').addEventListener('click',()=>this.el('dialog').close());
            this.el('dialog').addEventListener('close',()=>this.dialogGeneration++);
            this.run(()=>this.load());
        }
    }
    return {Controller,text,label};
})();
if(typeof module!=='undefined')module.exports=DiscoverUI;
if(typeof usingApiKey==='function')usingApiKey().then(key=>{
    const api=async(method,path,body,query={})=>{const params=new URLSearchParams({...query,api_key:key});const response=await fetch(url_base+'/api'+path+'?'+params,{method,headers:body?{'Content-Type':'application/json'}:{},body:body?JSON.stringify(body):undefined});const payload=await response.json();if(!response.ok||payload.error)throw new Error(payload.result?.reason||payload.error||'Request failed');return payload.result;};
    new DiscoverUI.Controller(api).bind();
});
