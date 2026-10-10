"""Shared, versioned dependency contract for the planner and visual reports."""

import hashlib
import json
import re
from copy import deepcopy
from datetime import datetime, timezone

from .settings import ROOT

MANIFEST = ROOT / "dependency-semantics.json"
SCHEMA_VERSION = 1
PHASES = {"runtime", "startup", "restart", "unknown"}
VERIFICATION = {"verified", "inferred", "unknown"}
OUTCOMES = {"unavailable", "degraded", "preserved", "uncertain"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _evidence(kind: str, locator: str, detail: str, observed_at: str | None = None,
              verification: str | None = None) -> dict:
    return {"kind": kind, "locator": locator, "detail": detail,
            "observed_at": observed_at or _now(),
            "verification": verification or ("inferred" if kind == "trace_inference" else "verified")}


def enrich(base: dict, active_services: set[str], source_state: dict,
           manifest: dict | None = None) -> dict:
    """Reconcile source-backed semantics with the existing discovered topology."""
    manifest = manifest or json.loads(MANIFEST.read_text())
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported dependency semantics version")
    snapshot = deepcopy(base)
    snapshot["schema_version"] = SCHEMA_VERSION
    snapshot["generated_at_utc"] = _now()
    snapshot["source_state"] = source_state
    snapshot["environment"] = "local-compose"
    nodes = {node["id"]: node for node in snapshot["nodes"]}
    for node in nodes.values():
        node["active"] = node["id"] in active_services if node.get("kind") == "container" else True
        node["component_kind"] = ("in_process_resource" if node.get("role") == "datastore"
                                  else "external" if node.get("role") == "external"
                                  else "platform" if node.get("role") == "platform" else "service")
        node["verification"] = "inferred" if node.get("role") in ("datastore", "external") else "verified"
        node["evidence"] = [_evidence("compose", "spring-petclinic-microservices/docker-compose.yml",
                                      "Declared Compose service")]
        if node.get("role") == "datastore" and node.get("jdbc_driver"):
            node["verification"] = "verified"
            node["evidence"] = [_evidence("zipkin", "Zipkin trace spans (jdbc.datasource.driver tag)",
                                          f"JDBC calls from {node['inside']} use {node['jdbc_driver']}",
                                          source_state.get("zipkin", {}).get("observed_at"))]
        elif node.get("role") == "datastore":
            node["evidence"] = [_evidence("trace_inference", "chaoslab/depmap.py#_datastore",
                                          "Synthetic datastore name; active datasource is unverified")]
        elif node.get("role") == "external":
            node["evidence"] = [_evidence("zipkin", "Zipkin dependency API",
                                          "Observed endpoint; purpose and ownership are unknown")]
        node["operational"] = {
            "running_instances": node.get("instances"),
            "registered_instances": node.get("registered_in_eureka"),
            "health": node.get("health", "unknown"),
        }

    # Profile-disabled services are absent from default `docker compose config`.
    for name in set(manifest["config_clients"] + manifest["discovery_clients"]):
        if name not in nodes:
            node = {"id": name, "role": "service", "kind": "container", "component_kind": "service",
                    "instances": 0, "registered_in_eureka": None, "auto_restart": False,
                    "active": False, "verification": "verified", "operational": {"running_instances": 0,
                    "registered_instances": None, "health": "inactive"},
                    "evidence": [_evidence("compose", "spring-petclinic-microservices/docker-compose.yml",
                                           "Profile-gated service")]}
            snapshot["nodes"].append(node)
            nodes[name] = node

    for service in ("customers-service", "visits-service", "vets-service"):
        name = service.removesuffix("-service") + "-db"
        if name in nodes or service not in nodes:
            continue
        node = {"id": name, "role": "datastore", "kind": f"embedded in {service}",
                "component_kind": "in_process_resource", "inside": service, "instances": None,
                "registered_in_eureka": None, "auto_restart": False,
                "active": nodes[service]["active"], "verification": "inferred",
                "share_of_requests": None, "risk_hints": ["datasource not verified"],
                "user_actions": [], "callers": [], "calls": [], "operational": {
                    "running_instances": None, "registered_instances": None, "health": "unknown"},
                "evidence": [_evidence("source", f"spring-petclinic-microservices/spring-petclinic-{service}/pom.xml",
                                       "HSQLDB dependency is present; active datasource is unverified",
                                       verification="inferred")]}
        snapshot["nodes"].append(node)
        nodes[name] = node

    dependencies = []
    features = {}
    gateway = snapshot["entry_point"]
    for feature_name, feature in manifest["features"].items():
        steps = []
        for step in feature["steps"]:
            provider = step["provider"]
            dep_id = f"{_slug(feature_name)}:{provider}"
            dependencies.append({
                "id": dep_id, "consumer": gateway, "provider": provider, "feature": feature_name,
                "purpose": step["purpose"], "interaction": "synchronous_http", "phases": ["runtime"],
                "requirement": step["requirement"], "verification": "verified",
                "effects": {"unavailable": step["unavailable"]},
                "evidence": [_evidence("source", step["evidence"],
                                       "Route or controller establishes this call and its unavailable behavior")],
            })
            steps.append(dep_id)
        features[feature_name] = {"route": feature["route"], "entry_point": gateway,
                                  "steps": steps, "oracle": feature.get("oracle"),
                                  "cache": feature.get("cache"),
                                  "active": all(nodes[s["provider"]]["active"] for s in feature["steps"])}

    compose_edges = {(edge["from"], edge["to"]) for edge in snapshot["edges"]
                     if edge["type"] == "needs at startup"}
    for client in manifest["config_clients"]:
        if client not in nodes:
            continue
        locator = ("spring-petclinic-microservices/spring-petclinic-" + client
                   + "/src/main/resources/application.yml")
        evidence = [_evidence("source", locator, "Docker profile imports Config Server")]
        if (client, "config-server") in compose_edges:
            evidence.append(_evidence("compose", "spring-petclinic-microservices/docker-compose.yml",
                                      "Compose waits for Config Server health"))
        dependencies.append({
            "id": f"config:{client}", "consumer": client, "provider": "config-server", "feature": None,
            "purpose": "configuration", "interaction": "config_import", "phases": ["startup", "restart"],
            "requirement": "required", "verification": "verified", "evidence": evidence,
            "effects": {"unavailable": {"outcome": "unavailable", "behavior": "Docker-profile config import fails",
                                        "propagate_service": True}},
        })
        dependencies.append({
            "id": f"config-runtime:{client}", "consumer": client, "provider": "config-server",
            "feature": None, "purpose": "configuration", "interaction": "runtime_config_behavior_unknown",
            "phases": ["runtime"], "requirement": "unknown", "verification": "unknown",
            "evidence": evidence,
            "effects": {"unavailable": {"outcome": "uncertain",
                                        "behavior": "Running service may retain its loaded configuration"}},
        })
    for client in manifest["discovery_clients"]:
        if client not in nodes:
            continue
        dependencies.append({
            "id": f"discovery:{client}", "consumer": client, "provider": "discovery-server",
            "feature": None, "purpose": "service_discovery", "interaction": "registration_and_lookup",
            "phases": ["runtime", "restart"], "requirement": "unknown",
            "verification": "inferred", "evidence": [
                _evidence("compose", "spring-petclinic-microservices/docker-compose.yml",
                          "Compose waits for Discovery; cached runtime addresses are unverified")],
            "effects": {"unavailable": {"outcome": "uncertain",
                                        "behavior": "Registration or lookup may fail; existing addresses may remain usable"}},
        })
        dependencies.append({
            "id": f"discovery-startup:{client}", "consumer": client, "provider": "discovery-server",
            "feature": None, "purpose": "service_discovery", "interaction": "compose_health_gate",
            "phases": ["startup"], "requirement": "required", "verification": "verified",
            "evidence": [_evidence("compose", "spring-petclinic-microservices/docker-compose.yml",
                                   "Compose waits for Discovery health before starting this service")],
            "effects": {"unavailable": {"outcome": "unavailable",
                                        "behavior": "Compose startup waits for healthy Discovery",
                                        "propagate_service": True}},
        })
    for service in ("customers-service", "visits-service", "vets-service"):
        database = service.removesuffix("-service") + "-db"
        dependencies.append({
            "id": f"persistence:{service}", "consumer": service, "provider": database,
            "feature": None, "purpose": "persistence", "interaction": "in_process_resource",
            "phases": ["runtime"], "requirement": "unknown",
            # Observed JDBC calls verify the relationship, not what depends on it.
            "verification": nodes[database]["verification"],
            "effects": {}, "evidence": list(nodes[database]["evidence"]),
        })

    # Zipkin adds evidence to declared edges. An undeclared call is connectivity
    # evidence, not evidence that the call is required for any feature.
    for edge in snapshot["edges"]:
        if edge["type"] != "calls" or edge["source"] != "zipkin":
            continue
        pair = edge["from"], edge["to"]
        if pair[0] not in nodes or pair[1] not in nodes:
            continue
        observation = _evidence("zipkin", "Zipkin dependency API", f"{edge['calls']} observed calls",
                                source_state.get("zipkin", {}).get("observed_at"))
        matching = [dep for dep in dependencies if (dep["consumer"], dep["provider"]) == pair]
        if matching:
            for dep in matching:
                dep["evidence"].append(observation)
        else:
            dependencies.append({
                "id": f"observed:{pair[0]}:{pair[1]}", "consumer": pair[0], "provider": pair[1],
                "feature": None, "purpose": "unknown", "interaction": "observed_call",
                "phases": ["runtime"], "requirement": "unknown",
                "verification": "inferred" if nodes[pair[1]].get("role") == "datastore" else "verified",
                "effects": {}, "evidence": [observation],
            })

    snapshot["dependencies"] = dependencies
    snapshot["features"] = features
    for name, feature in features.items():
        if name in snapshot["user_actions"]:
            action = snapshot["user_actions"][name]
            by_id = {dep["id"]: dep for dep in dependencies}
            action["declared_services"] = [gateway] + [by_id[step]["provider"] for step in feature["steps"]]
    # Traffic exposure follows source-backed feature paths even when Zipkin is
    # empty. Keep it unknown if Locust is unavailable.
    for node in snapshot["nodes"]:
        on = [name for name, action in snapshot["user_actions"].items()
              if node["id"] in set(action.get("services", [])) | set(action.get("declared_services", []))]
        node["user_actions"] = on
        shares = [snapshot["user_actions"][name].get("share_of_requests") for name in on]
        node["share_of_requests"] = (round(sum(shares), 3) if shares and all(s is not None for s in shares)
                                     else 0.0 if not on and source_state.get("locust", {}).get("status") == "available"
                                     else None)
        node["risk_hints"] = [hint for hint in node.get("risk_hints", []) if not hint.startswith("on ")]
        if node["share_of_requests"]:
            node["risk_hints"].append(f"on {node['share_of_requests']:.0%} of user requests")
    for name in features:
        old = f"'{name}': no traces yet; its dependencies are unknown"
        if old in snapshot["warnings"]:
            snapshot["warnings"].remove(old)
            snapshot["warnings"].append(
                f"'{name}': no recent trace; source-backed route is retained, additional runtime calls may be unknown")
    fingerprint = json.dumps({"nodes": snapshot["nodes"], "dependencies": dependencies,
                              "features": features, "generated_at": snapshot["generated_at_utc"]},
                             sort_keys=True).encode()
    snapshot["snapshot_id"] = hashlib.sha256(fingerprint).hexdigest()[:16]
    validate(snapshot)
    return snapshot


def validate(snapshot: dict) -> None:
    if snapshot.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported dependency snapshot version")
    nodes = {node["id"] for node in snapshot["nodes"]}
    ids = set()
    for dep in snapshot["dependencies"]:
        if dep["id"] in ids:
            raise ValueError(f"duplicate dependency: {dep['id']}")
        ids.add(dep["id"])
        if dep["consumer"] not in nodes or dep["provider"] not in nodes:
            raise ValueError(f"dependency has an unknown endpoint: {dep['id']}")
        if not dep["evidence"] or dep["verification"] not in VERIFICATION:
            raise ValueError(f"dependency lacks evidence or verification: {dep['id']}")
        if any(not all(e.get(field) for field in
                       ("kind", "locator", "detail", "observed_at", "verification"))
               or e["verification"] not in VERIFICATION for e in dep["evidence"]):
            raise ValueError(f"incomplete evidence: {dep['id']}")
        if not dep["phases"] or not set(dep["phases"]) <= PHASES:
            raise ValueError(f"invalid phases: {dep['id']}")
        if any(effect["outcome"] not in OUTCOMES for effect in dep["effects"].values()):
            raise ValueError(f"invalid outcome: {dep['id']}")
    for feature in snapshot["features"].values():
        if not set(feature["steps"]) <= ids:
            raise ValueError("feature references an unknown dependency")


def refresh_freshness(snapshot: dict, now: datetime | None = None) -> dict:
    """Mark old observations stale while retaining their original status and evidence."""
    now = now or datetime.now(timezone.utc)
    result = deepcopy(snapshot)
    for state in result.get("source_state", {}).values():
        ttl = state.get("ttl_seconds")
        observed = state.get("observed_at")
        state["stale"] = (ttl is not None and observed is not None
                          and (now - datetime.fromisoformat(observed)).total_seconds() > ttl)
    return result
