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
from .calibrate import estimate
from .chunks import analyze_chunks
from .events import list_events
from .summary import _pp, best_alternative, digest, event_summary
from .pctseries import pct_at, series

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


def report_events(con: sqlite3.Connection, since_ms: int, with_calls: bool = False) -> list[dict]:
    """Recorded events, flattened for display, each with its savings summary sentence."""
    names = _names(con) if with_calls else {}
    ratio = estimate(con, "five_hour")["usd_per_pct"]
    events = []
    for e in list_events(con, since_ms):
        ev = (e["spike"] or {}).get("evidence", {})
        d = dict(id=e["id"], kind=e["kind"], start=e["start_ms"], end=e["end_ms"],
                 rise=ev.get("rise_pp"), frm=ev.get("from_pct"), to=ev.get("to_pct"), basis=ev.get("basis"),
                 headline=e["headline"], note=e["note"], agents=e["agents"],
                 causes=[{k: c.get(k) for k in ("label", "fact_text", "interpretation", "advice", "share",
                                                "saving_usd", "saving_share", "alternative")} for c in e["causes"]],
                 calls=_timeline(con, names, e["start_ms"], e["end_ms"]) if with_calls else [])
        d["summary"] = event_summary(d, ratio)
        b = best_alternative(d)
        pp = _pp(d, b, ratio) if b else None
        d["save_pp"] = round(pp, 2) if pp is not None else None
        events.append(d)
    return events


def build_data(con: sqlite3.Connection, now_ms: int, days: int = 7) -> dict:
    start = now_ms - days * DAY
    fh = _pack(series(con, "five_hour", start, now_ms), now_ms)
    sd = _pack(series(con, "seven_day", start, now_ms), now_ms)
    events = report_events(con, start, with_calls=True)
    main = [e for e in events if e["kind"] == "pct"] or [e for e in events if e["kind"] == "rate"]
    dg = digest(main, estimate(con, "five_hour")["usd_per_pct"], estimate(con, "seven_day")["usd_per_pct"])
    ch = analyze_chunks(con, start, now_ms)
    try:
        seen = json.loads((config.data_dir() / "statusline.seen").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        seen = None
    return dict(generated=now_ms, start=start, end=now_ms, five_hour=fh, seven_day=sd, events=events, statusline=seen, digest=dg, chunks=ch)


def render(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return TEMPLATE.replace("/*DATA*/null", payload)


def write_report(con: sqlite3.Connection, out: Path, now_ms: int | None = None, days: int = 7) -> Path:
    html = render(build_data(con, now_ms or int(time.time() * 1000), days))
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(html, encoding="utf-8")
    tmp.replace(out)   # atomic: a browser refresh never sees half a file
    return out


TEMPLATE = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>moni_token 한도 사용률</title>
<style>
:root{--bg:#f4f4f2;--surface-1:#fcfcfb;--text-primary:#1a1a19;--text-secondary:#57564f;--muted:#8a897f;--grid:#e4e3dd;
--border:#d9d8d1;--line:#2a78d6;--band:rgba(196,43,42,.08);--critical:#c42b2a;--warn:#b86e00;
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--s5:#e87ba4;--s6:#008300;--s7:#4a3aa7}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#121211;--surface-1:#1a1a19;--text-primary:#fff;
--text-secondary:#c3c2b7;--muted:#8f8e85;--grid:#2c2c2a;--border:#3a3a37;--line:#3987e5;--band:rgba(230,103,103,.12);
--critical:#e66767;--warn:#e0a030;--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#008300;--s7:#9085e9}}
:root[data-theme="dark"]{--bg:#121211;--surface-1:#1a1a19;--text-primary:#fff;--text-secondary:#c3c2b7;--muted:#8f8e85;
--grid:#2c2c2a;--border:#3a3a37;--line:#3987e5;--band:rgba(230,103,103,.12);--critical:#e66767;--warn:#e0a030;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#008300;--s7:#9085e9}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text-primary);
font:14px/1.5 system-ui,-apple-system,"Segoe UI","Malgun Gothic",sans-serif}
main{max-width:1200px;margin:0 auto;padding:24px 16px 64px}
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
.chart{width:100%;position:relative}svg text{fill:var(--muted);font-size:11px}
.tip{position:absolute;pointer-events:none;background:var(--surface-1);border:1px solid var(--border);border-radius:8px;
padding:8px 10px;font-size:12px;box-shadow:0 4px 16px rgba(0,0,0,.12);display:none;z-index:2;white-space:nowrap}
.tip b{font-size:16px}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--grid);vertical-align:top}
th{color:var(--text-secondary);font-weight:500;font-size:12px}td.n,th.n{text-align:right}
tr.ev{cursor:pointer}tr.ev:hover{background:var(--grid)}tr.ev:focus{outline:2px solid var(--line)}
.detail td{background:var(--bg)}.cause{margin:6px 0 10px}.cause b{display:block}
.interp{color:var(--text-secondary)}.adv{color:var(--line)}.note{color:var(--muted);font-size:12px}.scroll{overflow-x:auto}
details summary{cursor:pointer;color:var(--text-secondary)}.tl{font-size:12px}.tl td{padding:3px 6px}
.share{display:flex;height:14px;border-radius:4px;overflow:hidden;margin:6px 0 8px;gap:2px}.share i{display:block;height:100%}
.sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}
.badge{font-size:11px;padding:1px 6px;border-radius:4px;border:1px solid var(--border);color:var(--text-secondary)}
</style></head><body><main>
<h1>Claude 한도 사용률 · 급상승 원인</h1>
<p class="sub" id="gen"></p>
<div class="tiles" id="tiles"></div>
<h2>절약 요약 (최근 7일)</h2>
<div class="card" id="digest"></div>
<h2>큰 덩어리 전달 (최근 7일)</h2>
<div class="card scroll" id="chunks"></div>
<h2>현재 세션 (5시간 한도)<span class="range" id="r5"></span></h2>
<div class="card"><div class="legend"><span><i class="ln"></i> 실측(상태줄)</span><span><i class="ln d"></i> 추정(로그 ÷ 보정)</span>
<span><i class="bandk"></i> 급상승 사건</span><span id="cal5"></span></div><div class="chart" id="c5"></div></div>
<h2>이번 주 (7일 한도)</h2>
<div class="card"><div class="legend"><span><i class="ln"></i> 실측(상태줄)</span><span><i class="ln d"></i> 추정</span>
<span id="cal7"></span></div><div class="chart" id="c7"></div></div>
<h2>급상승 사건</h2>
<div class="card scroll"><table id="events"><thead><tr><th>시각</th><th>상승</th><th>주 원인</th><th class="n">절약 가능</th><th>에이전트 점유율</th></tr></thead><tbody></tbody></table>
<p class="note">행을 누르면 에이전트(프로젝트)별 점유율, 원인 근거, 그 구간의 호출 타임라인이 열립니다. 점유율은 구간 안 로컬 로그 사용량(토큰 종류별 가중) 기준이며 해석은 규칙에 따른 추정입니다.</p></div>
</main>
<script id="data" type="application/json">/*DATA*/null</script>
<script>
const D=JSON.parse(document.getElementById('data').textContent);
const $=s=>document.querySelector(s), el=(t,a={},h='')=>{const e=document.createElement(t);for(const k in a)e.setAttribute(k,a[k]);e.innerHTML=h;return e};
const pad=n=>String(n).padStart(2,'0');
const fmt=ms=>{const d=new Date(ms);return `${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`};
const hm=ms=>{const d=new Date(ms);return `${pad(d.getHours())}:${pad(d.getMinutes())}`};
const pc=v=>v==null?'—':(v<10?v.toFixed(1):Math.round(v))+'%';
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const PAL=['var(--s1)','var(--s2)','var(--s3)','var(--s4)','var(--s5)','var(--s6)','var(--s7)'];
const projColor={};let pi=0;const colorOf=p=>projColor[p]??=(pi<PAL.length?PAL[pi++]:'var(--muted)');
const BASIS={measured:'실측',estimated:'추정',none:'데이터 없음'};
const SL=D.statusline, slTxt=!SL?'상태줄 기록기: 아직 호출된 적 없음 (Claude Code를 새로 시작하면 연결됨)':
 SL.has_five_hour?`상태줄 실측 수신 중 (마지막 ${fmt(SL.ts_ms)})`:`상태줄은 호출되지만 한도 % 정보가 오지 않음 (마지막 ${fmt(SL.ts_ms)})`;
$('#gen').textContent=`${fmt(D.start)} ~ ${fmt(D.end)} · 생성 ${fmt(D.generated)} · LLM 호출 없음 · ${slTxt}`;

// tiles
const F=D.five_hour,W=D.seven_day,ev24=D.events.filter(e=>e.end>=D.end-86400000&&e.kind==='pct');
function tile(k,v,basis,d){const w=v==null?0:Math.min(100,v);
 return `<div class="tile"><div class="k">${k} <span class="badge">${BASIS[basis]||''}</span></div><div class="v">${pc(v)}</div>
 <div class="meter"><i style="width:${w}%"></i></div><div class="d">${esc(d)}</div></div>`}
$('#tiles').innerHTML=tile('현재 세션 (5시간)',F.current,F.basis,F.reset?`초기화 ${fmt(F.reset)}`:'')+
 tile('이번 주 (7일)',W.current,W.basis,W.reset?`초기화 ${fmt(W.reset)}`:(W.current==null?'상태줄 실측이 쌓이면 표시':''))+
 `<div class="tile"><div class="k">최근 24시간 급상승</div><div class="v">${ev24.length}</div><div class="d">${esc(ev24.at(-1)?.headline||'')}</div></div>`;
const calTxt=s=>s.reliable?`보정 표본 ${s.calib_n}건`:`보정 표본 ${s.calib_n||0}건 · 3건부터 추정선 표시`;
$('#cal5').textContent=calTxt(F);$('#cal7').textContent=calTxt(W);

function niceTicks(a,b,W){const span=b-a, H=3600000, opts=[H,3*H,6*H,12*H,24*H];
 const st=opts.find(o=>span/o<=Math.max(4,W/110))||24*H, out=[];let t=new Date(a);t.setMinutes(0,0,0);
 t=+t;while(t%st&&st<24*H)t+=H;if(st===24*H){const d=new Date(a);d.setHours(24,0,0,0);t=+d}
 for(;t<=b;t+=st)out.push(t);return {ticks:out,st}}
function line(boxId,S,from,events){
 const box=$(boxId),W=box.clientWidth,H=Math.max(200,Math.min(300,W*.3)),m={l:40,r:10,t:12,b:24},iw=W-m.l-m.r,ih=H-m.t-m.b;
 const to=D.end,x=t=>m.l+(t-from)/(to-from)*iw,y=v=>m.t+ih-Math.min(100,v)/100*ih;
 let g='';
 for(const v of [0,25,50,75,100])g+=`<line x1="${m.l}" x2="${W-m.r}" y1="${y(v)}" y2="${y(v)}" stroke="var(--grid)"/><text x="${m.l-6}" y="${y(v)+4}" text-anchor="end">${v}%</text>`;
 const {ticks,st}=niceTicks(from,to,iw);for(const t of ticks)g+=`<text x="${x(t)}" y="${H-6}" text-anchor="middle">${st>=86400000?fmt(t).slice(0,5):hm(t)==='00:00'?fmt(t).slice(0,5):hm(t)}</text>`;
 for(const [a] of S.windows)if(a>from&&a<to)g+=`<line x1="${x(a)}" x2="${x(a)}" y1="${m.t}" y2="${m.t+ih}" stroke="var(--grid)" stroke-dasharray="2 3"/>`;
 for(const e of events||[]){if(e.end<from)continue;const a=Math.max(x(e.start),m.l),b=Math.max(x(e.end),a+2);
  g+=`<rect class="evb" data-id="${e.id}" x="${a}" y="${m.t}" width="${b-a}" height="${ih}" fill="var(--band)" stroke="var(--critical)" stroke-opacity=".5" style="cursor:pointer"/>`}
 const path=(arr)=>{let d='',on=false;arr.forEach((v,i)=>{const t=S.t0+i*S.step;if(t<from||v==null){on=false;return}
  d+=(on?'L':'M')+x(t).toFixed(1)+','+y(v).toFixed(1);on=true});return d};
 const pe=path(S.e),pm=path(S.m);
 if(pe)g+=`<path d="${pe}" fill="none" stroke="var(--line)" stroke-width="1.5" stroke-dasharray="4 3" opacity=".75"/>`;
 if(pm)g+=`<path d="${pm}" fill="none" stroke="var(--line)" stroke-width="2"/>`;
 if(!pe&&!pm)g+=`<text x="${m.l+iw/2}" y="${m.t+ih/2}" text-anchor="middle" style="font-size:13px">아직 표시할 % 데이터가 없습니다 (상태줄 실측 또는 보정 표본 필요)</text>`;
 g+=`<line class="xh" y1="${m.t}" y2="${m.t+ih}" stroke="var(--text-secondary)" visibility="hidden"/><circle class="dot" r="4" fill="var(--line)" stroke="var(--surface-1)" stroke-width="2" visibility="hidden"/>`;
 box.innerHTML=`<svg width="${W}" height="${H}" role="img" aria-label="한도 사용률 추이">${g}</svg><div class="tip"></div>`;
 const svg=box.querySelector('svg'),tip=box.querySelector('.tip'),xh=box.querySelector('.xh'),dot=box.querySelector('.dot');
 svg.onmousemove=ev=>{const r=svg.getBoundingClientRect(),px=ev.clientX-r.left;if(px<m.l){return}
  const t=from+(px-m.l)/iw*(to-from),i=Math.round((t-S.t0)/S.step);if(i<0||i>=S.e.length)return;
  const tt=S.t0+i*S.step,mv=S.m[i],evv=S.e[i],v=mv??evv;xh.setAttribute('x1',x(tt));xh.setAttribute('x2',x(tt));xh.setAttribute('visibility','visible');
  if(v!=null){dot.setAttribute('cx',x(tt));dot.setAttribute('cy',y(v));dot.setAttribute('visibility','visible')}else dot.setAttribute('visibility','hidden');
  const e=(events||[]).find(e=>e.start<=tt&&tt<e.end);
  tip.innerHTML=`<div>${fmt(tt)}</div><b>${pc(v)}</b> <span class="badge">${mv!=null?'실측':evv!=null?'추정':'없음'}</span>`+(e?`<div style="color:var(--critical)">+${e.rise}%p · ${esc(e.causes[0]?.label||'')}</div>`:'');
  tip.style.display='block';tip.style.left=Math.min(px+12,W-200)+'px';tip.style.top='4px'};
 svg.onmouseleave=()=>{tip.style.display='none';xh.setAttribute('visibility','hidden');dot.setAttribute('visibility','hidden')};
 svg.querySelectorAll('.evb').forEach(b=>b.onclick=()=>openEvent(+b.dataset.id,true));
}
const RANGES=[['24시간',86400000],['3일',3*86400000],['7일',7*86400000]];let r5=86400000;
RANGES.forEach(([k,v])=>{const b=el('button',{'aria-pressed':v===r5},k);b.onclick=()=>{r5=v;$('#r5').querySelectorAll('button').forEach(x=>x.setAttribute('aria-pressed',x===b));draw()};$('#r5').append(b)});
const pctEvents=D.events.filter(e=>e.kind==='pct'||e.kind==='cache_write');
function draw(){line('#c5',F,D.end-r5,pctEvents);line('#c7',W,D.start,null)}

const G=D.digest;$('#digest').innerHTML=`<p style="margin:0 0 8px">${esc(G.headline)}</p>`+(G.items.length?'<table class="tl"><tr><th>원인</th><th class="n">사건</th><th class="n">절약 가능</th><th>대안</th></tr>'+G.items.map(d=>`<tr><td>${esc(d.label)}</td><td class="n">${d.events}</td><td class="n">≈ ${d.pp.toFixed(1)}%p</td><td>${esc(d.advice)}<div class="note">${esc(d.alternative)}</div></td></tr>`).join('')+'</table>':'');

const C=D.chunks,CO=C.overall;$('#chunks').innerHTML=`<p style="margin:0 0 8px">도구 결과가 결론이 아니라 ${C.min_tokens.toLocaleString()}토큰 이상 덩어리로 전달된 경우: ${CO.chunks}건, 끌고 다닌 양이 전체 토큰의 <b>${(CO.token_share*100).toFixed(1)}%</b> (비용 가중 ${(CO.cost_share*100).toFixed(1)}%)</p>`+
 '<table class="tl"><tr><th>에이전트</th><th class="n">호출</th><th class="n">덩어리</th><th class="n">100호출당</th><th class="n">중앙값(토큰)</th><th class="n">토큰 비중</th><th class="n">비용 비중</th><th>주된 출처</th></tr>'+
 C.agents.filter(g=>g.chunks).map(g=>`<tr><td><span class="sw" style="background:${colorOf(g.project)}"></span>${esc(g.project)}</td><td class="n">${g.calls}</td><td class="n">${g.chunks}</td><td class="n">${g.per_100_calls}</td><td class="n">${g.median_chunk.toLocaleString()}</td><td class="n">${(g.token_share*100).toFixed(1)}%</td><td class="n">${(g.cost_share*100).toFixed(1)}%</td><td>${g.categories.slice(0,3).map(c=>esc(c.category)+' '+c.chunks+'건').join(', ')}</td></tr>`).join('')+'</table><p class="note">크기는 받은 호출의 컨텍스트 증가분으로 잰 실측 토큰. 끌고 다닌 양 = 크기 × (1 + 같은 대화에서 이후 호출 수, compact 전까지). 비용 비중은 캐시 쓰기 1.25~2배·읽기 0.1배 가중.</p>';

// events
const tb=$('#events tbody');
const shareBar=a=>`<div class="share">${a.map(g=>`<i title="${esc(g.project)} ${Math.round(g.share*100)}%" style="width:${g.share*100}%;background:${colorOf(g.project)}"></i>`).join('')}</div>`;
for(const e of [...D.events].reverse()){
 const rise=e.kind==='pct'?`<b>+${e.rise}%p</b> ${pc(e.frm)}→${pc(e.to)}${e.basis==='estimated'?' <span class="badge">추정</span>':''}`:e.kind==='cache_write'?'캐시 재기록':e.kind==='rate'?'사용량 급증':'지정 구간';
 const top=e.agents.slice(0,3).map(g=>`<span class="sw" style="background:${colorOf(g.project)}"></span>${esc(g.project)} ${Math.round(g.share*100)}%`).join(' &nbsp;');
 const tr=el('tr',{class:'ev',tabindex:'0','data-id':e.id},`<td>${fmt(e.start)}~${hm(e.end)}</td><td>${rise}</td><td>${esc(e.causes[0]?.label||'규칙에 맞는 원인 없음')}</td><td class="n">${e.save_pp!=null&&e.save_pp>=0.05?'≈ '+e.save_pp.toFixed(1)+'%p':'—'}</td><td>${top}</td>`);
 tr.onclick=()=>openEvent(e.id);tr.onkeydown=k=>{if(k.key==='Enter')openEvent(e.id)};tb.append(tr)}
function openEvent(id,scroll){
 const tr=tb.querySelector(`tr[data-id="${id}"]`);if(!tr)return;const nx=tr.nextElementSibling;
 if(nx&&nx.classList.contains('detail')){nx.remove();return}
 const e=D.events.find(x=>x.id===id);
 let h=`<div class="cause"><b>절약 요약</b><div>${esc(e.summary)}</div></div><b>에이전트별 점유율</b>${shareBar(e.agents)}<table class="tl"><tr><th>에이전트(프로젝트)</th><th class="n">점유율</th>${e.rise?'<th class="n">≈ 기여 %p</th>':''}<th class="n">호출</th><th class="n">세션</th><th class="n">서브에이전트</th><th class="n">headless</th></tr>`+
  e.agents.map(g=>`<tr><td><span class="sw" style="background:${colorOf(g.project)}"></span>${esc(g.project)}</td><td class="n">${Math.round(g.share*100)}%</td>${e.rise?`<td class="n">+${(g.share*e.rise).toFixed(1)}</td>`:''}
  <td class="n">${g.calls}</td><td class="n">${g.sessions}</td><td class="n">${Math.round(g.subagent_share*100)}%</td><td class="n">${Math.round(g.headless_share*100)}%</td></tr>`).join('')+'</table>';
 h+='<div style="margin-top:12px"><b>원인</b></div>'+(e.causes.map(c=>`<div class="cause"><b>${esc(c.label)} · 구간의 ${Math.round((c.share||0)*100)}%</b><div>사실: ${esc(c.fact_text)}</div>
 <div class="interp">해석: ${esc(c.interpretation)}</div><div class="adv">권고: ${esc(c.advice)}</div>${c.alternative?`<div>대안: ${esc(c.alternative)}${c.saving_share?` (구간의 약 ${Math.round(c.saving_share*100)}% 절약)`:''}</div>`:''}</div>`).join('')||'<p>규칙에 맞는 원인 없음</p>');
 h+=`<details><summary>호출 타임라인 (${e.calls.length}건)</summary><div class="scroll"><table class="tl"><tr><th>시각</th><th>에이전트</th><th>세션</th><th class="n">컨텍스트</th><th class="n">캐시 쓰기</th><th class="n">output</th><th>계기</th><th>도구</th></tr>`+
  e.calls.map(c=>`<tr><td>${hm(c.t)}:${pad(new Date(c.t).getSeconds())}</td><td>${esc(c.p)}${c.sc?' (sub)':''}${c.hl?' (headless)':''}</td><td>${esc(c.s)}</td><td class="n">${c.ctx.toLocaleString()}</td>
  <td class="n">${c.w?c.w.toLocaleString():''}</td><td class="n">${c.o.toLocaleString()}</td>
  <td>${c.tr==='human'?'사람 입력':c.tr==='tool_result'?'도구 결과'+(c.rb>=100000?` ${Math.round(c.rb/1024)}KB`:'')+(c.img?' 이미지':''):''}</td><td>${esc(c.tools.join(', '))}</td></tr>`).join('')+'</table></div></details>';
 h+=`<p class="note">${esc(e.note||'')}</p>`;
 const d=el('tr',{class:'detail'},`<td colspan="5">${h}</td>`);tr.after(d);if(scroll)tr.scrollIntoView({behavior:'smooth',block:'center'})}
draw();addEventListener('resize',()=>{clearTimeout(window._r);window._r=setTimeout(draw,120)});
const hid=/^#event-(\d+)$/.exec(location.hash);if(hid)openEvent(+hid[1],true);
</script></body></html>"""
