"""The public dashboard must represent recorded evidence accurately."""

import json
from copy import deepcopy
import tempfile
import unittest
from pathlib import Path

from chaoslab import dashboard


class DashboardTest(unittest.TestCase):
    def setUp(self):
        self.snapshot = json.loads((dashboard.ROOT / "reports/dependency-map.json").read_text())
        self.demo = json.loads(dashboard.DEMO_FILE.read_text())

    def test_demo_shows_semantic_failure_and_historical_snapshot(self):
        page = dashboard.render(self.snapshot, self.demo, "recorded demo")
        self.assertIn("HTTP 200 with 6 missing baseline visits", page)
        self.assertIn("2 baseline visits still missing", page)
        self.assertIn("Recovery not confirmed", page)
        self.assertIn("9.3s", page)
        self.assertIn("10,000 ms", page)
        self.assertIn("Run prediction used an earlier dependency snapshot", page)
        self.assertIn("data-ttl=\"30\"", page)
        self.assertNotIn("<button", page)
        self.assertNotIn("<form", page)
        self.assertNotIn("src=\"http", page)

    def test_newest_finished_local_run_wins_and_demo_is_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            runs.mkdir()
            for name, state in (("20261010-100000", "finished"),
                                ("20261011-100000", "pending"),
                                ("20261012-100000", "finished")):
                folder = runs / name
                folder.mkdir()
                (folder / "result.json").write_text(json.dumps({
                    "id": name, "outcome": "completed", "timeline": [{"elapsed": 1}],
                    "recovery_pending": state == "pending"}))
            selected, source = dashboard.select_run(runs)
            self.assertEqual(selected["id"], "20261012-100000")
            self.assertEqual(source, "local run")
            selected, source = dashboard.select_run(runs, use_demo=True)
            self.assertEqual(selected["id"], self.demo["id"])
            self.assertEqual(source, "recorded demo")
            for file in runs.glob("*/result.json"):
                file.unlink()
            selected, source = dashboard.select_run(runs)
            self.assertEqual(selected["id"], self.demo["id"])
            self.assertEqual(source, "recorded demo")
            output = Path(tmp) / "dashboard.html"
            dashboard.build(snapshot=self.snapshot, runs_dir=runs, output_path=output)
            self.assertTrue(output.exists())
            self.assertIn("HTTP 200 with 6 missing baseline visits", output.read_text())

    def test_matching_snapshot_has_no_mismatch_warning(self):
        self.snapshot["snapshot_id"] = self.demo["prediction"]["snapshot_id"]
        page = dashboard.render(self.snapshot, self.demo, "recorded demo")
        self.assertIn("share a snapshot", page)
        self.assertNotIn("differs from displayed map", page)

    def test_other_finished_run_does_not_inherit_visits_demo_claims(self):
        run = deepcopy(self.demo)
        run["fault"]["target"] = "customers-service"
        run["semantic_observations"][0]["status"] = 503
        run["outcome"] = "completed"
        run["abort_reason"] = None
        run["recovered"] = True
        run["semantic_observations"][-1]["missing_visit_count"] = 0
        run["semantic_observations"][-1]["correctness"] = "preserved"
        page = dashboard.render(self.snapshot, run, "local run")
        self.assertIn("What changed when the service failed?", page)
        self.assertIn("The experiment confirmed recovery", page)
        self.assertNotIn("A controlled Visits-service crash", page)
        self.assertNotIn("Watchdog stopped the fault", page)

    def test_newer_runs_distinguish_service_recovery_from_data_loss(self):
        run = deepcopy(self.demo)
        run["recovered"] = True
        run["data_loss"] = {"missing_visit_ids": [4, 7], "canary_visit_id": 7,
                            "canary_lost": True}
        page = dashboard.render(self.snapshot, run, "local run")
        self.assertIn("Recovered with data loss", page)
        self.assertIn("The service recovered with data loss", page)
        self.assertNotIn("Recovery confirmed", page)


if __name__ == "__main__":
    unittest.main()
