"""Collect ordered measurements and render a dependency-free HTML report."""

from datetime import datetime, timezone
from html import escape
import platform
import threading
from time import perf_counter_ns


class ScenarioTrace:
    """Serialize event timestamps; the first event defines this run's origin."""

    def __init__(self, scenario_id, label):
        self.scenario_id = scenario_id
        self.label = label
        self._lock = threading.Lock()
        self._origin_ns = None
        self._events = []

    def record(self, kind, index=None, active_calls=0):
        with self._lock:
            now = perf_counter_ns()
            if self._origin_ns is None:
                self._origin_ns = now
            self._events.append({
                "sequence": len(self._events) + 1,
                "elapsed_ns": now - self._origin_ns,
                "kind": kind,
                "call": None if index is None else index + 1,
                "active_calls": active_calls,
            })

    def snapshot(self, peak):
        with self._lock:
            return {
                "id": self.scenario_id,
                "label": self.label,
                "peak_active_calls": peak,
                "events": [dict(event) for event in self._events],
            }


def build_trace(scenarios):
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runtime": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
            "system": platform.system(),
        },
        "clock": "time.perf_counter_ns",
        "time_unit": "ns",
        "scenarios": scenarios,
    }


_EVENT_LABELS = {
    "scenario_started": "Scenario started",
    "awaiter_started": "Awaiter started",
    "worker_started": "Blocking call started",
    "cancellation_requested": "Cancellation requested",
    "awaiter_cancelled": "Awaiter cancelled",
    "workers_released": "Release gate opened",
    "worker_finished": "Blocking call finished",
    "awaiter_completed": "Awaiter completed",
    "scenario_finished": "Scenario finished",
}


def _text(value):
    return escape(str(value), quote=True)


def _ms(nanoseconds):
    return f"{nanoseconds / 1_000_000:.3f}"


def _event_time(scenario, kind, call):
    return next(
        event["elapsed_ns"] for event in scenario["events"]
        if event["kind"] == kind and event["call"] == call
    )


def _bar(start, end, maximum, style, label):
    return (
        f'<span class="bar {style}" style="left:{100 * start / maximum:.6f}%;'
        f'width:{100 * (end - start) / maximum:.6f}%" '
        f'title="{_text(label)}"></span>'
    )


def _scenario_html(scenario, number, maximum):
    cancelled = _event_time(scenario, "awaiter_cancelled", 1)
    worker_end = _event_time(scenario, "worker_finished", 1)
    continued = worker_end - cancelled
    rows = []
    descriptions = []
    for call in (1, 2):
        await_start = _event_time(scenario, "awaiter_started", call)
        await_end = _event_time(
            scenario, "awaiter_cancelled" if call == 1 else "awaiter_completed", call
        )
        work_start = _event_time(scenario, "worker_started", call)
        work_end = _event_time(scenario, "worker_finished", call)
        status = "cancelled" if call == 1 else "completed"
        for lane, start, end, bar_style in (
            (f"Call {call} awaiter", await_start, await_end, "awaiting"),
            (f"Call {call} worker", work_start, work_end, "working"),
        ):
            title = f"{lane}: {_ms(start)}–{_ms(end)} ms"
            bars = _bar(start, end, maximum, bar_style, title)
            if call == 1 and bar_style == "working":
                bars += _bar(cancelled, end, maximum, "continued", "Still running after cancellation")
            suffix = status if bar_style == "awaiting" else "blocking call"
            rows.append(
                f'<div class="lane"><div class="lane-label">{lane}<small>{suffix}</small></div>'
                f'<div class="track">{bars}</div></div>'
            )
        descriptions.append(
            f"Call {call}'s awaiter {status} at {_ms(await_end)} ms; "
            f"its blocking call ran from {_ms(work_start)} to {_ms(work_end)} ms."
        )
    event_rows = "".join(
        f'<tr><td>{_text(event["sequence"])}</td><td>{event["elapsed_ns"] / 1_000_000:.6f}</td>'
        f'<td>{_text(_EVENT_LABELS.get(event["kind"], event["kind"]))}</td>'
        f'<td>{_text(event["call"]) if event["call"] is not None else "—"}</td>'
        f'<td>{_text(event["active_calls"])}</td></tr>'
        for event in scenario["events"]
    )
    semaphore = scenario["id"] == "semaphore"
    explanation = (
        "Call 2 starts while call 1 is still running. The cancelled awaiter has released the permit."
        if semaphore else
        "Call 2 waits for the occupied worker. It starts after call 1's blocking function finishes."
    )
    peak = scenario["peak_active_calls"]
    label = _text(scenario["label"])
    chart_description = _text(
        f"Measured timeline for {scenario['label']}. " + " ".join(descriptions)
        + f" Peak active blocking calls: {peak}."
    )
    return f"""
    <section class="scenario" aria-labelledby="scenario-{number}">
      <div class="scenario-top"><span class="eyebrow">Scenario {number:02d}</span>
        <span class="peak {'overlap' if semaphore else 'bounded'}"><strong>{_text(peak)}</strong> peak active calls</span></div>
      <h2 id="scenario-{number}">{label}</h2>
      <p class="scenario-description">{explanation}</p>
      <div class="timeline" role="img" aria-label="{chart_description}">
        <div class="axis" aria-hidden="true"><span>0</span><span>{_ms(maximum / 2)}</span><span>{_ms(maximum)} ms</span></div>
        <div class="lanes" aria-hidden="true">{''.join(rows)}</div>
      </div>
      <div class="finding"><span class="finding-mark" aria-hidden="true">↳</span><p>Call 1 kept running for
        <strong>{_ms(continued)} ms</strong> after its awaiter was cancelled.</p></div>
      <details><summary>Inspect {len(scenario['events'])} recorded events</summary>
        <div class="event-scroll" tabindex="0" role="region" aria-label="Scrollable event ledger for {label}">
          <table><caption>{label}: measured event ledger</caption>
            <thead><tr><th scope="col">#</th><th scope="col">Time (ms)</th><th scope="col">Event</th><th scope="col">Call</th><th scope="col">Active</th></tr></thead>
            <tbody>{event_rows}</tbody>
          </table>
        </div>
      </details>
    </section>"""


_STYLES = """
:root{color-scheme:light;--ink:#142f3a;--muted:#52636b;--paper:#f6f3ec;--line:#d8ddd7;--await:#387c75;--work:#254c69;--after:#b85723}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.55 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.shell{max-width:1320px;margin:auto;padding:36px 40px 28px}.masthead{display:flex;gap:20px;align-items:center;justify-content:space-between;padding-bottom:25px;border-bottom:1px solid var(--line)}
.brand{font-weight:750;letter-spacing:-.02em}.brand span{color:var(--after);padding-right:10px}.run-tag{font:12px/1.4 ui-monospace,SFMono-Regular,Consolas,monospace;color:var(--muted);text-align:right}
header{max-width:920px;padding:50px 0 26px}.eyebrow{text-transform:uppercase;letter-spacing:.13em;font-size:11px;font-weight:750;color:var(--muted)}
h1{font-size:clamp(32px,4.4vw,56px);line-height:1.1;letter-spacing:-.04em;margin:14px 0 20px;max-width:820px;font-weight:750}h1 em{font-style:normal;color:var(--after)}
.intro{font-size:18px;line-height:1.6;max-width:750px;color:var(--muted);margin:0}.reading-guide{display:flex;flex-wrap:wrap;gap:12px 24px;align-items:center;margin:8px 0 22px;font-size:12px;color:var(--muted)}
.key{display:inline-flex;gap:8px;align-items:center}.swatch{width:20px;height:8px;border-radius:2px;background:var(--work)}.swatch.awaiting{background:var(--await)}.swatch.continued{background:var(--after)}
.comparison{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:22px}.scenario{background:#fffefa;border:1px solid var(--line);border-radius:12px;padding:25px 24px 0;overflow:hidden;min-width:0}
.scenario-top{display:flex;align-items:center;justify-content:space-between;gap:12px}.peak{font-size:11px;border-radius:5px;padding:5px 8px;white-space:nowrap}.peak strong{font-size:16px;padding-right:4px}.overlap{background:#fbebdf;color:#8d431d}.bounded{background:#e8f2eb;color:#225f4e}
h2{font-size:22px;line-height:1.3;letter-spacing:-.035em;margin:22px 0 10px;overflow-wrap:anywhere}.scenario-description{color:var(--muted);font-size:14px;margin:0;min-height:66px;max-width:420px}
.timeline{padding:12px 0 16px}.axis{margin-left:111px;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));font:10px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace;color:var(--muted);padding-bottom:9px;white-space:nowrap}.axis span:nth-child(2){text-align:center}.axis span:last-child{text-align:right}
.lane{display:grid;grid-template-columns:111px minmax(0,1fr);min-height:53px}.lane-label{font-size:12px;font-weight:650;padding-top:9px;line-height:1.4}.lane-label small{display:block;font-weight:400;color:var(--muted);font-size:11px}
.track{position:relative;background:linear-gradient(to right,var(--line) 1px,transparent 1px) left/50% 100%;border-right:1px solid var(--line);border-bottom:1px dashed #e4e6df;min-width:0}.bar{position:absolute;top:16px;height:14px;border-radius:2px;background:var(--work)}.bar.awaiting{background:var(--await)}.bar.continued{background:var(--after);border-radius:0 2px 2px 0}
.finding{display:flex;gap:11px;align-items:flex-start;border-top:1px solid var(--line);padding:17px 0 20px}.finding-mark{font-size:25px;color:var(--after);line-height:1.25}.finding p{margin:0;color:var(--muted);font-size:13px}.finding strong{color:var(--ink);white-space:nowrap;font-variant-numeric:tabular-nums}
details{border-top:1px solid var(--line);margin:0 -24px}summary{cursor:pointer;padding:16px 24px;font-size:12px;font-weight:650}summary:hover{background:#f3f4ee}summary:focus-visible,.event-scroll:focus-visible{outline:3px solid var(--after);outline-offset:-3px}
.event-scroll{overflow-x:auto;padding:0 12px 12px}table{width:100%;border-collapse:collapse;text-align:left;font-size:12px;white-space:nowrap;font-variant-numeric:tabular-nums}caption{text-align:left;font-size:12px;font-weight:650;padding:4px 8px 10px}th,td{padding:8px;border-top:1px solid #e5e7e1}th{color:var(--muted);font-weight:650}td:nth-child(2){font-family:ui-monospace,SFMono-Regular,Consolas,monospace}
.method{margin-top:26px;display:grid;grid-template-columns:1fr 1fr;gap:34px;padding:24px 0 30px;border-bottom:1px solid var(--line)}.method h2{font-size:14px;letter-spacing:0;margin:0 0 9px}.method p{color:var(--muted);font-size:13px;margin:0;max-width:540px}code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:.93em}footer{display:flex;flex-wrap:wrap;justify-content:space-between;gap:10px;font-size:11px;color:var(--muted);padding-top:20px}footer time{overflow-wrap:anywhere}
@media(max-width:980px){.comparison{grid-template-columns:1fr}.scenario-description{min-height:0;max-width:none}.timeline{margin-top:10px}.shell{max-width:760px;padding:28px 24px}header{padding-top:35px}.method{gap:24px}}
@media(max-width:540px){.shell{padding:20px 16px}.masthead{align-items:flex-start;gap:12px;padding-bottom:20px}.brand{font-size:13px}.run-tag{font-size:10px;max-width:145px}header{padding-top:28px}.intro{font-size:15px}.reading-guide{gap:9px 14px;font-size:11px}.scenario{padding:20px 16px 0}.scenario-top{gap:8px}.eyebrow{font-size:10px}.peak{font-size:10px;padding:3px 6px}h2{font-size:20px}.lane{grid-template-columns:91px minmax(0,1fr)}.axis{margin-left:91px}.lane-label{font-size:11px}.lane-label small{font-size:10px}.method{grid-template-columns:1fr;gap:22px}details{margin:0 -16px}summary{padding:15px 16px}.finding p{font-size:12px}}
@media print{body{background:white}.shell{max-width:none;padding:0}header{padding-top:25px}h1{font-size:34px}.scenario{break-inside:avoid}.comparison{grid-template-columns:1fr 1fr;gap:12px}.run-tag{font-size:10px}.method{margin-top:15px}details{display:none}}
"""


def render_html(trace):
    """Render a completed experiment; all trace text is escaped as HTML."""
    maximum = max(event["elapsed_ns"] for scenario in trace["scenarios"] for event in scenario["events"])
    maximum = max(maximum, 1)
    runtime = trace["runtime"]
    runtime_label = _text(f"{runtime['implementation']} {runtime['version']} · {runtime['system']}")
    scenarios = "".join(
        _scenario_html(scenario, number, maximum)
        for number, scenario in enumerate(trace["scenarios"], 1)
    )
    generated = _text(trace["generated_at"])
    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cancellation &amp; worker lifetime · Asyncio lab</title><style>{_STYLES}</style></head>
<body><main class="shell">
  <div class="masthead"><div class="brand"><span aria-hidden="true">◈</span> Asyncio / thread timeout lab</div>
    <div class="run-tag">MEASURED RUN<br>{runtime_label}</div></div>
  <header><div class="eyebrow">Cancellation, observed</div>
    <h1>The awaiter stops.<br><em>The worker keeps going.</em></h1>
    <p class="intro">A semaphore follows the awaiting coroutine. A shared executor limits concurrent blocking calls.
    These two experiments show where their lifetimes separate.</p></header>
  <div class="reading-guide" aria-label="Timeline legend">
    <span class="key"><span class="swatch awaiting" aria-hidden="true"></span>Awaiting</span>
    <span class="key"><span class="swatch" aria-hidden="true"></span>Blocking call</span>
    <span class="key"><span class="swatch continued" aria-hidden="true"></span>Running after cancellation</span>
  </div>
  <div class="comparison">{scenarios}</div>
  <section class="method" aria-label="How to read this experiment">
    <div><h2>Real events, a shared scale</h2><p>Each scenario runs separately, starting at zero on the same millisecond scale.
    Bar positions come from <code>time.perf_counter_ns()</code>. The ledgers show the recorded nanoseconds as milliseconds.
    Durations include scheduling and tracing overhead; this is a lifecycle demonstration, not a benchmark.</p></div>
    <div><h2>What the worker lane means</h2><p>The lane shows a blocking function's execution, not the lifetime of its executor thread.
    A start signal precedes cancellation. A release gate holds the work open, and finish signals let the demo drain it.
    Cancelling the awaiter does not stop a running blocking function.</p></div>
  </section>
  <footer><span>Event coordinated · Python standard library · No external assets</span>
    <span>Recorded <time datetime="{generated}">{generated}</time></span></footer>
</main></body></html>
"""
