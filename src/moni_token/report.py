"""Static, offline HTML report (no server, no CDN). Data is embedded as JSON; drawing is inline SVG + vanilla JS.

Only numbers, project folder names, session id prefixes, tool names and template sentences are embedded.
"""
import json
import sqlite3
import time
from pathlib import Path

from .blocks import compute_blocks, status
from .events import list_events
from .units import call_usd

HALF_HOUR = 30 * 60_000
TOP_PROJECTS = 6
MAX_TIMELINE_CALLS = 400


def _project_names(con) -> dict[str, str]:
    return {d: n for d, n in con.execute(
        "SELECT project_dir, project FROM calls WHERE project IS NOT NULL GROUP BY project_dir")}


def build_data(con: sqlite3.Connection, now_ms: int, days: int = 7) -> dict:
    start = now_ms - days * 86_400_000
    start -= start % HALF_HOUR
    names = _project_names(con)
    series: dict[str, dict[int, float]] = {}
    for t5, proj, usd in con.execute("SELECT t5_ms, project_dir, SUM(usd) FROM buckets WHERE t5_ms >= ? "
                                     "GROUP BY t5_ms, project_dir", (start,)):
        name = names.get(proj, proj)
        b = t5 - t5 % HALF_HOUR
        series.setdefault(name, {})
        series[name][b] = series[name].get(b, 0.0) + usd
    totals = sorted(series, key=lambda n: -sum(series[n].values()))
    keep, rest = totals[:TOP_PROJECTS], totals[TOP_PROJECTS:]
    if rest:
        other: dict[int, float] = {}
        for n in rest:
            for b, v in series[n].items():
                other[b] = other.get(b, 0.0) + v
        series = {**{n: series[n] for n in keep}, "기타": other}
        keep = keep + ["기타"]
    bins = list(range(start, now_ms + HALF_HOUR, HALF_HOUR))

    events = []
    for e in list_events(con, start):
        calls = []
        for (ts, proj, sid, side, model, inp, out, cr, c5, c1, ws, trig, prb, img, mid) in con.execute(
                "SELECT ts_ms, project_dir, session_id, is_sidechain, model, input, output, cache_read, cache_5m, "
                "cache_1h, web_search_n, trigger, prev_result_bytes, prev_result_image, msg_id FROM calls "
                "WHERE ts_ms >= ? AND ts_ms < ? ORDER BY ts_ms LIMIT ?", (e["start_ms"], e["end_ms"], MAX_TIMELINE_CALLS)):
            tools = [n for (n,) in con.execute("SELECT name FROM call_tools WHERE msg_id = ?", (mid,))]
            calls.append(dict(t=ts, p=names.get(proj, proj), s=(sid or "")[:8], sc=side,
                              usd=round(call_usd(model, inp, out, cr, c5, c1, ws), 4),
                              ctx=inp + cr + c5 + c1, w=c5 + c1, o=out, tr=trig, rb=prb, img=img, tools=tools))
        events.append(dict(id=e["id"], kind=e["kind"], start=e["start_ms"], end=e["end_ms"], usd=e["usd"],
                           ratio=e["ratio"], headline=e["headline"], note=e["note"],
                           causes=[{k: c[k] for k in ("label", "fact_text", "interpretation", "advice", "usd", "scope")}
                                   for c in e["causes"]],
                           sessions=e["sessions"][:6], calls=calls))
    blocks = [dict(start=b.start_ms, end=b.end_ms, source=b.source, usd=round(b.usd, 2))
              for b in compute_blocks(con, start - 5 * 3_600_000) if b.end_ms > start]
    return dict(generated=now_ms, start=start, end=now_ms, bin=HALF_HOUR, bins=bins,
                series=[dict(name=n, values=[round(series[n].get(b, 0.0), 4) for b in bins]) for n in keep],
                blocks=blocks, events=events, status=status(con, now_ms))


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
<title>moni_token 사용량</title>
<style>
:root{--bg:#f4f4f2;--surface-1:#fcfcfb;--text-primary:#1a1a19;--text-secondary:#57564f;--muted:#8a897f;--grid:#e4e3dd;
--border:#d9d8d1;--accent:#2a78d6;--critical:#c42b2a;
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--s5:#e87ba4;--s6:#008300;--s7:#4a3aa7}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#121211;--surface-1:#1a1a19;--text-primary:#fff;
--text-secondary:#c3c2b7;--muted:#8f8e85;--grid:#2c2c2a;--border:#3a3a37;--accent:#3987e5;--critical:#e66767;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#008300;--s7:#9085e9}}
:root[data-theme="dark"]{--bg:#121211;--surface-1:#1a1a19;--text-primary:#fff;--text-secondary:#c3c2b7;--muted:#8f8e85;
--grid:#2c2c2a;--border:#3a3a37;--accent:#3987e5;--critical:#e66767;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#008300;--s7:#9085e9}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text-primary);
font:14px/1.5 system-ui,-apple-system,"Segoe UI","Malgun Gothic",sans-serif}
main{max-width:1200px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:32px 0 12px}.sub{color:var(--text-secondary);margin:0 0 20px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
.tile{background:var(--surface-1);border:1px solid var(--border);border-radius:10px;padding:14px 16px}
.tile .k{color:var(--text-secondary);font-size:12px}.tile .v{font-size:24px;font-weight:600;font-variant-numeric:tabular-nums}
.tile .d{color:var(--muted);font-size:12px}
.card{background:var(--surface-1);border:1px solid var(--border);border-radius:10px;padding:16px;margin-top:12px}
.legend{display:flex;flex-wrap:wrap;gap:14px;margin-bottom:8px;color:var(--text-secondary);font-size:12px}
.legend span{display:inline-flex;align-items:center;gap:6px}.sw{width:10px;height:10px;border-radius:2px;display:inline-block}
.legend .blk{width:14px;height:0;border-top:1px dashed var(--muted)}.legend .ev{color:var(--critical)}
#chart{width:100%;overflow:hidden;position:relative}svg text{fill:var(--muted);font-size:11px}
#tip{position:absolute;pointer-events:none;background:var(--surface-1);border:1px solid var(--border);border-radius:8px;
padding:8px 10px;font-size:12px;box-shadow:0 4px 16px rgba(0,0,0,.12);display:none;min-width:160px;z-index:2}
#tip .r{display:flex;justify-content:space-between;gap:12px}#tip .t{color:var(--text-secondary);margin-bottom:4px}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--grid);vertical-align:top}
th{color:var(--text-secondary);font-weight:500;font-size:12px}td.n,th.n{text-align:right}
tr.ev{cursor:pointer}tr.ev:hover{background:var(--grid)}tr.ev:focus{outline:2px solid var(--accent)}
.detail td{background:var(--bg)}.cause{margin:6px 0 10px}.cause b{display:block}
.fact{color:var(--text-primary)}.interp{color:var(--text-secondary)}.adv{color:var(--accent)}
.note{color:var(--muted);font-size:12px}.scroll{overflow-x:auto}
details summary{cursor:pointer;color:var(--text-secondary)}
.tl{font-size:12px}.tl td{padding:3px 6px}
</style></head><body><main>
<h1>Claude Code 사용량 · 급증 원인</h1>
<p class="sub" id="gen"></p>
<div class="tiles" id="tiles"></div>
<h2>사용량 추이 (30분 단위, 단위$ = API 정가 환산)</h2>
<div class="card"><div class="legend" id="legend"></div><div id="chart"><div id="tip"></div></div>
<details><summary>표로 보기 (일별 프로젝트 합계)</summary><div class="scroll" id="dtable"></div></details></div>
<h2>사건</h2>
<div class="card scroll"><table id="events"><thead><tr><th>시각</th><th>종류</th><th class="n">단위$</th>
<th class="n">배수</th><th>주원인</th></tr></thead><tbody></tbody></table>
<p class="note">행을 누르면 원인 근거와 그 구간의 호출 타임라인이 열립니다. 해석은 규칙에 따른 추정입니다. output 토큰은 로그 특성상 과소일 수 있습니다.</p></div>
</main>
<script id="data" type="application/json">/*DATA*/null</script>
<script>
const D=JSON.parse(document.getElementById('data').textContent);
const $=s=>document.querySelector(s), el=(t,a={},h='')=>{const e=document.createElement(t);for(const k in a)e.setAttribute(k,a[k]);e.innerHTML=h;return e};
const pad=n=>String(n).padStart(2,'0');
const fmt=ms=>{const d=new Date(ms);return `${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`};
const hm=ms=>{const d=new Date(ms);return `${pad(d.getHours())}:${pad(d.getMinutes())}`};
const usd=v=>'$'+(v>=100?v.toFixed(0):v.toFixed(2));
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const KIND={rate:'속도 급증',cache_write:'캐시 재기록',manual:'지정 구간'};
$('#gen').textContent=`${fmt(D.start)} ~ ${fmt(D.end)} · 생성 ${fmt(D.generated)} · 로컬 로그만 사용, LLM 호출 없음`;

// tiles
const st=D.status, b=st.block, day=D.events.filter(e=>e.start>=D.end-86400000);
const tiles=[['지금 속도 (15분)',usd(st.rate_usd_per_min*60)+'/h',''],
 ['현재 5시간 구간',b?usd(b.usd):'—',b?`${fmt(b.start_ms)}~${hm(b.end_ms)} · ${b.source==='estimated'?'경계 추정':'경계 확인됨'}`:'활동 없음'],
 ['구간 끝 예상',b?usd(b.projected_usd):'—','현재 속도로 선형 외삽'],
 ['최근 24시간 사건',String(day.length),day.length?day[day.length-1].headline:'']];
for(const [k,v,d] of tiles)$('#tiles').append(el('div',{class:'tile'},`<div class="k">${k}</div><div class="v">${v}</div><div class="d">${esc(d)}</div>`));

// chart: stacked columns per 30 min
const S=D.series, n=D.bins.length, colors=S.map((s,i)=>s.name==='기타'?'var(--muted)':`var(--s${i+1})`);
S.forEach((s,i)=>$('#legend').append(el('span',{},`<i class="sw" style="background:${colors[i]}"></i>${esc(s.name)}`)));
$('#legend').append(el('span',{},'<i class="blk"></i>5시간 구간 경계'),el('span',{class:'ev'},'▼ 사건'));
function draw(){
 const box=$('#chart'), W=box.clientWidth, H=Math.max(220,Math.min(340,W*.35)), m={l:48,r:8,t:18,b:24};
 const iw=W-m.l-m.r, ih=H-m.t-m.b, tot=D.bins.map((_,j)=>S.reduce((a,s)=>a+s.values[j],0));
 const maxv=Math.max(1e-9,...tot), step=niceStep(maxv/4), ymax=Math.ceil(maxv/step)*step;
 const x=t=>m.l+(t-D.start)/(D.end+D.bin-D.start)*iw, y=v=>m.t+ih-v/ymax*ih, bw=Math.max(1,iw/n-1);
 let g='';
 for(let v=0;v<=ymax+1e-9;v+=step)g+=`<line x1="${m.l}" x2="${W-m.r}" y1="${y(v)}" y2="${y(v)}" stroke="var(--grid)"/><text x="${m.l-6}" y="${y(v)+4}" text-anchor="end">${usd(v)}</text>`;
 for(let t=new Date(D.start).setHours(24,0,0,0);t<D.end;t+=86400000)g+=`<text x="${x(t)}" y="${H-6}" text-anchor="middle">${fmt(t).slice(0,5)}</text>`;
 for(const k of D.blocks){if(k.start>=D.start)g+=`<line x1="${x(k.start)}" x2="${x(k.start)}" y1="${m.t}" y2="${m.t+ih}" stroke="var(--muted)" stroke-dasharray="3 3"/>`}
 D.bins.forEach((t,j)=>{let acc=0;S.forEach((s,i)=>{const v=s.values[j];if(v<=0)return;const y0=y(acc),y1=y(acc+v);acc+=v;
  const h=Math.max(0,y0-y1-(acc>v?1:0));if(h>0)g+=`<rect x="${x(t)}" y="${y1}" width="${bw}" height="${h}" fill="${colors[i]}" rx="${bw>6?2:0}"/>`})});
 for(const e of D.events)g+=`<text class="mk" data-id="${e.id}" x="${x(e.start)}" y="${m.t-4}" text-anchor="middle" style="fill:var(--critical);cursor:pointer;font-size:12px">▼</text>`;
 g+=`<rect id="hit" x="${m.l}" y="${m.t}" width="${iw}" height="${ih}" fill="transparent"/><line id="xh" y1="${m.t}" y2="${m.t+ih}" stroke="var(--text-secondary)" visibility="hidden"/>`;
 box.querySelector('svg')?.remove();
 box.insertAdjacentHTML('afterbegin',`<svg width="${W}" height="${H}" role="img" aria-label="30분 단위 프로젝트별 사용량">${g}</svg>`);
 const tip=$('#tip'), xh=box.querySelector('#xh');
 box.querySelector('#hit').onmousemove=ev=>{const r=box.getBoundingClientRect(),px=ev.clientX-r.left;
  const j=Math.min(n-1,Math.max(0,Math.floor((px-m.l)/iw*(D.end+D.bin-D.start)/D.bin)));const t=D.bins[j];
  xh.setAttribute('x1',x(t)+bw/2);xh.setAttribute('x2',x(t)+bw/2);xh.setAttribute('visibility','visible');
  let h=`<div class="t">${fmt(t)}~${hm(t+D.bin)} · 합계 ${usd(tot[j])}</div>`;
  S.forEach((s,i)=>{if(s.values[j]>0)h+=`<div class="r"><span><i class="sw" style="background:${colors[i]}"></i> ${esc(s.name)}</span><span>${usd(s.values[j])}</span></div>`});
  tip.innerHTML=h;tip.style.display='block';tip.style.left=Math.min(px+12,W-180)+'px';tip.style.top='8px'};
 box.querySelector('#hit').onmouseleave=()=>{tip.style.display='none';xh.setAttribute('visibility','hidden')};
 box.querySelectorAll('.mk').forEach(mk=>mk.onclick=()=>openEvent(+mk.dataset.id,true));
}
function niceStep(r){const p=10**Math.floor(Math.log10(r)),f=r/p;return (f<=1?1:f<=2?2:f<=5?5:10)*p}

// daily table
(function(){const days={};D.bins.forEach((t,j)=>{const d=fmt(t).slice(0,5);days[d]=days[d]||S.map(()=>0);S.forEach((s,i)=>days[d][i]+=s.values[j])});
 let h='<table><thead><tr><th>날짜</th>'+S.map(s=>`<th class="n">${esc(s.name)}</th>`).join('')+'<th class="n">합계</th></tr></thead><tbody>';
 for(const d in days)h+=`<tr><td>${d}</td>${days[d].map(v=>`<td class="n">${usd(v)}</td>`).join('')}<td class="n">${usd(days[d].reduce((a,b)=>a+b,0))}</td></tr>`;
 $('#dtable').innerHTML=h+'</tbody></table>'})();

// events
const tb=$('#events tbody');
for(const e of [...D.events].reverse()){
 const tr=el('tr',{class:'ev',tabindex:'0','data-id':e.id},`<td>${fmt(e.start)}~${hm(e.end)}</td><td>${KIND[e.kind]||e.kind}</td>
 <td class="n">${usd(e.usd)}</td><td class="n">${e.ratio?'×'+e.ratio.toFixed(1):'—'}</td><td>${esc(e.causes[0]?.label||'규칙에 맞는 원인 없음')}</td>`);
 tr.onclick=()=>openEvent(e.id);tr.onkeydown=k=>{if(k.key==='Enter')openEvent(e.id)};tb.append(tr)}
function openEvent(id,scroll){
 const tr=tb.querySelector(`tr[data-id="${id}"]`), nx=tr.nextElementSibling;
 if(nx&&nx.classList.contains('detail')){nx.remove();return}
 const e=D.events.find(x=>x.id===id);
 let h=e.causes.map(c=>`<div class="cause"><b>${esc(c.label)} · ${usd(c.usd)}</b><div class="fact">사실: ${esc(c.fact_text)}</div>
 <div class="interp">해석: ${esc(c.interpretation)}</div><div class="adv">권고: ${esc(c.advice)}</div></div>`).join('')||'<p>규칙에 맞는 원인 없음</p>';
 h+='<details open><summary>세션별</summary><table class="tl"><tr><th>세션</th><th>프로젝트</th><th class="n">호출</th><th class="n">단위$</th><th class="n">비중</th></tr>'+
  e.sessions.map(s=>`<tr><td>${esc(s.session_id.slice(0,8))}</td><td>${esc(s.project)}</td><td class="n">${s.calls}</td><td class="n">${usd(s.usd)}</td><td class="n">${Math.round(s.share*100)}%</td></tr>`).join('')+'</table></details>';
 h+=`<details><summary>호출 타임라인 (${e.calls.length}건)</summary><div class="scroll"><table class="tl"><tr><th>시각</th><th>프로젝트</th><th>세션</th><th class="n">컨텍스트</th><th class="n">캐시 쓰기</th><th class="n">output</th><th class="n">단위$</th><th>계기</th><th>도구</th></tr>`+
  e.calls.map(c=>`<tr><td>${hm(c.t)}:${pad(new Date(c.t).getSeconds())}</td><td>${esc(c.p)}${c.sc?' (sub)':''}</td><td>${esc(c.s)}</td><td class="n">${c.ctx.toLocaleString()}</td>
  <td class="n">${c.w?c.w.toLocaleString():''}</td><td class="n">${c.o.toLocaleString()}</td><td class="n">${usd(c.usd)}</td>
  <td>${c.tr==='human'?'사람 입력':c.tr==='tool_result'?'도구 결과'+(c.rb>=100000?` ${Math.round(c.rb/1024)}KB`:'')+(c.img?' 이미지':''):''}</td><td>${esc(c.tools.join(', '))}</td></tr>`).join('')+'</table></div></details>';
 h+=`<p class="note">${esc(e.note||'')}</p>`;
 const d=el('tr',{class:'detail'},`<td colspan="5">${h}</td>`);tr.after(d);if(scroll)tr.scrollIntoView({behavior:'smooth',block:'center'})}
draw();addEventListener('resize',()=>{clearTimeout(window._r);window._r=setTimeout(draw,120)});
const hid=/^#event-(\d+)$/.exec(location.hash);if(hid&&D.events.some(e=>e.id===+hid[1]))openEvent(+hid[1],true);
</script></body></html>"""
