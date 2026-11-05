"""Local server for the live agent map (ADR-0004): 127.0.0.1 only, no external resources.

A background thread runs the incremental collector every few seconds; each page poll reads the state from SQLite.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .agentmap import build_state
from .collector import collect
from .db import connect

HOST = "127.0.0.1"


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
                return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            if u.path == "/api/state":
                q = parse_qs(u.query)
                con = connect(db)
                try:
                    at = int((q.get("at") or [0])[0])   # ?at=<epoch ms>: look back
                    st = build_state(con, projects, at or int(time.time() * 1000), (q.get("project") or [None])[0],
                                     int((q.get("window") or ["30"])[0]), live=not at)
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
        while not stop.is_set():
            try:
                collect(con, projects)
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


PAGE = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>moni_token 에이전트 지도</title>
<style>
:root{--bg:#f4f4f2;--surface:#fcfcfb;--text:#1a1a19;--text2:#57564f;--muted:#8a897f;--grid:#e4e3dd;--border:#d9d8d1;
--hl:#2a78d6;--edit:#c42b2a;--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--s5:#e87ba4;--s6:#008300;--s7:#4a3aa7}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#121211;--surface:#1a1a19;--text:#fff;
--text2:#c3c2b7;--muted:#8f8e85;--grid:#2c2c2a;--border:#3a3a37;--hl:#3987e5;--edit:#e66767;--s1:#3987e5;--s2:#d95926;
--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#008300;--s7:#9085e9}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:14px/1.5 system-ui,-apple-system,"Segoe UI","Malgun Gothic",sans-serif}
header{display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:14px 16px;border-bottom:1px solid var(--border)}
h1{font-size:18px;margin:0}select,button{font:inherit;padding:2px 8px;border-radius:6px;border:1px solid var(--border);
background:var(--surface);color:var(--text)}.sub{color:var(--text2);font-size:12px}
main{display:grid;grid-template-columns:minmax(260px,1fr) minmax(320px,1.4fr) minmax(260px,1fr);gap:12px;padding:12px 16px}
@media (max-width:900px){main{grid-template-columns:1fr}}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px;min-width:0}
h2{font-size:14px;margin:0 0 8px}.ag{border-bottom:1px solid var(--grid);padding:6px 0}.ag:last-child{border:0}
.sw{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;vertical-align:-1px}
.badge{font-size:11px;padding:0 6px;border-radius:4px;border:1px solid var(--border);color:var(--text2)}
.run{color:var(--s3);border-color:var(--s3)}.done{color:var(--muted)}.k{color:var(--text2);font-size:12px}
.path{font-family:ui-monospace,Consolas,monospace;font-size:12px;word-break:break-all}
ul.tree{list-style:none;margin:0;padding-left:14px}ul.tree.root{padding-left:0}
.tree li{white-space:nowrap}.tree .f{font-family:ui-monospace,Consolas,monospace;font-size:12px}
.tree .hot{font-weight:600}.tree .ed{color:var(--edit)}.dots{margin-left:6px}.dots i{display:inline-block;width:8px;
height:8px;border-radius:50%;margin-right:2px}.dots i.now{outline:2px solid var(--text);outline-offset:1px}
details>summary{cursor:pointer;list-style:none}details>summary::before{content:"▸ ";color:var(--muted)}
details[open]>summary::before{content:"▾ "}.scroll{max-height:72vh;overflow:auto}
.pg,.ft{padding:4px 0;border-bottom:1px solid var(--grid)}.pg.on,.ft.on{background:var(--grid);border-radius:4px}
.bar{height:5px;background:var(--grid);border-radius:3px;overflow:hidden;margin:3px 0}.bar i{display:block;height:100%;background:var(--hl)}
.empty{color:var(--muted)}
</style></head><body>
<header><h1>에이전트 지도</h1><select id="proj" aria-label="프로젝트"></select>
<select id="win" aria-label="기간"><option value="15">15분</option><option value="30" selected>30분</option>
<option value="120">2시간</option></select><span class="sub" id="meta"></span></header>
<main>
<section class="card"><h2>에이전트</h2><div id="agents" class="scroll"></div></section>
<section class="card"><h2>소스 트리 <span class="k">· 굵게 = 이 기간에 다룸, 빨강 = 수정, 테두리 점 = 지금 위치</span></h2>
<div id="tree" class="scroll"></div></section>
<section class="card"><h2>UI 페이지</h2><div id="pages"></div><h2 style="margin-top:14px">기능 <span class="k">· 계약 문서 기준</span></h2>
<div id="feats" class="scroll"></div></section>
</main>
<script>
const $=s=>document.querySelector(s),esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const PAL=['var(--s1)','var(--s2)','var(--s3)','var(--s4)','var(--s5)','var(--s6)','var(--s7)'];
const ST={running:'실행 중',idle:'멈춤',done:'완료'};let open=new Set();
const U=new URLSearchParams(location.search),AT=U.get('at');let proj=U.get('project');
async function tick(){
 const q=new URLSearchParams({window:$('#win').value});if(proj)q.set('project',proj);if(AT)q.set('at',AT);
 let D;try{D=await (await fetch('/api/state?'+q)).json()}catch(e){$('#meta').textContent='서버 연결 끊김';return}
 const sel=$('#proj');sel.innerHTML=D.projects.map(p=>`<option value="${esc(p.project_dir)}">${esc(p.name)}</option>`).join('')||'<option>최근 활동 없음</option>';
 if(D.project){proj=D.project.project_dir;sel.value=proj}
 $('#meta').textContent=(AT?`과거 시점 ${new Date(D.now).toLocaleString()} 기준`:`갱신 ${new Date(D.now).toLocaleTimeString()}`)+' · LLM 호출 없음 · 로컬 전용';
 const col={};D.agents.forEach((a,i)=>col[a.key]=i<PAL.length?PAL[i]:'var(--muted)');
 const ago=ms=>{const s=Math.round((D.now-ms)/1000);return s<60?s+'초 전':s<3600?Math.round(s/60)+'분 전':Math.round(s/3600)+'시간 전'};
 // agents
 $('#agents').innerHTML=D.agents.map(a=>{const t=a.touches.at(-1);
  return `<div class="ag"><div><span class="sw" style="background:${col[a.key]}"></span><b>${esc(a.type)}</b> <span class="badge ${a.status==='running'?'run':a.status==='done'?'done':''}">${ST[a.status]}</span> <span class="k">${esc(a.session)}${a.agent?' · '+esc(a.agent.slice(0,8)):''}</span></div>
  ${a.desc?`<div>${esc(a.desc)}</div>`:''}<div class="k">${a.calls}회 호출 · ${(a.tokens/1000).toFixed(0)}k 토큰 · 마지막 ${ago(a.last_ms)}${a.tool?' · '+esc(a.tool):''}</div>
  ${t?`<div class="path">${t.edit?'✎ ':'👁 '}${esc(t.path)}</div>${t.page?`<div class="k">페이지 ${esc(t.page)}</div>`:''}${t.features.length?`<div class="k">기능 ${t.features.map(i=>esc(D.features[i].title)).join(', ')}</div>`:''}`:'<div class="k">아직 파일을 다루지 않음</div>'}</div>`}).join('')||'<p class="empty">이 기간에 활동한 에이전트가 없습니다.</p>';
 // touched paths -> agents
 const hit={},now={},ed={};
 for(const a of D.agents){a.touches.forEach(t=>{(hit[t.path]??=new Set()).add(a.key);if(t.edit)ed[t.path]=1});
  if(a.status==='running'&&a.where)(now[a.where]??=new Set()).add(a.key)}
 const hot=new Set();for(const p in hit){const s=p.split('/');for(let i=1;i<=s.length;i++)hot.add(s.slice(0,i).join('/'))}
 // tree
 const kids={};for(const e of D.tree){const i=e.path.lastIndexOf('/'),par=i<0?'':e.path.slice(0,i);(kids[par]??=[]).push(e)}
 const dots=p=>{const ks=[...(hit[p]||[])];return ks.length?`<span class="dots">${ks.map(k=>`<i class="${now[p]?.has(k)?'now':''}" style="background:${col[k]}"></i>`).join('')}</span>`:''};
 const render=par=>`<ul class="tree${par?'':' root'}">`+(kids[par]||[]).map(e=>{const nm=esc(e.path.split('/').pop()),cls=(hot.has(e.path)?'hot ':'')+(ed[e.path]?'ed':'');
  if(e.more)return `<li class="k">… 외 ${e.more}개</li>`;
  if(!e.dir)return `<li class="f ${cls}">${nm}${dots(e.path)}</li>`;
  const o=hot.has(e.path)||open.has(e.path);return `<li><details data-p="${esc(e.path)}"${o?' open':''}><summary class="f ${cls}">${nm}/${dots(e.path)}</summary>${o?render(e.path):''}</details></li>`}).join('')+'</ul>';
 $('#tree').innerHTML=D.tree.length?render(''):'<p class="empty">프로젝트 폴더를 읽을 수 없습니다.</p>';
 $('#tree').querySelectorAll('details').forEach(d=>d.ontoggle=()=>{d.open?open.add(d.dataset.p):open.delete(d.dataset.p);if(d.open&&!d.querySelector('ul'))tick()});
 // pages and features
 const usedPg=new Set(),usedF={};D.agents.forEach(a=>a.touches.forEach(t=>{if(t.page)usedPg.add(t.page);t.features.forEach(i=>(usedF[i]??=new Set()).add(a.key))}));
 $('#pages').innerHTML=D.pages.length?D.pages.map(p=>`<div class="pg ${usedPg.has(p.route)?'on':''}"><span class="path">${esc(p.route)}</span> <span class="k">${esc(p.file)}</span></div>`).join(''):'<p class="empty">UI 페이지를 찾지 못했습니다(Next.js app/·pages/, *.html 기준).</p>';
 $('#feats').innerHTML=D.features.length?D.features.map((f,i)=>`<div class="ft ${usedF[i]?'on':''}"><div><b>${esc(f.title)}</b> ${[...(usedF[i]||[])].map(k=>`<span class="sw" style="background:${col[k]}"></span>`).join('')}</div>
  <div class="k">${esc(f.doc)}${f.total?` · 완료 ${f.done}/${f.total}`:''}${f.paths.length?` · 연결 ${f.paths.length}곳`:''}</div>${f.total?`<div class="bar"><i style="width:${f.done/f.total*100}%"></i></div>`:''}</div>`).join(''):'<p class="empty">계약 문서(docs/PLAN.md, PLAN.md, SPEC.md, docs/tasks/*.md)가 없거나, 경로·체크리스트가 적혀 있지 않습니다.</p>';
}
$('#proj').onchange=e=>{proj=e.target.value;open=new Set();tick()};$('#win').onchange=tick;
tick();setInterval(tick,3000);
</script></body></html>"""
