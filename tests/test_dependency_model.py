import unittest
import json
from unittest.mock import patch

from chaoslab import depmap
from chaoslab.dependency_model import enrich, validate


class DependencyModelTest(unittest.TestCase):
    def test_unknown_trace_endpoint_is_not_invented_as_database(self):
        self.assertEqual(depmap._trace_child("customers-service", "payments-api", set()), "payments-api")
        self.assertEqual(depmap._trace_child("customers-service", "mysql", set()), "customers-db")

    def test_jdbc_schema_name_maps_to_each_services_own_database(self):
        driver = "org.hsqldb.jdbc.JDBCDriver"
        root = {"id": "1", "tags": {"outcome": "SUCCESS", "http.url": "/api/vet/vets"},
                "localEndpoint": {"serviceName": "api-gateway"}}
        jdbc_span = {"id": "2", "parentId": "1", "kind": "CLIENT",
                     "localEndpoint": {"serviceName": "customers-service"},
                     "remoteEndpoint": {"serviceName": "public"},
                     "tags": {"jdbc.datasource.driver": driver, "jdbc.datasource.name": "PUBLIC"}}
        unnamed_jdbc_span = {"id": "3", "parentId": "1", "kind": "CLIENT",
                             "localEndpoint": {"serviceName": "vets-service"},
                             "tags": {"jdbc.datasource.driver": driver}}
        jdbc = depmap._jdbc_calls([[root, jdbc_span, unnamed_jdbc_span]])
        self.assertEqual(jdbc[("customers-service", "public")], driver)
        # The same schema name from two services is two databases, not one shared endpoint.
        self.assertEqual(depmap._trace_child("customers-service", "public", set(), jdbc), "customers-db")
        self.assertEqual(depmap._trace_child("visits-service", "public", set(),
                                             {("visits-service", "public"): driver}), "visits-db")
        # Without JDBC evidence an unfamiliar name stays an unknown endpoint.
        self.assertEqual(depmap._trace_child("customers-service", "public", set()), "public")
        paths = depmap._paths_from_traces([[root, unnamed_jdbc_span]], set())
        self.assertIn("vets-db", paths["list vets"]["services"])

    def test_report_renders_external_endpoints_and_verified_databases(self):
        names = ("api-gateway", "customers-service", "visits-service", "vets-service",
                 "config-server", "discovery-server", "admin-server", "genai-service")
        nodes = [{"id": n, "role": "service", "kind": "container", "instances": 1,
                  "auto_restart": False, "risk_hints": []} for n in names]
        nodes += [{"id": "payments-api", "role": "external", "kind": "observed endpoint",
                   "instances": None, "risk_hints": []},
                  {"id": "customers-db", "role": "datastore", "kind": "embedded in customers-service",
                   "inside": "customers-service", "instances": None, "risk_hints": [],
                   "jdbc_driver": "org.hsqldb.jdbc.JDBCDriver"}]
        base = {"generated_at": "now", "entry_point": "api-gateway", "nodes": nodes,
                "edges": [], "user_actions": {}, "warnings": [], "sources": {}, "not_mapped": []}
        snapshot = enrich(base, active_services=set(names) - {"genai-service"}, source_state={})
        database = next(n for n in snapshot["nodes"] if n["id"] == "customers-db")
        self.assertEqual(database["verification"], "verified")
        self.assertIn("org.hsqldb.jdbc.JDBCDriver", database["evidence"][0]["detail"])
        persistence = next(d for d in snapshot["dependencies"] if d["id"] == "persistence:customers-service")
        self.assertEqual(persistence["verification"], "verified")
        self.assertIn("payments-api", depmap.markdown(snapshot))
        self.assertIn("payments-api", depmap.html_page(snapshot))

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

    def test_reviewed_manifest_carries_measured_effects_and_restart_data_loss(self):
        names = ("api-gateway", "customers-service", "visits-service", "vets-service",
                 "config-server", "discovery-server", "admin-server", "genai-service")
        base = {"generated_at": "now", "entry_point": "api-gateway",
                "nodes": [{"id": n, "kind": "container"} for n in names],
                "edges": [], "user_actions": {}, "warnings": []}
        snapshot = enrich(base, active_services=set(names), source_state={})
        visits = next(d for d in snapshot["dependencies"] if d["id"] == "owner-details:visits-service")
        self.assertEqual(visits["effects"]["slow"]["basis"], "measured")
        self.assertTrue(any(e["kind"] == "experiment" for e in visits["evidence"]))
        restart = snapshot["restart_effects"]["visits-service"]
        self.assertEqual(restart["outcome"], "degraded")
        self.assertEqual(restart["evidence"][0]["verification"], "verified")
        self.assertEqual(snapshot["restart_effects"]["customers-service"]["evidence"][0]["verification"],
                         "inferred")
        snapshot["restart_effects"]["visits-service"]["features"] = ["no such feature"]
        with self.assertRaises(ValueError):
            validate(snapshot)

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
