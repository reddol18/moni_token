"""The forecast accuracy page (issue #9), served by the map server at /accuracy. Data is embedded as JSON."""
import json

from .ui import head, shell_html, uplot_js

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
<h1>예측 정확도</h1>
<p class="sub">지도에 나온 진행 예측(P50·P85)이 실제로 맞았는지. 실제 전체 호출 수가 P50 이하면 "P50 적중", P85 이하면 "P85 적중"입니다.
목표는 각각 약 50%·85% — 더 높으면 범위가 넓고(안전한 쪽), 더 낮으면 좁습니다. LLM 호출 없음.</p>
<div class="tabs" id="tabs"></div>
<p class="k" id="srcnote"></p>
<div class="tiles" id="tiles"></div>
<h2>주별 추이 <span class="k">· 기록이 쌓일수록 나아지는지</span></h2>
<div class="card"><div class="lg"><span><i style="border-color:var(--s1)"></i>P50 적중</span><span><i style="border-color:var(--s2)"></i>P85 적중</span>
<span><i style="border-color:var(--muted);border-top-style:dashed"></i>목표 50%·85%</span></div><div class="chart" id="wk"></div></div>
<h2>에이전트 종류별 <span class="k">· 머리글을 누르면 정렬</span></h2>
<div class="card scroll"><table id="types"></table></div>
<h2>세션별 <span class="k" id="sessk"></span></h2>
<div class="card"><div class="lg"><span><i style="border-color:var(--s1)"></i>P50 예측</span><span><i style="border-color:var(--s2)"></i>P85 예측</span>
<span><i style="border-color:var(--text-primary);border-top-style:dashed"></i>실제 전체 호출</span><span>가로축 = 예측 시점의 호출 수</span></div><div id="sess"></div></div>
<h2>문서 완료 예측 <span class="k">· 체크리스트가 다 끝난 문서만</span></h2>
<div class="card" id="docs"></div>
</main>
<script id="data" type="application/json">/*DATA*/null</script>
<script>/*UPLOT*/</script>
<script>
const D=JSON.parse(document.getElementById('data').textContent);
const $=s=>document.querySelector(s),esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const pct=v=>v==null?'—':Math.round(v*100)+'%',pad=n=>String(n).padStart(2,'0');
const fmt=ms=>{const d=new Date(ms);return `${d.getMonth()+1}/${d.getDate()} ${pad(d.getHours())}:${pad(d.getMinutes())}`};
const VD={few:['표본 부족',''],wide:['범위가 넓음 (안전한 쪽)','wide'],ok:['적절','ok'],narrow:['범위가 좁음 (자주 넘음)','narrow']};
const vd=v=>v&&VD[v]?`<span class="st ${VD[v][1]}">${VD[v][0]}</span>`:'—';
let src=new URLSearchParams(location.search).get('src')==='backtest'?'backtest':'live',sortKey='n',sortDir=-1;
[['live','실시간 기록'],['backtest','과거 재현(백테스트)']].forEach(([k,l])=>{const b=document.createElement('button');b.textContent=l;
 b.setAttribute('aria-pressed',k===src);b.onclick=()=>{src=k;$('#tabs').querySelectorAll('button').forEach(x=>x.setAttribute('aria-pressed',x===b));draw()};$('#tabs').append(b)});
$('#lim').innerHTML=shellLimits(D.header);$('#meta').textContent='LLM 호출 없음 · 로컬 전용';
function draw(){const S=D[src],O=S.overall;
 $('#srcnote').textContent=src==='live'?`지도가 실제로 보여 준 예측을 호출 수가 바뀔 때마다 기록하고, 에이전트가 끝나면 채점합니다. 아직 실행 중 ${S.pending}건 · 세션이 닫혀 끝을 모르는 미확정 ${S.unresolved}건(틀린 것으로 세지 않음).`:
  '완료된 과거 실행마다, 그보다 앞서 끝난 실행만으로 호출 1·2·4·8…회째의 예측을 다시 만들어 채점합니다(moni-token forecast와 같은 계산). 시간 예측은 재현하지 않습니다.';
 const tile=(k,v,d,sm)=>`<div class="tile"><div class="k">${k}</div><div class="v${sm?' sm':''}">${v}</div><div class="d">${d}</div></div>`;
 $('#tiles').innerHTML=O.n?tile('P50 적중',pct(O.cov_p50),'목표 약 50%')+tile('P85 적중',pct(O.cov_p85),'목표 약 85%')+
  tile('판정',vd(O.verdict),`예측 ${O.n}건 · 실행 ${O.runs}개 (${D.min_cases}건부터 판정)`,1)+
  tile('오차 중앙값',`${O.mae_calls}회`,`|실제 − P50| 호출 수 · 실제/P50 중앙값 ×${O.ratio}`)+
  (O.mae_min!=null?tile('남은 시간 오차',`${O.mae_min}분`,`중앙값 · P85 안 ${pct(O.cov_time_p85)}`):'')
  :`<p class="empty">${src==='live'?'아직 채점된 실시간 예측이 없습니다. 서브에이전트가 돌고 끝나면 여기에 쌓입니다(기록은 이 기능을 켠 뒤부터).':'완료된 과거 실행이 부족합니다.'}</p>`;
 weeks(S.by_week);types(S.by_type);sessions(S);docs()}
function weeks(W){const box=$('#wk');box.innerHTML='';if(!W.length){box.innerHTML='<p class="empty">아직 없음</p>';return}
 const xs=W.map(w=>w.ts/1000),muted=cssVar('--muted'),grid=cssVar('--grid');
 new uPlot({width:box.clientWidth,height:220,scales:{x:{time:true,range:()=>[Math.min(...xs)-4*86400,Math.max(...xs)+4*86400]},y:{range:[0,100]}},legend:{live:true},cursor:{drag:{x:false,y:false}},
  axes:[{stroke:muted,grid:{stroke:grid},incrs:[86400,2*86400,7*86400,14*86400,28*86400],values:(u,t)=>t.map(v=>{const d=new Date(v*1000);return `${d.getMonth()+1}/${d.getDate()}`})},
   {stroke:muted,grid:{stroke:grid},values:(u,t)=>t.map(v=>v+'%'),size:44}],
  series:[{label:'주',value:(u,v)=>{const w=W.find(x=>x.ts/1000===v);return w?`${w.week} (예측 ${w.n}건)`:'—'}},
   {label:'P50 적중',stroke:cssVar('--s1'),width:2,points:{size:8},value:(u,v)=>v==null?'—':Math.round(v)+'%'},
   {label:'P85 적중',stroke:cssVar('--s2'),width:2,points:{size:8},value:(u,v)=>v==null?'—':Math.round(v)+'%'},
   {label:'목표 50%',stroke:muted,width:1,dash:[4,4],points:{show:false},value:()=>'50%'},
   {label:'목표 85%',stroke:muted,width:1,dash:[4,4],points:{show:false},value:()=>'85%'}]},
  [xs,W.map(w=>w.cov_p50*100),W.map(w=>w.cov_p85*100),W.map(()=>50),W.map(()=>85)],box)}
function types(T){const cols=[['type','에이전트 종류'],['n','예측'],['runs','실행'],['cov_p50','P50 적중'],['cov_p85','P85 적중'],['mae_calls','오차 중앙값'],['ratio','실제/P50'],['verdict','판정']];
 const rows=[...T].sort((a,b)=>(a[sortKey]>b[sortKey]?1:a[sortKey]<b[sortKey]?-1:0)*sortDir);
 $('#types').innerHTML='<thead><tr>'+cols.map(([k,l])=>`<th class="s${k!=='type'&&k!=='verdict'?' n':''}" data-k="${k}">${l}${k===sortKey?(sortDir<0?' ▾':' ▴'):''}</th>`).join('')+'</tr></thead><tbody>'+
  (rows.map(t=>`<tr><td class="mono">${esc(t.type)}</td><td class="n">${t.n}</td><td class="n">${t.runs}</td><td class="n">${pct(t.cov_p50)}</td><td class="n">${pct(t.cov_p85)}</td><td class="n">${t.mae_calls}회</td><td class="n">×${t.ratio}</td><td>${vd(t.verdict)}</td></tr>`).join('')||'<tr><td colspan="8" class="empty">아직 없음</td></tr>')+'</tbody>';
 $('#types').querySelectorAll('th').forEach(th=>th.onclick=()=>{const k=th.dataset.k;sortDir=k===sortKey?-sortDir:-1;sortKey=k;types(T)})}
function spark(r){const W=220,H=46,P=r.points,mx=Math.max(r.actual,...P.map(p=>p[2]),1),xm=Math.max(...P.map(p=>p[0]),1);
 const x=c=>4+(P.length>1?(Math.log2(c)/Math.log2(xm||2)):0.5)*(W-8),y=v=>H-4-v/mx*(H-8);
 const line=(i,c)=>`<polyline fill="none" stroke="${c}" stroke-width="2" points="${P.map(p=>x(p[0]).toFixed(1)+','+y(p[i]).toFixed(1)).join(' ')}"/>`+P.map(p=>`<circle cx="${x(p[0]).toFixed(1)}" cy="${y(p[i]).toFixed(1)}" r="2.5" fill="${c}"/>`).join('');
 return `<svg width="${W}" height="${H}" role="img" aria-label="예측과 실제"><line x1="0" x2="${W}" y1="${y(r.actual)}" y2="${y(r.actual)}" stroke="var(--text-primary)" stroke-dasharray="4 3"/>${line(1,'var(--s1)')}${line(2,'var(--s2)')}<title>${P.map(p=>`${p[0]}회째: P50 ${p[1]} · P85 ${p[2]}`).join('\n')}\n실제 ${r.actual}회</title></svg>`}
function sessions(S){$('#sessk').textContent=`· 최근 ${S.sessions.length}개 / 전체 ${S.sessions_total}개`;
 $('#sess').innerHTML=S.sessions.map(s=>`<div class="sess"><div><b>${esc(s.project)}</b> <span class="k mono">${esc(s.session)}</span> <span class="k">· 마지막 ${fmt(s.last)} · 예측 ${s.n}건 · P50 ${pct(s.cov_p50)} · P85 ${pct(s.cov_p85)}</span></div>`+
  (()=>{const row=r=>`<div class="run">${spark(r)}<span class="mono">${esc(r.type)}</span><span class="k mono">${esc(r.agent)}</span><span>실제 ${r.actual}회</span><span>${r.hit_p85?'✓ 모든 예측의 P85 안':'✗ P85를 넘은 예측 있음'}</span></div>`,
   R=[...s.runs].reverse();return R.slice(0,6).map(row).join('')+(R.length>6?`<details><summary class="k">이전 실행 ${R.length-6}개 더 보기</summary>${R.slice(6).map(row).join('')}</details>`:'')})()+'</div>').join('')||'<p class="empty">아직 없음</p>'}
function docs(){const C=D.checklists;$('#docs').innerHTML=C.length?'<table><thead><tr><th>문서</th><th>완료</th><th class="n">예측</th><th class="n">P50 안</th><th class="n">P85 안</th></tr></thead><tbody>'+
 C.map(c=>`<tr><td><span class="mono">${esc(c.root_name)}/${esc(c.doc)}</span></td><td>${fmt(c.finished_ms)}</td><td class="n">${c.cases}</td><td class="n">${pct(c.cov_p50)}</td><td class="n">${pct(c.cov_p85)}</td></tr>`).join('')+'</tbody></table>':
 '<p class="empty">체크리스트를 다 끝낸 계약 문서가 아직 없습니다. 끝나면 그동안의 완료 예상 시각이 맞았는지 여기서 채점합니다.</p>'}
draw();addEventListener('themechange',draw);addEventListener('resize',()=>{clearTimeout(window._r);window._r=setTimeout(draw,150)});
</script></body></html>"""


def render(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return (PAGE.replace("/*HEAD*/", head("moni_token 예측 정확도", ACC_CSS)).replace("/*SHELL*/", shell_html("accuracy"))
            .replace("/*UPLOT*/", uplot_js()).replace("/*DATA*/null", payload))
