import unittest

from freshpolicy import simulation
from freshpolicy.simulation import RULES, run_simulation, validate_rule_params


def make_setup(days=("2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"), skus=("鱼",)):
    return {
        "skus": list(skus),
        "prices": {sku: 39.0 for sku in skus},
        "costs": {sku: 23.0 for sku in skus},
        "holding_per_kg": 0.2,
        "disposal_per_kg": 0.6,
        "ordering_fee": 10.0,
        "days": list(days),
        "eval_start": days[0],
        "initial_history": [],
    }


def make_scenario(setup, intent=0.0, delay=0, shelf_life=2, capacity=None, ratio=0.0):
    days = setup["days"]
    expected = []
    from datetime import date, timedelta
    for day in days:
        expected.append((date.fromisoformat(day) + timedelta(days=1)).isoformat())
    return {
        "name": "测试情景", "kind": "test",
        "intent": {day: {sku: float(intent) for sku in setup["skus"]} for day in days},
        "observed": {day: True for day in days},
        "return_ratio": {day: {sku: float(ratio) for sku in setup["skus"]} for day in days},
        "delay": {day: delay for day in expected},
        "capacity": {day: capacity for day in expected},
        "shelf_life": {day: {sku: shelf_life for sku in setup["skus"]} for day in days},
    }


def fixed_rule(quantities):
    def rule(observation, params):
        return {sku: quantities.get(sku, quantities.get("*", 0.0)) for sku in observation["skus"]}
    return rule


def schedule_rule(plan):
    def rule(observation, params):
        return plan.get(observation["date"], {sku: 0.0 for sku in observation["skus"]})
    return rule


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.added = []

    def tearDown(self):
        for name in self.added:
            RULES.pop(name, None)

    def register(self, name, quantities):
        RULES[name] = {"run": fixed_rule(quantities), "defaults": {}}
        self.added.append(name)
        return name

    def register_plan(self, name, plan):
        RULES[name] = {"run": schedule_rule(plan), "defaults": {}}
        self.added.append(name)
        return name

    def test_fefo_consumes_earliest_expiry_first(self):
        setup = make_setup()
        scenario = make_scenario(setup, intent=0.0)
        scenario["intent"]["2026-09-03"]["鱼"] = 5.0
        scenario["shelf_life"]["2026-09-01"]["鱼"] = 2
        scenario["shelf_life"]["2026-09-02"]["鱼"] = 3
        name = self.register_plan("plan_fefo", {"2026-09-01": {"鱼": 10.0}, "2026-09-02": {"鱼": 10.0}})
        metrics, trace = run_simulation(setup, scenario, name, {}, collect_trace=True)
        day3 = trace[2]
        self.assertAlmostEqual(day3["skus"]["鱼"]["sold_kg"], 5.0)
        expired = day3["skus"]["鱼"]["expired"]
        self.assertEqual(len(expired), 1)
        self.assertAlmostEqual(expired[0]["quantity_kg"], 5.0)
        closing = day3["skus"]["鱼"]["closing_batches"]
        self.assertEqual(len(closing), 1)
        self.assertEqual(closing[0]["expires_day"], "2026-09-06")
        self.assertAlmostEqual(closing[0]["quantity_kg"], 10.0)

    def test_expiry_at_midnight_blocks_sale_and_counts_waste(self):
        setup = make_setup()
        scenario = make_scenario(setup, intent=0.0, shelf_life=2)
        scenario["intent"]["2026-09-04"]["鱼"] = 50.0
        name = self.register_plan("plan_expiry", {"2026-09-01": {"鱼": 10.0}})
        metrics, trace = run_simulation(setup, scenario, name, {}, collect_trace=True)
        day3, day4 = trace[2], trace[3]
        self.assertEqual(len(day3["skus"]["鱼"]["expired"]), 1)
        self.assertAlmostEqual(day3["skus"]["鱼"]["expired"][0]["quantity_kg"], 10.0)
        self.assertAlmostEqual(day4["skus"]["鱼"]["sold_kg"], 0.0)
        self.assertAlmostEqual(day4["skus"]["鱼"]["stockout_kg"], 50.0)
        self.assertAlmostEqual(metrics["expired_kg"], 10.0)
        self.assertAlmostEqual(metrics["waste_kg"], metrics["expired_kg"] + metrics["returned_kg"])

    def test_sellable_on_arrival_day_and_day_before_expiry(self):
        setup = make_setup()
        scenario = make_scenario(setup, intent=0.0, shelf_life=2)
        scenario["intent"]["2026-09-02"]["鱼"] = 4.0
        scenario["intent"]["2026-09-03"]["鱼"] = 4.0
        name = self.register("fixed_window", {"鱼": 10.0})
        metrics, trace = run_simulation(setup, scenario, name, {})
        self.assertAlmostEqual(metrics["sold_kg"], 8.0)
        self.assertAlmostEqual(metrics["stockout_kg"], 0.0)

    def test_returns_are_scrapped_not_restocked(self):
        setup = make_setup()
        scenario = make_scenario(setup, intent=10.0, ratio=0.5, shelf_life=3)
        name = self.register("fixed_returns", {"鱼": 10.0})
        metrics, trace = run_simulation(setup, scenario, name, {}, collect_trace=True)
        day2 = trace[1]
        self.assertAlmostEqual(day2["skus"]["鱼"]["sold_kg"], 10.0)
        self.assertAlmostEqual(day2["skus"]["鱼"]["returned_kg"], 5.0)
        self.assertAlmostEqual(sum(b["quantity_kg"] for b in day2["skus"]["鱼"]["closing_batches"]), 0.0)
        self.assertAlmostEqual(metrics["returned_kg"], metrics["sold_kg"] * 0.5)
        self.assertGreater(metrics["disposal_cost"], 0.0)

    def test_carryover_and_holding_cost(self):
        setup = make_setup()
        scenario = make_scenario(setup, intent=0.0, shelf_life=4)
        name = self.register("fixed_hold", {"鱼": 6.0})
        metrics, trace = run_simulation(setup, scenario, name, {}, collect_trace=True)
        day2 = trace[1]
        self.assertAlmostEqual(sum(b["quantity_kg"] for b in day2["skus"]["鱼"]["closing_batches"]), 6.0)
        self.assertAlmostEqual(day2["money"]["holding_cost"], 6.0 * 0.2)
        self.assertGreater(metrics["holding_cost"], 0.0)

    def test_stockout_limited_by_intent_not_sales(self):
        setup = make_setup()
        scenario = make_scenario(setup, intent=8.0)
        name = self.register("fixed_stockout", {"鱼": 0.0})
        metrics, _ = run_simulation(setup, scenario, name, {})
        self.assertAlmostEqual(metrics["sold_kg"], 0.0)
        self.assertAlmostEqual(metrics["stockout_kg"], 8.0 * len(setup["days"]))
        self.assertAlmostEqual(metrics["fill_rate"], 0.0)

    def test_shared_delay_across_skus(self):
        setup = make_setup(skus=("鱼", "虾"))
        scenario = make_scenario(setup, delay=2)
        name = self.register("fixed_delay", {"*": 5.0})
        _, trace = run_simulation(setup, scenario, name, {}, collect_trace=True)
        arrivals = {}
        for entry in trace:
            for sku in setup["skus"]:
                for arrival in entry["skus"][sku]["arrivals"]:
                    arrivals.setdefault(arrival["batch"].split(":")[2], set()).add(entry["date"])
        for dates in arrivals.values():
            self.assertEqual(len(dates), 1)

    def test_capacity_caps_and_allocates_proportionally(self):
        setup = make_setup(skus=("鱼", "虾"))
        scenario = make_scenario(setup, capacity=30.0)
        name = self.register("fixed_capacity", {"*": 30.0})
        _, trace = run_simulation(setup, scenario, name, {}, collect_trace=True)
        day2 = trace[1]
        arrived = {sku: sum(a["quantity_kg"] for a in day2["skus"][sku]["arrivals"]) for sku in setup["skus"]}
        self.assertAlmostEqual(sum(arrived.values()), 30.0)
        self.assertAlmostEqual(arrived["鱼"], 15.0)
        self.assertAlmostEqual(arrived["虾"], 15.0)

    def test_contribution_identity(self):
        setup = make_setup()
        scenario = make_scenario(setup, intent=6.0, ratio=0.1)
        name = self.register("fixed_money", {"鱼": 7.0})
        metrics, _ = run_simulation(setup, scenario, name, {})
        expect = (metrics["revenue"] - metrics["purchase_cost"] - metrics["holding_cost"]
                  - metrics["disposal_cost"] - metrics["ordering_fees"])
        self.assertAlmostEqual(metrics["contribution"], expect)

    def test_no_peek_into_future_scenario(self):
        setup = make_setup(days=tuple(f"2026-09-0{index}" for index in range(1, 8)))
        base = make_scenario(setup, intent=6.0)
        diverged = make_scenario(setup, intent=6.0)
        for day in setup["days"][3:]:
            for sku in setup["skus"]:
                diverged["intent"][day][sku] = 99.0
                diverged["return_ratio"][day][sku] = 0.9
        for day in list(diverged["delay"])[3:]:
            diverged["delay"][day] = 4
        name = "intent_average"
        params = validate_rule_params(name, {"window": 3})
        _, trace_a = run_simulation(setup, base, name, params, collect_trace=True)
        _, trace_b = run_simulation(setup, diverged, name, params, collect_trace=True)
        for entry_a, entry_b in zip(trace_a[:3], trace_b[:3]):
            self.assertEqual(entry_a, entry_b)

    def test_warmup_days_excluded_from_metrics(self):
        setup = make_setup()
        setup["eval_start"] = "2026-09-03"
        scenario = make_scenario(setup, intent=5.0)
        name = self.register("fixed_warmup", {"鱼": 5.0})
        metrics, trace = run_simulation(setup, scenario, name, {}, collect_trace=True)
        self.assertEqual([entry["date"] for entry in trace], ["2026-09-03", "2026-09-04"])
        self.assertAlmostEqual(metrics["intent_kg"], 10.0)


class RuleTests(unittest.TestCase):
    def observation(self, history):
        return {"date": "2026-09-10", "skus": ["鱼"], "on_hand": {"鱼": 0.0},
                "on_order": {"鱼": 0.0}, "batches": {"鱼": []}, "history": history}

    def test_intent_average_ignores_missing_and_uses_known(self):
        history = [
            {"date": "2026-09-08", "sku": "鱼", "requested_kg": None, "sold_kg": 4.0, "returned_kg": 0.0},
            {"date": "2026-09-09", "sku": "鱼", "requested_kg": 10.0, "sold_kg": 9.0, "returned_kg": 0.0},
        ]
        params = validate_rule_params("intent_average", {"window": 2})
        orders = RULES["intent_average"]["run"](self.observation(history), params)
        self.assertAlmostEqual(orders["鱼"], 10.0)

    def test_intent_average_falls_back_to_sold_when_all_missing(self):
        history = [
            {"date": "2026-09-08", "sku": "鱼", "requested_kg": None, "sold_kg": 4.0, "returned_kg": 0.0},
            {"date": "2026-09-09", "sku": "鱼", "requested_kg": None, "sold_kg": 6.0, "returned_kg": 0.0},
        ]
        params = validate_rule_params("intent_average", {"window": 2})
        orders = RULES["intent_average"]["run"](self.observation(history), params)
        self.assertAlmostEqual(orders["鱼"], 5.0)

    def test_sold_average_uses_sales_only(self):
        history = [
            {"date": "2026-09-09", "sku": "鱼", "requested_kg": 20.0, "sold_kg": 6.0, "returned_kg": 0.0},
        ]
        params = validate_rule_params("sold_average", {})
        orders = RULES["sold_average"]["run"](self.observation(history), params)
        self.assertAlmostEqual(orders["鱼"], 6.0)

    def test_order_up_to_subtracts_position(self):
        history = [{"date": "2026-09-09", "sku": "鱼", "requested_kg": 10.0, "sold_kg": 10.0, "returned_kg": 0.0}]
        observation = self.observation(history)
        observation["on_hand"]["鱼"] = 8.0
        observation["on_order"]["鱼"] = 4.0
        params = validate_rule_params("order_up_to", {"lead_days": 1, "safety": 0.0})
        orders = RULES["order_up_to"]["run"](observation, params)
        self.assertAlmostEqual(orders["鱼"], 8.0)

    def test_param_validation(self):
        with self.assertRaises(ValueError):
            validate_rule_params("不存在的规则", {})
        with self.assertRaises(ValueError):
            validate_rule_params("sold_average", {"window": 1})
        with self.assertRaises(ValueError):
            validate_rule_params("sold_average", {"unknown": 3})
        with self.assertRaises(ValueError):
            validate_rule_params("order_up_to", {"safety": 9.0})
        merged = validate_rule_params("order_up_to", {"window": 10})
        self.assertEqual(merged["window"], 10)
        self.assertEqual(merged["lead_days"], 1)


if __name__ == "__main__":
    unittest.main()
