import json
import tempfile
import unittest
from pathlib import Path

from freshpolicy.catalog import Catalog
from freshpolicy.evaluation import compute_trace, fit_models, normalize_spec, run_trial
from freshpolicy.service import Application


class EvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = Catalog("data")

    def test_default_split_is_holdout(self):
        spec = normalize_spec(self.catalog, {})
        self.assertLess(spec["fit_end"], spec["eval_start"])
        self.assertEqual(spec["fit_start"], "2026-08-01")
        self.assertEqual(spec["eval_end"], "2026-10-23")

    def test_fit_uses_only_fit_window(self):
        spec = normalize_spec(self.catalog, {})
        first = fit_models(self.catalog, spec["fit_start"], spec["fit_end"])
        moved = fit_models(self.catalog, spec["fit_start"], spec["fit_end"])
        self.assertEqual(first["intents"], moved["intents"])
        shorter = fit_models(self.catalog, spec["fit_start"], "2026-08-31")
        self.assertNotEqual(len(first["intents"]["鱼"]), len(shorter["intents"]["鱼"]))

    def test_missing_intents_not_imputed_from_sales(self):
        spec = normalize_spec(self.catalog, {})
        models = fit_models(self.catalog, spec["fit_start"], spec["fit_end"])
        for sku in models["skus"]:
            self.assertNotIn(None, models["intents"][sku])
        evidence = models["evidence"]["demand"]
        self.assertTrue(all(item["missing_days"] > 0 for item in evidence.values()))
        known_total = sum(item["known_days"] for item in evidence.values())
        missing_total = sum(item["missing_days"] for item in evidence.values())
        self.assertEqual(known_total + missing_total, 58 * 3)

    def test_trial_is_reproducible(self):
        spec = normalize_spec(self.catalog, {"scenarios": 8})
        first = run_trial(self.catalog, spec)
        second = run_trial(self.catalog, spec)
        self.assertEqual(first["bootstrap"]["rules"], second["bootstrap"]["rules"])
        other = run_trial(self.catalog, normalize_spec(self.catalog, {"scenarios": 8, "seed": 7}))
        self.assertNotEqual(first["bootstrap"]["rules"], other["bootstrap"]["rules"])

    def test_scenarios_shared_across_rules(self):
        spec = normalize_spec(self.catalog, {"scenarios": 4})
        trace_a = compute_trace(self.catalog, spec, "sold_average", 1)
        trace_b = compute_trace(self.catalog, spec, "order_up_to", 1)
        intents_a = [(day["date"], sku, day["skus"][sku]["intent_kg"]) for day in trace_a["days"] for sku in day["skus"]]
        intents_b = [(day["date"], sku, day["skus"][sku]["intent_kg"]) for day in trace_b["days"] for sku in day["skus"]]
        self.assertEqual(intents_a, intents_b)

    def test_report_structure(self):
        spec = normalize_spec(self.catalog, {"scenarios": 4})
        report = run_trial(self.catalog, spec)
        self.assertTrue(report["assumptions"])
        self.assertIn("demand", report["evidence"])
        rules = report["bootstrap"]["rules"]
        self.assertEqual(set(rules), {"sold_average", "intent_average", "order_up_to"})
        for metrics in rules.values():
            for metric in ("stockout_kg", "fill_rate", "waste_kg", "contribution"):
                self.assertIn("ci95", metrics and metrics[metric])
        for diffs in report["bootstrap"]["paired_vs_baseline"].values():
            for metric in ("stockout_kg", "fill_rate", "waste_kg", "contribution"):
                self.assertIn("share_positive", diffs[metric])
        self.assertEqual(report["bootstrap"]["scenario_count"], 4)
        self.assertIn("历史回放", [item["name"] for item in report["scenario_index"]])
        self.assertTrue(report["stress"])
        self.assertIn("0.85", report["sensitivity"]["demand_scale"])
        self.assertIn("1.15", report["sensitivity"]["demand_scale"])
        json.dumps(report, ensure_ascii=False, allow_nan=False)

    def test_trace_matches_metrics(self):
        spec = normalize_spec(self.catalog, {"scenarios": 4})
        trace = compute_trace(self.catalog, spec, "intent_average", 1)
        self.assertEqual(trace["scenario"]["index"], 1)
        revenue = sum(day["money"]["revenue"] for day in trace["days"])
        self.assertAlmostEqual(revenue, trace["metrics"]["revenue"], places=4)
        stockout = sum(day["skus"][sku]["stockout_kg"] for day in trace["days"] for sku in day["skus"])
        self.assertAlmostEqual(stockout, trace["metrics"]["stockout_kg"], places=4)

    def test_spec_validation(self):
        with self.assertRaises(ValueError):
            normalize_spec(self.catalog, {"unknown": 1})
        with self.assertRaises(ValueError):
            normalize_spec(self.catalog, {"eval_start": "2026-08-01"})
        with self.assertRaises(ValueError):
            normalize_spec(self.catalog, {"eval_end": "2027-01-01"})
        with self.assertRaises(ValueError):
            normalize_spec(self.catalog, {"scenarios": 1})
        with self.assertRaises(ValueError):
            normalize_spec(self.catalog, {"seed": -1})
        with self.assertRaises(ValueError):
            normalize_spec(self.catalog, {"rules": []})
        with self.assertRaises(ValueError):
            normalize_spec(self.catalog, {"rules": [{"name": "sold_average"}, {"name": "sold_average"}]})
        with self.assertRaises(ValueError):
            normalize_spec(self.catalog, {"rules": [{"name": "intent_average"}], "baseline": "sold_average"})
        with self.assertRaises(ValueError):
            normalize_spec(self.catalog, {"demand_scale_band": [0.1, 3.0]})
        spec = normalize_spec(self.catalog, {"rules": [{"name": "intent_average", "params": {"window": 5}}]})
        self.assertEqual(spec["rules"][0]["params"]["window"], 5)
        self.assertEqual(spec["baseline"], "intent_average")


class TrialServiceTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)
        self.application = Application("data", self.root / "state")

    def post_trial(self, body=None):
        return self.application.dispatch("POST", "/trials", body if body is not None else {"scenarios": 4})

    def test_trial_round_trip(self):
        status, created = self.post_trial()
        self.assertEqual(status, 201)
        self.assertEqual(created["source_fingerprint"], self.application.catalog.fingerprint)
        status, restored = self.application.dispatch("GET", "/trials/" + created["id"])
        self.assertEqual(status, 200)
        self.assertEqual(restored, created)
        listing = self.application.dispatch("GET", "/trials")[1]["trials"]
        self.assertEqual([item["id"] for item in listing], [created["id"]])

    def test_trial_rejects_bad_requests(self):
        self.assertEqual(self.application.dispatch("POST", "/trials", None)[0], 400)
        self.assertEqual(self.post_trial({"extra": 1})[0], 400)
        self.assertEqual(self.post_trial({"scenarios": 1000})[0], 400)
        self.assertEqual(self.post_trial({"rules": [{"name": "不存在的规则"}]})[0], 400)
        self.assertEqual(self.application.dispatch("GET", "/trials/absent")[0], 404)

    def test_trial_record_integrity(self):
        _, created = self.post_trial()
        target = self.root / "state" / "trials" / (created["id"] + ".json")
        envelope = json.loads(target.read_text())
        envelope["content"]["report"]["bootstrap"]["baseline"] = "被改动"
        target.write_text(json.dumps(envelope))
        self.assertEqual(self.application.dispatch("GET", "/trials/" + created["id"])[0], 400)

    def test_trace_endpoint(self):
        _, created = self.post_trial()
        status, trace = self.application.dispatch("GET", f"/trials/{created['id']}/trace?rule=order_up_to&scenario=1")
        self.assertEqual(status, 200)
        self.assertEqual(trace["rule"], "order_up_to")
        self.assertTrue(trace["days"])
        day = trace["days"][0]
        self.assertIn("closing_batches", day["skus"]["鱼"])
        self.assertIn("inventory_value_total", day)
        self.assertIn("contribution", day["money"])
        self.assertEqual(self.application.dispatch("GET", f"/trials/{created['id']}/trace?rule=absent")[0], 400)
        self.assertEqual(self.application.dispatch("GET", f"/trials/{created['id']}/trace?scenario=999")[0], 400)
        self.assertEqual(self.application.dispatch("GET", f"/trials/{created['id']}/trace?scenario=abc")[0], 400)

    def test_trace_is_deterministic(self):
        _, created = self.post_trial()
        first = self.application.dispatch("GET", f"/trials/{created['id']}/trace?rule=intent_average&scenario=2")[1]
        second = self.application.dispatch("GET", f"/trials/{created['id']}/trace?rule=intent_average&scenario=2")[1]
        self.assertEqual(first, second)

    def test_same_spec_same_metrics(self):
        _, first = self.post_trial({"scenarios": 4, "label": "甲"})
        _, second = self.post_trial({"scenarios": 4, "label": "乙"})
        self.assertEqual(first["report"]["bootstrap"]["rules"], second["report"]["bootstrap"]["rules"])
        self.assertNotEqual(first["id"], second["id"])


if __name__ == "__main__":
    unittest.main()
