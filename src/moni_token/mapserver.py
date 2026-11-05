"""Local server for the live agent map (ADR-0004): 127.0.0.1 only, no external resources.

A background thread runs the incremental collector every few seconds; each page poll reads the state from SQLite.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import accpage, accuracy
from .agentmap import build_state
from .collector import collect
from .calibrate import estimate
from .db import connect
from .forecast import backtest_cases, remaining_usage
from .report import build_data, header, render
from .ui import head, shell_html

HOST = "127.0.0.1"
RECORD_S = 20


def page() -> str:
    return PAGE.replace("/*HEAD*/", head("moni_token 에이전트 지도", MAP_CSS)).replace("/*SHELL*/", shell_html("map"))


_bt_cache: dict = {}


def _backtest(con) -> list[dict]:
    """The replay only changes when another subagent finishes: keep it until then."""
    key = con.execute("SELECT COUNT(*), MAX(mtime) FROM agent_meta WHERE finished = 1").fetchone()
    if _bt_cache.get("key") != key:
        _bt_cache.update(key=key, cases=backtest_cases(con))
    return _bt_cache["cases"]


def make_handler(db: Path, projects: Path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):   # keep the console quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; "
                                                        "script-src 'unsafe-inline'; connect-src 'self'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            host = (self.headers.get("Host") or "").split(":")[0]
            if host not in ("127.0.0.1", "localhost"):   # DNS rebinding guard
                return self._send(403, b"forbidden", "text/plain")
            u = urlparse(self.path)
            if u.path == "/":
                return self._send(200, page().encode("utf-8"), "text/html; charset=utf-8")
            if u.path == "/usage":   # the usage report, from live data (issue #4)
                con = connect(db)
                try:
                    html = render(build_data(con, int(time.time() * 1000)), map_base="")
                finally:
                    con.close()
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            if u.path == "/accuracy":   # issue #9
                con = connect(db)
                try:
                    now = int(time.time() * 1000)
                    data = accuracy.build(con, now, _backtest(con))
                    data["header"] = header(con, now)
                finally:
                    con.close()
                return self._send(200, accpage.render(data).encode("utf-8"), "text/html; charset=utf-8")
            if u.path == "/api/state":
                q = parse_qs(u.query)
                con = connect(db)
                try:
                    at = int((q.get("at") or [0])[0])   # ?at=<epoch ms>: look back
                    now = at or int(time.time() * 1000)
                    st = build_state(con, projects, now, (q.get("project") or [None])[0],
                                     int((q.get("window") or ["30"])[0]), live=not at)
                    st["header"] = header(con, now, live=not at)
                    st["usage_forecast"] = remaining_usage([a["forecast"] or {} for a in st["agents"]],
                                                           estimate(con, "five_hour")["usd_per_pct"],
                                                           st["header"]["five_hour"]["pct"])
                finally:
                    con.close()
                return self._send(200, json.dumps(st, ensure_ascii=False).encode("utf-8"),
                                  "application/json; charset=utf-8")
            return self._send(404, b"not found", "text/plain")
    return Handler


def serve(db: Path, projects: Path, port: int = 4990, interval_s: float = 5.0) -> None:
    stop = threading.Event()

    def collector():
        con = connect(db)
        last_rec = 0.0
        while not stop.is_set():
            try:
                collect(con, projects)
                if time.time() - last_rec >= RECORD_S:   # issue #9: keep the live forecasts for scoring
                    accuracy.record_live(con, int(time.time() * 1000))
                    last_rec = time.time()
            except Exception:   # a locked or half-written file must not stop the map
                pass
            stop.wait(interval_s)
        con.close()

    threading.Thread(target=collector, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, port), make_handler(db, projects))
    print(f"agent map: http://{HOST}:{port}/  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        srv.server_close()


MAP_CSS = r"""
.bar2{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:8px 16px;border-bottom:1px solid var(--border)}
.bar2 select,.bar2 input{font:inherit;padding:3px 8px;border-radius:6px;border:1px solid var(--border);
background:var(--surface-1);color:var(--text-primary)}.bar2 input{width:220px}
.wrap{padding:12px 16px 40px;display:grid;gap:12px}
.cols{display:grid;grid-template-columns:minmax(260px,1fr) minmax(340px,1.5fr) minmax(260px,1fr);gap:12px}
@media (max-width:960px){.cols{grid-template-columns:1fr}}
.card{background:var(--surface-1);border:1px solid var(--border);border-radius:10px;padding:12px;min-width:0}
h2{font-size:13px;margin:0 0 8px;display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}.k{color:var(--text-secondary);font-size:12px;font-weight:400}
.ag{padding:6px 0;border-bottom:1px solid var(--grid)}.ag:last-child{border:0}
.sw{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;vertical-align:0}
.run{color:var(--good);border-color:var(--good)}.done{color:var(--muted)}
.path{word-break:break-all}
ul.tree{list-style:none;margin:0;padding-left:14px}ul.tree.root{padding-left:0}.tree li{white-space:nowrap;line-height:1.7}
.tree .f{font-family:var(--mono);font-size:12px}.tree .hot{font-weight:600}.tree .ed{color:var(--critical)}
.tree .focus{background:color-mix(in srgb,var(--line) 22%,transparent);border-radius:3px}
.heat{display:inline-block;height:5px;border-radius:3px;background:var(--line);opacity:.55;margin-left:6px;vertical-align:1px}
.dots{margin-left:4px}.dots i{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:2px}
.dots i.now{outline:2px solid var(--text-primary);outline-offset:1px}
details>summary{cursor:pointer;list-style:none}details>summary::before{content:"▸ ";color:var(--muted)}
details[open]>summary::before{content:"▾ "}.scroll{max-height:64vh;overflow:auto}
.pg,.ft{padding:4px 6px;border-bottom:1px solid var(--grid)}.pg.on,.ft.on{background:var(--surface-2);border-radius:4px}
.meter{height:4px;background:var(--grid);border-radius:2px;overflow:hidden;margin:3px 0}.meter i{display:block;height:100%;background:var(--line)}
.empty{color:var(--muted)}.cost{margin-left:8px;color:var(--text-secondary);font-size:11px;font-family:var(--mono)}
#lanes svg{display:block;width:100%}#lanes text{fill:var(--text-secondary);font-size:11px}
#lanes .lbl{fill:var(--text-primary);font-size:12px}#lanes rect.seg{cursor:pointer}
.lg{display:flex;gap:12px;flex-wrap:wrap;color:var(--text-secondary);font-size:12px}.lg i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:-1px}
"""

PAGE = r"""/*HEAD*/<body>/*SHELL*/
<div class="bar2"><select id="proj" aria-label="프로젝트"></select>
<select id="win" aria-label="기간"><option value="15">15분</option><option value="30" selected>30분</option><option value="120">2시간</option></select>
<input id="q" type="search" placeholder="트리에서 파일 찾기" aria-label="트리 검색"><span class="k" id="when"></span></div>
<div class="wrap">
<section class="card"><h2>에이전트 타임라인 <span class="k">· 줄마다 한 에이전트, 칸마다 도구 호출. 칸을 누르면 그 파일을 트리에서 보여 줍니다</span></h2>
<div class="lg" id="lg"></div><div id="lanes"></div></section>
<div class="cols">
<section class="card"><h2>에이전트</h2><div class="k" id="ufc"></div><div id="agents" class="scroll"></div></section>
<section class="card"><h2>소스 트리 <span class="k">· 굵게 = 다룸, 빨강 = 수정, 막대 = 다룬 횟수, 테두리 점 = 지금 위치</span></h2><div id="tree" class="scroll"></div></section>
<section class="card"><h2>UI 페이지</h2><div id="pages"></div><h2 style="margin-top:14px">기능 <span class="k">· 계약 문서 기준</span></h2><div id="feats" class="scroll"></div></section>
</div></div>
<script>
const $=s=>document.querySelector(s),esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const PAL=['var(--s1)','var(--s2)','var(--s3)','var(--s4)','var(--s5)','var(--s6)','var(--s7)'];
const KIND={read:['읽기','var(--s1)'],edit:['수정','var(--critical)'],run:['실행','var(--s4)'],web:['웹','var(--s7)'],agent:['하위 에이전트','var(--s3)'],mcp:['MCP','var(--s5)'],other:['기타','var(--muted)']};
const ST={running:'실행 중',idle:'멈춤',done:'완료'},tk=n=>n>=1e6?(n/1e6).toFixed(1)+'M':n>=1e3?Math.round(n/1e3)+'k':String(n);let open=new Set(),focusPath=null,D=null;
const U=new URLSearchParams(location.search),AT=U.get('at');let proj=U.get('project');
if(U.get('window')){const w=U.get('window'),s=$('#win');if(![...s.options].some(o=>o.value===w))s.add(new Option(w+'분',w));s.value=w}
$('#lg').innerHTML=Object.values(KIND).map(([n,c])=>`<span><i style="background:${c}"></i>${n}</span>`).join('');
async function tick(){
 const q=new URLSearchParams({window:$('#win').value});if(proj)q.set('project',proj);if(AT)q.set('at',AT);
 try{D=await (await fetch('/api/state?'+q)).json()}catch(e){$('#meta').textContent='서버 연결 끊김';return}
 draw()}
function draw(){if(!D)return;
 const ago=ms=>{const s=Math.round((D.now-ms)/1000);return s<60?s+'초 전':s<3600?Math.round(s/60)+'분 전':Math.round(s/3600)+'시간 전'};
 const wsh=w=>w&&w.share?` · 5시간 구간의 ${Math.round(w.share*100)}%${w.pp!=null?` (≈${w.pp}%p 추정)`:''}`:'';
 $('#lim').innerHTML=shellLimits(D.header);
 $('#meta').textContent=(AT?`과거 시점 ${new Date(D.now).toLocaleString()} 기준`:`갱신 ${new Date(D.now).toLocaleTimeString()}`)+' · LLM 호출 없음 · 로컬 전용';
 const sel=$('#proj');sel.innerHTML=D.projects.map(p=>`<option value="${esc(p.project_dir)}">${esc(p.name)}${p.win&&p.win.share?` · ${Math.round(p.win.share*100)}%`:''}</option>`).join('')||'<option>최근 활동 없음</option>';
 if(D.project){proj=D.project.project_dir;sel.value=proj}
 const col={};D.agents.forEach((a,i)=>col[a.key]=i<PAL.length?PAL[i]:'var(--muted)');
 lanes(col);
 // issue #7: forecast from past runs of the same agentType (estimates, with sample size)
 const mins=ms=>ms<60000?Math.round(ms/1000)+'초':Math.round(ms/60000)+'분';
 const fc=f=>{if(!f)return '';if(!f.ok)return `<div class="k">진행 예측: 기록 부족 (비교할 과거 실행 ${f.n}개, 10개부터)</div>`;
  return `<div class="k">진행 예측 <span class="badge">추정</span> ${f.calls}회째 · 보통 ${f.calls_p50}회(P50)~${f.calls_p85}회(P85)에 끝남 · 남은 시간 ${mins(f.remain_ms_p50)}~${mins(f.remain_ms_p85)} · 과거 ${f.n}건${f.scope==='all'?'(전 프로젝트)':''}</div>
  <div class="meter" title="P50 기준 진행률"><i style="width:${Math.round((f.progress||0)*100)}%"></i></div>`};
 const UF=D.usage_forecast;$('#ufc').innerHTML=UF?`실행 중 에이전트 ${UF.agents}개가 더 쓸 양 ≈ <b>+${UF.pp_p50}~${UF.pp_p85}%p</b> (5시간 한도, P50~P85 추정)${UF.over_limit_p85?' <span class="warn">· P85면 한도를 넘음</span>':''}`:'';
 $('#agents').innerHTML=D.agents.map(a=>{const t=a.touches.at(-1);
  return `<div class="ag"><div><span class="sw" style="background:${col[a.key]}"></span><b>${esc(a.type)}</b> <span class="badge ${a.status==='running'?'run':a.status==='done'?'done':''}">${ST[a.status]}</span> <span class="k mono">${esc(a.session)}${a.agent?' · '+esc(a.agent.slice(0,8)):''}</span></div>
  ${a.desc?`<div>${esc(a.desc)}</div>`:''}<div class="k">${a.calls}회 호출 · ${(a.tokens/1000).toFixed(0)}k 토큰 · 마지막 ${ago(a.last_ms)}${a.tool?' · '+esc(a.tool):''}${wsh(a.win)}</div>
  ${t?`<div class="path mono">${t.edit?'✎ ':'👁 '}${esc(t.path)}</div>${t.page?`<div class="k">페이지 ${esc(t.page)}</div>`:''}${t.features.length?`<div class="k">기능 ${t.features.map(i=>esc(D.features[i].title)).join(', ')}</div>`:''}`:'<div class="k">아직 파일을 다루지 않음</div>'}${fc(a.forecast)}${a.top_file&&a.top_file.carried>=1000?`<div class="k">가장 무거운 파일 <span class="mono">${esc(a.top_file.path)}</span> · 끌고 다닌 양 ${tk(a.top_file.carried)}</div>`:''}</div>`}).join('')||'<p class="empty">이 기간에 활동한 에이전트가 없습니다.</p>';
 tree(col);pagesFeats(col)}
function lanes(col){
 const box=$('#lanes'),W=box.clientWidth||900,L=230,R=10,rh=22,ags=D.agents.filter(a=>a.lanes.length);
 if(!ags.length){box.innerHTML='<p class="empty">이 기간에 도구 호출이 없습니다.</p>';return}
 const t1=D.now,t0=t1-D.window_min*60000,x=t=>L+(t-t0)/(t1-t0)*(W-L-R),H=ags.length*rh+22;
 let g='';const hm=ms=>new Date(ms).toTimeString().slice(0,5);
 for(let i=0;i<=4;i++){const t=t0+(t1-t0)*i/4,xx=x(t);g+=`<line x1="${xx}" x2="${xx}" y1="0" y2="${H-18}" stroke="var(--grid)"/><text x="${xx}" y="${H-4}" text-anchor="middle">${hm(t)}</text>`}
 ags.forEach((a,i)=>{const y=i*rh;g+=`<circle cx="8" cy="${y+11}" r="4" fill="${col[a.key]}"/><text class="lbl" x="18" y="${y+15}">${esc((a.type+(a.desc?' · '+a.desc:'')).slice(0,32))}</text>`;
  a.lanes.forEach(([t,k,p])=>{if(t<t0)return;g+=`<rect class="seg" x="${x(t)-1.5}" y="${y+4}" width="4" height="14" rx="1" fill="${KIND[k]?.[1]||KIND.other[1]}" data-p="${esc(p||'')}"><title>${hm(t)} ${esc(KIND[k]?.[0]||k)}${p?' · '+esc(p):''}</title></rect>`})});
 box.innerHTML=`<svg height="${H}" role="img" aria-label="에이전트별 도구 호출 타임라인">${g}</svg>`;
 box.querySelectorAll('rect.seg').forEach(r=>r.onclick=()=>{const p=r.dataset.p;if(!p)return;focusPath=p;
  const s=p.split('/');for(let i=1;i<s.length;i++)open.add(s.slice(0,i).join('/'));tree(col);
  const el=[...$('#tree').querySelectorAll('[data-path]')].find(e=>e.dataset.path===p);if(el)el.scrollIntoView({block:'center'})})}
function tree(col){
 const hit={},now={},ed={},cnt={};
 for(const a of D.agents){a.touches.forEach(t=>{(hit[t.path]??=new Set()).add(a.key);if(t.edit)ed[t.path]=1;
  const s=t.path.split('/');for(let i=1;i<=s.length;i++){const k=s.slice(0,i).join('/');cnt[k]=(cnt[k]||0)+1}});
  if(a.status==='running'&&a.where)(now[a.where]??=new Set()).add(a.key)}
 const mx=Math.max(1,...Object.values(cnt)),q=$('#q').value.trim().toLowerCase();
 const match=new Set();if(q)for(const e of D.tree)if(!e.more&&e.path.toLowerCase().includes(q)){const s=e.path.split('/');for(let i=1;i<=s.length;i++)match.add(s.slice(0,i).join('/'))}
 const kids={};for(const e of D.tree){const i=e.path.lastIndexOf('/'),par=i<0?'':e.path.slice(0,i);(kids[par]??=[]).push(e)}
 const dots=p=>{const ks=[...(hit[p]||[])];return ks.length?`<span class="dots">${ks.map(k=>`<i class="${now[p]?.has(k)?'now':''}" style="background:${col[k]}"></i>`).join('')}</span>`:''};
 const heat=p=>cnt[p]?`<span class="heat" style="width:${Math.max(4,Math.round(cnt[p]/mx*60))}px" title="${cnt[p]}회"></span>`:'';
 // issue #6: tokens each file added to the context, and how much re-reading it carried
 const C=D.costs||{},fold={};for(const p in C){const s=p.split('/');for(let i=1;i<s.length;i++){const k=s.slice(0,i).join('/');fold[k]=(fold[k]||0)+C[p].carried}}
 const cost=(p,dir)=>{if(dir)return fold[p]?`<span class="cost" title="이 폴더 파일들이 끌고 다닌 토큰">${tk(fold[p])}</span>`:'';
  const c=C[p];if(!c)return '';const t=`읽은 결과 ${c.tokens.toLocaleString()}토큰, 이후 최대 ${c.later}회 다시 읽힘 → 끌고 다닌 양 ${c.carried.toLocaleString()}토큰${c.pp!=null?` (≈${c.pp}%p 추정)`:''}`;
  return `<span class="cost" title="${t}">${tk(c.tokens)}${c.later?` ×${c.later} → ${tk(c.carried)}`:''}${c.pp!=null&&c.pp>=0.05?` ≈${c.pp}%p`:''}</span>`};
 const render=par=>`<ul class="tree${par?'':' root'}">`+(kids[par]||[]).filter(e=>!q||e.more?!q:match.has(e.path)).map(e=>{
  if(e.more)return `<li class="k">… 외 ${e.more}개</li>`;
  const nm=esc(e.path.split('/').pop()),cls=(cnt[e.path]?'hot ':'')+(ed[e.path]?'ed ':'')+(focusPath===e.path?'focus':'');
  if(!e.dir)return `<li class="f ${cls}" data-path="${esc(e.path)}">${nm}${heat(e.path)}${dots(e.path)}${cost(e.path,false)}</li>`;
  const o=(q&&match.has(e.path))||cnt[e.path]||open.has(e.path);
  return `<li><details data-p="${esc(e.path)}"${o?' open':''}><summary class="f ${cls}" data-path="${esc(e.path)}">${nm}/${heat(e.path)}${dots(e.path)}${cost(e.path,true)}</summary>${o?render(e.path):''}</details></li>`}).join('')+'</ul>';
 $('#tree').innerHTML=D.tree.length?render(''):'<p class="empty">프로젝트 폴더를 읽을 수 없습니다.</p>';
 $('#tree').querySelectorAll('details').forEach(d=>d.ontoggle=()=>{const was=open.has(d.dataset.p);d.open?open.add(d.dataset.p):open.delete(d.dataset.p);if(d.open&&!was&&!d.querySelector('ul'))tree(col)})}
function pagesFeats(col){
 const usedPg=new Set(),usedF={};D.agents.forEach(a=>a.touches.forEach(t=>{if(t.page)usedPg.add(t.page);t.features.forEach(i=>(usedF[i]??=new Set()).add(a.key))}));
 $('#pages').innerHTML=D.pages.length?D.pages.map(p=>`<div class="pg ${usedPg.has(p.route)?'on':''}"><span class="mono">${esc(p.route)}</span> <span class="k">${esc(p.file)}</span></div>`).join(''):'<p class="empty">UI 페이지를 찾지 못했습니다(Next.js app/·pages/, *.html 기준).</p>';
 const day=ms=>{const d=new Date(ms);return `${d.getMonth()+1}/${d.getDate()} ${d.toTimeString().slice(0,5)}`};
 const docf=(D.docs||[]).filter(d=>d.total).map(d=>`<div class="k"><span class="mono">${esc(d.doc)}</span> 완료 ${d.done}/${d.total}${d.left===0?' · 다 끝남':d.ok?` · 남은 ${d.left}개 완료 예상 ${day(d.eta_p50)}(P50)~${day(d.eta_p85)}(P85) <span class="badge">추정</span>`:` · 완료 예측: 기록 부족 (완료 ${d.steps}번 관측, 3번부터)`}</div>`).join('');
 $('#feats').innerHTML=docf+(D.features.length?D.features.map((f,i)=>`<div class="ft ${usedF[i]?'on':''}"><div><b>${esc(f.title)}</b> ${[...(usedF[i]||[])].map(k=>`<span class="sw" style="background:${col[k]}"></span>`).join('')}</div>
  <div class="k">${esc(f.doc)}${f.total?` · 완료 ${f.done}/${f.total}`:''}${f.paths.length?` · 연결 ${f.paths.length}곳`:''}</div>${f.total?`<div class="meter"><i style="width:${f.done/f.total*100}%"></i></div>`:''}</div>`).join(''):'<p class="empty">계약 문서(docs/PLAN.md, PLAN.md, SPEC.md, docs/tasks/*.md)가 없거나, 경로·체크리스트가 적혀 있지 않습니다.</p>')}
$('#proj').onchange=e=>{proj=e.target.value;open=new Set();focusPath=null;tick()};$('#win').onchange=tick;
$('#q').oninput=()=>{if(D)tree(Object.fromEntries(D.agents.map((a,i)=>[a.key,i<PAL.length?PAL[i]:'var(--muted)'])))};
addEventListener('resize',()=>{clearTimeout(window._r);window._r=setTimeout(draw,150)});addEventListener('themechange',draw);
tick();setInterval(tick,3000);
</script></body></html>"""
