import unittest
import json
from unittest.mock import patch

from chaoslab import depmap
from chaoslab.dependency_model import enrich, validate


class DependencyModelTest(unittest.TestCase):
    def test_unknown_trace_endpoint_is_not_invented_as_database(self):
        self.assertEqual(depmap._trace_child("customers-service", "payments-api", set()), "payments-api")
        self.assertEqual(depmap._trace_child("customers-service", "mysql", set()), "customers-db")

    def test_map_retains_static_dependencies_when_runtime_sources_fail(self):
        names = ("config-server", "discovery-server", "api-gateway", "customers-service",
                 "visits-service", "vets-service", "admin-server", "load-generator")
        compose = {n: {"labels": {"chaoslab.target": "true"}, "depends_on": {}}
                   for n in names if n != "load-generator"}
        compose["load-generator"] = {"image": "locustio/locust:fake",
                                     "command": ["--host", "http://api-gateway:8080"]}
        all_compose = dict(compose, **{"genai-service": {"labels": {"chaoslab.target": "true"},
                                                          "profiles": ["genai"]}})

        def fake_docker(*args, **kwargs):
            if args[0] == "compose":
                return json.dumps({"services": all_compose if "genai" in args else compose})
            return ""

        with patch.object(depmap, "docker", side_effect=fake_docker), \
                patch.object(depmap, "_get_json", side_effect=OSError("offline")):
            snapshot = depmap.build()
        self.assertIsNone(snapshot["user_actions"]["owner details"]["share_of_requests"])
        self.assertEqual(snapshot["source_state"]["zipkin"]["status"], "unavailable")
        self.assertEqual(snapshot["source_state"]["locust"]["status"], "unavailable")
        self.assertFalse(snapshot["features"]["genai chat"]["active"])
        self.assertTrue(any(d["id"] == "owner-details:visits-service"
                            for d in snapshot["dependencies"]))
        self.assertIn("list vets", next(n for n in snapshot["nodes"]
                                         if n["id"] == "vets-service")["user_actions"])
        self.assertTrue(any(d["purpose"] == "persistence" and d["verification"] == "inferred"
                            for d in snapshot["dependencies"]))
        self.assertIn("Dependency semantics", depmap.markdown(snapshot))
        self.assertIn("Potential blast radius", depmap.html_page(snapshot))
        run = {"id": "fixture-run", "fault": {"target": "visits-service", "kind": "kill"},
               "outcome": "completed", "recovered": True,
               "prediction": {"snapshot_id": snapshot["snapshot_id"]},
               "prediction_comparison": {"owner details": {"predicted": "degraded",
                                                        "observed": "degraded", "match": "matched",
                                                        "fault_requests": 1}}}
        self.assertIn('"id": "fixture-run"', depmap.html_page(snapshot, run))
        run["prediction"]["snapshot_id"] = "older-map"
        self.assertNotIn('"id": "fixture-run"', depmap.html_page(snapshot, run))

    def test_static_facts_survive_missing_telemetry_and_inactive_profile(self):
        names = ("api-gateway", "customers-service", "visits-service", "vets-service",
                 "config-server", "discovery-server", "admin-server", "genai-service")
        base = {"generated_at": "2026-10-10 14:00:00", "entry_point": "api-gateway",
                "nodes": [{"id": n, "kind": "container", "instances": 0} for n in names],
                "edges": [], "user_actions": {}, "warnings": []}
        snapshot = enrich(base, active_services=set(names) - {"genai-service"}, source_state={})
        validate(snapshot)
        self.assertFalse(next(n for n in snapshot["nodes"] if n["id"] == "genai-service")["active"])
        owner = snapshot["features"]["owner details"]
        self.assertEqual(owner["steps"], ["owner-details:customers-service", "owner-details:visits-service"])
        self.assertTrue(any(d["provider"] == "visits-service" and d["verification"] == "verified"
                            for d in snapshot["dependencies"]))
        self.assertTrue(all({"kind", "locator", "detail", "observed_at", "verification"} <= set(e)
                            for dep in snapshot["dependencies"] for e in dep["evidence"]))
        self.assertFalse(any(d["provider"] == "grafana-server" for d in snapshot["dependencies"]))
        self.assertEqual(snapshot["features"]["list vets"]["cache"]["active_in_docker"], False)
        self.assertTrue(any(d["id"] == "config-runtime:api-gateway" and
                            d["effects"]["unavailable"]["outcome"] == "uncertain"
                            for d in snapshot["dependencies"]))

    def test_source_paths_supply_traffic_exposure_without_traces(self):
        names = ("api-gateway", "customers-service", "visits-service", "vets-service",
                 "config-server", "discovery-server", "admin-server", "genai-service")
        base = {"generated_at": "now", "entry_point": "api-gateway",
                "nodes": [{"id": n, "kind": "container"} for n in names],
                "edges": [], "warnings": [], "user_actions": {
                    "list owners": {"route": "/api/customer/owners", "services": [], "share_of_requests": 0.6},
                    "list vets": {"route": "/api/vet/vets", "services": [], "share_of_requests": 0.4}}}
        snapshot = enrich(base, set(names), {"locust": {"status": "available"}})
        shares = {n["id"]: n["share_of_requests"] for n in snapshot["nodes"]}
        self.assertEqual(shares["api-gateway"], 1.0)
        self.assertEqual(shares["customers-service"], 0.6)
        self.assertEqual(shares["vets-service"], 0.4)

    def test_invalid_edge_without_evidence_is_rejected(self):
        names = ("api-gateway", "customers-service", "visits-service", "vets-service",
                 "config-server", "discovery-server", "admin-server", "genai-service")
        base = {"generated_at": "now", "entry_point": "api-gateway",
                "nodes": [{"id": n, "kind": "container"} for n in names],
                "edges": [], "user_actions": {}, "warnings": []}
        snapshot = enrich(base, active_services=set(names), source_state={})
        snapshot["dependencies"].append({"id": "bad", "consumer": "api-gateway", "provider": "vets-service", "phases": ["runtime"],
                                         "verification": "verified", "evidence": []})
        with self.assertRaises(ValueError):
            validate(snapshot)


if __name__ == "__main__":
    unittest.main()
