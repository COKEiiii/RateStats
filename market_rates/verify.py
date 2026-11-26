"""Independent field comparison, evidence checks, temporal guards and review gates."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from .common import digest
from .schema import normalize, key, group_key, facts, FACTS, IDENTITY
from .clause_review import clause_differences
from .rules import display_tenor, build_views, build_updates, active
from .scope import in_scope
from .workbook_policy import board_rate_usable


def compact(s):
    return " ".join(s.split())


def assess(run, config, store):
    from .llm_validation import require_text_run
    require_text_run(run)
    as_of = run["as_of"]
    previous = store.previous(as_of)
    old = {key(r): r for r in previous["offers"]}
    overrides = store.overrides()
    pages = {p["id"]: p for p in run["pages"]}
    known_products = {p["id"]: p for p in config.get("products", [])}
    issues = []
    candidates = {}
    blockers = set()

    def issue(identity, reasons, llm=None, effective=None, group=None):
        payload = {"reasons": reasons, "llm": llm, "effective": effective,
                   "source_hash": run["evidence_hash"], "config_hash": digest(config)}
        # A new override can change the displayed effective value, but must not
        # invalidate the decision that just created it for this evidence snapshot.
        fingerprint = digest({"llm": llm, "identity": identity,
                              "source_hash": run["evidence_hash"], "config_hash": digest(config),
                              "reasons": reasons if not (llm or effective) else []})
        decision = store.decision(run["id"], identity, fingerprint)
        item = {"id": identity, "fingerprint": fingerprint, "group": group,
                **payload, "resolved": bool(decision), "decision": decision}
        issues.append(item)
        return decision

    for problem in run.get("errors", []):
        if not issue("source:" + digest(problem)[:16], [problem]):
            blockers.add("*")
    for difference in clause_differences(run.get("metadata", [])):
        identity = f'clause:{difference["page_id"]}:{difference["number"]}'
        reasons = [f'条款{difference["number"]}: {difference["reason"]}']
        for lane in ("llm",):
            reasons.extend(f'{lane.upper()} [{c["locator"]}]: {c["quote"]}' for c in difference[lane])
        if not issue(identity, reasons):
            blockers.add("*")
    expected_banks = set(config.get("expected_banks", []))
    obtained_banks = {p["bank"] for p in pages.values() if p.get("ok")}
    for bank in sorted(expected_banks - obtained_banks):
        if not issue("missing-source:" + bank, [f"{bank}没有本期成功采集的证据"]):
            blockers.add("*")
    lanes = {}
    for lane in ("llm",):
        result = run.get(lane, {})
        if result.get("coverage_complete") is not True or result.get("unreadable"):
            if not issue("coverage:" + lane, [f"{lane}覆盖不完整或页面不可读", *result.get("unreadable", [])]):
                blockers.add("*")
        inventory_pages = {p["page_id"] for p in result.get("inventory", [])}
        missing = set(pages) - inventory_pages
        if missing and not issue("inventory:" + lane, [f"{lane}未逐页核对清单: {sorted(missing)}"]):
            blockers.add("*")
        by_key = defaultdict(list)
        for raw in result.get("offers", []):
            try:
                r = normalize(raw)
                by_key[key(r)].append(r)
            except (ValueError, TypeError, KeyError) as exc:
                if not issue("invalid:" + lane + ":" + digest(raw)[:12], [str(exc)],
                             llm=raw):
                    blockers.add("*")
        lanes[lane] = by_key
    for identity in sorted(set(lanes["llm"]) | set(old)):
        lefts = lanes["llm"].get(identity, [])
        left = lefts[0] if lefts else None
        reasons = []
        base = left
        if not base:
            base = dict(old[identity])
            base["rate_pct"] = None
            base["availability"] = "unknown"
            reasons.append("上期存在，本期LLM未找到；不是已停止提供")
        if len(lefts) > 1:
            reasons.append("相同身份重复报价；不能任意取第一条")
        if not left:
            reasons.append("本期报价缺失，需核实覆盖或产品条件变化")
        for lane, rec in (("llm", left),):
            if not rec:
                continue
            for e in rec["evidence"]:
                p = pages.get(e["page_id"])
                if not p or not p.get("ok") or p["bank"] != rec["bank"]:
                    reasons.append(f"{lane}证据页不存在、失败或银行不符")
                    continue
                if lane == "llm" and compact(e["quote"]) not in compact(p["text"]):
                    reasons.append("LLM原文摘录不能在采集文字中定位")
                if not p.get("complete", False):
                    reasons.append("采集页面不完整或截图期间内容改变")
        product = known_products.get(base["product_id"])
        if not product or product["bank"] != base["bank"]:
            reasons.append("新产品身份尚未登记；人工确认后补入产品目录")
        elif base["audience"] != "personal" and product.get("personal_status") not in ("present", "absent"):
            reasons.append("尚未确认Personal是否存在，不能直接采用其他客群回退")
        if not in_scope(base,config):
            reasons.append("不在本批已配置币种/利率类别范围，禁止混入报表")
        if base["audience"] == "unknown":
            reasons.append("客群不明确")
        if base["rate_basis"] not in ("annual_nominal", "effective") and not board_rate_usable(base):
            reasons.append("利率口径不明确或分段利率未转换")
        if base["rate_pct"] is not None and not Decimal("0") <= Decimal(base["rate_pct"]) <= Decimal("25"):
            reasons.append("数值单位或利率异常，检查是否将0.017误作1.7%")
        if base["valid_from"] and base["valid_from"] > as_of:
            reasons.append("尚未生效")
        if base["valid_to"] and base["valid_to"] < as_of:
            reasons.append("已过期，不参与有效排名")
        if base["availability"] != "available" or base["rate_pct"] is None:
            reasons.append("本期停止提供或未核实，数值显示-并人工确认状态")
        if display_tenor(base, config) is None:
            reasons.append("实际期限尚未登记展示映射")
        effective = dict(base)
        if identity in overrides:
            effective.update(overrides[identity])
            if any(base[k] != v for k, v in overrides[identity].items()):
                reasons.append("沿用跨期人工修正；本期原始值不同或未找到，需要复核")
        effective = normalize(effective)
        if identity not in old:
            reasons.append("初次或新增报价，初期需人工检查")
        elif facts(effective) != facts(old[identity]):
            reasons.append("相较已发布上期有变化，初期需人工检查")
        group = group_key(effective)
        if reasons:
            decision = issue(identity, sorted(set(reasons)), left, effective, group)
            if not decision:
                blockers.add(group)
                continue
            if decision["action"] == "exclude":
                continue
            selected = decision.get("offer")
            if selected:
                effective = normalize(selected)
                group = group_key(effective)
        candidates[key(effective)] = effective
    # A missing Personal quote is a group error, not permission to use Premier.
    by_group = defaultdict(list)
    for r in candidates.values():
        by_group[group_key(r)].append(r)
    observed_products = {r["product_id"] for lane in lanes.values() for rows in lane.values() for r in rows}
    for product in config.get("products", []):
        if product.get("expected", True) and product["id"] not in observed_products:
            identity = "product-missing:" + product["id"]
            if not issue(identity, [f'产品{product["id"]}没有可采用报价；请确认缺失原因或停供']):
                blockers.add("*")
        for tenor in product.get("expected_tenors", []):
            for lane in ("llm",):
                present = any(r["product_id"] == product["id"] and f'{r["tenor_value"]}{r["tenor_unit"]}' == tenor
                              for records in lanes[lane].values() for r in records)
                if not present and not issue(f'tenor-missing:{product["id"]}:{tenor}:{lane}',
                                            [f'{lane}未覆盖已登记期限{product["id"]}/{tenor}']):
                    blockers.add("*")
    for group, records in by_group.items():
        p = known_products.get(records[0]["product_id"], {})
        if (p.get("personal_status") == "present" and any(active(r, as_of) for r in records)
                and not any(r["audience"] == "personal" and active(r, as_of) for r in records)):
            if not issue("personal-missing:" + group, ["目录显示有Personal，本期仅抓到其他客群"], group=group):
                blockers.add(group)
        bases = {r["rate_basis"] for r in records}
        if len(bases) > 1:
            if not issue("basis:" + group, ["同产品利率口径混合，必须确认统一排序口径"], group=group):
                blockers.add(group)
    accepted = [r for r in candidates.values() if "*" not in blockers and group_key(r) not in blockers]
    views = build_views(accepted, config, previous.get("order", {}), as_of)
    return {"run_id": run["id"], "as_of": as_of, "demo": run.get("demo", False),
            "pilot": run.get("pilot"),
            "offers": accepted, "issues": issues, "pending": sum(not i["resolved"] for i in issues),
            "overrides": sorted(overrides), "updates": build_updates(accepted, previous, config, as_of), **views}


def apply_decisions(run, config, store, document):
    current = assess(run, config, store)
    issues = {i["id"]: i for i in current["issues"]}
    for entry in document:
        if not entry.get("action"):
            continue
        item = issues.get(entry["id"])
        if not item or item["resolved"] or entry["fingerprint"] != item["fingerprint"]:
            raise ValueError("复核项已变化或已处理，请刷新复核清单")
        action = entry["action"]
        if action not in {"llm", "effective", "replace", "exclude", "acknowledge"}:
            raise ValueError("无效复核动作")
        offer = None
        if action in {"llm", "effective"}:
            offer = item[action]
            if not offer:
                raise ValueError("所选路线没有报价，不能采用")
        elif action == "replace":
            offer = entry.get("offer")
        elif action == "acknowledge" and item.get("effective"):
            raise ValueError("报价冲突需要选值、替换或排除，不能只确认已读")
        if action == "replace" and not offer:
            raise ValueError("替换动作必须提供完整记录")
        if offer:
            offer = normalize(offer)
            if not in_scope(offer,config):
                raise ValueError("人工修正超出本批已配置币种/利率类别范围")
            if display_tenor(offer, config) is None:
                raise ValueError("请先配置实际期限映射")
            if not any(p["id"] == offer["product_id"] and p["bank"] == offer["bank"] for p in config["products"]):
                raise ValueError("请先在目录登记产品身份，再刷新复核")
            if offer["audience"] == "unknown" or (offer["rate_basis"] in {"unknown", "step"} and not board_rate_usable(offer)):
                raise ValueError("采用前需明确客群及排序利率口径")
        author, reason = entry.get("author", ""), entry.get("reason", "")
        payload = {"action": action, "offer": offer}
        if entry.get("persist"):
            if not offer or not item.get("effective") or key(offer) != key(item["effective"]):
                raise ValueError("跨期修正不能改变产品或条件身份")
        store.record_decision(run["id"], item["id"], item["fingerprint"], payload, author, reason)
        if entry.get("persist"):
            store.set_override(key(offer), {"rate_pct": offer["rate_pct"]}, author, reason)
