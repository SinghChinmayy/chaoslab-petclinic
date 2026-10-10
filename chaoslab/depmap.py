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
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .docker import docker
from .settings import (ACTION_ROUTES, COMPOSE_FILE, COMPOSE_PROJECT, EUREKA_URL, LOCUST_URL, MAP_FILE,
                       MAP_OVERRIDES_FILE, REPORTS_DIR, TARGET_LABEL, ZIPKIN_URL)

LOOKBACK_MS = 15 * 60 * 1000   # use the last 15 minutes of traces
MAX_TRACES = 2000
CRITICAL_SHARE = 0.5           # a service on at least half of all user requests is critical


def _get_json(url: str):
    with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=10) as resp:
        return json.load(resp)


def _datastore(service: str) -> str:
    return f"{service.removesuffix('-service')}-db"


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
            if span.get("kind") == "CLIENT" and remote:
                services.add(remote if remote in containers else _datastore(local))
        seen[action].update(services - {None})
    # Keep services present in at least half of an action's traces, ignoring odd partial ones.
    return {action: {"services": sorted(s for s, n in seen[action].items() if n >= count[action] / 2),
                     "traces": count[action]}
            for action in count}


def build() -> dict:
    sources: dict[str, str] = {}
    warnings: list[str] = []

    # 1. Docker Compose: containers, their roles, and startup dependencies.
    compose = json.loads(docker("compose", "--file", str(COMPOSE_FILE), "config", "--format", "json"))["services"]
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

    def role(name: str) -> str:
        if name == entry:
            return "entry point"
        if len(needed_by[name]) >= 2:
            return "platform"
        return "service"

    # 2. Running instances come from Docker; Eureka is cross-checked against them,
    #    because a stale registration sends users to an address that is gone.
    running = Counter(docker("ps", "--filter", f"label=com.docker.compose.project={COMPOSE_PROJECT}",
                             "--format", '{{.Label "com.docker.compose.service"}}').split())
    try:
        apps = _get_json(f"{EUREKA_URL}/eureka/apps")["applications"]["application"]
        registered = {a["name"].lower(): len(a["instance"]) for a in apps}
        sources["eureka"] = f"{len(registered)} services registered"
        for name, count in sorted(registered.items()):
            if count != running.get(name, 0):
                warnings.append(f"Eureka lists {count} instance(s) of {name} but {running.get(name, 0)} "
                                "container(s) run: stale registrations can route users to a dead address")
    except Exception as err:
        registered = {}
        warnings.append(f"Eureka unavailable ({err}); registrations not checked")

    # 3. Zipkin: runtime calls and per-action paths.
    now_ms = int(time.time() * 1000)
    calls: Counter = Counter()
    errors: Counter = Counter()
    traced: dict[str, dict] = {}
    try:
        for dep in _get_json(f"{ZIPKIN_URL}/api/v2/dependencies?endTs={now_ms}&lookback={LOOKBACK_MS}"):
            parent = dep["parent"]
            child = dep["child"] if dep["child"] in compose else _datastore(parent)
            calls[parent, child] += dep["callCount"]
            errors[parent, child] += dep.get("errorCount", 0)
        traces = _get_json(f"{ZIPKIN_URL}/api/v2/traces?serviceName={entry}"
                           f"&limit={MAX_TRACES}&lookback={LOOKBACK_MS}&endTs={now_ms}")
        traced = _paths_from_traces(traces, set(compose))
        sources["zipkin"] = (f"{len(traces)} traces and {len(calls)} call edges "
                             f"from the last {LOOKBACK_MS // 60000} minutes")
    except Exception as err:
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
    except Exception as err:
        volume = {}
        warnings.append(f"Locust unavailable ({err}); traffic shares are unknown")
    total = sum(volume.values()) or 1
    for name, action in actions.items():
        action["share_of_requests"] = round(volume.get(name, 0) / total, 3)

    # Nodes with the facts a planner needs to judge how risky each one is.
    nodes: dict[str, dict] = {}
    for name in sorted(targets):
        nodes[name] = {
            "id": name, "role": role(name), "kind": "container",
            "instances": running.get(name, 0),
            "registered_in_eureka": registered.get(name, 0),
            "auto_restart": compose[name].get("restart") not in (None, "no"),
            "needs_at_startup": needs[name],
            "needed_at_startup_by": sorted(needed_by[name]),
        }
    for parent, child in calls:
        if child not in nodes:
            nodes[child] = {"id": child, "role": "datastore", "kind": f"embedded in {parent}",
                            "instances": 1, "inside": parent}
    for node in nodes.values():
        on = [a for a, action in actions.items() if node["id"] in action["services"]]
        node["user_actions"] = on
        node["share_of_requests"] = round(sum(actions[a]["share_of_requests"] for a in on), 3)
        node["callers"] = sorted({p for p, c in calls if c == node["id"]})
        node["calls"] = sorted({c for p, c in calls if p == node["id"]})
        hints = []
        if node["role"] != "datastore" and node["instances"] == 1:
            hints.append("single instance")
        if node["kind"] == "container" and not node["auto_restart"]:
            hints.append("no auto-restart")
        if node["role"] == "datastore":
            hints.append(f"embedded: fails and restarts together with {node['inside']}")
        if node.get("needed_at_startup_by"):
            hints.append(f"{len(node['needed_at_startup_by'])} services need it to start")
        if node["share_of_requests"] >= CRITICAL_SHARE:
            hints.append(f"on {node['share_of_requests']:.0%} of user requests")
        node["risk_hints"] = hints
        node["critical"] = (node["share_of_requests"] >= CRITICAL_SHARE
                            or len(node.get("needed_at_startup_by", [])) >= 2)

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

    return {
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


def action_services() -> dict[str, set[str]] | None:
    """Services each user action passes through, from the last saved map."""
    if not MAP_FILE.exists():
        return None
    return {name: set(a["services"]) for name, a in json.loads(MAP_FILE.read_text())["user_actions"].items()}


# ---------------------------------------------------------------- rendering

def mermaid(m: dict) -> str:
    def nid(name: str) -> str:
        return re.sub(r"\W", "_", name)

    def label(node: dict) -> str:
        lines = [node["id"]]
        if node["role"] == "datastore":
            lines.append(node["kind"])
        else:
            lines.append(f"{node['share_of_requests']:.0%} of requests, {node['instances']} instance"
                         + ("" if node["instances"] == 1 else "s"))
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
    platform = {n["id"] for n in by_role["platform"]}
    for e in m["edges"]:
        if e["type"] == "traffic":
            out.append(f"  users ==> {nid(e['to'])}")
        elif e["type"] == "calls":
            text = f"{e['calls']} calls" if e["source"] == "zipkin" else "manual correction"
            if e.get("errors"):
                text += f", {e['errors']} errors"
            arrow = "-->" if e["source"] == "zipkin" else "-.->"
            out.append(f'  {nid(e["from"])} {arrow}|"{text}"| {nid(e["to"])}')
        elif e["from"] in platform and e["to"] in platform:
            out.append(f'  {nid(e["from"])} -.->|"needs at startup"| {nid(e["to"])}')
    out.append('  app -.->|"needs at startup"| platform')
    critical = [nid(n["id"]) for n in m["nodes"] if n.get("critical")]
    out.append("  classDef critical stroke:#D85A30,stroke-width:3px")
    if critical:
        out.append(f"  class {','.join(critical)} critical")
    return "\n".join(out)


def _rows(m: dict) -> tuple[list[list[str]], list[list[str]]]:
    services = [[n["id"], n["role"], f"{n['share_of_requests']:.0%}", str(n["instances"]),
                 "n/a" if n["role"] == "datastore" else ("yes" if n["auto_restart"] else "no"),
                 ", ".join(n.get("needs_at_startup", [])) or "-",
                 "; ".join(n["risk_hints"]) or "-"]
                for n in sorted(m["nodes"], key=lambda n: -n["share_of_requests"])]
    actions = [[name, f"`{a['route']}`", f"{a['share_of_requests']:.0%}",
                " -> ".join(a["services"]) or "unknown",
                a["source"] + (f" ({a['traces']} traces)" if a["traces"] else "")]
               for name, a in m["user_actions"].items()]
    return services, actions


SERVICE_COLUMNS = ["Service", "Role", "Share of user requests", "Instances", "Auto-restart",
                   "Needs at startup", "Risk hints"]
ACTION_COLUMNS = ["User action", "Route", "Share of requests", "Passes through", "Source"]
INTRO = ("Discovered automatically from Docker Compose, Eureka, Zipkin traces and Locust traffic. "
         "Thick orange borders mark critical services: on at least half of all user requests, "
         "or needed by two or more services to start. Dashed arrows are startup dependencies "
         "or manual corrections.")


def markdown(m: dict) -> str:
    def table(columns, rows):
        lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
        return "\n".join(lines + ["| " + " | ".join(r) + " |" for r in rows])

    services, actions = _rows(m)
    parts = [f"# Dependency map\n\nGenerated {m['generated_at']}. {INTRO}\n",
             f"```mermaid\n{mermaid(m)}\n```\n",
             "## Services\n", table(SERVICE_COLUMNS, services) + "\n",
             "## User actions (data flows)\n", table(ACTION_COLUMNS, actions) + "\n",
             "## Notes and manual corrections\n"]
    parts += [f"- {w}" for w in m["warnings"]] or ["- none"]
    parts += ["\n## Sources\n"] + [f"- **{k}**: {v}" for k, v in m["sources"].items()]
    parts += [f"- Not mapped (our own tooling): {', '.join(m['not_mapped'])}\n"]
    return "\n".join(parts)


def html_page(m: dict) -> str:
    def table(columns, rows):
        head = "".join(f"<th>{html.escape(c)}</th>" for c in columns)
        body = "".join("<tr>" + "".join(f"<td>{html.escape(c.strip('`'))}</td>" for c in r) + "</tr>"
                       for r in rows)
        return f'<div class="wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'

    services, actions = _rows(m)
    notes = "".join(f"<li>{html.escape(w)}</li>" for w in m["warnings"]) or "<li>none</li>"
    sources = "".join(f"<li><b>{html.escape(k)}</b>: {html.escape(v)}</li>" for k, v in m["sources"].items())
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
</style></head><body>
<h1>Dependency map</h1>
<p class="meta">Generated {html.escape(m['generated_at'])}. {html.escape(INTRO)}</p>
<pre class="mermaid">{html.escape(mermaid(m))}</pre>
<h2>Services</h2>{table(SERVICE_COLUMNS, services)}
<h2>User actions (data flows)</h2>{table(ACTION_COLUMNS, actions)}
<h2>Notes and manual corrections</h2><ul>{notes}</ul>
<h2>Sources</h2><ul>{sources}</ul>
<script src="https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js"></script>
<script>mermaid.initialize({{ startOnLoad: true, securityLevel: "strict" }});</script>
</body></html>
"""


def save(m: dict) -> list[str]:
    REPORTS_DIR.mkdir(exist_ok=True)
    MAP_FILE.write_text(json.dumps(m, indent=2))
    (REPORTS_DIR / "dependency-map.md").write_text(markdown(m))
    (REPORTS_DIR / "dependency-map.html").write_text(html_page(m))
    return [str(p.relative_to(REPORTS_DIR.parent)) for p in
            (MAP_FILE, REPORTS_DIR / "dependency-map.md", REPORTS_DIR / "dependency-map.html")]
