"""Shared look of the usage page and the agent map (issue #5): tokens, app shell, theme toggle, vendored uPlot.

Everything is inlined into the page: no CDN, no web fonts, no requests. uPlot (MIT) is vendored under vendor/uplot.
"""
from functools import cache
from pathlib import Path

VENDOR = Path(__file__).parent / "vendor" / "uplot"

TOKENS = r"""
:root{color-scheme:light;--bg:#f4f4f2;--surface-1:#fcfcfb;--surface-2:#f0efeb;--text-primary:#1a1a19;--text-secondary:#57564f;
--muted:#8a897f;--grid:#e4e3dd;--border:#d9d8d1;--line:#2a78d6;--band:rgba(196,43,42,.10);--critical:#c42b2a;--warn:#b86e00;
--good:#1baf7a;--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--s5:#e87ba4;--s6:#008300;--s7:#4a3aa7;
--mono:ui-monospace,"Cascadia Mono",Consolas,"D2Coding",monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#121211;--surface-1:#1a1a19;
--surface-2:#222220;--text-primary:#f4f4f2;--text-secondary:#c3c2b7;--muted:#8f8e85;--grid:#2c2c2a;--border:#3a3a37;
--line:#3987e5;--band:rgba(230,103,103,.16);--critical:#e66767;--warn:#e0a030;--good:#199e70;--s1:#3987e5;--s2:#d95926;
--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#008300;--s7:#9085e9}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#121211;--surface-1:#1a1a19;--surface-2:#222220;--text-primary:#f4f4f2;
--text-secondary:#c3c2b7;--muted:#8f8e85;--grid:#2c2c2a;--border:#3a3a37;--line:#3987e5;--band:rgba(230,103,103,.16);
--critical:#e66767;--warn:#e0a030;--good:#199e70;--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;
--s6:#008300;--s7:#9085e9}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--text-primary)}
body{font:13px/1.5 system-ui,-apple-system,"Segoe UI","Malgun Gothic",sans-serif;font-variant-numeric:tabular-nums}
.mono,.path,code{font-family:var(--mono);font-size:12px}
.shell{position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:16px;flex-wrap:wrap;padding:8px 16px;
background:var(--surface-1);border-bottom:1px solid var(--border)}
.shell .brand{font-weight:700;letter-spacing:.2px}.shell nav{display:flex;gap:2px}
.shell nav a{padding:4px 10px;border-radius:6px;color:var(--text-secondary);text-decoration:none}
.shell nav a[aria-current=page]{background:var(--surface-2);color:var(--text-primary);font-weight:600}
.shell .lim{display:flex;gap:14px;color:var(--text-secondary)}.shell .lim b{color:var(--text-primary)}
.shell .warn{color:var(--critical);font-weight:600}.shell .sp{flex:1}
.shell .meta{color:var(--muted);font-size:12px}
.shell button{font:inherit;font-size:12px;padding:2px 8px;border-radius:6px;border:1px solid var(--border);
background:transparent;color:var(--text-secondary);cursor:pointer}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:5px 8px;vertical-align:top}
th{color:var(--text-secondary);font-weight:500;font-size:12px;border-bottom:1px solid var(--border)}
tbody tr:nth-child(even) td{background:var(--surface-2)}td.n,th.n{text-align:right}
.badge{font-size:11px;padding:0 6px;border-radius:4px;border:1px solid var(--border);color:var(--text-secondary)}
.u-legend{font-size:12px;color:var(--text-secondary)}.u-legend .u-marker{border-radius:2px}
.u-select{background:color-mix(in srgb,var(--line) 15%,transparent)}
"""

SHELL_JS = r"""
(function(){const k='moni_token.theme',r=document.documentElement;
 try{const t=localStorage.getItem(k);if(t)r.dataset.theme=t}catch(e){}
 window.toggleTheme=function(){const dark=r.dataset.theme?r.dataset.theme==='dark':matchMedia('(prefers-color-scheme: dark)').matches;
  r.dataset.theme=dark?'light':'dark';try{localStorage.setItem(k,r.dataset.theme)}catch(e){};
  window.dispatchEvent(new Event('themechange'))};
 window.cssVar=n=>getComputedStyle(r).getPropertyValue(n).trim();
 window.shellLimits=function(H){const pc=v=>v==null?'—':(v<10?v.toFixed(1):Math.round(v))+'%',
  hm=ms=>new Date(ms).toTimeString().slice(0,5),one=(k,x)=>x&&x.pct!=null?`<span>${k} <b>${pc(x.pct)}</b>${x.basis==='estimated'?' <span class="badge">추정</span>':''}${x.reset?' · 초기화 '+hm(x.reset):''}</span>`:'';
  const e=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  return one('5시간',H&&H.five_hour)+one('주간',H&&H.seven_day)+(H&&H.warning?`<span class="warn">${e(H.warning)}</span>`:'')}})();
"""


def shell_html(current: str, base: str = "") -> str:
    """Top bar: brand, the two pages, limit strip (filled by JS), meta, theme toggle."""
    tab = lambda key, href, label: f'<a href="{href}"{" aria-current=page" if key == current else ""}>{label}</a>'
    return (f'<header class="shell"><span class="brand">moni_token</span><nav>{tab("map", base + "/", "에이전트 지도")}'
            f'{tab("usage", base + "/usage", "사용량")}{tab("accuracy", base + "/accuracy", "예측 정확도")}</nav><span class="lim" id="lim"></span><span class="sp"></span>'
            f'<span class="meta" id="meta"></span><button type="button" onclick="toggleTheme()" '
            f'aria-label="테마 전환">◐ 테마</button></header>')


@cache
def uplot_js() -> str:
    return (VENDOR / "uPlot.iife.min.js").read_text(encoding="utf-8").replace("</script", "<\\/script")


@cache
def uplot_css() -> str:
    return (VENDOR / "uPlot.min.css").read_text(encoding="utf-8")


def head(title: str, extra_css: str = "") -> str:
    return (f'<!doctype html>\n<html lang="ko"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>'
            f'<style>{uplot_css()}{TOKENS}{extra_css}</style><script>{SHELL_JS}</script></head>')
