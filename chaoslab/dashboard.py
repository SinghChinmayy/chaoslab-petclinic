"""Build a read-only, standalone dashboard from saved local evidence."""

import html
import json
from datetime import datetime, timezone
from pathlib import Path

from .settings import MAP_FILE, REPORTS_DIR, ROOT, RUNS_DIR

DEMO_FILE = ROOT / "demo" / "visits-failure.json"
DASHBOARD_FILE = REPORTS_DIR / "dashboard.html"


def _h(value) -> str:
    return html.escape(str(value), quote=True)


def _pct(value) -> str:
    return "—" if value is None else f"{value:.1%}"


def _ms(value) -> str:
    return "—" if value is None else f"{value:,.0f} ms"


def _count_missing(observation: dict | None) -> int | None:
    if observation is None:
        return None
    if "missing_visit_count" in observation:
        return observation["missing_visit_count"]
    ids = observation.get("missing_visit_ids")
    return len(ids) if ids is not None else None


def _visit_count(baseline: dict) -> int | None:
    owner = baseline.get("owner_details") or {}
    if "visit_count" in owner:
        return owner["visit_count"]
    ids = owner.get("visit_ids")
    return len(ids) if ids is not None else None


def _recorded_at(run: dict) -> str:
    provenance = run.get("demo_provenance") or {}
    if provenance.get("recorded_date"):
        return provenance["recorded_date"]
    identifier = run.get("id", "")
    try:
        return datetime.strptime(identifier[:8], "%Y%m%d").date().isoformat()
    except ValueError:
        return "date unknown"


def select_run(runs_dir: Path = RUNS_DIR, demo_path: Path = DEMO_FILE,
               use_demo: bool = False) -> tuple[dict, str]:
    """Prefer the newest finished local run; retain a recorded example for clones."""
    if not use_demo:
        for path in sorted(runs_dir.glob("*/result.json"), reverse=True):
            try:
                candidate = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if (candidate.get("timeline") and candidate.get("outcome") not in (None, "refused")
                    and not candidate.get("recovery_pending")):
                return candidate, "local run"
    return json.loads(demo_path.read_text()), "recorded demo"


def _chart(samples: list[dict], key: str, title: str, color: str, unit: str) -> str:
    values = [(float(s["elapsed"]), float(s[key]), s.get("phase", ""))
              for s in samples if s.get(key) is not None]
    if not values:
        return f'<div class="chart empty">No { _h(title).lower() } samples</div>'
    width, height, left, top, bottom = 720, 206, 44, 22, 169
    max_x = max(x for x, _, _ in values) or 1
    max_y = max(y for _, y, _ in values) or 1
    plot_w, plot_h = width - left - 18, bottom - top
    x_at = lambda x: left + x / max_x * plot_w
    y_at = lambda y: bottom - y / max_y * plot_h
    segments = []
    phase_first = {}
    for x, y, phase in values:
        phase_first.setdefault(phase, x)
        segments.append(f"{x_at(x):.1f},{y_at(y):.1f}")
    boundaries = [phase_first[phase] for phase in ("fault", "recovery") if phase in phase_first]
    backgrounds = ""
    starts = [0, *boundaries]
    ends = [*boundaries, max_x]
    names = ["baseline", "fault", "recovery"][:len(starts)]
    for start, end, name in zip(starts, ends, names):
        backgrounds += (f'<rect class="phase-{name}" x="{x_at(start):.1f}" y="{top}" '
                        f'width="{max(0, x_at(end)-x_at(start)):.1f}" height="{plot_h}"/>')
    line = " ".join(segments)
    label = f"{title} across baseline, fault, and recovery; peak {max_y:,.1f} {unit}"
    return (f'<div class="chart"><svg viewBox="0 0 {width} {height}" role="img" '
            f'aria-label="{_h(label)}" preserveAspectRatio="xMidYMid meet">'
            f'{backgrounds}<line class="axis" x1="{left}" y1="{bottom}" x2="{width-18}" y2="{bottom}"/>'
            f'<line class="axis" x1="{left}" y1="{top}" x2="{left}" y2="{bottom}"/>'
            f'<polyline fill="none" stroke="{color}" stroke-width="3" '
            f'stroke-linejoin="round" stroke-linecap="round" points="{line}"/>'
            f'<text x="{left-7}" y="{top+4}" text-anchor="end">{max_y:,.0f}</text>'
            f'<text x="{left-7}" y="{bottom+4}" text-anchor="end">0</text>'
            f'<text x="{left}" y="{height-8}">0s</text>'
            f'<text x="{width-18}" y="{height-8}" text-anchor="end">{max_x:.0f}s</text>'
            '</svg></div>')


def _source_rows(snapshot: dict) -> str:
    rows = []
    for name, source in snapshot.get("source_state", {}).items():
        at = source.get("observed_at", "")
        ttl = source.get("ttl_seconds") or 0
        rows.append('<tr>'
                    f'<th scope="row">{_h(name)}</th>'
                    f'<td>{_h(source.get("detail", source.get("status", "unknown")))}</td>'
                    f'<td><time datetime="{_h(at)}">{_h(at[:16].replace("T", " "))} UTC</time></td>'
                    f'<td><span class="freshness" data-at="{_h(at)}" data-ttl="{ttl}">'
                    'Historical snapshot</span></td></tr>')
    return "".join(rows) or '<tr><td colspan="4">No runtime sources were recorded.</td></tr>'


def _comparison_rows(run: dict) -> str:
    rows = []
    for feature, comparison in sorted((run.get("prediction_comparison") or {}).items()):
        match = comparison.get("match", "unobserved")
        rows.append('<tr>'
                    f'<th scope="row">{_h(feature)}</th>'
                    f'<td>{_h(comparison.get("predicted", "unknown"))}</td>'
                    f'<td>{_h(comparison.get("observed", "unobserved"))}</td>'
                    f'<td><span class="pill {"good" if match == "matched" else "warn"}">{_h(match)}</span></td>'
                    '</tr>')
    return "".join(rows) or '<tr><td colspan="4">No prediction comparison was recorded.</td></tr>'


def _action_rows(run: dict) -> str:
    rows = []
    for action, impact in sorted((run.get("impact_by_action") or {}).items()):
        rows.append('<tr>'
                    f'<th scope="row">{_h(action)}</th>'
                    f'<td>{_pct(impact.get("baseline_error_rate"))}</td>'
                    f'<td>{_pct(impact.get("fault_error_rate"))}</td>'
                    f'<td>{"Yes" if impact.get("uses_target") else "No" if impact.get("uses_target") is False else "Unknown"}</td>'
                    '</tr>')
    return "".join(rows) or '<tr><td colspan="4">No action metrics were recorded.</td></tr>'


def render(snapshot: dict, run: dict, source: str) -> str:
    baseline = run.get("baseline") or {}
    fault = run.get("fault") or {}
    observations = run.get("semantic_observations") or []
    fault_probe = next((o for o in observations if o.get("phase") == "fault"), None)
    recovery_probe = next((o for o in reversed(observations) if o.get("phase") == "recovery"), None)
    baseline_visits = _visit_count(baseline)
    fault_missing = _count_missing(fault_probe)
    recovery_missing = _count_missing(recovery_probe)
    incomplete_200 = bool(fault_probe and fault_probe.get("status") == 200
                          and fault_missing is not None and fault_missing > 0)
    recorded_at = _recorded_at(run)
    current_id = snapshot.get("snapshot_id")
    run_id = (run.get("prediction") or {}).get("snapshot_id")
    snapshot_mismatch = bool(current_id and run_id and current_id != run_id)
    outcome = run.get("outcome", "unknown")
    recovered = run.get("recovered") is True
    data_loss = run.get("data_loss") or None
    outcome_label = ("Aborted by watchdog" if outcome == "aborted"
                     and (run.get("abort_reason") or "").startswith("watchdog:")
                     else outcome.replace("_", " ").title())
    recovery_label = ("Recovered with data loss" if recovered and data_loss else
                      "Recovery confirmed" if recovered else "Recovery not confirmed")
    notes = ["Static export · no live polling or fault controls"]
    if source == "recorded demo":
        notes.insert(0, "Recorded demo data")
    if snapshot_mismatch:
        notes.append("Run prediction used an earlier dependency snapshot")
    provenance = run.get("demo_provenance") or {}
    provenance_note = provenance.get("note", "Results loaded from a local saved experiment.")
    diagnosis = (f'HTTP {_h(fault_probe.get("status"))} with {fault_missing} missing baseline visits'
                 if fault_probe and fault_missing is not None else 'No semantic fault probe recorded')
    recovery_text = (f'{recovery_missing} baseline visits still missing in the final recovery probe'
                     if recovery_missing is not None else 'No final correctness probe recorded')
    hero_title = "When HTTP 200 still means failure." if incomplete_200 else "What changed when the service failed?"
    hero_description = ("A controlled Visits-service crash exposed incomplete owner data behind a "
                        "successful Gateway response. This report pairs dependency predictions "
                        "with recorded traffic, correctness probes, and automatic safety action."
                        if incomplete_200 and fault.get("target") == "visits-service" else
                        f"A controlled {fault.get('target', 'service')} {fault.get('kind', 'fault')} "
                        "experiment pairs dependency predictions with recorded traffic, "
                        "correctness probes, and safety action.")
    finding_title = ("The status code hid missing data" if incomplete_200
                     else "User-facing correctness under fault")
    finding_description = ("Owner details require Customers and Visits. The Visits circuit breaker "
                           "can preserve the response status while returning incomplete records."
                           if incomplete_200 else
                           "Owner details require Customers and Visits. The probe checks visit "
                           "completeness alongside status and latency.")
    recovery_explanation = ("Service traffic recovered, but pre-fault visit data was lost."
                            if data_loss else
                            "HTTP errors and latency alone would have missed this state."
                            if incomplete_200 and recovery_missing else
                            "Recovery assessment includes traffic and owner-data correctness.")
    experiment_title = ("Watchdog stopped the fault" if outcome_label == "Aborted by watchdog"
                        else "Fault outcome and user impact")
    experiment_note = ("The experiment did not claim complete recovery."
                       if not recovered else "The service recovered with data loss."
                       if data_loss else "The experiment confirmed recovery.")
    metadata = " · ".join(notes)
    snapshot_note = (f'Run snapshot {_h(run_id)} differs from displayed map {_h(current_id)}. '
                     'Predictions below are the ones captured with the run.' if snapshot_mismatch
                     else 'The run and displayed dependency map share a snapshot.')
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    owner_path = ('<div class="path"><div><small>ENTRY</small><strong>API Gateway</strong><span>GET /api/gateway/owners/{ownerId}</span></div>'
                  '<b aria-hidden="true">→</b><div><small>REQUIRED</small><strong>Customers</strong><span>Owner and pets</span></div>'
                  '<b aria-hidden="true">→</b><div class="path-risk"><small>PARTIAL FALLBACK</small><strong>Visits</strong><span>Missing records can still return 200</span></div></div>')
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<meta name="description" content="ChaosLab recorded resilience experiment and dependency dashboard">
<title>ChaosLab · Resilience dashboard</title>
<style>
:root{{--ink:#e9f0f4;--muted:#9badb9;--bg:#08121b;--panel:#101f2a;--line:#29404e;--cyan:#55d7d2;--orange:#ffb66b;--red:#ff777d;--green:#83dcac}}
*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
a{{color:var(--cyan)}}a:focus-visible{{outline:2px solid var(--cyan);outline-offset:4px}}.shell{{max-width:1280px;margin:auto;padding:0 36px 70px}}
.top{{display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--line);padding:20px 0;gap:20px}}.brand{{font-size:21px;font-weight:800;letter-spacing:.02em;color:var(--ink);text-decoration:none}}.brand span{{color:var(--cyan)}}nav{{display:flex;gap:25px;flex-wrap:wrap}}nav a{{font-size:13px;color:var(--muted);text-decoration:none}}nav a:hover{{color:var(--ink)}}
.eyebrow{{color:var(--cyan);text-transform:uppercase;letter-spacing:.17em;font-size:11px;font-weight:800}}h1{{font-size:clamp(35px,5vw,65px);line-height:1.07;letter-spacing:-.045em;margin:12px 0 17px;max-width:880px}}h2{{font-size:27px;line-height:1.2;letter-spacing:-.02em;margin:0}}h3{{font-size:18px;margin:0 0 9px}}p{{margin:0}}.muted{{color:var(--muted)}}.hero{{padding:55px 0 35px}}.hero p{{max-width:760px;color:var(--muted);font-size:17px}}.meta{{margin-top:24px;display:flex;flex-wrap:wrap;gap:9px}}.tag,.pill{{display:inline-flex;align-items:center;border:1px solid var(--line);border-radius:100px;padding:5px 11px;font-size:12px;font-weight:700;color:var(--muted)}}.tag.warn,.pill.warn{{color:var(--orange);border-color:#79522c;background:#2e241b}}.pill.good{{color:var(--green);border-color:#345c4b;background:#172c25}}.tag.danger{{color:var(--red);border-color:#743c46;background:#321f27}}
.section{{margin-top:54px;scroll-margin-top:20px}}.section-head{{display:flex;align-items:end;justify-content:space-between;gap:25px;margin-bottom:18px}}.section-head p{{color:var(--muted);max-width:600px}}.grid{{display:grid;gap:16px}}.metrics{{grid-template-columns:repeat(4,minmax(0,1fr))}}.two{{grid-template-columns:repeat(2,minmax(0,1fr))}}.panel{{background:var(--panel);border:1px solid var(--line);border-radius:17px;padding:24px;min-width:0}}.metric small{{display:block;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.1em;font-weight:800}}.metric strong{{display:block;font-size:34px;line-height:1.2;letter-spacing:-.035em;margin:13px 0 5px}}.metric span{{color:var(--muted);font-size:13px}}.metric.alert strong{{color:var(--orange)}}.metric.danger strong{{color:var(--red)}}.metric.cyan strong{{color:var(--cyan)}}
.callout{{display:grid;grid-template-columns:1.45fr 1fr;gap:25px;background:linear-gradient(115deg,#18383c,#11242e 70%);border:1px solid #316469;border-radius:18px;padding:29px}}.callout .big{{font-size:25px;font-weight:800;line-height:1.25;margin:12px 0}}.callout p{{color:#bad2d5}}.callout aside{{border-left:1px solid #37616a;padding-left:24px}}.callout aside strong{{display:block;color:var(--orange);font-size:21px;margin:8px 0}}
.path{{display:flex;align-items:stretch;gap:13px}}.path div{{flex:1;border:1px solid var(--line);background:#122733;border-radius:13px;padding:18px;min-width:0}}.path div.path-risk{{border-color:#8b5b34;background:#2a251f}}.path small{{color:var(--muted);font-size:10px;font-weight:800;letter-spacing:.12em}}.path strong,.path span{{display:block}}.path strong{{font-size:20px;margin:5px 0}}.path span{{font-size:12px;color:var(--muted)}}.path b{{align-self:center;color:var(--cyan);font-size:26px}}.detail-strip{{display:flex;gap:18px;flex-wrap:wrap;margin-top:16px;color:var(--muted);font-size:13px}}.detail-strip strong{{color:var(--ink)}}
.chart svg{{width:100%;height:auto;display:block}}.chart text{{fill:var(--muted);font-size:11px;font-family:inherit}}.chart .axis{{stroke:#527080;stroke-width:1}}.phase-baseline{{fill:#12313a}}.phase-fault{{fill:#493327}}.phase-recovery{{fill:#17352d}}.chart-caption{{display:flex;gap:17px;flex-wrap:wrap;color:var(--muted);font-size:12px;margin-top:6px}}.swatch{{display:inline-block;width:9px;height:9px;margin-right:5px;border-radius:2px}}.swatch.baseline{{background:#2b7784}}.swatch.fault{{background:#a56b35}}.swatch.recovery{{background:#367b58}}.chart-title{{display:flex;align-items:baseline;justify-content:space-between;gap:15px}}.chart-title b{{color:var(--cyan)}}.chart-title b.orange{{color:var(--orange)}}
.table-wrap{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;text-align:left;font-size:14px}}th,td{{padding:13px 12px;border-bottom:1px solid var(--line);vertical-align:top}}thead th{{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.1em}}tbody th{{font-weight:700}}tbody tr:last-child th,tbody tr:last-child td{{border-bottom:0}}.subtle{{color:var(--muted);font-size:13px}}.notice{{border-left:3px solid var(--orange);padding:8px 0 8px 15px;color:var(--muted);margin-top:16px}}.notice strong{{color:var(--ink)}}.bullets{{padding-left:19px;margin:13px 0 0;color:var(--muted)}}.bullets li{{margin:7px 0}}.bullets strong{{color:var(--ink)}}footer{{margin-top:60px;border-top:1px solid var(--line);padding-top:20px;color:var(--muted);font-size:12px}}
@media(max-width:900px){{.metrics{{grid-template-columns:repeat(2,minmax(0,1fr))}}.callout{{grid-template-columns:1fr}}.callout aside{{border-left:0;border-top:1px solid #37616a;padding:18px 0 0}}}}
@media(max-width:650px){{.shell{{padding:0 18px 40px}}.top{{align-items:flex-start;flex-direction:column}}nav{{gap:14px}}.hero{{padding-top:37px}}.section{{margin-top:40px}}.section-head{{display:block}}.section-head p{{margin-top:8px}}.metrics,.two{{grid-template-columns:1fr}}.panel{{padding:19px}}.path{{flex-direction:column}}.path b{{transform:rotate(90deg);line-height:12px}}.metric strong{{font-size:31px}}th,td{{padding:10px 8px}}}}
</style></head><body><div class="shell">
<header class="top"><a class="brand" href="#top">CHAOS<span>LAB</span><span class="muted"> / FIELD REPORT</span></a><nav aria-label="Sections"><a href="#finding">Finding</a><a href="#experiment">Experiment</a><a href="#dependencies">Dependencies</a><a href="#evidence">Evidence</a></nav></header>
<main id="top"><section class="hero"><div class="eyebrow">Local resilience engineering · Spring PetClinic microservices</div>
<h1>{_h(hero_title)}</h1><p>{_h(hero_description)}</p>
<div class="meta"><span class="tag">{_h(source.title())} · {_h(recorded_at)}</span><span class="tag warn">{_h(outcome_label)}</span><span class="tag {'danger' if data_loss or not recovered else ''}">{recovery_label}</span><span class="tag">Read-only local export</span></div>
<p class="subtle" style="margin-top:13px">{_h(metadata)}. Generated {_h(generated_at)} UTC.</p></section>

<section class="section" id="finding"><div class="section-head"><div><div class="eyebrow">01 / The finding</div><h2>{_h(finding_title)}</h2></div><p>{_h(finding_description)}</p></div>
<div class="callout"><div><div class="eyebrow">Correctness probe · owner 6</div><div class="big">{diagnosis}</div><p>The healthy baseline had {_h(baseline_visits if baseline_visits is not None else 'unknown')} visit records. The recorded fault probe classified the owner response as {_h(fault_probe.get('correctness') if fault_probe else 'unobserved')}.</p></div>
<aside><div class="eyebrow">After automatic revert</div><strong>{recovery_label}</strong><p>{_h(recovery_text)}. {_h(recovery_explanation)}</p></aside></div>
<div class="grid metrics" style="margin-top:16px"><div class="panel metric cyan"><small>Fault active</small><strong>{_h(run.get('fault_active_s', '—'))}s</strong><span>Target: {_h(fault.get('target', 'unknown'))}</span></div>
<div class="panel metric alert"><small>First threshold breach</small><strong>{_h(run.get('time_to_first_breach_s', '—'))}s</strong><span>Watchdog monitored every second</span></div>
<div class="panel metric danger"><small>Peak p95 latency</small><strong>{_ms(run.get('peak_p95_ms'))}</strong><span>Baseline {_ms(baseline.get('p95_ms'))}</span></div>
<div class="panel metric alert"><small>Peak HTTP error rate</small><strong>{_pct(run.get('peak_error_rate'))}</strong><span>Baseline {_pct(baseline.get('error_rate'))}</span></div></div></section>

<section class="section" id="experiment"><div class="section-head"><div><div class="eyebrow">02 / Recorded experiment</div><h2>{_h(experiment_title)}</h2></div><p>{_h(fault.get('kind', 'unknown'))} {_h(fault.get('target', 'unknown'))} · run {_h(run.get('id', 'unknown'))}</p></div>
<div class="panel"><div class="eyebrow">{ 'Abort reason' if run.get('abort_reason') else 'Outcome' }</div><h3 style="margin-top:8px">{_h(run.get('abort_reason') or outcome_label)}</h3><p class="muted">The fault was reverted after {_h(run.get('fault_active_s', '—'))} seconds. {_h(experiment_note)}</p></div>
<div class="grid two" style="margin-top:16px"><div class="panel"><div class="chart-title"><h3>Traffic throughput</h3><b>Baseline {_h(baseline.get('rps', '—'))} rps</b></div>{_chart(run.get('timeline') or [], 'rps', 'Requests per second', '#55d7d2', 'rps')}</div>
<div class="panel"><div class="chart-title"><h3>p95 response time</h3><b class="orange">Peak {_ms(run.get('peak_p95_ms'))}</b></div>{_chart(run.get('timeline') or [], 'p95_ms', '95th percentile latency', '#ffb66b', 'ms')}</div></div>
<div class="chart-caption"><span><i class="swatch baseline"></i>Baseline</span><span><i class="swatch fault"></i>Fault</span><span><i class="swatch recovery"></i>Recovery</span><span>Shading follows recorded sample phases; charts are historical.</span></div>
<div class="grid two" style="margin-top:18px"><div class="panel"><h3>Prediction versus observation</h3><div class="table-wrap"><table><thead><tr><th>Action</th><th>Predicted</th><th>Observed</th><th>Result</th></tr></thead><tbody>{_comparison_rows(run)}</tbody></table></div></div>
<div class="panel"><h3>Impact by user action</h3><div class="table-wrap"><table><thead><tr><th>Action</th><th>Baseline errors</th><th>Fault + recovery errors</th><th>Uses target</th></tr></thead><tbody>{_action_rows(run)}</tbody></table></div><p class="subtle">These rates cover fault and recovery together; the correctness probe separately detects partial HTTP 200 responses.</p></div></div></section>

<section class="section" id="dependencies"><div class="section-head"><div><div class="eyebrow">03 / Dependency story</div><h2>One request, two downstream services</h2></div><p>The owner-details path is source-backed. The current saved map contains {_h(len(snapshot.get('nodes', [])))} nodes and {_h(len(snapshot.get('dependencies', [])))} semantic dependencies.</p></div>
{owner_path}<div class="detail-strip"><span><strong>Runtime path:</strong> Gateway → Customers → Visits</span><span><strong>Fallback:</strong> incomplete visits</span><span><strong>Startup dependencies:</strong> Config Server and Discovery Server</span></div>
<p class="notice"><strong>Snapshot context:</strong> {snapshot_note} <a href="dependency-map.html">Explore the detailed dependency map</a>.</p></section>

<section class="section" id="evidence"><div class="section-head"><div><div class="eyebrow">04 / Evidence and safety</div><h2>Auditable by design</h2></div><p>Runtime observations have timestamps and may be stale. Source-backed dependency paths remain visible when telemetry is unavailable.</p></div>
<div class="grid two"><div class="panel"><h3>Runtime source freshness</h3><div class="table-wrap"><table><thead><tr><th>Source</th><th>Recorded state</th><th>Observed at</th><th>Now</th></tr></thead><tbody>{_source_rows(snapshot)}</tbody></table></div><p class="subtle">Map snapshot {_h(current_id or 'unknown')}; generated {_h(snapshot.get('generated_at_utc', 'unknown'))}.</p></div>
<div class="panel"><h3>Guardrails around every run</h3><ul class="bullets"><li><strong>Allowlist:</strong> only labeled sandbox services can be faulted.</li><li><strong>Preflight:</strong> healthy traffic and owner data are required before injection.</li><li><strong>Watchdog:</strong> errors, p95 latency, throughput, and correctness are monitored.</li><li><strong>Time limit and dead-man's switch:</strong> faults are reverted even if the controller exits.</li><li><strong>Kill switch:</strong> an operator can stop all active faults.</li></ul><p class="notice"><strong>Recorded limit:</strong> {_h(run.get('guardrails', {}).get('max_duration_s', 300))}s maximum fault duration; {_h(run.get('guardrails', {}).get('breach_seconds', 3))} consecutive threshold breaches to abort.</p></div></div>
<p class="subtle" style="margin-top:18px">{_h(provenance_note)} This page is an export of saved evidence, not a live health view. AI planning and analysis agents are planned, not part of this recorded experiment.</p></section></main>
<footer>ChaosLab · local Docker Compose sandbox · generated from saved dependency and experiment JSON · <a href="../README.md">Project README</a></footer></div>
<script>for(const el of document.querySelectorAll('.freshness')){{const at=Date.parse(el.dataset.at);const ttl=Number(el.dataset.ttl);el.textContent=!Number.isFinite(at)?'Unknown':ttl>0&&Date.now()-at<=ttl*1000?'Within recorded TTL':'Stale / historical';}}</script>
</body></html>'''


def build(snapshot: dict | None = None, run: dict | None = None,
          map_path: Path = MAP_FILE, runs_dir: Path = RUNS_DIR,
          demo_path: Path = DEMO_FILE, output_path: Path = DASHBOARD_FILE,
          use_demo: bool = False) -> Path:
    if snapshot is None:
        snapshot = json.loads(map_path.read_text())
    if run is None:
        run, source = select_run(runs_dir, demo_path, use_demo=use_demo)
    else:
        source = "recorded demo" if run.get("demo_provenance") else "local run"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render(snapshot, run, source))
    return output_path
