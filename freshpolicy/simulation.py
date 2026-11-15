"""逐日逐批的补货策略模拟引擎与候选规则。

每日事件次序与资料约定一致：上午到货、午间需求、下午订货、零点过期结转。
规则只通过 observation 看到当时可知的信息，引擎不把后续情景暴露给规则。
金额在模拟中按浮点探索性口径累计，正式对账仍以原始十进制字段为准。
"""
import json
import math
from datetime import date, timedelta

EPSILON = 1e-9


def _recent(history, sku, field, window):
    values = [record[field] for record in history if record["sku"] == sku and record[field] is not None]
    return values[-window:]


def _mean(values):
    return sum(values) / len(values) if values else 0.0


def rule_sold_average(observation, params):
    window = params["window"]
    return {sku: _mean(_recent(observation["history"], sku, "sold_kg", window)) for sku in observation["skus"]}


def rule_intent_average(observation, params):
    window = params["window"]
    orders = {}
    for sku in observation["skus"]:
        known = _recent(observation["history"], sku, "requested_kg", window)
        if known:
            orders[sku] = _mean(known)
        else:
            orders[sku] = _mean(_recent(observation["history"], sku, "sold_kg", window))
    return orders


def rule_order_up_to(observation, params):
    window, lead_days, safety = params["window"], params["lead_days"], params["safety"]
    orders = {}
    for sku in observation["skus"]:
        known = _recent(observation["history"], sku, "requested_kg", window)
        sample = known if known else _recent(observation["history"], sku, "sold_kg", window)
        mu = _mean(sample)
        sigma = math.sqrt(sum((value - mu) ** 2 for value in sample) / (len(sample) - 1)) if len(sample) >= 2 else 0.0
        target = mu * (lead_days + 1) + safety * sigma * math.sqrt(lead_days + 1)
        position = observation["on_hand"][sku] + observation["on_order"][sku]
        orders[sku] = max(0.0, target - position)
    return orders


def rule_none(observation, params):
    return {sku: 0.0 for sku in observation["skus"]}


RULES = {
    "sold_average": {
        "run": rule_sold_average,
        "defaults": {"window": 7},
        "description": "基线：按最近 window 天实际售出均值订货，把销量当作想买量。",
    },
    "intent_average": {
        "run": rule_intent_average,
        "defaults": {"window": 7},
        "description": "按最近 window 天已知意向均值订货；窗口内意向全缺失时退回销量均值。",
    },
    "order_up_to": {
        "run": rule_order_up_to,
        "defaults": {"window": 14, "lead_days": 1, "safety": 1.0},
        "description": "订货至目标水平：意向均值×(提前期+1) 加安全项，扣减在库与在途。",
    },
    "none": {
        "run": rule_none,
        "defaults": {},
        "description": "对照：不订货，用于观察起点库存耗尽后的自然结果。",
    },
}

PARAM_BOUNDS = {
    "window": (int, 2, 30),
    "lead_days": (int, 0, 5),
    "safety": ((int, float), 0.0, 3.0),
}


def validate_rule_params(name, params):
    if name not in RULES:
        raise ValueError("未知补货规则：" + str(name))
    if not isinstance(params, dict):
        raise ValueError("规则参数应为对象")
    defaults = RULES[name]["defaults"]
    unknown = set(params) - set(defaults)
    if unknown:
        raise ValueError("规则参数包含未知字段：" + ",".join(sorted(unknown)))
    merged = dict(defaults)
    for field, value in params.items():
        kind, low, high = PARAM_BOUNDS[field]
        if isinstance(value, bool) or not isinstance(value, kind):
            raise ValueError("规则参数类型无效：" + field)
        if not low <= value <= high:
            raise ValueError("规则参数超出范围：" + field)
        merged[field] = value
    return merged


def _issue_fefo(batches, day, amount):
    sellable = [batch for batch in batches if batch["expires_day"] > day]
    sellable.sort(key=lambda batch: (batch["expires_day"], batch["arrived_day"], batch["id"]))
    issued = 0.0
    remaining = amount
    for batch in sellable:
        if remaining <= EPSILON:
            break
        take = min(batch["quantity_kg"], remaining)
        batch["quantity_kg"] -= take
        remaining -= take
        issued += take
    batches[:] = [batch for batch in batches if batch["quantity_kg"] > EPSILON]
    return issued


def _snapshot(batches):
    return [
        {"batch": batch["id"], "quantity_kg": round(batch["quantity_kg"], 6),
         "expires_day": batch["expires_day"], "unit_cost": batch["unit_cost"],
         "value": round(batch["quantity_kg"] * batch["unit_cost"], 6)}
        for batch in batches
    ]


def run_simulation(setup, scenario, rule_name, rule_params, collect_trace=False):
    """在单个情景下运行一条规则，返回指标与可选的逐日逐批追溯。

    setup 字段：skus、prices、costs、holding_per_kg、disposal_per_kg、
    ordering_fee、days（预热+评估全部日期，升序）、eval_start、initial_history。
    scenario 字段：name、kind、intent、observed、return_ratio、delay、capacity、
    shelf_life，均以 ISO 日期（及品种）为键；情景在规则运行前已生成完毕，
    同一组情景供全部候选规则复用，规则决策看不到情景后续内容。
    """
    rule = RULES[rule_name]["run"]
    skus = list(setup["skus"])
    eval_start = setup["eval_start"]
    batches = {sku: [] for sku in skus}
    outstanding = []
    history = [dict(record) for record in setup["initial_history"]]
    totals = {
        "intent_kg": 0.0, "sold_kg": 0.0, "stockout_kg": 0.0, "returned_kg": 0.0,
        "expired_kg": 0.0, "revenue": 0.0, "purchase_cost": 0.0, "holding_cost": 0.0,
        "disposal_cost": 0.0, "ordering_fees": 0.0,
    }
    trace_days = []
    sequence = 0
    for day in setup["days"]:
        in_eval = day >= eval_start
        opening = {sku: _snapshot(batches[sku]) for sku in skus}
        arrived_today = []
        still_waiting = []
        for order in outstanding:
            if order["arrival_day"] == day:
                arrived_today.append(order)
            else:
                still_waiting.append(order)
        outstanding = still_waiting
        for order in arrived_today:
            sequence += 1
            batches[order["sku"]].append({
                "id": order["id"], "quantity_kg": order["delivered_kg"],
                "arrived_day": day, "expires_day": order["expires_day"],
                "unit_cost": order["unit_cost"],
            })
            if in_eval:
                totals["purchase_cost"] += order["delivered_kg"] * order["unit_cost"]
        demand_today = {}
        for sku in skus:
            intent = scenario["intent"][day][sku]
            sold = _issue_fefo(batches[sku], day, intent)
            returned = sold * scenario["return_ratio"][day][sku]
            demand_today[sku] = {
                "intent_kg": intent, "sold_kg": sold,
                "stockout_kg": intent - sold, "returned_kg": returned,
            }
            history.append({
                "date": day, "sku": sku,
                "requested_kg": intent if scenario["observed"][day] else None,
                "sold_kg": sold, "returned_kg": returned,
            })
            if in_eval:
                totals["intent_kg"] += intent
                totals["sold_kg"] += sold
                totals["stockout_kg"] += intent - sold
                totals["returned_kg"] += returned
                totals["revenue"] += (sold - returned) * setup["prices"][sku]
                totals["disposal_cost"] += returned * setup["disposal_per_kg"]
        observation = {
            "date": day,
            "skus": skus,
            "on_hand": {sku: sum(batch["quantity_kg"] for batch in batches[sku]) for sku in skus},
            "on_order": {sku: sum(order["ordered_kg"] for order in outstanding if order["sku"] == sku) for sku in skus},
            "batches": {sku: _snapshot(batches[sku]) for sku in skus},
            "history": json.loads(json.dumps(history, ensure_ascii=False)),
        }
        decisions = rule(observation, rule_params)
        if not isinstance(decisions, dict):
            raise ValueError("规则返回应为对象")
        quantities = {sku: 0.0 for sku in skus}
        for sku, quantity in decisions.items():
            if sku not in batches:
                raise ValueError("规则返回了未知品种：" + str(sku))
            if not isinstance(quantity, (int, float)) or isinstance(quantity, bool) or not math.isfinite(quantity):
                raise ValueError("规则返回了无效订量")
            quantities[sku] = max(0.0, float(quantity))
        next_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
        ordered_total = sum(quantities.values())
        capacity = scenario["capacity"].get(next_day)
        scale = 1.0
        if capacity is not None and ordered_total > EPSILON:
            scale = min(1.0, capacity / ordered_total)
        placed_today = {sku: [] for sku in skus}
        for sku in skus:
            quantity = quantities[sku]
            if quantity <= EPSILON:
                continue
            if in_eval:
                totals["ordering_fees"] += setup["ordering_fee"]
            delay = scenario["delay"][next_day]
            arrival_day = (date.fromisoformat(next_day) + timedelta(days=delay)).isoformat()
            shelf_life = scenario["shelf_life"][day][sku]
            expires_day = (date.fromisoformat(arrival_day) + timedelta(days=shelf_life)).isoformat()
            sequence += 1
            order = {
                "id": f"{scenario['name']}:{rule_name}:{day}:{sku}:{sequence}",
                "sku": sku, "ordered_kg": quantity, "delivered_kg": quantity * scale,
                "order_day": day, "expected_day": next_day, "arrival_day": arrival_day,
                "expires_day": expires_day, "unit_cost": setup["costs"][sku],
                "shelf_life_days": shelf_life,
            }
            outstanding.append(order)
            placed_today[sku].append(order)
        expired_today = {sku: [] for sku in skus}
        closing = {}
        for sku in skus:
            kept = []
            for batch in batches[sku]:
                if batch["expires_day"] <= next_day:
                    expired_today[sku].append({
                        "batch": batch["id"], "quantity_kg": round(batch["quantity_kg"], 6),
                        "unit_cost": batch["unit_cost"],
                        "value": round(batch["quantity_kg"] * batch["unit_cost"], 6),
                    })
                    if in_eval:
                        totals["expired_kg"] += batch["quantity_kg"]
                        totals["disposal_cost"] += batch["quantity_kg"] * setup["disposal_per_kg"]
                else:
                    kept.append(batch)
            batches[sku] = kept
            closing[sku] = _snapshot(kept)
            if in_eval:
                totals["holding_cost"] += sum(batch["quantity_kg"] for batch in kept) * setup["holding_per_kg"]
        if in_eval and collect_trace:
            money = {
                "revenue": sum((demand_today[sku]["sold_kg"] - demand_today[sku]["returned_kg"]) * setup["prices"][sku] for sku in skus),
                "purchase_cost": sum(order["delivered_kg"] * order["unit_cost"] for order in arrived_today),
                "holding_cost": sum(batch["quantity_kg"] for sku in skus for batch in batches[sku]) * setup["holding_per_kg"],
                "disposal_cost": sum((demand_today[sku]["returned_kg"] + sum(item["quantity_kg"] for item in expired_today[sku])) * setup["disposal_per_kg"] for sku in skus),
                "ordering_fees": setup["ordering_fee"] * sum(1 for sku in skus if placed_today[sku]),
            }
            money["contribution"] = money["revenue"] - money["purchase_cost"] - money["holding_cost"] - money["disposal_cost"] - money["ordering_fees"]
            trace_days.append({
                "date": day,
                "skus": {
                    sku: {
                        "opening_batches": opening[sku],
                        "arrivals": [
                            {"batch": order["id"], "quantity_kg": round(order["delivered_kg"], 6),
                             "ordered_kg": round(order["ordered_kg"], 6),
                             "shelf_life_days": order["shelf_life_days"],
                             "expires_day": order["expires_day"], "unit_cost": order["unit_cost"]}
                            for order in arrived_today if order["sku"] == sku
                        ],
                        "intent_kg": round(demand_today[sku]["intent_kg"], 6),
                        "sold_kg": round(demand_today[sku]["sold_kg"], 6),
                        "stockout_kg": round(demand_today[sku]["stockout_kg"], 6),
                        "returned_kg": round(demand_today[sku]["returned_kg"], 6),
                        "orders": [
                            {"batch": order["id"], "ordered_kg": round(order["ordered_kg"], 6),
                             "delivered_kg": round(order["delivered_kg"], 6),
                             "expected_day": order["expected_day"], "arrival_day": order["arrival_day"],
                             "expires_day": order["expires_day"]}
                            for order in placed_today[sku]
                        ],
                        "expired": expired_today[sku],
                        "closing_batches": closing[sku],
                        "inventory_value": round(sum(item["value"] for item in closing[sku]), 6),
                    }
                    for sku in skus
                },
                "money": {field: round(value, 6) for field, value in money.items()},
                "inventory_value_total": round(sum(item["value"] for sku in skus for item in closing[sku]), 6),
            })
    terminal_kg = sum(batch["quantity_kg"] for sku in skus for batch in batches[sku])
    terminal_value = sum(batch["quantity_kg"] * batch["unit_cost"] for sku in skus for batch in batches[sku])
    metrics = dict(totals)
    metrics["waste_kg"] = totals["expired_kg"] + totals["returned_kg"]
    metrics["fill_rate"] = totals["sold_kg"] / totals["intent_kg"] if totals["intent_kg"] > EPSILON else None
    metrics["contribution"] = (totals["revenue"] - totals["purchase_cost"] - totals["holding_cost"]
                               - totals["disposal_cost"] - totals["ordering_fees"])
    metrics["terminal_inventory_kg"] = terminal_kg
    metrics["terminal_inventory_value"] = terminal_value
    metrics["terminal_outstanding_kg"] = sum(order["ordered_kg"] for order in outstanding)
    return metrics, trace_days
