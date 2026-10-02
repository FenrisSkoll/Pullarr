/* Policy editing and observations only. Never client-authoritative file facts. */
const QualityUI = (() => {
    const classes = ['unknown', 'digital', 'hd_digital', 'sd_digital', 'scan', 'upscaled', 'hd_upscaled'];
    const label = v => String(v ?? 'unknown').replaceAll('_', ' ');
    function text(p, tag, value = '') { const e = document.createElement(tag); e.textContent = String(value ?? ''); p.appendChild(e); return e; }
    function input(p, name, value = '', type = 'text') { const e = text(text(p, 'label', name), 'input'); e.type = type; e.value = value; e.setAttribute('aria-label', name); return e; }
    function select(p, name, options, value) { const e = text(text(p, 'label', name), 'select'); e.setAttribute('aria-label', name); for (const [id, title] of options) { const o = text(e, 'option', title); o.value = id; } e.value = String(value); return e; }
    function table(p, headers) { const wrap = text(p, 'div'); wrap.className = 'q-table'; const t = text(wrap, 'table'), h = text(text(t, 'thead'), 'tr'); headers.forEach(v => { text(h, 'th', v).scope = 'col'; }); return text(t, 'tbody'); }
    class Controller {
        constructor(root, api, sleep = ms => new Promise(r => setTimeout(r, ms))) { Object.assign(this, {root, api, sleep, generation: 0, dialogGeneration: 0, busy: false}); }
        el(id) { return this.root.querySelector('#q-'+id); }
        message(v) { this.el('message').textContent = v; }
        async perform(fn, mutation = false, button = null) {
            if (mutation && this.busy) return;
            if (mutation) { this.busy = true; if (button) button.disabled = true; }
            try { return await fn(); } catch (e) { this.message(e?.reason === 'revision_conflict' ? 'This policy or assignment changed. Reload and review again.' : 'Request could not complete: '+label(e?.reason || 'unavailable')+'. Reload durable state before retrying.'); }
            finally { if (mutation) { this.busy = false; if (button) button.disabled = false; } }
        }
        button(p, name, fn, mutation = false, disabled = false) { const b = text(p,'button',name); b.type = 'button'; b.disabled = disabled; b.onclick = () => this.perform(fn,mutation,b); return b; }
        dialog(name) { this.dialogGeneration++; this.invoker = document.activeElement; this.el('dialog-title').textContent = name; const p = this.el('dialog-body'); p.replaceChildren(); if (!this.el('dialog').open) this.el('dialog').showModal(); this.el('close').focus(); return p; }
        start(params = new URLSearchParams(location.search)) { this.el('list').onclick = () => this.perform(() => this.list()); this.el('create').onclick = () => this.edit(); this.el('close').onclick = () => this.el('dialog').close(); this.el('dialog').onclose = () => { this.dialogGeneration++; this.invoker?.focus(); }; this.perform(() => params.has('volume') ? this.volume(Number(params.get('volume'))) : params.has('node') ? this.node(Number(params.get('node'))) : this.list()); }
        async list() { const v = ++this.generation, result = await this.api('GET','/quality-profiles'); if (v !== this.generation) return; this.profiles = result; const p = this.el('content'); p.replaceChildren(); const t = table(p,['Profile','Policy','Assignments','Actions']); for (const r of result.items) { const tr = text(t,'tr'); text(tr,'td',`${r.name} r${r.revision}${r.is_default ? ' · Default' : ''}`); text(tr,'td',`${r.groups.length} groups · ${r.upgrades ? 'Upgrades enabled' : 'Upgrades disabled'} · cutoff ${r.groups[r.cutoff].name}`); text(tr,'td',`${r.volume_assignments} volumes, ${r.node_assignments} Collection nodes`); const a = text(tr,'td'); this.button(a,'Edit '+r.name,() => this.edit(r)); this.button(a,'Make Default',async () => { await this.api('POST','/quality-profiles/default',{profile_id:r.id,revision:result.default.revision}); await this.list(); },true,!!r.is_default); this.button(a,'Delete '+r.name,() => { const d = this.dialog('Delete Quality Profile'); text(d,'p','Only an unassigned, non-default profile can be deleted. Historical acquisition receipts remain.'); this.button(d,'Confirm Delete Profile',async () => { await this.api('POST',`/quality-profiles/${r.id}/delete`,{revision:r.revision,confirmed:true}); this.el('dialog').close(); await this.list(); },true); }); } }
        edit(profile = null) {
            const p = this.dialog(profile ? 'Edit Quality Profile' : 'Create Quality Profile'), dg = this.dialogGeneration;
            const name = input(p,'Profile name',profile?.name || ''), upgrade = input(p,'Enable automatic upgrades below cutoff','','checkbox'); upgrade.checked = !!profile?.upgrades;
            const minimum = input(p,'Minimum verified p10 short edge (pixels; 0 disables)',profile?.minimum_p10 || 0,'number'); minimum.min = 0; minimum.max = 20000;
            text(p,'p','Groups are ordered from lower to higher preference. Classes in one group are equivalent; same-group replacement is blocked. Cutoff never unmonitors an issue.');
            let groups = profile ? structuredClone(profile.groups) : classes.map(c => ({name:label(c),classes:[c],allowed:true}));
            let cutoff = profile?.cutoff ?? groups.length-1; const box = text(p,'div');
            const render = () => { box.replaceChildren(); const t = table(box,['Preference','Group','Allowed','Classes','Order']); groups.forEach((g,i) => { const tr = text(t,'tr'); text(tr,'td',`${i+1}${i === cutoff ? ' · Cutoff' : ''}`); const n = input(text(tr,'td'),'Group '+(i+1)+' name',g.name); n.oninput = () => g.name = n.value; const allowed = input(text(tr,'td'),'Allow group '+(i+1),'','checkbox'); allowed.checked = g.allowed; allowed.onchange = () => g.allowed = allowed.checked; const c = text(tr,'td'); g.classes.forEach(v => text(c,'p',label(v))); const a = text(tr,'td'); const move = delta => { const target = i+delta; [groups[i],groups[target]] = [groups[target],groups[i]]; if (cutoff === i) cutoff = target; else if (cutoff === target) cutoff = i; render(); }; this.button(a,'Lower '+g.name,() => move(-1),false,i===0); this.button(a,'Higher '+g.name,() => move(1),false,i===groups.length-1); this.button(a,'Set Cutoff '+g.name,() => { cutoff=i; render(); }); this.button(a,'Group with Lower',() => { groups[i-1].classes.push(...g.classes); groups.splice(i,1); if (cutoff>=i) cutoff=Math.max(0,cutoff-1); render(); },false,i===0); this.button(a,'Separate Classes',() => { if(g.classes.length<2)return; const extra=g.classes.splice(1).map(c=>({name:label(c),classes:[c],allowed:g.allowed})); groups.splice(i+1,0,...extra); if(cutoff>i)cutoff+=extra.length; render(); },false,g.classes.length<2); }); };
            render(); this.button(p,'Save Quality Profile',async () => { const policy = {groups,cutoff,upgrades:upgrade.checked,minimum_p10:Number(minimum.value)}, body = {name:name.value,policy}; if(profile)body.revision=profile.revision; await this.api('POST',profile ? `/quality-profiles/${profile.id}` : '/quality-profiles',body); if(dg !== this.dialogGeneration)return; this.el('dialog').close(); await this.list(); },true); name.focus();
        }
        async volume(id, offset = 0) {
            const v = ++this.generation, [page, profiles] = await Promise.all([this.api('GET',`/volumes/${id}/quality`,null,{offset,limit:50}),this.api('GET','/quality-profiles')]); if(v!==this.generation)return;
            const p = this.el('content'); p.replaceChildren(); text(p,'h2','Volume Quality'); const a=page.assignment; text(p,'p',a.conflict ? 'Profile conflict. Automatic upgrades are blocked; choose an explicit volume profile.' : `${a.profile.name} r${a.profile.revision} · ${label(a.source)}`);
            const assignment=select(p,'Volume profile override',[['','Inherit Collection / global default'],...profiles.items.map(r=>[r.id,r.name])],a.override || ''); this.button(p,'Save Volume Assignment',async()=>{ await this.api('POST',`/volumes/${id}/quality`,{profile_id:assignment.value ? Number(assignment.value) : null,expected_profile_id:a.override}); await this.volume(id,offset); },true);
            const t=table(p,['Issue','Exact ownership / quality','Policy result','Actions']); for(const issue of page.items){const tr=text(t,'tr'); text(tr,'td',`${issue.title} #${issue.issue_number}`); const q=text(tr,'td'); text(q,'p',issue.direct_owned?'Direct file owned':'No direct file'); if(issue.content_elsewhere)text(q,'p','Content represented elsewhere (not direct-file quality)'); for(const f of issue.files){text(q,'p',`${label(f.claims.quality_class)} · ${label(f.claims.origin)} · ${f.facts ? `verified p10 ${f.facts.short_edge?.p10 ?? 'unknown'}px · ${f.facts.container} · ${Object.keys(f.facts.codecs).join(', ')}` : 'Quality not analyzed'}`);} text(tr,'td',`${issue.upgrade_eligible?'Upgrade · ':''}${label(issue.reason)}`); const actions=text(tr,'td'); this.button(actions,'Acquisition History',()=>this.history(issue.id)); this.button(actions,'Analyze Quality',async()=>{let task=await this.api('POST','/quality-analysis',{file_ids:issue.files.map(f=>f.file_id)}); while(['queued','running'].includes(task.state)){this.message(`Analysis ${task.completed}/${task.total}`);await this.sleep(500);task=await this.api('GET',`/quality-analysis/${task.id}`);} this.message('Analysis '+task.state);if(v===this.generation)await this.volume(id,offset);},true,!issue.files.length); }
            this.button(p,'Previous page',()=>this.volume(id,Math.max(0,offset-50)),false,!offset); this.button(p,'Next page',()=>this.volume(id,offset+50),false,!page.has_next);
        }
        async node(id){const v=++this.generation,[assignment,profiles]=await Promise.all([this.api('GET',`/collections/nodes/${id}/quality`),this.api('GET','/quality-profiles')]);if(v!==this.generation)return;const p=this.el('content');p.replaceChildren();text(p,'h2','Collection Node Quality Inheritance');text(p,'p','Nearest configured ancestor applies. Multiple accepted memberships with different profiles produce a conflict. This does not change Collection or library monitoring.');const choice=select(p,'Node quality profile',[['','Inherit ancestor / default'],...profiles.items.map(r=>[r.id,r.name])],assignment.profile_id||'');this.button(p,'Save Node Assignment',async()=>{await this.api('POST',`/collections/nodes/${id}/quality`,{profile_id:choice.value?Number(choice.value):null,expected_profile_id:assignment.profile_id});await this.node(id);},true);}
        async history(id,offset=0){const p=this.dialog('Acquisition History'),dg=this.dialogGeneration,page=await this.api('GET',`/issues/${id}/acquisitions`,null,{offset,limit:50});if(dg!==this.dialogGeneration)return;const t=table(p,['Reason / State','Release / Source','Policy','Detail']);for(const r of page.items){const tr=text(t,'tr');text(tr,'td',`${label(r.reason)} · ${label(r.state)}`);text(tr,'td',`${r.release_title || 'Legacy / Unknown'} · ${r.source}`);text(tr,'td',`Profile ${r.profile_id ?? 'unknown'} r${r.profile_revision ?? '?'}`);this.button(text(tr,'td'),'Explain Acquisition',()=>this.detail(r.id));}if(!page.items.length)text(p,'p','No recorded acquisition. Legacy provenance remains unknown.');this.button(p,'Previous history',()=>this.history(id,Math.max(0,offset-50)),false,!offset);this.button(p,'Next history',()=>this.history(id,offset+50),false,!page.has_next);}
        async detail(id) {
            const p=this.dialog('Acquisition Explanation'),dg=this.dialogGeneration,r=await this.api('GET',`/acquisitions/${id}`);
            if(dg!==this.dialogGeneration)return;
            for(const [k,v] of [
                ['Reason',label(r.reason)],['State',label(r.state)],['Original release',r.release_title||'Unknown'],
                ['Source',r.decision.source_name||r.source],['Protocol',label(r.decision.protocol)],
                ['Claimed quality',label(r.claims.quality_class)],['Profile at selection',`${r.profile_snapshot.name||'Unknown'} r${r.profile_revision??'?'}`],
                ['Decision',label(r.decision.quality?.result)],['Total selection score',r.decision.score??'Unavailable'],
                ['Source priority',r.decision.source_priority??'Unavailable'],['Reported size (not quality)',r.decision.reported_size??'Unknown'],
                ['Verified p10',r.verified?.short_edge?.p10??'Unavailable'],['Current file identity',r.file_id??'Historical / removed'],
                ['Download correlation',r.client_job??'Unknown'],['Failure',r.error||'None']
            ])text(p,'p',`${k}: ${v}`);
            if(r.decision.components?.length) {
                const t=table(p,['Selection rule','Outcome','Points']);
                for(const c of r.decision.components) {
                    const row=text(t,'tr');text(row,'td',label(c.rule));text(row,'td',label(c.outcome));text(row,'td',c.points);
                }
            }
            if(r.remote_job_id)text(p,'p',`Client: ${r.client_kind} · Remote job: ${r.remote_job_id}`);
            if(r.torrent){
                text(p,'p',`Torrent lifecycle: ${label(r.torrent.state)} · Import: ${(r.torrent.import_methods||[]).join(', ') || 'Not imported'}`);
                text(p,'p',`Selection-time retention: ${label(r.torrent.policy.mode)} · Ratio ${r.torrent.policy.ratio_target} · Seeding time ${r.torrent.policy.seed_seconds}s`);
                if(r.torrent.infohash_v1)text(p,'p',`v1 infohash: ${r.torrent.infohash_v1}`);
                if(r.torrent.infohash_v2)text(p,'p',`v2 infohash: ${r.torrent.infohash_v2}`);
                text(p,'p','Torrent source bytes are retained separately from library ownership. Hardlinked bytes must not be edited in place.');
            }
            if(r.supersedes)this.button(p,'Previous File Acquisition',()=>this.detail(r.supersedes));
        }
    }
    return {Controller,text,label,classes};
})();
if(typeof module!=='undefined')module.exports=QualityUI;
if(typeof usingApiKey==='function')usingApiKey().then(key=>{const api=async(method,path,body=null,params={})=>{const response=await fetch(`${url_base}/api${path}?${new URLSearchParams({api_key:key,...params})}`,{method,headers:body===null?{}:{'Content-Type':'application/json'},body:body===null?undefined:JSON.stringify(body)});if(response.status===401){location.href=`${url_base}/login`;throw{reason:'authentication_required'};}const data=await response.json();if(!response.ok)throw data.result;return data.result;};new QualityUI.Controller(document.getElementById('quality'),api).start();});
