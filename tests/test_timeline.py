import json
import threading
from datetime import datetime
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

from moni_token import mapserver
from moni_token.agentmap import axis_ticks


def _ms(*a) -> int:
    return int(datetime(*a).timestamp() * 1000)


def _hm(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%H:%M:%S")


def test_ticks_sit_on_round_local_clock_times_inside_the_window():
    now = _ms(2026, 11, 13, 8, 37, 41)
    t = axis_ticks(now - 30 * 60_000, now, 30)
    assert [_hm(x) for x in t] == ["08:10:00", "08:15:00", "08:20:00", "08:25:00", "08:30:00", "08:35:00"]
    t2 = axis_ticks(now - 120 * 60_000, now, 120)
    assert [_hm(x) for x in t2] == ["06:45:00", "07:00:00", "07:15:00", "07:30:00", "07:45:00", "08:00:00",
                                    "08:15:00", "08:30:00"]


def test_ticks_do_not_move_between_polls_only_the_window_does():
    a = _ms(2026, 11, 13, 8, 37, 41)
    first = axis_ticks(a - 15 * 60_000, a, 15)
    later = axis_ticks(a + 3000 - 15 * 60_000, a + 3000, 15)
    assert first == later                      # same clock times -> they slide left with the marks
    on_edge = _ms(2026, 11, 13, 8, 40, 0)
    assert axis_ticks(on_edge - 15 * 60_000, on_edge, 15)[-1] == on_edge   # a tick exactly at "now" is kept


def test_page_has_pause_control_and_state_serves_a_frozen_moment(logs, tmp_path):
    """Issue #11: pause/resume, space bar, link to this moment, 1-minute steps; paused fetches reuse ?at=."""
    html = mapserver.page()
    for s in ('id="pause"', "⏸ 일시정지", "▶ 실시간", "일시정지 · ", "이 시점 링크", 'id="back"', 'id="fwd"',
              "e.code!=='Space'", "if(frozen==null)tick()", "q.set('at',at)"):
        assert s in html, s
    srv = ThreadingHTTPServer((mapserver.HOST, 0), mapserver.make_handler(tmp_path / "m.db", logs.root))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        at = _ms(2026, 11, 13, 8, 37, 41)
        c = HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        c.request("GET", f"/api/state?at={at}&window=15")
        r = c.getresponse()
        st = json.loads(r.read())
        assert r.status == 200 and st["now"] == at and st["window_min"] == 15
        assert [_hm(x) for x in st["ticks"]] == ["08:25:00", "08:30:00", "08:35:00"]
    finally:
        srv.shutdown()
        srv.server_close()

def test_lane_labels_are_fitted_by_measured_width_not_cut_by_characters():
    """Issue #13: labels are measured in the real font, cut with an ellipsis to fit the label column, clipped,
    and carry the full text in a <title>; L follows the widest label within [160px, 30% of the width]."""
    html = mapserver.page()
    for s in ("getComputedTextLength()", "fitLabel", "'…'", "Math.max(160,Math.min(W*0.3,",
              '<clipPath id="lblclip">', 'clip-path="url(#lblclip)"', "<title>${esc(full[i])}</title>",
              "x=t=>L+(t-t0)/(t1-t0)*(W-L-R)"):
        assert s in html, s
    assert ".slice(0,32)" not in html
    assert "L=230" not in html
