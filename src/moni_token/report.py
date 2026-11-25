"""Static, offline HTML report (no server, no CDN). Data is embedded as JSON; drawing is inline SVG + vanilla JS.

Y axis = limit usage in % (what `/usage` shows): "current session" (5-hour) and "current week" (7-day).
Measured values come from the status line; earlier periods are estimated from local logs via calibration
and drawn dashed. Only numbers, project folder names, session id prefixes, tool names and template sentences
are embedded.
"""
import json
import sqlite3
import time
from pathlib import Path

from . import config
from .analysis import localize_cause, localize_headline
from .baseline import baseline
from .calibrate import estimate
from .chunks import analyze_chunks
from .events import list_events
from .i18n import t
from .summary import _pp, best_alternative, digest, event_summary
from .pctseries import pct_at, series
from .ui import fill, head, shell_html, uplot_js

DAY = 86_400_000
MAX_TIMELINE_CALLS = 400


def _names(con) -> dict[str, str]:
    return {d: n for d, n in con.execute(
        "SELECT project_dir, project FROM calls WHERE project IS NOT NULL GROUP BY project_dir")}


def _pack(s: dict, now_ms: int) -> dict:
    pts = s["points"]
    cur, basis = pct_at(s, now_ms)
    win = next((w for w in s["windows"] if w[0] <= now_ms < w[1]), None)
    return dict(t0=pts[0].t if pts else now_ms, step=(pts[1].t - pts[0].t) if len(pts) > 1 else 300_000,
                m=[p.measured for p in pts], e=[p.estimated for p in pts],
                windows=[[a, b] for a, b, *_ in s["windows"]], current=cur, basis=basis,
                reset=win[1] if win else None, calib_n=s["n_samples"], reliable=s["reliable"])


def _timeline(con, names, start_ms, end_ms) -> list[dict]:
    calls = []
    for (ts, proj, sid, side, ep, inp, out, cr, c5, c1, trig, prb, img, mid) in con.execute(
            "SELECT ts_ms, project_dir, session_id, is_sidechain, entrypoint, input, output, cache_read, cache_5m, "
            "cache_1h, trigger, prev_result_bytes, prev_result_image, msg_id FROM calls "
            "WHERE ts_ms >= ? AND ts_ms < ? ORDER BY ts_ms LIMIT ?", (start_ms, end_ms, MAX_TIMELINE_CALLS)):
        tools = [n for (n,) in con.execute("SELECT name FROM call_tools WHERE msg_id = ?", (mid,))]
        calls.append(dict(t=ts, p=names.get(proj, proj), s=(sid or "")[:8], sc=side, hl=int(ep == "sdk-cli"),
                          ctx=inp + cr + c5 + c1, w=c5 + c1, o=out, tr=trig, rb=prb, img=img, tools=tools))
    return calls


def report_events(con: sqlite3.Connection, since_ms: int, with_calls: bool = False, lang: str = "ko") -> list[dict]:
    """Recorded events, flattened for display in `lang`, each with its savings summary sentence.
    Sentences are re-rendered from the stored template keys and numbers (issue #12)."""
    names = _names(con) if with_calls else {}
    ratio = estimate(con, "five_hour")["usd_per_pct"]
    events = []
    for e in list_events(con, since_ms):
        ev = (e["spike"] or {}).get("evidence", {})
        causes = [localize_cause(c, lang) for c in e["causes"]]
        d = dict(id=e["id"], kind=e["kind"], start=e["start_ms"], end=e["end_ms"],
                 rise=ev.get("rise_pp"), frm=ev.get("from_pct"), to=ev.get("to_pct"), basis=ev.get("basis"),
                 headline=localize_headline(e["usd"], causes, lang),
                 note=t("analysis.note", lang) if e["note"] else e["note"], agents=e["agents"],
                 causes=[{k: c.get(k) for k in ("label", "fact_text", "interpretation", "advice", "share",
                                                "saving_usd", "saving_share", "alternative")} for c in causes],
                 calls=_timeline(con, names, e["start_ms"], e["end_ms"]) if with_calls else [])
        d["summary"] = event_summary(d, ratio, lang)
        b = best_alternative(d)
        pp = _pp(d, b, ratio) if b else None
        d["save_pp"] = round(pp, 2) if pp is not None else None
        events.append(d)
    return events


MAP_BASE = "http://127.0.0.1:4990"   # where spike rows link to; "" when the page is served by the map server


def header(con: sqlite3.Connection, now_ms: int, live: bool = True, lang: str = "ko") -> dict:
    """Shared top line of the usage page and the agent map (issue #4): limit % now and the live spike warning."""
    from .statusline import warning
    out = {}
    for kind in ("five_hour", "seven_day"):
        s = series(con, kind, now_ms - 600_000, now_ms)
        cur, basis = pct_at(s, now_ms)
        win = next((w for w in s["windows"] if w[0] <= now_ms < w[1]), None)
        out[kind] = dict(pct=cur, basis=basis, reset=win[1] if win else None)
    out["warning"] = warning(now_ms, lang) if live else ""
    return out


def build_data(con: sqlite3.Connection, now_ms: int, days: int = 7, lang: str = "ko") -> dict:
    start = now_ms - days * DAY
    packs = {}
    for kind in ("five_hour", "seven_day"):
        s = series(con, kind, start, now_ms)
        packs[kind] = dict(_pack(s, now_ms), base=baseline(con, kind, s, now_ms))   # issue #8
    fh, sd = packs["five_hour"], packs["seven_day"]
    events = report_events(con, start, with_calls=True, lang=lang)
    main = [e for e in events if e["kind"] == "pct"] or [e for e in events if e["kind"] == "rate"]
    dg = digest(main, estimate(con, "five_hour")["usd_per_pct"], estimate(con, "seven_day")["usd_per_pct"], lang)
    ch = analyze_chunks(con, start, now_ms, lang=lang)
    try:
        seen = json.loads((config.data_dir() / "statusline.seen").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        seen = None
    from . import cloud   # issue #18: the static file shows values only; the server adds the settings form
    from .settings import load_cloud_params
    return dict(generated=now_ms, start=start, end=now_ms, five_hour=fh, seven_day=sd, events=events, statusline=seen,
                digest=dg, chunks=ch, header=header(con, now_ms, lang=lang),
                cloud=cloud.view(load_cloud_params(), now_ms, served=False))


def render(data: dict, map_base: str = MAP_BASE, lang: str = "ko") -> str:
    payload = json.dumps(dict(data, map_base=map_base), ensure_ascii=False).replace("</", "<\\/")
    return (fill(TEMPLATE, lang).replace("/*HEAD*/", head(t("p.title.usage", lang), USAGE_CSS, lang))
            .replace("/*SHELL*/", shell_html("usage", map_base, lang)).replace("/*UPLOT*/", uplot_js())
            .replace("/*CLOUDFORM*/", CLOUD_FORM_JS if (data.get("cloud") or {}).get("served") else "")
            .replace("/*DATA*/null", payload))


def write_report(con: sqlite3.Connection, out: Path, now_ms: int | None = None, days: int = 7,
                 lang: str = "ko") -> Path:
    html = render(build_data(con, now_ms or int(time.time() * 1000), days, lang), lang=lang)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(html, encoding="utf-8")
    tmp.replace(out)   # atomic: a browser refresh never sees half a file
    return out


USAGE_CSS = r"""
main{max-width:1280px;margin:0 auto;padding:18px 16px 64px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 10px;display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}
.sub{color:var(--text-secondary);margin:0 0 20px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
.tile{background:var(--surface-1);border:1px solid var(--border);border-radius:10px;padding:14px 16px}
.tile .k{color:var(--text-secondary);font-size:12px}.tile .v{font-size:28px;font-weight:600;font-variant-numeric:tabular-nums}
.tile .d{color:var(--muted);font-size:12px}.meter{height:6px;border-radius:3px;background:var(--grid);margin:6px 0 4px;overflow:hidden}
.meter i{display:block;height:100%;background:var(--line);border-radius:3px}
.card{background:var(--surface-1);border:1px solid var(--border);border-radius:10px;padding:16px;margin-top:8px}
.legend{display:flex;flex-wrap:wrap;gap:14px;margin-bottom:6px;color:var(--text-secondary);font-size:12px;align-items:center}
.ln{display:inline-block;width:18px;border-top:2px solid var(--line)}.ln.d{border-top-style:dashed}
.bandk{display:inline-block;width:12px;height:10px;background:var(--band);border:1px solid var(--critical)}
.range{margin-left:auto;display:flex;gap:4px}.range button{font:inherit;font-size:12px;padding:2px 10px;border-radius:6px;
border:1px solid var(--border);background:transparent;color:var(--text-secondary);cursor:pointer}
.range button[aria-pressed=true]{background:var(--text-primary);color:var(--surface-1)}
.chart{width:100%;position:relative;min-height:240px}.chart .empty{color:var(--muted);padding:80px 0;text-align:center}
.tip{position:absolute;pointer-events:none;background:var(--surface-1);border:1px solid var(--border);border-radius:8px;
padding:8px 10px;font-size:12px;box-shadow:0 4px 16px rgba(0,0,0,.12);display:none;z-index:2;white-space:nowrap}
.tip b{font-size:16px}
tr.ev{cursor:pointer}tr.ev:hover{background:var(--grid)}tr.ev:focus{outline:2px solid var(--line)}
.detail td{background:var(--bg)}.cause{margin:6px 0 10px}.cause b{display:block}
.interp{color:var(--text-secondary)}.adv{color:var(--line)}.note{color:var(--muted);font-size:12px}.scroll{overflow-x:auto}
details summary{cursor:pointer;color:var(--text-secondary)}.tl{font-size:12px}.tl td{padding:3px 6px}
.share{display:flex;height:14px;border-radius:4px;overflow:hidden;margin:6px 0 8px;gap:2px}.share i{display:block;height:100%}
.sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}
.ln.u{border-top-color:var(--muted)}.ln.lw{border-top:1.5px solid var(--s2)}
.bandu{display:inline-block;width:12px;height:10px;background:color-mix(in srgb,var(--muted) 22%,transparent)}
.vd{font-size:12px;font-weight:500;color:var(--text-secondary)}.vd b{color:var(--text-primary)}
.vd .st{padding:1px 7px;border-radius:4px;border:1px solid var(--border);margin-right:6px;font-weight:600;color:var(--text-primary)}
.vd .st.high{border-color:var(--critical)}.vd .st.low{border-color:var(--good)}
.pace{color:var(--text-secondary);font-size:12px;margin:0 0 4px}.pace .warn{color:var(--critical);font-weight:600}
.cl-top{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;margin-bottom:10px}
.cl-st{padding:1px 8px;border-radius:4px;border:1px solid var(--border);font-weight:700;color:var(--text-secondary)}
.cl-st.on{border-color:var(--good);color:var(--good)}
.cl-vals{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:8px 16px;margin:0 0 12px}
.cl-vals .k{color:var(--text-secondary);font-size:12px}.cl-vals .v{font-size:18px;font-weight:600}
.cl-form{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end;margin:6px 0}
.cl-form label{display:flex;flex-direction:column;gap:2px;font-size:12px;color:var(--text-secondary)}
.cl-form input{font:inherit;padding:4px 8px;border-radius:6px;border:1px solid var(--border);background:var(--surface-1);
color:var(--text-primary);width:220px}.cl-form input.wide{width:320px}
.cl-form button{font:inherit;padding:4px 12px;border-radius:6px;border:1px solid var(--border);background:var(--surface-2);
color:var(--text-primary);cursor:pointer}.cl-form button.pri{border-color:var(--line)}.cl-form button:disabled{opacity:.5;cursor:default}
ul.cl-steps{list-style:none;padding:0;margin:8px 0}.cl-steps li{padding:3px 0}.cl-steps .m{display:inline-block;width:44px;font-weight:700}
.cl-steps .ok{color:var(--good)}.cl-steps .no{color:var(--critical)}.cl-steps .na{color:var(--muted)}.cl-steps .h{color:var(--text-secondary);margin-left:44px}
.cl-msg{min-height:1.5em;color:var(--text-secondary)}.cl-msg.bad{color:var(--critical)}
.cl-guide ol{margin:6px 0;padding-left:22px}.cl-guide li{margin:4px 0}.cl-guide pre{font-family:var(--mono);font-size:12px;
background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:8px;overflow-x:auto;margin:6px 0}
.cl-why{color:var(--text-secondary);font-size:12px}.cl-why p{margin:4px 0}code{background:var(--surface-2);padding:0 4px;border-radius:3px}
"""

TEMPLATE = r"""/*HEAD*/<body>/*SHELL*/<main>
<h1>[[p.u.h1]]</h1>
<p class="sub" id="gen"></p>
<div class="tiles" id="tiles"></div>
<h2>[[p.u.h_digest]]</h2>
<div class="card" id="digest"></div>
<h2>[[p.u.h_chunks]]</h2>
<div class="card scroll" id="chunks"></div>
<h2>[[p.u.h_5h]]<span class="vd" id="v5"></span><span class="range" id="r5"></span></h2>
<div class="card"><div class="legend"><span><i class="ln"></i> [[p.u.lg_measured]]</span><span><i class="ln d"></i> [[p.u.lg_est_calib]]</span>
<span><i class="ln u"></i> [[p.u.lg_usual]]</span><span><i class="bandu"></i> [[p.u.lg_band]]</span><span><i class="ln lw"></i> [[p.u.lg_lastweek_same]]</span>
<span><i class="bandk"></i> [[p.u.lg_spikes]]</span><span id="cal5"></span></div><div class="pace" id="p5"></div><div class="chart" id="c5"></div></div>
<h2>[[p.u.h_7d]]<span class="vd" id="v7"></span></h2>
<div class="card"><div class="legend"><span><i class="ln"></i> [[p.u.lg_measured]]</span><span><i class="ln d"></i> [[p.u.lg_est]]</span>
<span><i class="ln u"></i> [[p.u.lg_avg_week]]</span><span><i class="bandu"></i> [[p.u.lg_band]]</span><span><i class="ln lw"></i> [[p.u.lg_lastweek]]</span>
<span id="cal7"></span></div><div class="pace" id="p7"></div><div class="chart" id="c7"></div></div>
<h2>[[p.u.h_events]]</h2>
<div class="card scroll"><table id="events"><thead><tr><th>[[p.u.th_time]]</th><th>[[p.u.th_rise]]</th><th>[[p.u.th_cause]]</th><th class="n">[[p.u.th_save]]</th><th>[[p.u.th_share]]</th><th>[[p.u.th_map]]</th></tr></thead><tbody></tbody></table>
<p class="note">[[p.u.events_note]]</p></div>
<h2 id="cloud-h">[[p.c.h]] <span class="vd">[[p.c.k]]</span></h2>
<div class="card" id="cloud"></div>
</main>
<script id="data" type="application/json">/*DATA*/null</script>
<script>/*UPLOT*/</script>
<script>
const D=JSON.parse(document.getElementById('data').textContent);
const $=s=>document.querySelector(s), el=(t,a={},h='')=>{const e=document.createElement(t);for(const k in a)e.setAttribute(k,a[k]);e.innerHTML=h;return e};
const pad=n=>String(n).padStart(2,'0');
const fmt=fmtDT,hm=fmtHM;
const pc=v=>v==null?'—':(v<10?v.toFixed(1):Math.round(v))+'%';
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const PAL=['var(--s1)','var(--s2)','var(--s3)','var(--s4)','var(--s5)','var(--s6)','var(--s7)'];
const projColor={};let pi=0;const colorOf=p=>projColor[p]??=(pi<PAL.length?PAL[pi++]:'var(--muted)');
const BASIS={measured:T('p.basis.measured'),estimated:T('p.basis.estimated'),none:T('p.basis.none')};
const SL=D.statusline, slTxt=!SL?T('p.u.sl_never'):
 SL.has_five_hour?T('p.u.sl_ok',{t:fmt(SL.ts_ms)}):T('p.u.sl_nopct',{t:fmt(SL.ts_ms)});
$('#gen').textContent=T('p.u.gen',{from:fmt(D.start),to:fmt(D.end),gen:fmt(D.generated),sl:slTxt});

// tiles
const F=D.five_hour,W=D.seven_day,ev24=D.events.filter(e=>e.end>=D.end-86400000&&e.kind==='pct');
function tile(k,v,basis,d){const w=v==null?0:Math.min(100,v);
 return `<div class="tile"><div class="k">${k} <span class="badge">${BASIS[basis]||''}</span></div><div class="v">${pc(v)}</div>
 <div class="meter"><i style="width:${w}%"></i></div><div class="d">${esc(d)}</div></div>`}
$('#tiles').innerHTML=tile(T('p.u.tile_5h'),F.current,F.basis,F.reset?T('p.reset',{t:fmt(F.reset)}):'')+
 tile(T('p.u.tile_7d'),W.current,W.basis,W.reset?T('p.reset',{t:fmt(W.reset)}):(W.current==null?T('p.u.tile_7d_wait'):''))+
 `<div class="tile"><div class="k">${T('p.u.tile_24h')}</div><div class="v">${ev24.length}</div><div class="d">${esc(ev24.at(-1)?.headline||'')}</div></div>`;
const calTxt=s=>s.reliable?T('p.u.cal',{n:s.calib_n}):T('p.u.cal_few',{n:s.calib_n||0});
$('#cal5').textContent=calTxt(F);$('#cal7').textContent=calTxt(W);

const sec=ms=>ms/1000;
const rgba=(c,a)=>{const h=/^#([0-9a-f]{6})$/i.exec(c);if(!h)return `rgba(128,128,128,${a})`;const n=parseInt(h[1],16);return `rgba(${n>>16},${n>>8&255},${n&255},${a})`};
// issue #8: is the current usage high for this point of the window? (statistics of earlier windows)
const dur=ms=>{const m=Math.round(ms/60000);return m<60?T('p.dur.m',{m}):m<1440?T('p.dur.hm',{h:Math.floor(m/60),m:m%60}):T('p.dur.dh',{d:Math.floor(m/1440),h:Math.floor(m%1440/60)})};
function verdict(id,pid,S,word){const B=S.base;if(!B)return;const N=B.now||{};
 const VD={high:[T('p.u.vd_high'),'high'],normal:[T('p.u.vd_normal'),'normal'],low:[T('p.u.vd_low'),'low']}[B.verdict];
 $(id).innerHTML=VD?`<span class="st ${VD[1]}">${VD[0]}</span>`+T('p.u.vd_now',{cur:`<b>${pc(N.cur)}</b>`,word,off:dur(N.offset_ms),p50:pc(N.p50),p25:pc(N.p25),p75:pc(N.p75),n:N.n}):
  N.offset_ms==null?'<span class="st">—</span>'+T('p.u.vd_none'):`<span class="st">${T('p.u.vd_few')}</span>`+T('p.u.vd_few_n',{n:N.n||0});
 const P=B.pace;$(pid).innerHTML=P?T('p.u.pace',{r:P.per_hour,v:`<b>${pc(P.at_reset)}</b>`})+(P.full_ms?` · <span class="warn">${T('p.u.pace_full',{t:fmt(P.full_ms)})}</span>`:'')+` <span class="badge">${T('p.est')}</span>`:''}
function chart(boxId,S,from,events){
 const box=$(boxId);box.innerHTML='';const xs=[],m=[],e=[],b50=[],b75=[],b25=[],lw=[],B=S.base||{};
 S.m.forEach((v,i)=>{const t=S.t0+i*S.step;if(t<from)return;xs.push(sec(t));m.push(v);e.push(S.e[i]);
  b50.push(B.p50?.[i]??null);b75.push(B.p75?.[i]??null);b25.push(B.p25?.[i]??null);lw.push(B.last_week?.[i]??null)});
 if(!m.some(v=>v!=null)&&!e.some(v=>v!=null)){box.innerHTML=`<div class="empty">${T('p.u.chart_empty')}</div>`;return}
 const line=cssVar('--line'),grid=cssVar('--grid'),muted=cssVar('--muted'),band=cssVar('--band'),crit=cssVar('--critical');
 const evs=(events||[]).filter(x=>x.end>=from);
 const bands=u=>{const c=u.ctx,{top,height}=u.bbox;c.save();
  for(const [a] of S.windows){const x=u.valToPos(sec(a),'x',true);if(x>u.bbox.left&&x<u.bbox.left+u.bbox.width){c.strokeStyle=grid;c.setLineDash([2,3]);c.beginPath();c.moveTo(x,top);c.lineTo(x,top+height);c.stroke()}}
  c.setLineDash([]);for(const v of evs){const x0=u.valToPos(sec(v.start),'x',true),x1=Math.max(u.valToPos(sec(v.end),'x',true),x0+2);
   c.fillStyle=band;c.fillRect(x0,top,x1-x0,height);c.strokeStyle=crit;c.globalAlpha=.5;c.strokeRect(x0,top,x1-x0,height);c.globalAlpha=1}
  c.restore()};
 const fmtV=(u,v)=>v==null?'—':pc(v);
 const u=new uPlot({width:box.clientWidth,height:240,cursor:{sync:{key:'moni'},drag:{x:true,y:false}},
  scales:{x:{time:true},y:{range:[0,100]}},legend:{live:true},
  axes:[{stroke:muted,grid:{stroke:grid,width:1},ticks:{stroke:grid},values:(u,t)=>t.map(v=>{const d=new Date(v*1000);
    return d.getHours()===0&&d.getMinutes()===0?fmtMD(v*1000):hm(v*1000)})},{stroke:muted,grid:{stroke:grid,width:1},ticks:{show:false},values:(u,t)=>t.map(v=>v+'%'),size:44}],
  series:[{label:T('p.u.s_time'),value:(u,v)=>v==null?'—':fmt(v*1000)},
   {label:T('p.u.lg_measured'),stroke:line,width:2,value:fmtV,spanGaps:false},
   {label:T('p.u.lg_est'),stroke:line,width:1.5,dash:[4,3],alpha:.8,value:fmtV,spanGaps:false},
   {label:T('p.u.s_usual'),stroke:muted,width:1.5,value:fmtV,spanGaps:false},
   {label:'P75',stroke:'transparent',width:0,value:fmtV,spanGaps:false},
   {label:'P25',stroke:'transparent',width:0,value:fmtV,spanGaps:false},
   {label:T('p.u.lg_lastweek'),stroke:cssVar('--s2'),width:1.5,alpha:.9,value:fmtV,spanGaps:false}],
  bands:[{series:[4,5],fill:rgba(muted,.18)}],
  hooks:{drawClear:[bands]}},[xs,m,e,b50,b75,b25,lw],box);
 u.over.addEventListener('click',ev=>{if(u.select.width>2)return;const t=u.posToVal(ev.offsetX,'x')*1000,hit=evs.find(x=>x.start<=t&&t<=x.end);if(hit)openEvent(hit.id,true)});
 u.over.title=T('p.u.chart_tip');
}
const RANGES=[[T('p.u.r24h'),86400000],[T('p.u.r3d'),3*86400000],[T('p.u.r7d'),7*86400000]];let r5=86400000;
RANGES.forEach(([k,v])=>{const b=el('button',{'aria-pressed':v===r5},k);b.onclick=()=>{r5=v;$('#r5').querySelectorAll('button').forEach(x=>x.setAttribute('aria-pressed',x===b));draw()};$('#r5').append(b)});
const pctEvents=D.events.filter(e=>e.kind==='pct'||e.kind==='cache_write');
function draw(){chart('#c5',F,D.end-r5,pctEvents);chart('#c7',W,D.start,null)}
verdict('#v5','#p5',F,T('p.u.word_5h'));verdict('#v7','#p7',W,T('p.u.word_7d'));
addEventListener('themechange',draw);

const G=D.digest;$('#digest').innerHTML=`<p style="margin:0 0 8px">${esc(G.headline)}</p>`+(G.items.length?`<table class="tl"><tr><th>${T('p.u.dg_cause')}</th><th class="n">${T('p.u.dg_events')}</th><th class="n">${T('p.u.th_save')}</th><th>${T('p.u.dg_alt')}</th></tr>`+G.items.map(d=>`<tr><td>${esc(d.label)}</td><td class="n">${d.events}</td><td class="n">≈ ${d.pp.toFixed(1)}%p</td><td>${esc(d.advice)}<div class="note">${esc(d.alternative)}</div></td></tr>`).join('')+'</table>':'');

const C=D.chunks,CO=C.overall;$('#chunks').innerHTML=`<p style="margin:0 0 8px">${T('p.u.ch_head',{min:fmtN(C.min_tokens),n:CO.chunks,tok:`<b>${(CO.token_share*100).toFixed(1)}%</b>`,cost:(CO.cost_share*100).toFixed(1)+'%'})}</p>`+
 `<table class="tl"><tr><th>${T('p.u.ch_agent')}</th><th class="n">${T('p.u.ch_calls')}</th><th class="n">${T('p.u.ch_chunks')}</th><th class="n">${T('p.u.ch_per100')}</th><th class="n">${T('p.u.ch_median')}</th><th class="n">${T('p.u.ch_tok')}</th><th class="n">${T('p.u.ch_cost')}</th><th>${T('p.u.ch_src')}</th></tr>`+
 C.agents.filter(g=>g.chunks).map(g=>`<tr><td><span class="sw" style="background:${colorOf(g.project)}"></span>${esc(g.project)}</td><td class="n">${g.calls}</td><td class="n">${g.chunks}</td><td class="n">${g.per_100_calls}</td><td class="n">${fmtN(g.median_chunk)}</td><td class="n">${(g.token_share*100).toFixed(1)}%</td><td class="n">${(g.cost_share*100).toFixed(1)}%</td><td>${g.categories.slice(0,3).map(c=>T('p.u.cat_n',{cat:esc(c.category),n:c.chunks})).join(', ')}</td></tr>`).join('')+`</table><p class="note">${T('p.u.ch_note')}</p>`;

// shared header and links to the agent map (issue #4)
const MB=D.map_base??'',H=D.header||{};
const mapUrl=e=>`${MB}/?at=${e.end}&window=${Math.max(15,Math.ceil((e.end-e.start)/60000)+5)}`+(e.agents[0]?`&project=${encodeURIComponent(e.agents[0].project)}`:'')+`&lang=${LANG}`;
$('#lim').innerHTML=shellLimits(H);$('#meta').textContent=T('p.meta');

// events
const tb=$('#events tbody');
const shareBar=a=>`<div class="share">${a.map(g=>`<i title="${esc(g.project)} ${Math.round(g.share*100)}%" style="width:${g.share*100}%;background:${colorOf(g.project)}"></i>`).join('')}</div>`;
for(const e of [...D.events].reverse()){
 const rise=e.kind==='pct'?`<b>+${e.rise}%p</b> ${pc(e.frm)}→${pc(e.to)}${e.basis==='estimated'?` <span class="badge">${T('p.est')}</span>`:''}`:e.kind==='cache_write'?T('p.u.k_cw'):e.kind==='rate'?T('p.u.k_rate'):T('p.u.k_manual');
 const top=e.agents.slice(0,3).map(g=>`<span class="sw" style="background:${colorOf(g.project)}"></span>${esc(g.project)} ${Math.round(g.share*100)}%`).join(' &nbsp;');
 const tr=el('tr',{class:'ev',tabindex:'0','data-id':e.id},`<td>${fmt(e.start)}~${hm(e.end)}</td><td>${rise}</td><td>${esc(e.causes[0]?.label||T('p.u.no_cause'))}</td><td class="n">${e.save_pp!=null&&e.save_pp>=0.05?'≈ '+e.save_pp.toFixed(1)+'%p':'—'}</td><td>${top}</td><td><a class="maplink" href="${mapUrl(e)}">${T('p.u.map_then')}</a></td>`);
 tr.querySelector('.maplink').onclick=ev=>ev.stopPropagation();
 tr.onclick=()=>openEvent(e.id);tr.onkeydown=k=>{if(k.key==='Enter')openEvent(e.id)};tb.append(tr)}
function openEvent(id,scroll){
 const tr=tb.querySelector(`tr[data-id="${id}"]`);if(!tr)return;const nx=tr.nextElementSibling;
 if(nx&&nx.classList.contains('detail')){nx.remove();return}
 const e=D.events.find(x=>x.id===id);
 let h=`<div class="cause"><b>${T('p.u.ev_summary')}</b><div>${esc(e.summary)}</div></div><b>${T('p.u.ev_shares')}</b>${shareBar(e.agents)}<table class="tl"><tr><th>${T('p.u.ev_agent')}</th><th class="n">${T('p.u.ev_share')}</th>${e.rise?`<th class="n">${T('p.u.ev_pp')}</th>`:''}<th class="n">${T('p.u.ch_calls')}</th><th class="n">${T('p.u.session')}</th><th class="n">${T('p.u.ev_sub')}</th><th class="n">headless</th></tr>`+
  e.agents.map(g=>`<tr><td><span class="sw" style="background:${colorOf(g.project)}"></span>${esc(g.project)}</td><td class="n">${Math.round(g.share*100)}%</td>${e.rise?`<td class="n">+${(g.share*e.rise).toFixed(1)}</td>`:''}
  <td class="n">${g.calls}</td><td class="n">${g.sessions}</td><td class="n">${Math.round(g.subagent_share*100)}%</td><td class="n">${Math.round(g.headless_share*100)}%</td></tr>`).join('')+'</table>';
 h+=`<div style="margin-top:12px"><b>${T('p.u.causes')}</b></div>`+(e.causes.map(c=>`<div class="cause"><b>${esc(c.label)} · ${T('p.u.ev_cshare',{p:Math.round((c.share||0)*100)})}</b><div>${T('p.u.fact')} ${esc(c.fact_text)}</div>
 <div class="interp">${T('p.u.interp')} ${esc(c.interpretation)}</div><div class="adv">${T('p.u.advice')} ${esc(c.advice)}</div>${c.alternative?`<div>${T('p.u.alt')} ${esc(c.alternative)}${c.saving_share?' '+T('p.u.alt_save',{p:Math.round(c.saving_share*100)}):''}</div>`:''}</div>`).join('')||`<p>${T('p.u.no_cause')}</p>`);
 h+=`<details><summary>${T('p.u.tl',{n:e.calls.length})}</summary><div class="scroll"><table class="tl"><tr><th>${T('p.u.th_time')}</th><th>${T('p.u.ch_agent')}</th><th>${T('p.u.session')}</th><th class="n">${T('p.u.tl_ctx')}</th><th class="n">${T('p.u.tl_cw')}</th><th class="n">output</th><th>${T('p.u.tl_trig')}</th><th>${T('p.u.tl_tools')}</th></tr>`+
  e.calls.map(c=>`<tr><td>${fmtHMS(c.t)}</td><td>${esc(c.p)}${c.sc?' (sub)':''}${c.hl?' (headless)':''}</td><td>${esc(c.s)}</td><td class="n">${fmtN(c.ctx)}</td>
  <td class="n">${c.w?fmtN(c.w):''}</td><td class="n">${fmtN(c.o)}</td>
  <td>${c.tr==='human'?T('p.u.tr_human'):c.tr==='tool_result'?T('p.u.tr_tool')+(c.rb>=100000?` ${Math.round(c.rb/1024)}KB`:'')+(c.img?' '+T('p.u.tr_img'):''):''}</td><td>${esc(c.tools.join(', '))}</td></tr>`).join('')+'</table></div></details>';
 h+=`<p class="note">${esc(e.note||'')}</p>`;
 const d=el('tr',{class:'detail'},`<td colspan="6">${h}</td>`);tr.after(d);if(scroll)tr.scrollIntoView({behavior:'smooth',block:'center'})}
// issue #18 (ADR-0008): AWS cost, opt-in. The static file shows values and a note; the live page adds the form.
let CL=D.cloud||null,CK=null,MSG=null;const FV=Object.assign({aws_profile:'',budget_name:'',aws_path:''},CL&&CL.settings||{});
const money=(v,u)=>v==null?'—':(!u||u==='USD'?'$'+v.toFixed(2):v.toFixed(2)+' '+u);
const agoC=ms=>{const m=Math.max(0,Math.round((Date.now()-ms)/60000));return m<60?T('p.c.ago_m',{n:m}):m<2880?T('p.c.ago_h',{n:Math.round(m/60)}):T('p.c.ago_d',{n:Math.round(m/1440)})};
const clCur=C=>{const M=C.metric,B=C.budget;return M?[M.amount,M.currency,M.ts_ms||M.fetched_ms,M.fetched_ms]:B?[B.actual,B.unit,B.updated_ms||B.fetched_ms,B.fetched_ms]:null};
const hint=(code,C)=>window.I18N['p.c.hint.'+code]!=null?T('p.c.hint.'+code,{code,cap:fmtN(C.cap)}):T('p.c.hint.Error',{code:esc(code)});
const usedN=U=>(U.cloudwatch||0)+(U.budgets||0)+(U.sts||0);
$('#tiles').insertAdjacentHTML('beforeend','<span id="cltile" style="display:contents"></span>');
function clTile(C){if(!C||!C.enabled)return '';const c=clCur(C);if(!c)return '';const B=C.budget;
 return `<div class="tile"><div class="k">${T('p.c.tile')}</div><div class="v">${money(c[0],c[1])}</div><div class="d">${B&&B.forecast!=null?T('p.c.fc')+' '+money(B.forecast,B.unit)+' · ':''}${agoC(c[2])}</div></div>`}
function stepLine(s,C){const m=s.ok===true?['ok','OK']:s.ok===false?['no','FAIL']:['na','—'],v=s.value||(s.id==='calls'?Object.assign({cap:C.cap,used:C.calls},C.expected):null);let d='';
 if(s.id==='aws'&&s.path)d=`<span class="mono">${esc(s.path)}</span>`;
 if(s.id==='profile'&&s.ok)d=T('p.c.d_profile');
 if(s.id==='metric'&&v)d=`${T('p.c.mtd')} <b>${money(v.amount,v.currency)}</b> · ${agoC(v.ts_ms)}`;
 if(s.id==='budget'&&v)d=`${T('p.c.mtd')} ${money(v.actual,v.unit)} · ${T('p.c.fc')} <b>${v.forecast!=null?money(v.forecast,v.unit):T('p.c.no_fc')}</b>${v.limit!=null?' · '+T('p.c.limit')+' '+money(v.limit,v.unit):''}`;
 if(s.id==='calls'&&v)d=T('p.c.d_calls',{metric:fmtN(v.metric),budget:fmtN(v.budget),total:fmtN(v.total),cap:fmtN(v.cap),used:fmtN(usedN(v.used||{}))});
 return `<li><span class="m ${m[0]}">${m[1]}</span>${T('p.c.s_'+s.id)}${d?' · '+d:''}${s.code?`<div class="h">${hint(s.code,C)}</div>`:''}</li>`}
function cloudCard(){const C=CL,box=$('#cloud');if(!C){box.innerHTML='';return}
 $('#cltile').innerHTML=clTile(C);
 const on=C.enabled,c=clCur(C),B=C.budget,E=C.expected,used=usedN(C.calls||{});
 let h=`<div class="cl-top"><span class="cl-st${on?' on':''}">${on?T('p.c.on'):T('p.c.off')}</span><span>${on?T('p.c.state_on',{m:C.metric_min,h:C.budget_h}):T('p.c.state_off')}</span></div>`;
 if(c){const stale=Date.now()-c[3]>C.stale_ms;
  h+=`<div class="cl-vals"><div><div class="k">${T('p.c.mtd')}</div><div class="v">${money(c[0],c[1])}</div></div>`+
   `<div><div class="k">${T('p.c.fc')}</div><div class="v">${B&&B.forecast!=null?money(B.forecast,B.unit):'—'}</div>${B&&B.forecast==null?`<div class="k">${T('p.c.no_fc')}</div>`:''}</div>`+
   (B&&B.limit!=null?`<div><div class="k">${T('p.c.limit')}</div><div class="v">${money(B.limit,B.unit)}</div></div>`:'')+
   `<div><div class="k">${T('p.c.age')}</div><div class="v">${agoC(c[2])}</div>${stale?`<div class="k">${T('p.c.stale')}</div>`:''}</div>`+
   `<div><div class="k">${T('p.c.calls')} (${esc(C.month)})</div><div class="v">${T('p.c.calls_v',{used:fmtN(used),cap:fmtN(C.cap)})}</div></div></div>`}
 if(on)for(const k of ['metric','budget']){const e=C.err&&C.err[k];if(e)h+=`<p class="cl-msg bad">${T('p.c.err',{code:esc(e.code),t:fmtDT(C.next[k])})} ${hint(e.code,C)}</p>`}
 if(!C.served||typeof cloudForm!=='function'){box.innerHTML=h+`<p class="note">${T('p.c.static',{url:'<code>http://127.0.0.1:4990/usage</code>'})}</p>`;return}
 cloudForm(box,h,C,on)}
/*CLOUDFORM*/
cloudCard();
draw();addEventListener('resize',()=>{clearTimeout(window._r);window._r=setTimeout(draw,120)});
if(location.hash==='#cloud')$('#cloud-h').scrollIntoView();
const hid=/^#event-(\d+)$/.exec(location.hash);if(hid)openEvent(+hid[1],true);
</script></body></html>"""

# issue #18: only the page served by `moni_token map` can change settings, so only it carries this code (the static
# report makes no request at all)
CLOUD_FORM_JS = r"""function cloudForm(box,h,C,on){
 const E=C.expected,ck=C.check,okNow=ck&&ck.ok&&ck.matches;
 h+=`<div class="cl-form"><label>${T('p.c.f_profile')}<input id="cl-profile" maxlength="64" autocomplete="off" spellcheck="false" value="${esc(FV.aws_profile)}"></label>`+
  `<label>${T('p.c.f_budget')}<input id="cl-budget" maxlength="100" autocomplete="off" spellcheck="false" value="${esc(FV.budget_name)}"></label>`+
  `<label>${T('p.c.f_path')}<input id="cl-path" class="wide" maxlength="260" autocomplete="off" spellcheck="false" placeholder="${esc(T('p.c.f_path_ph'))}" value="${esc(FV.aws_path)}"></label>`+
  `<button type="button" id="cl-check" class="pri">${T('p.c.b_check')}</button>`+
  (on?`<button type="button" id="cl-save">${T('p.c.b_save')}</button><button type="button" id="cl-toggle">${T('p.c.b_off')}</button>`:
   `<button type="button" id="cl-toggle"${okNow||(CK&&CK.ok)?'':' title="'+esc(T('p.c.need_check'))+'"'}>${T('p.c.b_on')}</button>`)+`</div>`;
 h+=`<div class="k">${C.aws_found?T('p.c.found',{path:`<span class="mono">${esc(C.aws_found)}</span>`}):T('p.c.notfound')}</div>`;
 h+=`<div class="cl-msg${MSG&&MSG[1]?' bad':''}" id="cl-msg">${MSG?MSG[0]:!on&&!okNow?T('p.c.need_check'):ck?T(ck.ok?'p.c.passed':'p.c.failed',{t:fmtDT(ck.at_ms)}):''}</div>`;
 const steps=CK?CK.steps:(ck&&ck.matches?ck.steps:null);if(steps)h+=`<ul class="cl-steps">${steps.map(s=>stepLine(s,C)).join('')}</ul>`;
 const prof=esc(FV.aws_profile||'moni-token-billing');
 h+=`<details class="cl-guide"${on?'':' open'}><summary>${T('p.c.g_h')}</summary><ol><li>${T('p.c.g1')}</li><li>${T('p.c.g2')}</li><li>${T('p.c.g3')}</li><li>${T('p.c.g4')}<pre>${esc(C.policy)}</pre></li><li>${T('p.c.g5',{profile:prof})}</li><li>${T('p.c.g6')}</li></ol>`+
  `<div class="cl-why"><b>${T('p.c.w_h')}</b><p>${T('p.c.w1',{m:C.metric_min,h:C.budget_h,metric:fmtN(E.metric),budget:fmtN(E.budget||Math.round(720/C.budget_h))})}</p><p>${T('p.c.w2',{cap:fmtN(C.cap)})}</p><p>${T('p.c.w3')}</p><p>${T('p.c.w4')}</p><p>${T('p.c.org')}</p></div></details>`;
 box.innerHTML=h;
 const bind=(id,k)=>$(id).oninput=e=>{FV[k]=e.target.value;CK=null;MSG=null};
 bind('#cl-profile','aws_profile');bind('#cl-budget','budget_name');bind('#cl-path','aws_path');
 const busy=t=>{MSG=[t,false];$('#cl-msg').textContent=t;$('#cloud').querySelectorAll('button').forEach(b=>b.disabled=true)};
 const errText=(s,j)=>j.error==='check_first'?T('p.c.e.check_first'):j.error==='fields'?T('p.c.e.fields',{f:esc((j.fields||[]).join(', '))}):T('p.c.e.http',{s});
 const post=async(path,body)=>{try{const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Moni-Token':CL.token},body:JSON.stringify(body)});
  const j=await r.json().catch(()=>({}));if(j.view)CL=j.view;return [r.status,j]}catch(e){return [0,{}]}};
 $('#cl-check').onclick=async()=>{busy(T('p.c.checking'));const [s,j]=await post('/api/cloud/check',FV);
  if(s===200){CK=j.check;MSG=[T(CK.ok?'p.c.passed':'p.c.failed',{t:fmtDT(CK.at_ms)}),!CK.ok]}else MSG=[T('p.c.refused',{e:errText(s,j)}),true];cloudCard()};
 const save=async body=>{busy(T('p.c.saving'));const [s,j]=await post('/api/cloud/settings',body);
  if(s===200){Object.assign(FV,CL.settings);MSG=[T('p.c.saved'),false]}else MSG=[T('p.c.refused',{e:errText(s,j)}),true];cloudCard()};
 $('#cl-toggle').onclick=()=>save(on?{enabled:false}:Object.assign({enabled:true},FV));
 if($('#cl-save'))$('#cl-save').onclick=()=>save(Object.assign({},FV))}
"""
