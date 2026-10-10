import io
import json
import unittest
from unittest.mock import patch

from chaoslab import semantic_probe
from chaoslab.experiment import _action_observations
from chaoslab.metrics import Sample, Snapshot
from chaoslab.safety import Guardrails


class Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class SemanticProbeTest(unittest.TestCase):
    def test_http_200_missing_baseline_visit_is_degraded(self):
        owner = {"pets": [{"visits": [{"id": 1}]}]}
        with patch.object(semantic_probe, "urlopen", return_value=Response(json.dumps(owner).encode())):
            result = semantic_probe.check({1, 2})
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["correctness"], "degraded")
        self.assertEqual(result["missing_visit_ids"], [2])

    def test_comparison_marks_unobserved_when_no_probe_sample(self):
        prediction = {"runtime": [{"component": "owner details", "outcome": "degraded"}]}
        self.assertEqual(semantic_probe.compare(prediction, [])["owner details"]["match"], "unobserved")

    def test_correctness_beats_successful_status_and_triggers_watchdog(self):
        prediction = {"runtime": [{"component": "owner details", "outcome": "degraded"}]}
        observation = {"phase": "fault", "status": 200, "correctness": "degraded"}
        self.assertEqual(semantic_probe.compare(prediction, [observation])["owner details"]["match"],
                         "matched")
        sample = Sample(elapsed=1, phase="fault", rps=5, error_rate=0, p95_ms=30,
                        stalled=False, semantic_correctness="degraded")
        self.assertIn("correctness", Guardrails().breach(sample, baseline_rps=5))

    def test_recovery_traffic_does_not_turn_unsampled_fault_into_success(self):
        prediction = {"runtime": [{"component": "add visit", "outcome": "unavailable"}]}
        before = Snapshot(1, {"add visit": (10, 0)}, None, 1)
        after = Snapshot(2, {"add visit": (10, 0)}, None, 1)
        result = semantic_probe.compare(prediction, [], _action_observations(before, after))
        self.assertEqual(result["add visit"]["observed"], "unobserved")
        self.assertEqual(result["add visit"]["fault_requests"], 0)

    def test_hung_requests_that_fail_after_the_undo_still_count(self):
        prediction = {"runtime": [{"component": "list owners", "outcome": "unavailable"}]}
        inject = Snapshot(1, {"list owners": (100, 0)}, None, 10)
        revert = Snapshot(2, {"list owners": (103, 0)}, None, 10)      # the rest are still hanging
        recovered = Snapshot(3, {"list owners": (125, 7)}, None, 10)   # ...and fail after the undo
        fault_only = semantic_probe.compare(prediction, [], _action_observations(inject, revert))
        disturbance = semantic_probe.compare(prediction, [], _action_observations(inject, recovered))
        self.assertEqual(fault_only["list owners"]["match"], "contradicted")  # the old, false verdict
        self.assertEqual(disturbance["list owners"]["match"], "matched")
        self.assertEqual(disturbance["list owners"]["fault_failures"], 7)

    def test_too_few_finished_requests_stay_unobserved(self):
        prediction = {"runtime": [{"component": "pet types", "outcome": "unavailable"}]}
        before = Snapshot(1, {"pet types": (10, 0)}, None, 10)
        after = Snapshot(2, {"pet types": (12, 0)}, None, 10)
        result = semantic_probe.compare(prediction, [], _action_observations(before, after))
        self.assertEqual(result["pet types"]["observed"], "unobserved")
        self.assertEqual(result["pet types"]["match"], "unobserved")

    def test_multiple_prediction_paths_remain_visible(self):
        prediction = {"runtime": [
            {"component": "owner details", "outcome": "degraded", "path": ["a"], "rule": "a"},
            {"component": "owner details", "outcome": "unavailable", "path": ["b"], "rule": "b"}]}
        result = semantic_probe.compare(prediction, [{"correctness": "degraded"}])
        self.assertEqual(result["owner details"]["predicted"], "uncertain")
        self.assertEqual(len(result["owner details"]["alternatives"]), 2)


if __name__ == "__main__":
    unittest.main()
