"""Local server for the live agent map (ADR-0004): 127.0.0.1 only, no external resources.

A background thread runs the incremental collector every few seconds; each page poll reads the state from SQLite.
"""
import hmac
import json
import secrets
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import accpage, accuracy, cloud
from .agentmap import build_state
from .collector import collect
from .calibrate import estimate
from .db import connect
from .forecast import backtest_cases, remaining_usage
from .i18n import normalize, resolve_lang, t
from .report import build_data, header, render
from .settings import load_cloud_params, load_map_params, update_cloud_params, validate_cloud_fields
from .ui import fill, head, shell_html

HOST = "127.0.0.1"
RECORD_S = 20
MAX_BODY = 4096
POST_PATHS = ("/api/cloud/check", "/api/cloud/settings")


def page(lang: str = "ko") -> str:
    return (fill(PAGE, lang).replace("/*HEAD*/", head(t("p.title.map", lang), MAP_CSS, lang))
            .replace("/*SHELL*/", shell_html("map", "", lang)))


_bt_cache: dict = {}


def _backtest(con) -> list[dict]:
    """The replay only changes when another subagent finishes: keep it until then."""
    key = con.execute("SELECT COUNT(*), MAX(mtime) FROM agent_meta WHERE finished = 1").fetchone()
    if _bt_cache.get("key") != key:
        _bt_cache.update(key=key, cases=backtest_cases(con))
    return _bt_cache["cases"]


def make_handler(db: Path, projects: Path, lang: str | None = None):
    """`lang` fixes the default language; None resolves it per request (config.toml, then the OS locale).
    `?lang=` on any request wins (issue #12).
    POST (issue #18, the AWS cost settings): same Host and Origin as this server, the per-process token embedded in
    the served usage page (`Handler.token`), JSON only, known fields only."""
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):   # keep the console quiet
            pass

        def _json(self, code: int, obj: dict) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _post_allowed(self) -> str | None:
            """None if the request may change settings, else the reason it may not."""
            port = self.server.server_address[1]
            hosts = (f"127.0.0.1:{port}", f"localhost:{port}")
            host = self.headers.get("Host") or ""
            if host not in hosts:
                return "host"
            if (self.headers.get("Origin") or "") not in tuple("http://" + h for h in hosts):
                return "origin"
            if (self.headers.get("Sec-Fetch-Site") or "same-origin") != "same-origin":
                return "origin"
            if not hmac.compare_digest((self.headers.get("X-Moni-Token") or "").encode(), token.encode()):
                return "token"
            if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
                return "content_type"
            return None

        def do_POST(self):
            u = urlparse(self.path)
            if u.path not in POST_PATHS:
                return self._send(404, b"not found", "text/plain")
            why = self._post_allowed()
            if why:
                return self._json(403, dict(error=why))
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0 or n > MAX_BODY:
                return self._json(413, dict(error="size"))
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._json(400, dict(error="json"))
            vals, bad = validate_cloud_fields(body)
            if u.path == "/api/cloud/check" and "enabled" in vals:
                bad.append("enabled")
            if bad:
                return self._json(400, dict(error="fields", fields=sorted(set(bad))))
            now = int(time.time() * 1000)
            cur = load_cloud_params()
            want = replace(cur, **vals)
            if u.path == "/api/cloud/check":
                res = cloud.check(want, now)
                return self._json(200, dict(check=res, view=cloud.view(cur, now, True, token)))
            if want.enabled and not cloud.check_passed(want, now):
                return self._json(409, dict(error="check_first", view=cloud.view(cur, now, True, token)))
            p = update_cloud_params(vals)
            return self._json(200, dict(view=cloud.view(p, now, True, token)))

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
            q = parse_qs(u.query)
            lg = normalize((q.get("lang") or [None])[0]) or lang or resolve_lang()
            if u.path == "/":
                return self._send(200, page(lg).encode("utf-8"), "text/html; charset=utf-8")
            if u.path == "/usage":   # the usage report, from live data (issue #4)
                con = connect(db)
                try:
                    now = int(time.time() * 1000)
                    data = build_data(con, now, lang=lg)
                    data["cloud"] = cloud.view(load_cloud_params(), now, served=True, token=token)   # issue #18
                    html = render(data, map_base="", lang=lg)
                finally:
                    con.close()
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            if u.path == "/accuracy":   # issue #9
                con = connect(db)
                try:
                    now = int(time.time() * 1000)
                    data = accuracy.build(con, now, _backtest(con))
                    data["header"] = header(con, now, lang=lg)
                finally:
                    con.close()
                return self._send(200, accpage.render(data, lg).encode("utf-8"), "text/html; charset=utf-8")
            if u.path == "/api/state":
                con = connect(db)
                try:
                    at = int((q.get("at") or [0])[0])   # ?at=<epoch ms>: look back
                    now = at or int(time.time() * 1000)
                    st = build_state(con, projects, now, (q.get("project") or [None])[0],
                                     int((q.get("window") or ["30"])[0]), live=not at,
                                     map_params=load_map_params())
                    st["header"] = header(con, now, live=not at, lang=lg)
                    st["usage_forecast"] = remaining_usage([a["forecast"] or {} for a in st["agents"]],
                                                           estimate(con, "five_hour")["usd_per_pct"],
                                                           st["header"]["five_hour"]["pct"])
                finally:
                    con.close()
                return self._send(200, json.dumps(st, ensure_ascii=False).encode("utf-8"),
                                  "application/json; charset=utf-8")
            return self._send(404, b"not found", "text/plain")
    Handler.token = token
    return Handler


def serve(db: Path, projects: Path, port: int = 4990, interval_s: float = 5.0, lang: str | None = None) -> None:
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
    # issue #18 (ADR-0008): AWS cost refresher; reads [cloud] each round and starts no process while it is off
    threading.Thread(target=cloud.run_forever, args=(stop,), daemon=True).start()
    srv = ThreadingHTTPServer((HOST, port), make_handler(db, projects, lang))
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
.bar2 button{font:inherit;padding:3px 10px;border-radius:6px;border:1px solid var(--border);background:var(--surface-1);
color:var(--text-primary);cursor:pointer}.bar2 button.on{border-color:var(--line);background:var(--surface-2)}
#steps button{padding:3px 8px;margin-right:2px}#atlink{color:var(--line)}
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
.tree .sr{text-decoration:underline dashed var(--text-secondary);text-underline-offset:3px}
.dots i.ho{background:transparent;border:1.5px dashed;box-sizing:border-box;cursor:help}
.tlg{margin:-2px 0 8px}.tlg .f{font-family:var(--mono)}.tlg b.ed{color:var(--critical)}
.tlg .dots i{background:var(--muted)}.tlg .dots i.ho{background:transparent;border-color:var(--muted)}
details>summary{cursor:pointer;list-style:none}details>summary::before{content:"▸ ";color:var(--muted)}
details[open]>summary::before{content:"▾ "}.scroll{max-height:64vh;overflow:auto}
.pg,.ft{padding:4px 6px;border-bottom:1px solid var(--grid)}.pg.on,.ft.on{background:var(--surface-2);border-radius:4px}
.pg.ui{cursor:pointer}.ued{color:var(--critical)}
.uh{display:flex;align-items:flex-start;gap:4px}.uh .dots{flex:none;margin:0;line-height:1;padding-top:.45em}.uh .dots i{vertical-align:top}.uh>span:last-child{min-width:0;overflow-wrap:anywhere}.un{font-size:12px;line-height:1.6;white-space:nowrap;color:var(--text-secondary)}
.un.on{color:var(--text-primary);font-weight:600}
.meter{height:4px;background:var(--grid);border-radius:2px;overflow:hidden;margin:3px 0}.meter i{display:block;height:100%;background:var(--line)}
.empty{color:var(--muted)}.cost{margin-left:8px;color:var(--text-secondary);font-size:11px;font-family:var(--mono)}
#lanes svg{display:block;width:100%}#lanes text{fill:var(--text-secondary);font-size:11px}
#lanes .lbl{fill:var(--text-primary);font-size:12px}#lanes rect.seg{cursor:pointer}
.lg{display:flex;gap:12px;flex-wrap:wrap;color:var(--text-secondary);font-size:12px}.lg i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:-1px}
"""

PAGE = r"""/*HEAD*/<body>/*SHELL*/
<div class="bar2"><select id="proj" aria-label="[[p.m.proj]]"></select>
<select id="win" aria-label="[[p.m.period]]"><option value="15">[[p.m.w15]]</option><option value="30" selected>[[p.m.w30]]</option><option value="120">[[p.m.w120]]</option></select>
<button type="button" id="pause" title="[[p.m.pause_tip]]">[[p.m.pause]]</button>
<span id="steps" hidden><button type="button" id="back" title="[[p.m.back]]">◀</button><button type="button" id="fwd" title="[[p.m.fwd]]">▶</button></span>
<a id="atlink" class="k" hidden>[[p.m.atlink]]</a>
<input id="q" type="search" placeholder="[[p.m.search]]" aria-label="[[p.m.search_aria]]"><span class="k" id="when"></span></div>
<div class="wrap">
<section class="card"><h2>[[p.m.h_lanes]] <span class="k">[[p.m.h_lanes_k]]</span></h2>
<div class="lg" id="lg"></div><div id="lanes"></div></section>
<div class="cols">
<section class="card"><h2>[[p.m.h_agents]]</h2><div class="k" id="ufc"></div><div id="agents" class="scroll"></div></section>
<section class="card"><h2>[[p.m.h_tree]] <span class="k">[[p.m.h_tree_k]]</span></h2>
<div class="lg tlg" id="tlg"><span class="f"><b>[[p.m.tl_read]]</b></span><span class="f"><b class="ed">[[p.m.tl_edit]]</b></span><span class="f sr">[[p.m.tl_search]]<span class="dots"><i class="ho"></i></span></span><span><span class="dots"><i class="now"></i></span>[[p.m.tl_now]]</span></div><div id="tree" class="scroll"></div></section>
<section class="card"><h2>[[p.m.h_pages]] <span class="k">[[p.m.h_pages_k]]</span></h2><div id="pages" class="scroll" style="max-height:40vh"></div><h2 style="margin-top:14px">[[p.m.h_feats]] <span class="k">[[p.m.h_feats_k]]</span></h2><div id="feats" class="scroll"></div></section>
</div></div>
<script>
const $=s=>document.querySelector(s),esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const PAL=['var(--s1)','var(--s2)','var(--s3)','var(--s4)','var(--s5)','var(--s6)','var(--s7)'];
const KIND={read:[T('p.m.k_read'),'var(--s1)'],search:[T('p.m.k_search'),'var(--s2)'],edit:[T('p.m.k_edit'),'var(--critical)'],run:[T('p.m.k_run'),'var(--s4)'],web:[T('p.m.k_web'),'var(--s7)'],agent:[T('p.m.k_agent'),'var(--s3)'],mcp:['MCP','var(--s5)'],other:[T('p.m.k_other'),'var(--muted)']};
const ST={running:T('p.m.st_running'),idle:T('p.m.st_idle'),done:T('p.m.st_done')},tk=n=>n>=1e6?(n/1e6).toFixed(1)+'M':n>=1e3?Math.round(n/1e3)+'k':String(n);let open=new Set(),focusPath=null,D=null;
const U=new URLSearchParams(location.search),AT=U.get('at');let proj=U.get('project');
if(U.get('window')){const w=U.get('window'),s=$('#win');if(![...s.options].some(o=>o.value===w))s.add(new Option(T('p.m.wn',{n:w}),w));s.value=w}
// issue #15: a search scope (Grep/Glob) is a hollow mark, a read or edit a filled one
const segFill=(k,c)=>k==='search'?`fill="none" stroke="${c}" stroke-width="1.5"`:`fill="${c}"`;
$('#lg').innerHTML=Object.entries(KIND).map(([k,[n,c]])=>`<span><i style="${k==='search'?`border:1.5px solid ${c};box-sizing:border-box`:`background:${c}`}"></i>${n}</span>`).join('');
// issue #11: `frozen` is the moment shown while paused (epoch ms); null = live. A page opened with ?at= starts frozen.
let frozen=AT?+AT:null,seq=0;
async function tick(){
 const at=frozen,n=++seq,q=new URLSearchParams({window:$('#win').value,lang:LANG});if(proj)q.set('project',proj);if(at!=null)q.set('at',at);
 let d;try{d=await (await fetch('/api/state?'+q)).json()}catch(e){$('#meta').textContent=T('p.m.offline');return}
 if(n!==seq||at!==frozen)return;   // a newer request, or paused/resumed while this one was in flight
 D=d;draw()}
function pauseUi(){const p=frozen!=null;
 $('#pause').textContent=p?T('p.m.live'):T('p.m.pause');$('#pause').classList.toggle('on',p);$('#steps').hidden=!p;$('#atlink').hidden=!p;
 if(p){const q=new URLSearchParams({at:frozen,window:$('#win').value});if(proj)q.set('project',proj);q.set('lang',LANG);$('#atlink').href='?'+q}}
function pause(){if(frozen!=null||!D)return;frozen=D.now;seq++;draw()}   // keep the data already shown; no refetch
function resume(){
 if(AT){const q=new URLSearchParams(location.search);q.delete('at');q.delete('paused');q.set('window',$('#win').value);if(proj)q.set('project',proj);
  location.href=location.pathname+'?'+q;return}
 frozen=null;pauseUi();tick()}
function toggle(){frozen!=null?resume():pause()}
function step(d){if(frozen==null)return;frozen=Math.min(frozen+d,Date.now());pauseUi();tick()}
function draw(){if(!D)return;
 const ago=ms=>{const s=Math.round((D.now-ms)/1000);return s<60?T('p.m.ago_s',{n:s}):s<3600?T('p.m.ago_m',{n:Math.round(s/60)}):T('p.m.ago_h',{n:Math.round(s/3600)})};
 const wsh=w=>w&&w.share?T('p.m.win_share',{p:Math.round(w.share*100)})+(w.pp!=null?T('p.m.pp_est',{pp:w.pp}):''):'';
 $('#lim').innerHTML=shellLimits(D.header);
 const hms=fmtHMS(D.now);
 $('#meta').textContent=(frozen==null?T('p.m.updated',{t:hms}):AT&&+AT===D.now?T('p.m.past_at',{d:fmtDate(D.now),t:hms}):
  T('p.m.paused_at',{t:hms}))+' · '+T('p.meta');
 const sel=$('#proj');sel.innerHTML=D.projects.map(p=>`<option value="${esc(p.project_dir)}">${esc(p.name)}${p.win&&p.win.share?` · ${Math.round(p.win.share*100)}%`:''}</option>`).join('')||`<option>${T('p.m.no_recent')}</option>`;
 if(D.project){proj=D.project.project_dir;sel.value=proj}
 pauseUi();
 const col={};D.agents.forEach((a,i)=>col[a.key]=i<PAL.length?PAL[i]:'var(--muted)');
 lanes(col);
 // issue #7: forecast from past runs of the same agentType (estimates, with sample size)
 const mins=ms=>ms<60000?T('p.m.secs',{n:Math.round(ms/1000)}):T('p.m.mins',{n:Math.round(ms/60000)});
 const fc=f=>{if(!f)return '';if(!f.ok)return `<div class="k">${T('p.m.fc_few',{n:f.n})}</div>`;
  return `<div class="k">${T('p.m.fc')} <span class="badge">${T('p.est')}</span> ${T('p.m.fc_body',{calls:f.calls,p50:f.calls_p50,p85:f.calls_p85,r50:mins(f.remain_ms_p50),r85:mins(f.remain_ms_p85),n:f.n})}${f.scope==='all'?T('p.m.fc_all'):''}</div>
  <div class="meter" title="${T('p.m.fc_meter')}"><i style="width:${Math.round((f.progress||0)*100)}%"></i></div>`};
 const UF=D.usage_forecast;$('#ufc').innerHTML=UF?T('p.m.uf',{n:UF.agents,amount:`<b>+${UF.pp_p50}~${UF.pp_p85}%p</b>`})+(UF.over_limit_p85?` <span class="warn">${T('p.m.uf_over')}</span>`:''):'';
 $('#agents').innerHTML=D.agents.map(a=>{const t=a.touches.findLast(x=>x.kind!=='search')||a.touches.at(-1);
  return `<div class="ag"><div><span class="sw" style="background:${col[a.key]}"></span><b>${esc(a.type)}</b> <span class="badge ${a.status==='running'?'run':a.status==='done'?'done':''}">${ST[a.status]}</span> <span class="k mono">${esc(a.session)}${a.agent?' · '+esc(a.agent.slice(0,8)):''}</span></div>
  ${a.desc?`<div>${esc(a.desc)}</div>`:''}<div class="k">${T('p.m.ag_stats',{calls:a.calls,tok:(a.tokens/1000).toFixed(0),ago:ago(a.last_ms)})}${a.tool?' · '+esc(a.tool):''}${wsh(a.win)}</div>
  ${t?`<div class="path mono">${t.edit?'✎ ':t.kind==='search'?'⌕ ':'👁 '}${esc(t.path)}</div>${t.page?`<div class="k">${T('p.m.page',{p:esc(t.page)})}</div>`:''}${t.features.length?`<div class="k">${T('p.m.feats',{f:t.features.map(i=>esc(D.features[i].title)).join(', ')})}</div>`:''}`:`<div class="k">${T('p.m.no_files')}</div>`}${a.agent?fc(a.forecast):`<div class="k" title="${T('p.m.main_tip')}" style="cursor:help">${T('p.m.main_na')}</div>`}${a.top_file&&a.top_file.carried>=1000?`<div class="k">${T('p.m.top_file',{path:`<span class="mono">${esc(a.top_file.path)}</span>`,tok:tk(a.top_file.carried)})}</div>`:''}</div>`}).join('')||`<p class="empty">${T('p.m.no_agents')}</p>`;
 tree(col);pagesFeats(col)}
function lanes(col){
 const box=$('#lanes'),W=box.clientWidth||900,R=10,rh=22,ags=D.agents.filter(a=>a.lanes.length);
 if(!ags.length){box.innerHTML=`<p class="empty">${T('p.m.no_calls')}</p>`;return}
 // issue #13: measure labels in the real font (a hidden <text class="lbl">), size the label column L to the widest
 // label clamped to [160px, 30% of the width], and cut each label to fit with an ellipsis - by width, not by characters
 box.innerHTML='<svg height="0" style="position:absolute;visibility:hidden" aria-hidden="true"><text class="lbl"></text></svg>';
 const mt=box.querySelector('text'),tw=s=>{mt.textContent=s;return mt.getComputedTextLength()},LX=18,PAD=8;
 const full=ags.map(a=>a.type+(a.desc?' · '+a.desc:''));
 const L=Math.round(Math.max(160,Math.min(W*0.3,LX+PAD+Math.max(...full.map(tw))))),room=L-LX-PAD;
 const fitLabel=s=>{if(tw(s)<=room)return s;const c=[...s];let lo=0,hi=c.length;
  while(lo<hi){const k=(lo+hi+1)>>1;tw(c.slice(0,k).join('')+'…')<=room?lo=k:hi=k-1}return c.slice(0,lo).join('').trimEnd()+'…'};
 const lbls=full.map(fitLabel);
 const t1=D.now,t0=t1-D.window_min*60000,x=t=>L+(t-t0)/(t1-t0)*(W-L-R),H=ags.length*rh+22;
 let g='';const hm=fmtHM,hms=fmtHMS;
 // issue #10: grid lines on round clock times from the server, so they slide with the marks and labels are exact
 for(const t of D.ticks||[]){const xx=x(t),a=xx>W-R-20?'end':xx<L+20?'start':'middle';
  g+=`<line x1="${xx}" x2="${xx}" y1="0" y2="${H-18}" stroke="var(--grid)"/><text x="${xx}" y="${H-4}" text-anchor="${a}">${hm(t)}</text>`}
 ags.forEach((a,i)=>{const y=i*rh;g+=`<circle cx="8" cy="${y+11}" r="4" fill="${col[a.key]}"/><text class="lbl" x="${LX}" y="${y+15}" clip-path="url(#lblclip)"><title>${esc(full[i])}</title>${esc(lbls[i])}</text>`;
  a.lanes.forEach(([t,k,p])=>{if(t<t0)return;g+=`<rect class="seg" x="${x(t)-1.5}" y="${y+4}" width="4" height="14" rx="1" ${segFill(k,KIND[k]?.[1]||KIND.other[1])} data-p="${esc(p||'')}"><title>${hms(t)} ${esc(KIND[k]?.[0]||k)}${p?' · '+esc(p):''}</title></rect>`})});
 // the clipPath is a backstop: even if measuring fails (e.g. a hidden box), no label can reach the plot area
 box.innerHTML=`<svg height="${H}" role="img" aria-label="${T('p.m.lanes_aria')}"><defs><clipPath id="lblclip"><rect x="0" y="0" width="${L-PAD/2}" height="${H}"/></clipPath></defs>${g}</svg>`;
 box.querySelectorAll('rect.seg').forEach(r=>r.onclick=()=>goTree(r.dataset.p,col))}
function tree(col){
 const hit={},now={},ed={},cnt={},srch={},sopen=new Set();
 // issue #15: a search scope (Grep/Glob path) gets its own hollow mark only: no bold, no bar, no count on folders,
 // never styles the files under it; its parent folders open so the mark can be seen
 for(const a of D.agents){a.touches.forEach(t=>{if(t.kind==='search'){(srch[t.path]??=[]).push([a,t]);
   const s=t.path.split('/');for(let i=1;i<s.length;i++)sopen.add(s.slice(0,i).join('/'));return}
  (hit[t.path]??=new Set()).add(a.key);if(t.edit)ed[t.path]=1;
  const s=t.path.split('/');for(let i=1;i<=s.length;i++){const k=s.slice(0,i).join('/');cnt[k]=(cnt[k]||0)+1}});
  if(a.status==='running'&&a.where)(now[a.where]??=new Set()).add(a.key)}
 const scope=p=>{const L=srch[p];if(!L)return '';const by=new Map();for(const [a,t] of L)(by.get(a.key)??by.set(a.key,[]).get(a.key)).push(T('p.m.scope',{tool:t.tool,agent:a.type,t:fmtHMS(t.t)}));
  return `<span class="dots">${[...by].map(([k,ls])=>`<i class="ho" style="border-color:${col[k]}" title="${esc(ls.slice(-5).join('\n'))}"></i>`).join('')}</span>`};
 const mx=Math.max(1,...Object.values(cnt)),q=$('#q').value.trim().toLowerCase();
 const match=new Set();if(q)for(const e of D.tree)if(!e.more&&e.path.toLowerCase().includes(q)){const s=e.path.split('/');for(let i=1;i<=s.length;i++)match.add(s.slice(0,i).join('/'))}
 const kids={};for(const e of D.tree){const i=e.path.lastIndexOf('/'),par=i<0?'':e.path.slice(0,i);(kids[par]??=[]).push(e)}
 const dots=p=>{const ks=[...(hit[p]||[])];return ks.length?`<span class="dots">${ks.map(k=>`<i class="${now[p]?.has(k)?'now':''}" style="background:${col[k]}"></i>`).join('')}</span>`:''};
 const heat=p=>cnt[p]?`<span class="heat" style="width:${Math.max(4,Math.round(cnt[p]/mx*60))}px" title="${T('p.m.times',{n:cnt[p]})}"></span>`:'';
 // issue #6: tokens each file added to the context, and how much re-reading it carried
 const C=D.costs||{},fold={};for(const p in C){const s=p.split('/');for(let i=1;i<s.length;i++){const k=s.slice(0,i).join('/');fold[k]=(fold[k]||0)+C[p].carried}}
 const cost=(p,dir)=>{if(dir)return fold[p]?`<span class="cost" title="${T('p.m.dir_cost')}">${tk(fold[p])}</span>`:'';
  const c=C[p];if(!c)return '';const t=T('p.m.file_cost',{tok:fmtN(c.tokens),later:c.later,carried:fmtN(c.carried)})+(c.pp!=null?T('p.m.pp_est',{pp:c.pp}):'');
  return `<span class="cost" title="${t}">${tk(c.tokens)}${c.later?` ×${c.later} → ${tk(c.carried)}`:''}${c.pp!=null&&c.pp>=0.05?` ≈${c.pp}%p`:''}</span>`};
 const render=par=>`<ul class="tree${par?'':' root'}">`+(kids[par]||[]).filter(e=>!q||e.more?!q:match.has(e.path)).map(e=>{
  if(e.more)return `<li class="k">${T('p.m.more',{n:e.more})}</li>`;
  const nm=esc(e.path.split('/').pop()),cls=(cnt[e.path]?'hot ':'')+(ed[e.path]?'ed ':'')+(srch[e.path]&&!cnt[e.path]?'sr ':'')+(focusPath===e.path?'focus':'');
  if(!e.dir)return `<li class="f ${cls}" data-path="${esc(e.path)}">${nm}${heat(e.path)}${dots(e.path)}${scope(e.path)}${cost(e.path,false)}</li>`;
  const o=(q&&match.has(e.path))||cnt[e.path]||sopen.has(e.path)||open.has(e.path);
  return `<li><details data-p="${esc(e.path)}"${o?' open':''}><summary class="f ${cls}" data-path="${esc(e.path)}">${nm}/${heat(e.path)}${dots(e.path)}${scope(e.path)}${cost(e.path,true)}</summary>${o?render(e.path):''}</details></li>`}).join('')+'</ul>';
 $('#tree').innerHTML=D.tree.length?render(''):`<p class="empty">${T('p.m.no_tree')}</p>`;
 $('#tree').querySelectorAll('details').forEach(d=>d.ontoggle=()=>{const was=open.has(d.dataset.p);d.open?open.add(d.dataset.p):open.delete(d.dataset.p);if(d.open&&!was&&!d.querySelector('ul'))tree(col)})}
function goTree(p,col){if(!p)return;focusPath=p;
 const s=p.split('/');for(let i=1;i<s.length;i++)open.add(s.slice(0,i).join('/'));tree(col);
 const el=[...$('#tree').querySelectorAll('[data-path]')].find(e=>e.dataset.path===p);if(el)el.scrollIntoView({block:'center'})}
// issue #16 (ADR-0007): only UI read or edited in the window - an HTML file, a route URL, desktop elements
function uiPanel(col){
 const U=D.ui||{},ag={};D.agents.forEach(a=>ag[a.key]=a);
 const who=m=>T('p.m.ui_mark',{tool:m.tool,agent:ag[m.a]?ag[m.a].type:'',t:fmtHMS(m.t)});
 const mk=ms=>{const by=new Map();for(const m of ms)(by.get(m.a)??by.set(m.a,[]).get(m.a)).push(who(m));
  return `<span class="dots">${[...by].map(([k,ls])=>`<i style="background:${col[k]||'var(--muted)'}" title="${esc(ls.slice(-5).join('\n'))}"></i>`).join('')}</span>`};
 const nodes=ns=>ns.map(n=>`<div class="un${n.on?' on':''}" style="padding-left:${n.d*12}px">${n.on?'☑':'☐'} <span class="k">${esc(n.type)}</span> <span class="mono">${esc(n.name)}</span>${n.aid&&n.aid!==n.name?` <span class="k mono">#${esc(n.aid)}</span>`:''}</div>`).join('');
 let h='';
 for(const w of U.web||[]){const m=w.marks.at(-1);
  h+=`<div class="pg on ui" data-go="${esc(m.path)}"><div class="uh">${mk(w.marks)}<span><span class="mono${w.edit?' ued':''}">${w.edit?'✎ ':'👁 '}${esc(w.route)}</span> <span class="k">${T('p.m.ui_k_'+w.kind)}</span></span></div>
  <div class="k">${w.kind==='html'?'':w.files.map(f=>`<span class="mono">${esc(f)}</span>`).join(', ')+' · '}${esc(who(m))}</div></div>`}
 for(const d of U.desktop||[]){const m=d.marks.at(-1);
  h+=`<div class="pg on ui" data-go="${esc(m.path)}"><div class="uh">${mk(d.marks)}<span><span class="mono${d.edit?' ued':''}">${d.edit?'✎ ':'👁 '}${esc(d.file)}</span> <span class="k">${T('p.m.ui_k_'+d.kind)}</span></span></div>
  <div class="k">${esc(who(m))}</div>${nodes(d.nodes)||`<div class="k">${T('p.m.ui_no_nodes')}</div>`}</div>`}
 if(!h)h=`<p class="empty">${T(U.has_ui?'p.m.ui_none_touched':'p.m.ui_none_found')}</p>`;
 const X=U.uia;
 if(X)h+=`<div class="pg"><b>${T('p.m.uia_h')}</b> `+(X.past?`<span class="k">${T('p.m.uia_past')}</span>`:!X.window?`<span class="k">${T(X.pending?'p.m.uia_pending':X.error?'p.m.uia_err':'p.m.uia_none')}</span>`:
  `<span class="mono">${esc(X.window)}</span> <span class="k">${T('p.m.uia_meta',{pid:X.pid,by:X.by==='path'?T('p.m.uia_by_path'):T('p.m.uia_by_title'),ms:X.ms})}</span>${nodes(X.nodes)}`)+'</div>';
 else if(U.has_desktop)h+=`<p class="k">${T('p.m.uia_off')}</p>`;
 $('#pages').innerHTML=h;
 $('#pages').querySelectorAll('[data-go]').forEach(e=>e.onclick=()=>goTree(e.dataset.go,col))}
function pagesFeats(col){
 const usedF={};D.agents.forEach(a=>a.touches.forEach(t=>{t.features.forEach(i=>(usedF[i]??=new Set()).add(a.key))}));
 uiPanel(col);
 const day=ms=>fmtMD(ms)+' '+fmtHM(ms);
 const docf=(D.docs||[]).filter(d=>d.total).map(d=>`<div class="k"><span class="mono">${esc(d.doc)}</span> ${T('p.m.done_n',{done:d.done,total:d.total})}${d.left===0?T('p.m.doc_done'):d.ok?T('p.m.doc_eta',{left:d.left,p50:day(d.eta_p50),p85:day(d.eta_p85)})+` <span class="badge">${T('p.est')}</span>`:T('p.m.doc_few',{n:d.steps})}</div>`).join('');
 $('#feats').innerHTML=docf+(D.features.length?D.features.map((f,i)=>`<div class="ft ${usedF[i]?'on':''}"><div><b>${esc(f.title)}</b> ${[...(usedF[i]||[])].map(k=>`<span class="sw" style="background:${col[k]}"></span>`).join('')}</div>
  <div class="k">${esc(f.doc)}${f.total?' · '+T('p.m.done_n',{done:f.done,total:f.total}):''}${f.paths.length?T('p.m.linked',{n:f.paths.length}):''}</div>${f.total?`<div class="meter"><i style="width:${f.done/f.total*100}%"></i></div>`:''}</div>`).join(''):`<p class="empty">${T('p.m.no_feats')}</p>`)}
$('#proj').onchange=e=>{proj=e.target.value;open=new Set();focusPath=null;tick()};$('#win').onchange=tick;
$('#q').oninput=()=>{if(D)tree(Object.fromEntries(D.agents.map((a,i)=>[a.key,i<PAL.length?PAL[i]:'var(--muted)'])))};
addEventListener('resize',()=>{clearTimeout(window._r);window._r=setTimeout(draw,150)});addEventListener('themechange',draw);
$('#pause').onclick=toggle;$('#back').onclick=()=>step(-60000);$('#fwd').onclick=()=>step(60000);
// space toggles pause, except while typing or choosing (input/select) and on a focused button (it clicks itself)
addEventListener('keydown',e=>{if(e.code!=='Space'||e.ctrlKey||e.altKey||e.metaKey)return;
 if(e.target.closest&&e.target.closest('input,select,textarea,button,a,[contenteditable]'))return;e.preventDefault();toggle()});
pauseUi();tick().then(()=>{if(U.has('paused')&&!AT)pause()});   // ?paused=1: open frozen at the first state
setInterval(()=>{if(frozen==null)tick()},3000);
</script></body></html>"""
