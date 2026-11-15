"""策略试算：拟合、情景生成、规则比较、差异区间与追溯。

拟合只使用留时段之前的资料；情景是规则无关的外生张量，同一组情景
供全部候选规则复用（共同随机数），差异按情景配对。需求意向缺失日
不按销量回填，模型只从已知意向分布抽样，并以需求水平区间给出结论
的敏感范围。极端供货情景单独报告，不并入平均。
"""
import math
from collections import Counter
from datetime import date, timedelta

import numpy as np
from scipy.stats import t as t_distribution

from .simulation import RULES, run_simulation, validate_rule_params

HEADLINE_METRICS = ("stockout_kg", "fill_rate", "waste_kg", "contribution")
DETAIL_METRICS = (
    "intent_kg", "sold_kg", "returned_kg", "expired_kg", "revenue", "purchase_cost",
    "holding_cost", "disposal_cost", "ordering_fees", "terminal_inventory_kg",
    "terminal_inventory_value", "terminal_outstanding_kg",
)
DEFAULT_SEED = 20260801


def _days_between(start, end):
    days = []
    current = date.fromisoformat(start)
    last = date.fromisoformat(end)
    while current <= last:
        days.append(current.isoformat())
        current += timedelta(days=1)
    return days


def _median(values):
    return float(np.median(np.asarray(values, dtype=float))) if values else 0.0


def fit_models(catalog, fit_start, fit_end):
    demand = catalog.rows("demand")
    receipts = catalog.rows("receipts")
    supplier_days = catalog.rows("supplier_days")
    policy = catalog.rows("policy")[0]
    skus = sorted({record["sku"] for record in demand})
    fit_demand = [record for record in demand if fit_start <= record["requested_at"][:10] <= fit_end]
    intents, ratios, prices = {sku: [] for sku in skus}, {sku: [] for sku in skus}, {}
    missing_days = set()
    evidence = {}
    for sku in skus:
        rows = [record for record in fit_demand if record["sku"] == sku]
        known = [record["requested_kg"] for record in rows if record["requested_kg"] is not None]
        intents[sku] = known
        evidence[sku] = {"known_days": len(known), "missing_days": len(rows) - len(known)}
        for record in rows:
            if record["requested_kg"] is None:
                missing_days.add(record["requested_at"][:10])
            ratios[sku].append(record["returned_kg"] / record["sold_kg"] if record["sold_kg"] > 0 else 0.0)
        prices[sku] = float(Counter(record["price"] for record in rows).most_common(1)[0][0])
    fit_receipts = [record for record in receipts if fit_start <= record["expected_day"] <= fit_end]
    shelf_life = {sku: [] for sku in skus}
    costs = {}
    for record in fit_receipts:
        available = date.fromisoformat(record["available_at"][:10])
        expires = date.fromisoformat(record["expires_at"][:10])
        shelf_life[record["sku"]].append((expires - available).days)
    for sku in skus:
        sku_costs = [float(record["unit_cost"]) for record in fit_receipts if record["sku"] == sku]
        costs[sku] = _median(sku_costs)
        if not shelf_life[sku]:
            raise ValueError("拟合窗口内缺少到货记录，无法估计剩余保质期：" + sku)
    fit_days = [record for record in supplier_days if fit_start <= record["day"] <= fit_end]
    if not fit_days:
        raise ValueError("拟合窗口内缺少供应商日记录")
    totals = {}
    for record in fit_receipts:
        totals[record["supplier_day_id"]] = totals.get(record["supplier_day_id"], 0.0) + record["quantity_kg"]
    capacity_note = sum(1 for record in fit_days if totals.get(record["id"], 0.0) > record["capacity_kg"])
    return {
        "skus": skus,
        "intents": intents,
        "return_ratios": ratios,
        "prices": prices,
        "costs": costs,
        "shelf_life": shelf_life,
        "supplier_days": [(record["common_delay_days"], record["capacity_kg"]) for record in fit_days],
        "missing_rate": len(missing_days) / len({record["requested_at"][:10] for record in fit_demand}) if fit_demand else 0.0,
        "evidence": {
            "demand": evidence,
            "supplier_day_count": len(fit_days),
            "capacity_exceeded_days": capacity_note,
            "receipt_count": len(fit_receipts),
        },
        "policy": {
            "holding_per_kg": float(policy["daily_holding_per_kg"]),
            "disposal_per_kg": float(policy["disposal_per_kg"]),
            "ordering_fee": float(policy["ordering_fee"]),
        },
    }


def _empty_scenario(name, kind, sim_days, expected_days, skus):
    return {
        "name": name, "kind": kind,
        "intent": {day: {sku: 0.0 for sku in skus} for day in sim_days},
        "observed": {day: True for day in sim_days},
        "return_ratio": {day: {sku: 0.0 for sku in skus} for day in sim_days},
        "delay": {day: 0 for day in expected_days},
        "capacity": {day: None for day in expected_days},
        "shelf_life": {day: {sku: 1 for sku in skus} for day in sim_days},
    }


def _bootstrap_scenario(name, models, sim_days, expected_days, rng, demand_scale=1.0):
    skus = models["skus"]
    scenario = _empty_scenario(name, "bootstrap", sim_days, expected_days, skus)
    for day in sim_days:
        scenario["observed"][day] = rng.random() >= models["missing_rate"]
        for sku in skus:
            pool = models["intents"][sku]
            intent = pool[rng.integers(0, len(pool))] * demand_scale
            scenario["intent"][day][sku] = round(intent, 6)
            ratio_pool = models["return_ratios"][sku]
            scenario["return_ratio"][day][sku] = ratio_pool[rng.integers(0, len(ratio_pool))]
            scenario["shelf_life"][day][sku] = models["shelf_life"][sku][rng.integers(0, len(models["shelf_life"][sku]))]
    for day in expected_days:
        delay, capacity = models["supplier_days"][rng.integers(0, len(models["supplier_days"]))]
        scenario["delay"][day] = delay
        scenario["capacity"][day] = capacity
    return scenario


def _replay_scenario(catalog, models, sim_days, expected_days):
    skus = models["skus"]
    scenario = _empty_scenario("历史回放", "replay", sim_days, expected_days, skus)
    demand = {}
    for record in catalog.rows("demand"):
        demand[(record["requested_at"][:10], record["sku"])] = record
    supplier_days = {record["day"]: record for record in catalog.rows("supplier_days")}
    medians = {sku: _median(models["intents"][sku]) for sku in skus}
    for day in sim_days:
        for sku in skus:
            record = demand.get((day, sku))
            if record is None or record["requested_kg"] is None:
                scenario["intent"][day][sku] = round(medians[sku], 6)
                if record is not None:
                    scenario["observed"][day] = False
            else:
                scenario["intent"][day][sku] = record["requested_kg"]
            if record is not None:
                scenario["return_ratio"][day][sku] = record["returned_kg"] / record["sold_kg"] if record["sold_kg"] > 0 else 0.0
            scenario["shelf_life"][day][sku] = int(_median(models["shelf_life"][sku]))
    fallback_delay = int(_median([delay for delay, _ in models["supplier_days"]]))
    fallback_capacity = _median([capacity for _, capacity in models["supplier_days"]])
    for day in expected_days:
        record = supplier_days.get(day)
        if record is None:
            scenario["delay"][day] = fallback_delay
            scenario["capacity"][day] = fallback_capacity
        else:
            scenario["delay"][day] = record["common_delay_days"]
            scenario["capacity"][day] = record["capacity_kg"]
    return scenario


def _stress_scenarios(models, sim_days, expected_days, eval_days):
    skus = models["skus"]
    delays = [delay for delay, _ in models["supplier_days"]]
    capacities = [capacity for _, capacity in models["supplier_days"]]
    median_intent = {sku: _median(models["intents"][sku]) for sku in skus}
    typical_ratio = {sku: _median(models["return_ratios"][sku]) for sku in skus}
    min_life = {sku: min(models["shelf_life"][sku]) for sku in skus}
    median_life = {sku: int(_median(models["shelf_life"][sku])) for sku in skus}

    extreme = _empty_scenario("极端延误与缩量", "stress", sim_days, expected_days, skus)
    outage = _empty_scenario("连续三日断供", "stress", sim_days, expected_days, skus)
    block = set(eval_days[len(eval_days) // 2: len(eval_days) // 2 + 3])
    for day in sim_days:
        for sku in skus:
            extreme["intent"][day][sku] = round(median_intent[sku], 6)
            outage["intent"][day][sku] = round(median_intent[sku], 6)
            extreme["return_ratio"][day][sku] = typical_ratio[sku]
            outage["return_ratio"][day][sku] = typical_ratio[sku]
            extreme["shelf_life"][day][sku] = min_life[sku]
            outage["shelf_life"][day][sku] = median_life[sku]
    for day in expected_days:
        extreme["delay"][day] = max(delays)
        extreme["capacity"][day] = min(capacities)
        outage["delay"][day] = int(_median(delays))
        previous = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
        outage["capacity"][day] = 0.0 if previous in block else _median(capacities)
    return [extreme, outage]


def build_scenarios(catalog, models, spec):
    eval_days = _days_between(spec["eval_start"], spec["eval_end"])
    first = date.fromisoformat(spec["eval_start"]) - timedelta(days=spec["warmup_days"])
    sim_days = _days_between(first.isoformat(), spec["eval_end"])
    last_expected = date.fromisoformat(spec["eval_end"]) + timedelta(days=1)
    expected_days = _days_between((first + timedelta(days=1)).isoformat(), last_expected.isoformat())
    scenarios = [_replay_scenario(catalog, models, sim_days, expected_days)]
    for index in range(spec["scenarios"]):
        rng = np.random.default_rng([spec["seed"], index])
        scenarios.append(_bootstrap_scenario(f"抽样{index + 1}", models, sim_days, expected_days, rng))
    if spec["stress"]:
        scenarios.extend(_stress_scenarios(models, sim_days, expected_days, eval_days))
    return scenarios, sim_days


def _setup(models, sim_days, eval_start, initial_history):
    return {
        "skus": models["skus"],
        "prices": models["prices"],
        "costs": models["costs"],
        "holding_per_kg": models["policy"]["holding_per_kg"],
        "disposal_per_kg": models["policy"]["disposal_per_kg"],
        "ordering_fee": models["policy"]["ordering_fee"],
        "days": sim_days,
        "eval_start": eval_start,
        "initial_history": initial_history,
    }


def _initial_history(catalog, fit_start, fit_end):
    return [
        {"date": record["requested_at"][:10], "sku": record["sku"],
         "requested_kg": record["requested_kg"], "sold_kg": record["sold_kg"],
         "returned_kg": record["returned_kg"]}
        for record in catalog.rows("demand")
        if fit_start <= record["requested_at"][:10] <= fit_end
    ]


def _summarize(values):
    array = np.asarray(values, dtype=float)
    count = len(array)
    mean = float(array.mean())
    result = {"mean": round(mean, 6), "min": round(float(array.min()), 6), "max": round(float(array.max()), 6)}
    if count >= 2:
        half = float(t_distribution.ppf(0.975, count - 1)) * float(array.std(ddof=1)) / math.sqrt(count)
        result["ci95"] = [round(mean - half, 6), round(mean + half, 6)]
    else:
        result["ci95"] = [None, None]
    return result


def _paired(differences):
    result = _summarize(differences)
    result["share_positive"] = round(sum(1 for value in differences if value > 0) / len(differences), 6)
    return result


def _round_metrics(metrics):
    return {field: (round(value, 6) if isinstance(value, float) else value) for field, value in metrics.items()}


def normalize_spec(catalog, raw):
    if not isinstance(raw, dict):
        raise ValueError("请求内容应为对象")
    allowed = {"label", "fit_start", "fit_end", "eval_start", "eval_end", "scenarios",
               "seed", "warmup_days", "rules", "baseline", "demand_scale_band", "stress"}
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError("请求包含未知字段：" + ",".join(sorted(unknown)))
    demand_days = sorted({record["requested_at"][:10] for record in catalog.rows("demand")})
    split = max(1, int(len(demand_days) * 0.7))
    spec = {
        "fit_start": demand_days[0], "fit_end": demand_days[split - 1],
        "eval_start": demand_days[split], "eval_end": demand_days[-1],
        "scenarios": 32, "seed": DEFAULT_SEED, "warmup_days": 7,
        "demand_scale_band": [0.85, 1.15], "stress": True,
    }
    for field in ("fit_start", "fit_end", "eval_start", "eval_end"):
        if field in raw:
            try:
                spec[field] = date.fromisoformat(raw[field]).isoformat()
            except (TypeError, ValueError) as error:
                raise ValueError("日期格式无效：" + field) from error
    if not (demand_days[0] <= spec["fit_start"] <= spec["fit_end"] < spec["eval_start"] <= spec["eval_end"] <= demand_days[-1]):
        raise ValueError("窗口需满足：资料起点 ≤ 拟合起 ≤ 拟合止 < 评估起 ≤ 评估止 ≤ 资料终点")
    if "scenarios" in raw:
        if isinstance(raw["scenarios"], bool) or not isinstance(raw["scenarios"], int):
            raise ValueError("情景数应为整数")
        if not 4 <= raw["scenarios"] <= 200:
            raise ValueError("情景数需在四至二百之间")
        spec["scenarios"] = raw["scenarios"]
    if "seed" in raw:
        if isinstance(raw["seed"], bool) or not isinstance(raw["seed"], int) or raw["seed"] < 0:
            raise ValueError("种子应为非负整数")
        spec["seed"] = raw["seed"]
    if "warmup_days" in raw:
        if isinstance(raw["warmup_days"], bool) or not isinstance(raw["warmup_days"], int) or not 0 <= raw["warmup_days"] <= 30:
            raise ValueError("预热天数需在零至三十之间")
        spec["warmup_days"] = raw["warmup_days"]
    if "demand_scale_band" in raw:
        band = raw["demand_scale_band"]
        if (not isinstance(band, list) or len(band) != 2
                or any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in band)):
            raise ValueError("需求假设区间应为两个数值")
        low, high = float(band[0]), float(band[1])
        if not (0.5 <= low <= 1.0 <= high <= 2.0):
            raise ValueError("需求假设区间需满足 0.5 ≤ 下限 ≤ 1 ≤ 上限 ≤ 2")
        spec["demand_scale_band"] = [low, high]
    if "stress" in raw:
        if not isinstance(raw["stress"], bool):
            raise ValueError("stress 应为布尔值")
        spec["stress"] = raw["stress"]
    raw_rules = raw["rules"] if "rules" in raw else [{"name": "sold_average"}, {"name": "intent_average"}, {"name": "order_up_to"}]
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ValueError("规则列表无效")
    rules = []
    for entry in raw_rules:
        if not isinstance(entry, dict) or set(entry) - {"name", "params"} or not isinstance(entry.get("name"), str):
            raise ValueError("规则项应只含 name 与 params")
        rules.append({"name": entry["name"], "params": validate_rule_params(entry["name"], entry.get("params", {}))})
    if len({rule["name"] for rule in rules}) != len(rules):
        raise ValueError("规则名称重复")
    spec["rules"] = rules
    baseline = raw.get("baseline", rules[0]["name"])
    if baseline not in {rule["name"] for rule in rules}:
        raise ValueError("基线规则不在候选列表中")
    spec["baseline"] = baseline
    label = raw.get("label", "")
    if not isinstance(label, str) or len(label) > 120:
        raise ValueError("试算名称需为一至一百二十个字符")
    spec["label"] = label.strip()
    return spec


def _assumptions(spec, models):
    evidence = models["evidence"]
    missing = sum(item["missing_days"] for item in evidence["demand"].values())
    known = sum(item["known_days"] for item in evidence["demand"].values())
    low, high = spec["demand_scale_band"]
    return [
        "试算仅为模拟比较，不生成也不执行任何实际订货。",
        "金额按浮点探索性口径累计，正式对账仍以原始十进制字段为准。",
        "采购成本在到货日确认，订货费在下单日确认，持有费按跨日结转公斤计，过期与退货均计处置费。",
        "退货一律报废不回库，收入按（售出−退货）×挂牌价计。",
        "期末在库按成本单列，不计入经营贡献也不视为损耗；期末未达订单仅计订货费。",
        f"拟合窗内意向缺失 {missing} 天、已知 {known} 天；缺失不按销量回填，抽样只来自已知意向分布，"
        f"需求水平敏感区间为 {low}～{high} 倍，结论在该区间内的变化见敏感性部分。",
        "同一供应商日各品种共享延误与容量冲击；容量视为当日可交付上限，"
        f"拟合窗内有 {evidence['capacity_exceeded_days']}/{evidence['supplier_day_count']} 个供应商日"
        "历史到货总量超过名义容量，试算口径偏保守。",
        "规则决策只使用当时可知信息（拟合窗历史与已发生的模拟事件），不读取后续真实到货或需求。",
        "到货剩余保质期按品类经验分布抽样；极端情景单独报告，不并入抽样平均。",
        "跨品种需求独立抽样，未建模品种间需求相关；回放情景中缺失意向按拟合窗中位数填补并标记。",
    ]


def run_trial(catalog, spec):
    models = fit_models(catalog, spec["fit_start"], spec["fit_end"])
    scenarios, sim_days = build_scenarios(catalog, models, spec)
    history = _initial_history(catalog, spec["fit_start"], spec["fit_end"])
    setup = _setup(models, sim_days, spec["eval_start"], history)
    scenario_index = [{"index": index, "name": scenario["name"], "kind": scenario["kind"]}
                      for index, scenario in enumerate(scenarios)]
    results = {}
    for rule in spec["rules"]:
        for index, scenario in enumerate(scenarios):
            metrics, _ = run_simulation(setup, scenario, rule["name"], rule["params"])
            results[(rule["name"], index)] = metrics
    bootstrap_ids = [index for index, scenario in enumerate(scenarios) if scenario["kind"] == "bootstrap"]
    baseline = spec["baseline"]
    rule_names = [rule["name"] for rule in spec["rules"]]
    bootstrap_rules = {}
    for name in rule_names:
        bootstrap_rules[name] = {
            metric: _summarize([results[(name, index)][metric] for index in bootstrap_ids])
            for metric in HEADLINE_METRICS + DETAIL_METRICS
        }
    paired = {}
    for name in rule_names:
        if name == baseline:
            continue
        paired[name] = {
            metric: _paired([results[(name, index)][metric] - results[(baseline, index)][metric] for index in bootstrap_ids])
            for metric in HEADLINE_METRICS
        }
    replay_id = next(index for index, scenario in enumerate(scenarios) if scenario["kind"] == "replay")
    replay = {
        "rules": {name: _round_metrics(results[(name, replay_id)]) for name in rule_names},
        "delta_vs_baseline": {
            name: {metric: round(results[(name, replay_id)][metric] - results[(baseline, replay_id)][metric], 6)
                   for metric in HEADLINE_METRICS}
            for name in rule_names if name != baseline
        },
    }
    stress = {}
    for index, scenario in enumerate(scenarios):
        if scenario["kind"] != "stress":
            continue
        stress[scenario["name"]] = {
            "scenario_index": index,
            "rules": {name: _round_metrics(results[(name, index)]) for name in rule_names},
            "delta_vs_baseline": {
                name: {metric: round(results[(name, index)][metric] - results[(baseline, index)][metric], 6)
                       for metric in HEADLINE_METRICS}
                for name in rule_names if name != baseline
            },
        }
    sensitivity = {}
    for scale in spec["demand_scale_band"]:
        if scale == 1.0:
            continue
        scaled_results = {}
        for rule in spec["rules"]:
            contributions, stockouts = [], []
            for index in bootstrap_ids:
                base = scenarios[index]
                scaled = dict(base)
                scaled["intent"] = {day: {sku: round(value * scale, 6) for sku, value in base["intent"][day].items()}
                                    for day in base["intent"]}
                metrics, _ = run_simulation(setup, scaled, rule["name"], rule["params"])
                contributions.append(metrics["contribution"])
                stockouts.append(metrics["stockout_kg"])
            scaled_results[rule["name"]] = {
                "contribution_mean": round(float(np.mean(contributions)), 6),
                "stockout_kg_mean": round(float(np.mean(stockouts)), 6),
            }
        sensitivity[str(scale)] = {
            "rules": scaled_results,
            "best_by_contribution": max(scaled_results, key=lambda name: scaled_results[name]["contribution_mean"]),
        }
    base_best = max(bootstrap_rules, key=lambda name: bootstrap_rules[name]["contribution"]["mean"])
    report = {
        "spec": spec,
        "assumptions": _assumptions(spec, models),
        "evidence": models["evidence"],
        "scenario_index": scenario_index,
        "bootstrap": {
            "scenario_count": len(bootstrap_ids),
            "baseline": baseline,
            "rules": bootstrap_rules,
            "paired_vs_baseline": paired,
            "best_by_contribution": base_best,
        },
        "replay": replay,
        "stress": stress,
        "sensitivity": {
            "demand_scale": sensitivity,
            "ranking_changed": any(entry["best_by_contribution"] != base_best for entry in sensitivity.values()),
        },
    }
    return report


def compute_trace(catalog, spec, rule_name, scenario_index):
    names = {rule["name"] for rule in spec["rules"]}
    if rule_name not in names:
        raise ValueError("规则不在试算候选中：" + str(rule_name))
    models = fit_models(catalog, spec["fit_start"], spec["fit_end"])
    scenarios, sim_days = build_scenarios(catalog, models, spec)
    if isinstance(scenario_index, bool) or not isinstance(scenario_index, int) or not 0 <= scenario_index < len(scenarios):
        raise ValueError("情景序号无效")
    scenario = scenarios[scenario_index]
    history = _initial_history(catalog, spec["fit_start"], spec["fit_end"])
    setup = _setup(models, sim_days, spec["eval_start"], history)
    params = next(rule["params"] for rule in spec["rules"] if rule["name"] == rule_name)
    metrics, trace = run_simulation(setup, scenario, rule_name, params, collect_trace=True)
    return {
        "rule": rule_name,
        "scenario": {"index": scenario_index, "name": scenario["name"], "kind": scenario["kind"]},
        "metrics": _round_metrics(metrics),
        "days": trace,
    }
