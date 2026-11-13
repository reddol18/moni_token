"""The forecast accuracy page (issue #9), served by the map server at /accuracy. Data is embedded as JSON."""
import json

from .i18n import t
from .ui import fill, head, shell_html, uplot_js

ACC_CSS = r"""
main{max-width:1280px;margin:0 auto;padding:18px 16px 64px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 10px;display:flex;gap:12px;align-items:baseline;flex-wrap:wrap}
.sub{color:var(--text-secondary);margin:0 0 16px}.k{color:var(--text-secondary);font-size:12px;font-weight:400}
.tabs{display:flex;gap:4px;margin:0 0 14px}.tabs button{font:inherit;padding:4px 12px;border-radius:6px;border:1px solid var(--border);
background:transparent;color:var(--text-secondary);cursor:pointer}.tabs button[aria-pressed=true]{background:var(--text-primary);color:var(--surface-1)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}
.tile{background:var(--surface-1);border:1px solid var(--border);border-radius:10px;padding:14px 16px}
.tile .v{font-size:28px;font-weight:600}.tile .v.sm{font-size:15px;padding:8px 0 6px}.tile .d{color:var(--muted);font-size:12px}
.card{background:var(--surface-1);border:1px solid var(--border);border-radius:10px;padding:16px;margin-top:8px;min-width:0}
.scroll{overflow-x:auto}.empty{color:var(--muted)}th.s{cursor:pointer}th.s:hover{color:var(--text-primary)}
.st{padding:1px 7px;border-radius:4px;border:1px solid var(--border);font-weight:600;white-space:nowrap}
.st.ok{border-color:var(--good)}.st.narrow{border-color:var(--critical)}.st.wide{border-color:var(--warn)}
.sess{border-top:1px solid var(--grid);padding:10px 0}.sess:first-child{border:0}
.run{display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:2px 0}.run svg{flex:none}
.lg{display:flex;gap:14px;flex-wrap:wrap;color:var(--text-secondary);font-size:12px;margin-bottom:6px}
.lg i{display:inline-block;width:16px;border-top:2px solid;vertical-align:3px;margin-right:4px}
.chart{width:100%;min-height:220px}
"""

PAGE = r"""/*HEAD*/<body>/*SHELL*/<main>
<h1>[[p.a.h1]]</h1>
<p class="sub">[[p.a.sub]]</p>
<div class="tabs" id="tabs"></div>
<p class="k" id="srcnote"></p>
<div class="tiles" id="tiles"></div>
<h2>[[p.a.h_week]] <span class="k">[[p.a.h_week_k]]</span></h2>
<div class="card"><div class="lg"><span><i style="border-color:var(--s1)"></i>[[p.a.hit50]]</span><span><i style="border-color:var(--s2)"></i>[[p.a.hit85]]</span>
<span><i style="border-color:var(--muted);border-top-style:dashed"></i>[[p.a.target]]</span></div><div class="chart" id="wk"></div></div>
<h2>[[p.a.h_types]] <span class="k">[[p.a.h_types_k]]</span></h2>
<div class="card scroll"><table id="types"></table></div>
<h2>[[p.a.h_sess]] <span class="k" id="sessk"></span></h2>
<div class="card"><div class="lg"><span><i style="border-color:var(--s1)"></i>[[p.a.fc50]]</span><span><i style="border-color:var(--s2)"></i>[[p.a.fc85]]</span>
<span><i style="border-color:var(--text-primary);border-top-style:dashed"></i>[[p.a.actual_total]]</span><span>[[p.a.xaxis]]</span></div><div id="sess"></div></div>
<h2>[[p.a.h_docs]] <span class="k">[[p.a.h_docs_k]]</span></h2>
<div class="card" id="docs"></div>
</main>
<script id="data" type="application/json">/*DATA*/null</script>
<script>/*UPLOT*/</script>
<script>
const D=JSON.parse(document.getElementById('data').textContent);
const $=s=>document.querySelector(s),esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const pct=v=>v==null?'—':Math.round(v*100)+'%',pad=n=>String(n).padStart(2,'0');
const fmt=ms=>fmtMD(ms)+' '+fmtHM(ms);
const VD={few:[T('p.a.v_few'),''],wide:[T('p.a.v_wide'),'wide'],ok:[T('p.a.v_ok'),'ok'],narrow:[T('p.a.v_narrow'),'narrow']};
const vd=v=>v&&VD[v]?`<span class="st ${VD[v][1]}">${VD[v][0]}</span>`:'—';
let src=new URLSearchParams(location.search).get('src')==='backtest'?'backtest':'live',sortKey='n',sortDir=-1;
[['live',T('p.a.tab_live')],['backtest',T('p.a.tab_bt')]].forEach(([k,l])=>{const b=document.createElement('button');b.textContent=l;
 b.setAttribute('aria-pressed',k===src);b.onclick=()=>{src=k;$('#tabs').querySelectorAll('button').forEach(x=>x.setAttribute('aria-pressed',x===b));draw()};$('#tabs').append(b)});
$('#lim').innerHTML=shellLimits(D.header);$('#meta').textContent=T('p.meta');
function draw(){const S=D[src],O=S.overall;
 $('#srcnote').textContent=src==='live'?T('p.a.note_live',{pending:S.pending,unresolved:S.unresolved}):T('p.a.note_bt');
 const tile=(k,v,d,sm)=>`<div class="tile"><div class="k">${k}</div><div class="v${sm?' sm':''}">${v}</div><div class="d">${d}</div></div>`;
 $('#tiles').innerHTML=O.n?tile(T('p.a.hit50'),pct(O.cov_p50),T('p.a.target50'))+tile(T('p.a.hit85'),pct(O.cov_p85),T('p.a.target85'))+
  tile(T('p.a.verdict'),vd(O.verdict),T('p.a.verdict_d',{n:O.n,runs:O.runs,min:D.min_cases}),1)+
  tile(T('p.a.mae'),T('p.a.calls_n',{n:O.mae_calls}),T('p.a.mae_d',{ratio:O.ratio}))+
  (O.mae_min!=null?tile(T('p.a.time_err'),T('p.a.min_n',{n:O.mae_min}),T('p.a.time_err_d',{p:pct(O.cov_time_p85)})):'')
  :`<p class="empty">${src==='live'?T('p.a.empty_live'):T('p.a.empty_bt')}</p>`;
 weeks(S.by_week);types(S.by_type);sessions(S);docs()}
function weeks(W){const box=$('#wk');box.innerHTML='';if(!W.length){box.innerHTML=`<p class="empty">${T('p.a.none')}</p>`;return}
 const xs=W.map(w=>w.ts/1000),muted=cssVar('--muted'),grid=cssVar('--grid');
 new uPlot({width:box.clientWidth,height:220,scales:{x:{time:true,range:()=>[Math.min(...xs)-4*86400,Math.max(...xs)+4*86400]},y:{range:[0,100]}},legend:{live:true},cursor:{drag:{x:false,y:false}},
  axes:[{stroke:muted,grid:{stroke:grid},incrs:[86400,2*86400,7*86400,14*86400,28*86400],values:(u,t)=>t.map(v=>fmtMD(v*1000))},
   {stroke:muted,grid:{stroke:grid},values:(u,t)=>t.map(v=>v+'%'),size:44}],
  series:[{label:T('p.a.week'),value:(u,v)=>{const w=W.find(x=>x.ts/1000===v);return w?T('p.a.week_v',{w:w.week,n:w.n}):'—'}},
   {label:T('p.a.hit50'),stroke:cssVar('--s1'),width:2,points:{size:8},value:(u,v)=>v==null?'—':Math.round(v)+'%'},
   {label:T('p.a.hit85'),stroke:cssVar('--s2'),width:2,points:{size:8},value:(u,v)=>v==null?'—':Math.round(v)+'%'},
   {label:T('p.a.goal',{p:50}),stroke:muted,width:1,dash:[4,4],points:{show:false},value:()=>'50%'},
   {label:T('p.a.goal',{p:85}),stroke:muted,width:1,dash:[4,4],points:{show:false},value:()=>'85%'}]},
  [xs,W.map(w=>w.cov_p50*100),W.map(w=>w.cov_p85*100),W.map(()=>50),W.map(()=>85)],box)}
function types(TY){const cols=[['type',T('p.a.c_type')],['n',T('p.a.c_n')],['runs',T('p.a.c_runs')],['cov_p50',T('p.a.hit50')],['cov_p85',T('p.a.hit85')],['mae_calls',T('p.a.mae')],['ratio',T('p.a.c_ratio')],['verdict',T('p.a.verdict')]];
 const rows=[...TY].sort((a,b)=>(a[sortKey]>b[sortKey]?1:a[sortKey]<b[sortKey]?-1:0)*sortDir);
 $('#types').innerHTML='<thead><tr>'+cols.map(([k,l])=>`<th class="s${k!=='type'&&k!=='verdict'?' n':''}" data-k="${k}">${l}${k===sortKey?(sortDir<0?' ▾':' ▴'):''}</th>`).join('')+'</tr></thead><tbody>'+
  (rows.map(t=>`<tr><td class="mono">${esc(t.type)}</td><td class="n">${t.n}</td><td class="n">${t.runs}</td><td class="n">${pct(t.cov_p50)}</td><td class="n">${pct(t.cov_p85)}</td><td class="n">${T('p.a.calls_n',{n:t.mae_calls})}</td><td class="n">×${t.ratio}</td><td>${vd(t.verdict)}</td></tr>`).join('')||`<tr><td colspan="8" class="empty">${T('p.a.none')}</td></tr>`)+'</tbody>';
 $('#types').querySelectorAll('th').forEach(th=>th.onclick=()=>{const k=th.dataset.k;sortDir=k===sortKey?-sortDir:-1;sortKey=k;types(TY)})}
function spark(r){const W=220,H=46,P=r.points,mx=Math.max(r.actual,...P.map(p=>p[2]),1),xm=Math.max(...P.map(p=>p[0]),1);
 const x=c=>4+(P.length>1?(Math.log2(c)/Math.log2(xm||2)):0.5)*(W-8),y=v=>H-4-v/mx*(H-8);
 const line=(i,c)=>`<polyline fill="none" stroke="${c}" stroke-width="2" points="${P.map(p=>x(p[0]).toFixed(1)+','+y(p[i]).toFixed(1)).join(' ')}"/>`+P.map(p=>`<circle cx="${x(p[0]).toFixed(1)}" cy="${y(p[i]).toFixed(1)}" r="2.5" fill="${c}"/>`).join('');
 return `<svg width="${W}" height="${H}" role="img" aria-label="${T('p.a.spark_aria')}"><line x1="0" x2="${W}" y1="${y(r.actual)}" y2="${y(r.actual)}" stroke="var(--text-primary)" stroke-dasharray="4 3"/>${line(1,'var(--s1)')}${line(2,'var(--s2)')}<title>${P.map(p=>T('p.a.spark_pt',{c:p[0],p50:p[1],p85:p[2]})).join('\n')}\n${T('p.a.actual_n',{n:r.actual})}</title></svg>`}
function sessions(S){$('#sessk').textContent=T('p.a.sess_k',{n:S.sessions.length,total:S.sessions_total});
 $('#sess').innerHTML=S.sessions.map(s=>`<div class="sess"><div><b>${esc(s.project)}</b> <span class="k mono">${esc(s.session)}</span> <span class="k">${T('p.a.sess_meta',{last:fmt(s.last),n:s.n,p50:pct(s.cov_p50),p85:pct(s.cov_p85)})}</span></div>`+
  (()=>{const row=r=>`<div class="run">${spark(r)}<span class="mono">${esc(r.type)}</span><span class="k mono">${esc(r.agent)}</span><span>${T('p.a.actual_n',{n:r.actual})}</span><span>${r.hit_p85?T('p.a.all_in'):T('p.a.some_out')}</span></div>`,
   R=[...s.runs].reverse();return R.slice(0,6).map(row).join('')+(R.length>6?`<details><summary class="k">${T('p.a.more_runs',{n:R.length-6})}</summary>${R.slice(6).map(row).join('')}</details>`:'')})()+'</div>').join('')||`<p class="empty">${T('p.a.none')}</p>`}
function docs(){const C=D.checklists;$('#docs').innerHTML=C.length?`<table><thead><tr><th>${T('p.a.d_doc')}</th><th>${T('p.a.d_done')}</th><th class="n">${T('p.a.c_n')}</th><th class="n">${T('p.a.d_in50')}</th><th class="n">${T('p.a.d_in85')}</th></tr></thead><tbody>`+
 C.map(c=>`<tr><td><span class="mono">${esc(c.root_name)}/${esc(c.doc)}</span></td><td>${fmt(c.finished_ms)}</td><td class="n">${c.cases}</td><td class="n">${pct(c.cov_p50)}</td><td class="n">${pct(c.cov_p85)}</td></tr>`).join('')+'</tbody></table>':
 `<p class="empty">${T('p.a.no_docs')}</p>`}
draw();addEventListener('themechange',draw);addEventListener('resize',()=>{clearTimeout(window._r);window._r=setTimeout(draw,150)});
</script></body></html>"""


def render(data: dict, lang: str = "ko") -> str:
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return (fill(PAGE, lang).replace("/*HEAD*/", head(t("p.title.acc", lang), ACC_CSS, lang))
            .replace("/*SHELL*/", shell_html("accuracy", "", lang))
            .replace("/*UPLOT*/", uplot_js()).replace("/*DATA*/null", payload))
