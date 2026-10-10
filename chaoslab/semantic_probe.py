"""Check owner-details response content, including successful but incomplete 200s.

Correctness of one probe, against the visit IDs owner 6 had before the fault:
  preserved    every expected visit is present
  degraded     no visits at all: the gateway's circuit-breaker fallback answered
  data_lost    visits are served but some expected ones are gone for good
               (the visits fallback is all-or-nothing, so a partial list means
               the store itself lost them, e.g. an in-memory database restarted)
  unavailable  an error or no answer
"""

import json
import time
from datetime import date
from urllib.error import HTTPError
from urllib.request import Request, urlopen

OWNER_ID = 6
OWNER_URL = f"http://localhost:8080/api/gateway/owners/{OWNER_ID}"
CANARY_PET_ID = 7          # one of owner 6's pets
CANARY_URL = f"http://localhost:8080/api/visit/owners/{OWNER_ID}/pets/{CANARY_PET_ID}/visits"
MIN_ACTION_REQUESTS = 3   # fewer finished requests cannot confirm or contradict a prediction


def visit_ids(owner: dict) -> set[int]:
    return {visit["id"] for pet in owner["pets"] for visit in pet["visits"]}


def check(expected_ids: set[int] | None = None, url: str = OWNER_URL, timeout_s: float = 12) -> dict:
    """Return observed status, latency, and visit completeness for owner 6."""
    started = time.monotonic()
    try:
        with urlopen(url, timeout=timeout_s) as response:
            status = response.status
            owner = json.load(response)
        ids = visit_ids(owner)
        missing = sorted(expected_ids - ids) if expected_ids is not None else []
        correctness = "preserved" if not missing else "data_lost" if ids else "degraded"
        return {"status": status, "latency_ms": round((time.monotonic() - started) * 1000),
                "visit_ids": sorted(ids), "missing_visit_ids": missing,
                "correctness": correctness}
    except HTTPError as err:
        return {"status": err.code, "latency_ms": round((time.monotonic() - started) * 1000),
                "visit_ids": [], "missing_visit_ids": sorted(expected_ids or []),
                "correctness": "unavailable"}
    # OSError also covers connection resets, which faults cause; an uncaught one
    # would end the probe thread silently.
    except (OSError, ValueError, KeyError, TypeError) as err:
        return {"status": None, "latency_ms": round((time.monotonic() - started) * 1000),
                "visit_ids": [], "missing_visit_ids": sorted(expected_ids or []),
                "correctness": "unavailable", "error": str(err)}


def add_canary(label: str, url: str = CANARY_URL) -> int:
    """Write one visit to owner 6 and return its ID.

    A write acknowledged before a fault must survive it; checking this visit
    afterwards is how the run detects lost data.
    """
    body = json.dumps({"date": date.today().isoformat(),
                       "description": f"chaoslab canary {label}"}).encode()
    request = Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=5) as response:
            return int(json.load(response)["id"])
    except (OSError, ValueError, KeyError, TypeError) as err:  # OSError covers HTTP and URL errors
        raise ValueError(f"could not write the canary visit: {err}") from err


def baseline(url: str = OWNER_URL) -> dict:
    observation = check(url=url, timeout_s=3)
    if observation["status"] != 200 or len(observation["visit_ids"]) < 4:
        raise ValueError("owner 6 must return HTTP 200 with at least four seeded visits")
    return observation


def compare(prediction: dict | None, observations: list[dict],
            action_observations: dict | None = None) -> dict:
    """Compare only observable effects; lack of samples remains unobserved.

    `action_observations` should cover the whole disturbance (injection until
    users get normal service back): requests that hang during a fault often
    finish, and fail, only after it has been undone.
    """
    if prediction is None:
        return {}
    action_observations = action_observations or {}
    comparisons = {}
    grouped = {}
    for impact in prediction["runtime"]:
        grouped.setdefault(impact["component"], []).append(impact)
    for name, impacts in grouped.items():
        outcomes = {impact["outcome"] for impact in impacts}
        expected = next(iter(outcomes)) if len(outcomes) == 1 else "uncertain"
        observed = "unobserved"
        fault_probes = [item for item in observations if item.get("phase", "fault") == "fault"]
        recovery_probes = [item for item in observations if item.get("phase") == "recovery"]
        if name == "owner details" and fault_probes:
            values = {item["correctness"] for item in fault_probes}
            observed = ("unavailable" if "unavailable" in values else
                        "degraded" if values & {"degraded", "data_lost"} else "preserved")
        elif action_observations.get(name, {}).get("request_count", 0) >= MIN_ACTION_REQUESTS:
            rate = action_observations[name]["error_rate"]
            observed = "unavailable" if rate > 0.1 else "preserved"
        comparisons[name] = {"predicted": expected, "observed": observed,
                             "observed_statuses": sorted({item["status"] for item in fault_probes
                                                          if item.get("status") is not None})
                             if name == "owner details" else [],
                             "peak_observed_latency_ms": max((item["latency_ms"] for item in fault_probes
                                                               if item.get("latency_ms") is not None),
                                                              default=None)
                             if name == "owner details" else None,
                             "recovery_correctness": (recovery_probes[-1]["correctness"]
                                                      if name == "owner details" and recovery_probes else None),
                             "alternatives": [{"outcome": impact["outcome"],
                                               "path": impact.get("path", []), "rule": impact.get("rule")}
                                              for impact in impacts],
                             "fault_requests": action_observations.get(name, {}).get("request_count", 0),
                             "fault_failures": action_observations.get(name, {}).get("failure_count", 0),
                             "fault_probes": len(fault_probes) if name == "owner details" else 0,
                             "match": ("unobserved" if observed == "unobserved" or expected == "uncertain"
                                       else "matched" if expected == observed else "contradicted")}
    # Restart predictions (data an in-memory store loses) are checked by the last
    # correctness probe once users have service again.
    recovery_probes = [item for item in observations if item.get("phase") == "recovery"]
    final = recovery_probes[-1]["correctness"] if recovery_probes else None
    for impact in prediction.get("startup_restart", []):
        if impact.get("probe") != "owner details":
            continue
        observed = {"data_lost": "degraded", "preserved": "preserved"}.get(final, "unobserved")
        comparisons[f"{impact['component']} after restart"] = {
            "predicted": impact["outcome"], "observed": observed, "observed_statuses": [],
            "peak_observed_latency_ms": None, "recovery_correctness": final,
            "alternatives": [{"outcome": impact["outcome"], "path": impact.get("path", []),
                              "rule": impact.get("rule")}],
            "fault_requests": 0, "fault_failures": 0, "fault_probes": len(recovery_probes),
            "match": ("unobserved" if observed == "unobserved"
                      else "matched" if impact["outcome"] == observed else "contradicted")}
    return comparisons
