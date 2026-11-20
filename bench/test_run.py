import run


def _row(version, session_id, started_at, latency_ms):
    return {"version": version, "session_id": session_id, "started_at": started_at, "latency_ms": latency_ms}


def test_attach_matches_spans_in_time_order_per_session():
    rows = [
        _row("V1", "cold-1", "t1", 3000.0),
        _row("V2", "warm", "t3", 900.0),
        _row("V2", "warm", "t2", 2500.0),  # earlier call in the same session, listed out of order
    ]
    spans = {("V1", "cold-1"): [2000.0], ("V2", "warm"): [2400.0, 800.0]}
    missing = run.attach_server_times(rows, lambda v, s: spans[(v, s)])
    assert missing == 0
    assert rows[0]["server_ms"] == 2000.0 and rows[0]["overhead_ms"] == 1000.0
    by_time = sorted(rows[1:], key=lambda r: r["started_at"])
    assert [r["server_ms"] for r in by_time] == [2400.0, 800.0]


def test_attach_leaves_blank_when_span_count_differs():
    rows = [_row("V1", "s", "t1", 1000.0), _row("V1", "s", "t2", 1000.0)]
    assert run.attach_server_times(rows, lambda v, s: [500.0]) == 2
    assert rows[0]["server_ms"] == "" and rows[1]["overhead_ms"] == ""


def test_stats():
    s = run.stats([float(x) for x in range(1, 11)])
    assert (s["n"], s["min"], s["p50"], s["max"], s["mean"]) == (10, 1.0, 5.5, 10.0, 5.5)
    assert round(s["p90"], 1) == 9.1
    assert run.stats([]) is None
