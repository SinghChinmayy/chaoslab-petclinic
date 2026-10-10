import unittest

from chaoslab.blast_radius import analyze


def snapshot():
    return {
        "schema_version": 1,
        "snapshot_id": "fixture",
        "nodes": [{"id": name, "active": True} for name in
                  ("api-gateway", "customers-service", "visits-service", "config-server", "discovery-server")],
        "dependencies": [
            {"id": "owner-customers", "consumer": "api-gateway", "provider": "customers-service",
             "feature": "owner details", "phases": ["runtime"], "verification": "verified",
             "evidence": [{"kind": "source", "locator": "CustomersServiceClient.java"}],
             "effects": {"unavailable": {"outcome": "unavailable", "behavior": "owner lookup fails"}}},
            {"id": "owner-visits", "consumer": "api-gateway", "provider": "visits-service",
             "feature": "owner details", "phases": ["runtime"], "verification": "verified",
             "evidence": [{"kind": "source", "locator": "ApiGatewayController.java"}],
             "effects": {"unavailable": {"outcome": "degraded", "behavior": "empty visits", "http_status": 200}}},
            {"id": "route-visits", "consumer": "api-gateway", "provider": "visits-service",
             "feature": "add visit", "phases": ["runtime"], "verification": "verified",
             "evidence": [{"kind": "source", "locator": "application.yml"}],
             "effects": {"unavailable": {"outcome": "unavailable", "behavior": "gateway fallback", "http_status": 503}}},
            {"id": "gateway-config", "consumer": "api-gateway", "provider": "config-server",
             "feature": None, "phases": ["startup", "restart"], "verification": "verified",
             "evidence": [{"kind": "source", "locator": "application.yml"}],
             "effects": {"unavailable": {"outcome": "unavailable", "behavior": "config import fails",
                                          "propagate_service": True}}},
            {"id": "gateway-discovery", "consumer": "api-gateway", "provider": "discovery-server",
             "feature": None, "phases": ["runtime", "startup", "restart"], "verification": "inferred",
             "evidence": [{"kind": "source", "locator": "compose.yml"}],
             "effects": {"unavailable": {"outcome": "uncertain", "behavior": "cached addresses may work"}}},
        ],
    }


class BlastRadiusTest(unittest.TestCase):
    def test_visits_failure_is_partial_for_owner_details(self):
        result = analyze(snapshot(), "visits-service", "kill")
        runtime = {(i["component"], i["outcome"]): i for i in result["runtime"]}
        self.assertEqual(runtime[("owner details", "degraded")]["http_status"], 200)
        self.assertEqual(runtime[("owner details", "degraded")]["path"],
                         ["api-gateway", "visits-service"])
        self.assertEqual(runtime[("add visit", "unavailable")]["http_status"], 503)
        self.assertEqual(result["startup_restart"], [])

    def test_customers_failure_blocks_owner_lookup(self):
        result = analyze(snapshot(), "customers-service", "kill")
        self.assertTrue(any(i["component"] == "customers-service" and i["outcome"] == "unavailable"
                            for i in result["runtime"]))
        self.assertEqual(next(i for i in result["runtime"] if i["component"] == "owner details")["outcome"],
                         "unavailable")

    def test_config_loss_blocks_restart_not_running_gateway(self):
        result = analyze(snapshot(), "config-server", "kill")
        self.assertFalse(any(i["component"] == "api-gateway" for i in result["runtime"]))
        self.assertTrue(any(i["component"] == "api-gateway" and i["outcome"] == "unavailable"
                            for i in result["startup_restart"]))

    def test_discovery_impact_remains_uncertain(self):
        result = analyze(snapshot(), "discovery-server", "kill")
        impact = next(i for i in result["runtime"] if i["component"] == "api-gateway")
        self.assertEqual(impact["outcome"], "uncertain")
        self.assertEqual(impact["evidence"][0]["locator"], "compose.yml")

    def test_slow_failure_cannot_be_promoted_to_outage(self):
        result = analyze(snapshot(), "visits-service", "delay")
        self.assertEqual(next(i for i in result["runtime"] if i["component"] == "owner details")["outcome"],
                         "uncertain")

    def test_phase_and_feature_selection(self):
        s = snapshot()
        s["features"] = {"owner details": {}}
        result = analyze(s, "visits-service", "kill", phase="runtime", feature="owner details")
        self.assertEqual([i["component"] for i in result["runtime"]], ["owner details"])
        self.assertEqual(result["startup_restart"], [])
        with self.assertRaises(ValueError):
            analyze(s, "visits-service", "kill", feature="missing")

    def test_cycle_does_not_loop(self):
        s = snapshot()
        s["dependencies"].append({"id": "cycle", "consumer": "config-server", "provider": "api-gateway",
                                  "feature": None, "phases": ["startup"], "verification": "inferred",
                                  "evidence": [{"kind": "source", "locator": "fixture"}],
                                  "effects": {"unavailable": {"outcome": "unavailable",
                                                               "propagate_service": True}}})
        self.assertLess(len(analyze(s, "config-server", "kill")["startup_restart"]), 10)


if __name__ == "__main__":
    unittest.main()
