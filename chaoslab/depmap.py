"""Dependency map, discovered automatically from four sources.

  1. Docker Compose  - which containers exist and what each one needs to start
  2. Docker + Eureka - which instances really run, cross-checked against the registry
  3. Zipkin traces   - who calls whom at runtime, and which services each user action touches
  4. Locust          - how much real user traffic flows through each service

What the sources cannot see is filled in from map-overrides.json (manual
corrections, each with a reason), and every fact records where it came from.
"""

import html
import json
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .docker import docker
from .dependency_model import enrich, validate
from .settings import (ACTION_ROUTES, COMPOSE_FILE, COMPOSE_PROJECT, EUREKA_URL, LOCUST_URL, MAP_FILE,
                       MAP_OVERRIDES_FILE, PROMETHEUS_URL, REPORTS_DIR, TARGET_LABEL, ZIPKIN_URL)

LOOKBACK_MS = 15 * 60 * 1000   # use the last 15 minutes of traces
MAX_TRACES = 2000


def _get_json(url: str):
    with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=10) as resp:
        return json.load(resp)


def _datastore(service: str) -> str:
    return f"{service.removesuffix('-service')}-db"


def _is_jdbc(span: dict) -> bool:
    """A client span instrumented by a JDBC datasource proxy (it carries jdbc.* tags)."""
    return any(key.startswith("jdbc.") for key in span.get("tags", {}))


def _jdbc_calls(traces: list) -> dict[tuple[str, str], str]:
    """(service, remote name) of JDBC client spans, mapped to the driver they report.

    The remote name of such a span is a schema (HSQLDB's default is PUBLIC), so
    the same name seen from several services is several databases, not one.
    """
    found = {}
    for trace in traces:
        for span in trace:
            if span.get("kind") == "CLIENT" and _is_jdbc(span):
                local = span.get("localEndpoint", {}).get("serviceName")
                remote = (span.get("remoteEndpoint") or {}).get("serviceName")
                found[local, remote] = span["tags"].get("jdbc.datasource.driver", "unknown driver")
    return found


def _trace_child(parent: str, child: str, containers: set[str],
                 jdbc: dict[tuple[str, str], str] | None = None) -> str:
    if child in containers:
        return child
    if parent and ((parent, child) in (jdbc or {})
                   or re.search(r"mysql|postgres|hsql|h2|jdbc|database", child, re.IGNORECASE)):
        return _datastore(parent)
    return child


def _paths_from_traces(traces: list, containers: set[str]) -> dict[str, dict]:
    """Which services each user action passes through, according to the traces.

    Only successful requests count: a failed request often stops at the gateway
    (fallback, no route) and says nothing about the path it normally takes.
    """
    seen: dict[str, Counter] = defaultdict(Counter)
    count: Counter = Counter()
    for trace in traces:
        root = next((s for s in trace if "parentId" not in s), None)
        tags = (root or {}).get("tags", {})
        if tags.get("outcome") != "SUCCESS":
            continue
        action = next((a for a, route in ACTION_ROUTES.items()
                       if re.fullmatch(route, tags.get("http.url", ""))), None)
        if action is None:
            continue
        count[action] += 1
        services = set()
        for span in trace:
            local = span.get("localEndpoint", {}).get("serviceName")
            remote = (span.get("remoteEndpoint") or {}).get("serviceName")
            services.add(local)
            if span.get("kind") == "CLIENT" and local and _is_jdbc(span):
                services.add(_datastore(local))
            elif span.get("kind") == "CLIENT" and remote:
                services.add(_trace_child(local, remote, containers))
        seen[action].update(services - {None})
    # Keep services present in at least half of an action's traces, ignoring odd partial ones.
    return {action: {"services": sorted(s for s, n in seen[action].items() if n >= count[action] / 2),
                     "traces": count[action]}
            for action in count}


def build() -> dict:
    sources: dict[str, str] = {}
    warnings: list[str] = []
    source_state: dict[str, dict] = {}

    def source(name: str, status: str, detail: str, ttl: int | None = None) -> None:
        observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        source_state[name] = {"status": status, "detail": detail, "observed_at": observed_at,
                              "ttl_seconds": ttl}

    # 1. Docker Compose: containers, their roles, and startup dependencies.
    compose = json.loads(docker("compose", "--file", str(COMPOSE_FILE), "config", "--format", "json"))["services"]
    # Include profile-gated services in the catalog without claiming they run.
    all_compose = json.loads(docker("compose", "--profile", "genai", "--file", str(COMPOSE_FILE),
                                    "config", "--format", "json"))["services"]
    load_gen, entry = next(
        (name, urlparse(cmd[cmd.index("--host") + 1]).hostname)
        for name, svc in compose.items()
        if svc.get("image", "").startswith("locustio/") and "--host" in (cmd := svc.get("command") or [])
    )
    targets = {n for n, s in compose.items() if (s.get("labels") or {}).get(TARGET_LABEL) == "true"}
    needs = {n: sorted(set(compose[n].get("depends_on") or {}) & targets) for n in targets}
    needed_by: dict[str, list[str]] = defaultdict(list)
    for name, deps in needs.items():
        for dep in deps:
            needed_by[dep].append(name)
    sources["docker compose"] = f"{len(compose)} containers, {len(targets)} app services"
    source("docker compose", "available", sources["docker compose"])

    def role(name: str) -> str:
        if name == entry:
            return "entry point"
        if len(needed_by[name]) >= 2:
            return "platform"
        return "service"

    # 2. Running instances come from Docker; Eureka is cross-checked against them,
    #    because a stale registration sends users to an address that is gone.
    try:
        running = Counter(docker("ps", "--filter", f"label=com.docker.compose.project={COMPOSE_PROJECT}",
                                 "--format", '{{.Label "com.docker.compose.service"}}').split())
        source("docker", "available", f"{sum(running.values())} running containers", 30)
    except Exception as err:
        running = None
        source("docker", "unavailable", str(err), 30)
        warnings.append(f"Docker runtime unavailable ({err}); instance counts are unknown")
    try:
        apps = _get_json(f"{EUREKA_URL}/eureka/apps")["applications"]["application"]
        registered = {a["name"].lower(): len(a["instance"]) for a in apps}
        sources["eureka"] = f"{len(registered)} services registered"
        source("eureka", "available", sources["eureka"], 30)
        if running is not None:
            for name, count in sorted(registered.items()):
                if count != running.get(name, 0):
                    warnings.append(f"Eureka lists {count} instance(s) of {name} but {running.get(name, 0)} "
                                    "container(s) run: stale registrations can route users to a dead address")
    except Exception as err:
        registered = None
        source("eureka", "unavailable", str(err), 30)
        warnings.append(f"Eureka unavailable ({err}); registrations not checked")

    # Prometheus scrape health is a signal, not proof of an application call.
    try:
        target_data = _get_json(f"{PROMETHEUS_URL}/api/v1/targets?state=active")["data"]["activeTargets"]
        scrape_health = {item.get("labels", {}).get("job"): item.get("health", "unknown")
                         for item in target_data}
        source("prometheus", "available", f"{len(target_data)} active scrape targets", 30)
    except Exception as err:
        scrape_health = {}
        source("prometheus", "unavailable", str(err), 30)
        warnings.append(f"Prometheus unavailable ({err}); scrape health is unknown")

    # 3. Zipkin: runtime calls and per-action paths.
    now_ms = int(time.time() * 1000)
    calls: Counter = Counter()
    errors: Counter = Counter()
    traced: dict[str, dict] = {}
    jdbc: dict[tuple[str, str], str] = {}
    try:
        # Traces first: their JDBC tags tell which remote names are databases.
        traces = _get_json(f"{ZIPKIN_URL}/api/v2/traces?serviceName={entry}"
                           f"&limit={MAX_TRACES}&lookback={LOOKBACK_MS}&endTs={now_ms}")
        jdbc = _jdbc_calls(traces)
        for dep in _get_json(f"{ZIPKIN_URL}/api/v2/dependencies?endTs={now_ms}&lookback={LOOKBACK_MS}"):
            parent = dep["parent"]
            child = _trace_child(parent, dep["child"], set(all_compose), jdbc)
            calls[parent, child] += dep["callCount"]
            errors[parent, child] += dep.get("errorCount", 0)
        traced = _paths_from_traces(traces, set(compose))
        sources["zipkin"] = (f"{len(traces)} traces and {len(calls)} call edges "
                             f"from the last {LOOKBACK_MS // 60000} minutes")
        source("zipkin", "available" if traces else "empty", sources["zipkin"], LOOKBACK_MS // 1000)
    except Exception as err:
        source("zipkin", "unavailable", str(err), LOOKBACK_MS // 1000)
        warnings.append(f"Zipkin unavailable ({err}); runtime calls are unknown")

    # User actions: traces first, manual corrections where the traces fall short.
    overrides = json.loads(MAP_OVERRIDES_FILE.read_text()).get("actions", {}) if MAP_OVERRIDES_FILE.exists() else {}
    actions = {}
    for name, route in ACTION_ROUTES.items():
        found = traced.get(name, {"services": [], "traces": 0})
        fix = overrides.get(name)
        action = {"route": route, "services": found["services"], "traces": found["traces"], "source": "traces"}
        if fix and not set(fix["services"]) <= set(found["services"]):
            action.update(services=sorted(set(found["services"]) | set(fix["services"])),
                          source="manual correction", reason=fix["reason"])
            warnings.append(f"'{name}': traces only show {', '.join(found['services']) or 'nothing'}; "
                            f"added from map-overrides.json: {fix['reason']}")
        elif not found["services"]:
            action["source"] = "unknown"
            warnings.append(f"'{name}': no traces yet; its dependencies are unknown")
        actions[name] = action

    # 4. Locust: how much traffic each action (and so each service) carries.
    try:
        volume = {s["name"]: s["num_requests"] for s in _get_json(f"{LOCUST_URL}/stats/requests")["stats"]
                  if s["name"] in ACTION_ROUTES}
        sources["locust"] = f"{sum(volume.values())} user requests across {len(volume)} actions"
        source("locust", "available" if sum(volume.values()) else "empty", sources["locust"], 30)
    except Exception as err:
        volume = None
        source("locust", "unavailable", str(err), 30)
        warnings.append(f"Locust unavailable ({err}); traffic shares are unknown")
    total = sum(volume.values()) if volume else 0
    for name, action in actions.items():
        action["share_of_requests"] = round(volume.get(name, 0) / total, 3) if total else None

    # Nodes with observed exposure and availability facts, not severity scores.
    nodes: dict[str, dict] = {}
    for name in sorted(targets):
        nodes[name] = {
            "id": name, "role": role(name), "kind": "container",
            "instances": running.get(name, 0) if running is not None else None,
            "registered_in_eureka": registered.get(name, 0) if registered is not None else None,
            "health": scrape_health.get(name, "unknown"),
            "auto_restart": compose[name].get("restart") not in (None, "no"),
            "needs_at_startup": needs[name],
            "needed_at_startup_by": sorted(needed_by[name]),
        }
    for parent, child in calls:
        if child not in nodes:
            datastore = child == _datastore(parent)
            nodes[child] = {"id": child, "role": "datastore" if datastore else "external",
                            "kind": f"embedded in {parent}" if datastore else "observed endpoint",
                            "instances": None, **({"inside": parent} if datastore else {})}
    # JDBC spans prove an in-process database even when they name no remote endpoint.
    for (service, _), driver in jdbc.items():
        if service in nodes:
            nodes.setdefault(_datastore(service), {
                "id": _datastore(service), "role": "datastore", "kind": f"embedded in {service}",
                "instances": None, "inside": service})["jdbc_driver"] = driver
    for node in nodes.values():
        on = [a for a, action in actions.items() if node["id"] in action["services"]]
        node["user_actions"] = on
        node["share_of_requests"] = (round(sum(actions[a]["share_of_requests"] for a in on), 3)
                                     if total else None)
        node["callers"] = sorted({p for p, c in calls if c == node["id"]})
        node["calls"] = sorted({c for p, c in calls if p == node["id"]})
        hints = []
        if node["role"] not in ("datastore", "external") and node["instances"] == 1:
            hints.append("single instance")
        if node["kind"] == "container" and not node["auto_restart"]:
            hints.append("no auto-restart")
        if node["role"] == "datastore":
            hints.append(f"embedded: fails and restarts together with {node['inside']}")
        if node.get("needed_at_startup_by"):
            hints.append(f"{len(node['needed_at_startup_by'])} services need it to start")
        if node["share_of_requests"] is not None and node["share_of_requests"] > 0:
            hints.append(f"on {node['share_of_requests']:.0%} of user requests")
        node["risk_hints"] = hints

    edges = [{"from": "users", "to": entry, "type": "traffic", "source": "locust"}]
    edges += [{"from": p, "to": c, "type": "calls", "calls": n, "errors": errors[p, c], "source": "zipkin"}
              for (p, c), n in sorted(calls.items())]
    for name, action in actions.items():
        if action["source"] == "manual correction":
            for service in action["services"]:
                if service in targets and service != entry and (entry, service) not in calls:
                    edges.append({"from": entry, "to": service, "type": "calls",
                                  "source": "manual correction"})
    edges += [{"from": n, "to": d, "type": "needs at startup", "source": "docker compose"}
              for n in sorted(needs) for d in needs[n]]

    base = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "entry_point": entry,
        "load_generator": load_gen,
        "sources": sources,
        "warnings": warnings,
        "nodes": list(nodes.values()),
        "edges": edges,
        "user_actions": actions,
        "not_mapped": sorted(n for n in compose if n not in targets),
    }
    # Keep inactive profile services visible without treating them as live.
    profile_targets = set()
    for name, service in all_compose.items():
        if name not in compose and (service.get("labels") or {}).get(TARGET_LABEL) == "true":
            profile_targets.add(name)
            profile_running = running.get(name, 0) if running is not None else None
            base["nodes"].append({"id": name, "role": "service", "kind": "container",
                                  "instances": profile_running,
                                  "registered_in_eureka": registered.get(name, 0) if registered is not None else None,
                                  "auto_restart": False,
                                  "health": scrape_health.get(name, "unknown" if profile_running else "inactive"),
                                  "needs_at_startup": list(service.get("depends_on") or {}),
                                  "needed_at_startup_by": [], "user_actions": [],
                                  "share_of_requests": None, "callers": [], "calls": [],
                                  "risk_hints": [] if profile_running else ["inactive profile"]})
    active_services = targets | ({name for name in profile_targets if running and running.get(name, 0)})
    return enrich(base, active_services=active_services, source_state=source_state)


def action_services() -> dict[str, set[str]] | None:
    """Services each user action passes through, from the last saved map."""
    if not MAP_FILE.exists():
        return None
    return {name: set(a["services"]) | set(a.get("declared_services", []))
            for name, a in json.loads(MAP_FILE.read_text())["user_actions"].items()}


def load() -> dict:
    """Read a V1 snapshot, refreshing source staleness for the current time."""
    from .dependency_model import refresh_freshness

    snapshot = json.loads(MAP_FILE.read_text())
    validate(snapshot)
    return refresh_freshness(snapshot)


# ---------------------------------------------------------------- rendering

def mermaid(m: dict) -> str:
    def nid(name: str) -> str:
        return re.sub(r"\W", "_", name)

    def label(node: dict) -> str:
        lines = [html.escape(node["id"], quote=True)]
        if node["role"] == "datastore":
            lines.append(html.escape(node["kind"], quote=True))
        else:
            share = (f"{node['share_of_requests']:.0%} of requests"
                     if node.get("share_of_requests") is not None else "traffic unknown")
            instances = node.get("instances")
            lines.append(f"{share}, {instances if instances is not None else '?'} instance"
                         + ("" if instances == 1 else "s"))
        return "<br/>".join(lines)

    by_role = defaultdict(list)
    for node in m["nodes"]:
        by_role[node["role"]].append(node)
    out = ["flowchart LR", '  users(["Users (Locust)"])', '  subgraph app["App services"]']
    out += [f'    {nid(n["id"])}["{label(n)}"]' for n in by_role["entry point"] + by_role["service"]]
    out += ["  end", '  subgraph platform["Platform"]']
    out += [f'    {nid(n["id"])}["{label(n)}"]' for n in by_role["platform"]]
    out += ["  end"]
    out += [f'  {nid(n["id"])}[("{label(n)}")]' for n in by_role["datastore"]]
    out += [f'  {nid(n["id"])}["{label(n)}"]' for n in by_role["external"]]
    startup_edges = {(e["from"], e["to"]) for e in m["edges"]
                     if e["type"] == "needs at startup"}
    drawn_calls = set()
    for e in m["edges"]:
        if e["type"] == "traffic":
            out.append(f"  users ==> {nid(e['to'])}")
        elif e["type"] == "calls":
            drawn_calls.add((e["from"], e["to"]))
            text = f"{e['calls']} calls" if e["source"] == "zipkin" else "manual correction"
            if e.get("errors"):
                text += f", {e['errors']} errors"
            arrow = "-->" if e["source"] == "zipkin" else "-.->"
            out.append(f'  {nid(e["from"])} {arrow}|"{text}"| {nid(e["to"])}')
    for dep in m.get("dependencies", []):
        pair = (dep["consumer"], dep["provider"])
        if dep.get("feature") and pair not in drawn_calls:
            label_text = html.escape(dep["feature"], quote=True)
            out.append(f'  {nid(pair[0])} -->|"{label_text}"| {nid(pair[1])}')
            drawn_calls.add(pair)
        elif dep["id"].startswith("persistence:") and pair not in drawn_calls:
            out.append(f'  {nid(pair[0])} -.->|"inferred persistence"| {nid(pair[1])}')
            drawn_calls.add(pair)
        elif dep["purpose"] in ("configuration", "service_discovery") and "startup" in dep["phases"]:
            if pair not in startup_edges:
                continue
            label_text = "config at startup" if dep["purpose"] == "configuration" else "discovery at startup"
            out.append(f'  {nid(pair[0])} -.->|"{label_text}"| {nid(pair[1])}')
    return "\n".join(out)


def _rows(m: dict) -> tuple[list[list[str]], list[list[str]]]:
    services = [[n["id"], n["role"],
                 f"{n['share_of_requests']:.0%}" if n.get("share_of_requests") is not None else "unknown",
                 str(n["instances"]) if n.get("instances") is not None else "unknown",
                 "n/a" if n["role"] in ("datastore", "external") else ("yes" if n["auto_restart"] else "no"),
                 ", ".join(n.get("needs_at_startup", [])) or "-",
                 "; ".join(n["risk_hints"]) or "-"]
                for n in sorted(m["nodes"], key=lambda n: -(n.get("share_of_requests") or 0))]
    actions = [[name, f"`{a['route']}`",
                f"{a['share_of_requests']:.0%}" if a.get("share_of_requests") is not None else "unknown",
                " -> ".join(a.get("declared_services") or a["services"]) or "unknown",
                a["source"] + (f" ({a['traces']} traces)" if a["traces"] else "")]
               for name, a in m["user_actions"].items()]
    return services, actions


def _dependency_rows(m: dict) -> list[list[str]]:
    return [[d["consumer"], d["provider"], d.get("feature") or "service", d["purpose"],
             ", ".join(d["phases"]), d["verification"],
             d.get("effects", {}).get("unavailable", {}).get("behavior", "unknown"),
             "; ".join(e["locator"] for e in d["evidence"])]
            for d in m.get("dependencies", [])]


DEPENDENCY_COLUMNS = ["Consumer", "Provider", "Feature", "Purpose", "Phase", "Verified",
                      "If unavailable", "Evidence"]


SERVICE_COLUMNS = ["Service", "Role", "Share of user requests", "Instances", "Auto-restart",
                   "Needs at startup", "Risk hints"]
ACTION_COLUMNS = ["User action", "Route", "Share of requests", "Passes through", "Source"]
INTRO = ("Source-backed relationships are reconciled with Docker Compose, Eureka, Zipkin, "
         "Prometheus and Locust observations. Missing telemetry is unknown, not zero impact. "
         "Dashed arrows are startup dependencies or manual corrections.")


def markdown(m: dict) -> str:
    def table(columns, rows):
        lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
        return "\n".join(lines + ["| " + " | ".join(r) + " |" for r in rows])

    services, actions = _rows(m)
    parts = [f"# Dependency map\n\nGenerated {m['generated_at']}. {INTRO}\n",
             f"```mermaid\n{mermaid(m)}\n```\n",
             "## Services\n", table(SERVICE_COLUMNS, services) + "\n",
             "## User actions (data flows)\n", table(ACTION_COLUMNS, actions) + "\n",
             "## Dependency semantics\n", table(DEPENDENCY_COLUMNS, _dependency_rows(m)) + "\n",
             "## Runtime sources\n"]
    parts += [f"- **{name}**: {state['status']}; observed {state['observed_at']}"
              for name, state in m.get("source_state", {}).items()]
    parts += ["\n## Notes and manual corrections\n"]
    parts += [f"- {w}" for w in m["warnings"]] or ["- none"]
    parts += ["\n## Sources\n"] + [f"- **{k}**: {v}" for k, v in m["sources"].items()]
    parts += [f"- Not mapped (our own tooling): {', '.join(m['not_mapped'])}\n"]
    return "\n".join(parts)


def html_page(m: dict, run: dict | None = None) -> str:
    from .blast_radius import FAILURE_MODES, analyze

    def table(columns, rows):
        head = "".join(f"<th>{html.escape(c)}</th>" for c in columns)
        body = "".join("<tr>" + "".join(f"<td>{html.escape(c.strip('`'))}</td>" for c in r) + "</tr>"
                       for r in rows)
        return f'<div class="wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'

    services, actions = _rows(m)
    notes = "".join(f"<li>{html.escape(w)}</li>" for w in m["warnings"]) or "<li>none</li>"
    sources = "".join(f"<li><b>{html.escape(k)}</b>: {html.escape(v)}</li>" for k, v in m["sources"].items())
    states = "".join(f'<li data-observed="{html.escape(state["observed_at"])}" '
                     f'data-ttl="{state.get("ttl_seconds") or 0}">'
                     f"<b>{html.escape(name)}</b>: {html.escape(state['status'])} "
                     f"(observed {html.escape(state['observed_at'])})</li>"
                     for name, state in m.get("source_state", {}).items())
    candidates = [n["id"] for n in m["nodes"] if n.get("active") and n.get("kind") == "container"]
    predictions = {name: {failure: analyze(m, name, failure) for failure in FAILURE_MODES}
                   for name in candidates}
    prediction_json = json.dumps(predictions).replace("<", "\\u003c").replace(">", "\\u003e")
    run_view = None
    if run and (run.get("prediction") or {}).get("snapshot_id") == m.get("snapshot_id"):
        run_view = {"id": run["id"], "fault": run["fault"], "outcome": run["outcome"],
                    "recovered": run.get("recovered"), "recovery_pending": run.get("recovery_pending", False),
                    "comparison": run.get("prediction_comparison", {})}
    run_json = json.dumps(run_view).replace("<", "\\u003c").replace(">", "\\u003e")
    options = "".join(f'<option value="{html.escape(name)}">{html.escape(name)}</option>'
                      for name in candidates)
    failure_options = "".join(f'<option value="{name}">{name}</option>' for name in FAILURE_MODES)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dependency map</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; margin: 24px; color: #1f1f1f; background: #fff; }}
  .wrap {{ overflow-x: auto; margin: 8px 0 24px; }}
  table {{ border-collapse: collapse; font-size: 13px; }}
  th, td {{ border: 1px solid #ddd; padding: 6px 10px; text-align: left; vertical-align: top; }}
  th {{ background: #f5f5f3; }}
  .meta {{ color: #555; max-width: 900px; }}
  #impact li {{ margin: 8px 0; }}
</style></head><body>
<h1>Dependency map</h1>
<p class="meta">Generated {html.escape(m['generated_at'])}. {html.escape(INTRO)}</p>
<pre class="mermaid">{html.escape(mermaid(m))}</pre>
<h2>Services</h2>{table(SERVICE_COLUMNS, services)}
<h2>User actions (data flows)</h2>{table(ACTION_COLUMNS, actions)}
<h2>Dependency semantics</h2>{table(DEPENDENCY_COLUMNS, _dependency_rows(m))}
<h2>Potential blast radius</h2>
<p>Predictions are qualitative. Select a target, failure, and phase; evidence and uncertainty are shown for each impact.</p>
<label>Target <select id="target">{options}</select></label>
<label>Failure <select id="failure">{failure_options}</select></label>
<label>Phase <select id="phase"><option value="runtime">Running services</option>
<option value="startup_restart">Startup and restart</option></select></label>
<ul id="impact"></ul>
<h2>Latest run comparison</h2><p id="run-meta">No run for this snapshot yet.</p>
<ul id="run-comparison"></ul>
<h2>Runtime source status</h2><ul>{states}</ul>
<h2>Notes and manual corrections</h2><ul>{notes}</ul>
<h2>Sources</h2><ul>{sources}</ul>
<script src="https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js"></script>
<script>
mermaid.initialize({{ startOnLoad: true, securityLevel: "strict" }});
const predictions = {prediction_json};
const latestRun = {run_json};
const target = document.getElementById('target');
const failure = document.getElementById('failure');
const phase = document.getElementById('phase');
const impact = document.getElementById('impact');
const runMeta = document.getElementById('run-meta');
const runComparison = document.getElementById('run-comparison');
function renderImpact() {{
  impact.replaceChildren();
  runComparison.replaceChildren();
  if (latestRun && latestRun.fault.target === target.value && latestRun.fault.kind === failure.value && phase.value === 'runtime') {{
    runMeta.textContent = `${{latestRun.id}}: ${{latestRun.outcome}}; recovery ${{latestRun.recovery_pending ? 'pending' : latestRun.recovered ? 'confirmed' : 'not confirmed'}}`;
    for (const [name, comparison] of Object.entries(latestRun.comparison)) {{
      const li = document.createElement('li');
      const signals = (comparison.observed_statuses || []).length ? `; HTTP ${{comparison.observed_statuses.join('/')}}, peak probe ${{comparison.peak_observed_latency_ms}}ms, recovery ${{comparison.recovery_correctness || 'unobserved'}}` : '';
      li.textContent = `${{name}}: predicted ${{comparison.predicted}}, observed ${{comparison.observed}} (${{comparison.match}}; ${{comparison.fault_requests}} fault requests, ${{comparison.fault_probes || 0}} correctness probes${{signals}})`;
      runComparison.append(li);
    }}
  }} else {{
    runMeta.textContent = latestRun ? 'Select the latest run target, failure, and running phase to compare.' : 'No run for this snapshot yet.';
  }}
  const items = predictions[target.value]?.[failure.value]?.[phase.value] || [];
  if (!items.length) {{ const li = document.createElement('li'); li.textContent = 'No supported impact predicted for this phase.'; impact.append(li); return; }}
  for (const item of items) {{
    const li = document.createElement('li');
    const evidence = item.evidence.map(e => e.locator).join('; ');
    li.textContent = `${{item.component}}: ${{item.outcome}} — ${{item.behavior}}. Path: ${{item.path.join(' → ')}}. Evidence: ${{evidence}}.` +
      (item.uncertainties.length ? ` Uncertainty: ${{item.uncertainties.join('; ')}}.` : '');
    impact.append(li);
  }}
}}
for (const item of document.querySelectorAll('[data-observed]')) {{
  const ttl = Number(item.dataset.ttl);
  if (ttl && Date.now() - Date.parse(item.dataset.observed) > ttl * 1000) {{
    item.append(' — stale observation');
  }}
}}
for (const control of [target, failure, phase]) control.addEventListener('change', renderImpact);
renderImpact();
</script>
</body></html>
"""


def save(m: dict) -> list[str]:
    REPORTS_DIR.mkdir(exist_ok=True)
    MAP_FILE.write_text(json.dumps(m, indent=2))
    (REPORTS_DIR / "dependency-map.md").write_text(markdown(m))
    (REPORTS_DIR / "dependency-map.html").write_text(html_page(m))
    return [str(p.relative_to(REPORTS_DIR.parent)) for p in
            (MAP_FILE, REPORTS_DIR / "dependency-map.md", REPORTS_DIR / "dependency-map.html")]


def update_run_report(run: dict) -> bool:
    """Show a completed or checkpointed run in the saved snapshot's HTML view."""
    try:
        snapshot = load()
    except (FileNotFoundError, ValueError):
        return False
    if (run.get("prediction") or {}).get("snapshot_id") != snapshot.get("snapshot_id"):
        return False
    (REPORTS_DIR / "dependency-map.html").write_text(html_page(snapshot, run))
    return True
