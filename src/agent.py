"""
The conversational agent — a thin, own-the-loop tool-use layer over the deterministic
engine (signals + docs). The LLM does NOT reason about risk; it orchestrates our tools
and phrases their grounded results. All the intelligence lives in signals.py / docs.py.

Design:
  * BRIEFING-BASED architecture (briefings.py). Three scoped briefings — RETENTION,
    EXPANSION, PROSPECT — each a bundle of {a system-prompt preamble, a subset of the
    tools below}. A router (briefings.route) picks exactly ONE active briefing per user
    turn from the message's intent (keyword-first, LLM fallback, sticky across turns,
    explicit "switch to <briefing>" override). Only the active briefing's tools are
    exposed to the model for that turn — a smaller, better-scoped surface than one flat
    tool bag.
  * Tools are plain Python functions returning JSON. Most are unchanged from the flat
    design (assess_account, account_details, search_playbook, list_customers,
    list_prospects, todays_priorities); a handful are new thin wrappers that expose an
    existing engine capability as a standalone tool (list_expansion_candidates, icp_fit,
    discovery_brief, discovery_pack, match_case_study) plus switch_briefing, which lets
    the model itself request a briefing change mid-conversation.
  * The system prompt = shared trust rules (ground, cite, say "no data", terse, one AE's
    book scope) + the active briefing's preamble. Both live in briefings.py.
  * LLM backend is a thin swap: OpenAI here; the tools/engine are vendor-agnostic.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
from datetime import date

import briefings
import data
import docs
import signals
from brand import BRAND

# --- config -------------------------------------------------------------------
_ENV = pathlib.Path.home() / ".config" / "nimbus-case" / ".env"
for _line in _ENV.read_text().splitlines() if _ENV.exists() else []:
    if _line.startswith("OPENAI_API_KEY=") and "OPENAI_API_KEY" not in os.environ:
        os.environ["OPENAI_API_KEY"] = _line.split("=", 1)[1].strip()

MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1")
# This assistant is scoped to ONE AE's book (the case persona, Mara). Only her accounts
# are in scope — the retention thresholds were validated on her book, and briefing another
# AE's account (esp. an enterprise one) would apply a model it wasn't checked against.
BOOK_AE = os.environ.get("BOOK_AE", "mara.lindqvist@nimbus.example")


# --- tool implementations (plain Python, JSON-returning) ----------------------

def _resolve(name: str):
    """Resolve an account name within THIS assistant's book (BOOK_AE). If the name only
    matches another AE's account, refuse rather than brief on data the engine was not
    scoped/validated for — this enforces the single-AE design in code, not by convention."""
    if len(name.strip()) < 2:            # guard: a blank/1-char query would LIKE-match the whole book
        return None, {"error": "Which account? Give me an account name."}
    m = data.resolve_account(name, owner=BOOK_AE)
    if not m:
        other = data.resolve_account(name)  # exists, but in someone else's book?
        if other:
            return None, {"out_of_scope": f"'{other[0]['COMPANY_NAME']}' is not in your book "
                          f"(owned by {other[0]['OWNER_AE'].split('@')[0]}). This assistant "
                          f"covers your accounts only."}
        return None, {"error": f"No account found matching '{name}'."}
    if len(m) > 1:
        return None, {"ambiguous": [{"name": r["COMPANY_NAME"], "status": r["STATUS"]} for r in m]}
    return m[0], None


def _days_to(opps, typ=None):
    pool = [o for o in opps if not o["WON_LOST_REASON"] and o["CLOSE_DATE"]
            and (typ is None or o["TYPE"] == typ)]
    if not pool:
        return None
    return (date.fromisoformat(min(o["CLOSE_DATE"] for o in pool)[:10]) - data.ASOF).days


def _discovery_brief(acc: dict, opps: list, out: dict) -> dict:
    """Discovery prep for a PROSPECT (the persona's second call). A prospect has no usage or
    tickets, so retention signals don't apply — instead we brief on FIRMOGRAPHICS: ICP-fit,
    the closest cited case study (proof point), the open deal's stage/momentum, and — if the
    prospect is evaluating a rival — the matching battlecard. Same grounded, cited pattern."""
    # a churned account is NOT a prospect — don't mislabel a lost customer or frame case
    # studies as "how to win them"; give it a short win-back framing instead
    if acc["STATUS"] == "churned":
        out["mode"] = "churned"
        out["note"] = "Churned — no live retention data; win-back context only, not a live deal."
    else:
        out["mode"] = "discovery"
        out["note"] = "Prospect — no product-usage/support data; discovery prep (firmographics + proof points)."
    out["icp_fit"] = docs.icp_fit(acc)                                   # size / vertical / region vs the sweet spot
    match = docs.match_case_study(acc["INDUSTRY"], acc["REGION"])        # closest proof point by industry+region
    out["case_study"] = (docs.retrieve_case_study(match) if match else
                         {"citation": None,
                          "text": f"No close reference on file for {acc['INDUSTRY']} — ping #customer-references."})
    aid = acc["ACCOUNT_ID"]
    # what a prospect DOES have: the open deal's stage + momentum (days-in-stage)
    out["open_opportunities"] = [{"name": o["NAME"], "type": o["TYPE"], "stage": o["STAGE"],
                                  "amount_eur": o["AMOUNT_EUR"], "days_in_stage": o["DAYS_IN_STAGE"]}
                                 for o in opps if not o["WON_LOST_REASON"]]
    # if the prospect names a competitor in their activity log, hand over the battlecard
    # (reusing the exact same de-noised competitor detection we use for customers)
    comp = signals._competitor_mention(data.get_activities(aid, 40))
    if comp:
        rec = docs.recommend_for_signal(comp)
        out["competitor"] = {"summary": comp["summary"], "cite": rec["action"]["citation"],
                             "playbook_text": rec["action"]["text"]}
    # discovery-call support (prospects only): questions to ask + the MEDDIC qualification checklist,
    # tuned to the named competitor if there is one — so it's real call prep, not just firmographics
    if out["mode"] == "discovery":
        out["discovery"] = docs.discovery_pack(comp["competitor"] if comp else None)
    return out


def assess_account(name: str) -> dict:
    """The pre-call brief. For a CUSTOMER: fired retention/expansion signals + a code-computed
    RED/AMBER/GREEN verdict + playbook recommendations. For a PROSPECT: a discovery brief
    (ICP fit, matched case study, open-deal context, competitor battlecard)."""
    acc, err = _resolve(name)
    if err:
        return err
    aid = acc["ACCOUNT_ID"]
    acc = data.get_account(aid)  # resolve() returns only a few columns — load the full record
    opps = data.get_opportunities(aid)
    out = {"account": acc["COMPANY_NAME"], "status": acc["STATUS"], "segment": acc["SEGMENT"],
           "region": acc["REGION"], "arr_eur": acc["ARR_EUR"],
           "renewal_closes_in_days": _days_to(opps, "Renewal"),
           "expansion_closes_in_days": _days_to(opps, "Expansion")}
    res = signals.compute_signals(aid)
    if not res["assessable"]:                        # prospect/churned → discovery prep, not retention
        return _discovery_brief(acc, opps, out)
    # the headline verdict — RED/AMBER/GREEN — is decided in code (signals.band), not by the LLM
    out["verdict"] = signals.band(res["signals"])
    out["signals"] = [{"signal": s["signal"], "severity": s["severity"], "summary": s["summary"],
                       "evidence": s["evidence"]} for s in res["signals"]]
    out["recommendations"] = [
        {"for": r["signal"], "cite": r["action"]["citation"], "playbook_text": r["action"]["text"]}
        for r in docs.recommendations(res["signals"])]
    return out


def account_details(name: str, dimension: str) -> dict:
    """Drill-in on one dimension of an account. dimension ∈
    usage | tickets | contacts | opportunities | activities."""
    acc, err = _resolve(name)
    if err:
        return err
    aid = acc["ACCOUNT_ID"]
    fn = {"usage": data.get_usage, "tickets": data.get_tickets, "contacts": data.get_contacts,
          "opportunities": data.get_opportunities}.get(dimension)
    if fn:
        return {"account": acc["COMPANY_NAME"], dimension: fn(aid)}
    if dimension == "activities":
        return {"account": acc["COMPANY_NAME"], "activities": data.get_activities(aid, 15)}
    return {"error": f"Unknown dimension '{dimension}'."}


def search_playbook(query: str) -> dict:
    """Search the enablement docs (playbook, ICP, battlecards, objection-handling,
    pricing, case studies) for a follow-up question."""
    return {"results": docs.search(query, limit=3)}


_VERDICT_RANK = {"RED": 0, "AMBER": 1, "GREEN": 2, "—": 3}


def _book() -> dict:
    """Compute the AE's whole book once: customers (with RED/AMBER/GREEN verdict + renewal timing,
    ranked worst-first) and prospects (with open-deal status, most-stalled first). Internal — the
    two public tools below expose the customer side and the prospect side SEPARATELY, so a
    retention question grounds on customers only and a pipeline question on prospects only."""
    customers, prospects, churned = [], [], []
    for a in data.accounts_by_owner(BOOK_AE):
        if a["STATUS"] == "customer":
            res = signals.compute_signals(a["ACCOUNT_ID"])
            if res.get("assessable"):
                b = signals.band(res["signals"])
                customers.append({
                    "name": a["COMPANY_NAME"], "segment": a["SEGMENT"], "region": a["REGION"],
                    "verdict": b["level"], "risk_signals": b["risk_signal_count"],
                    "signals": [s["signal"] for s in res["signals"] if s["severity"] in ("strong", "moderate")],
                    # renewal timing is the urgency lever the AE ranks by — carry it so the model can
                    # justify the shortlist ("renewal in 18 days") without another tool call
                    "renewal_in_days": _days_to(data.get_opportunities(a["ACCOUNT_ID"]), "Renewal"),
                    "reason": b["reason"] if b["level"] != "GREEN" else "",
                })
            else:
                customers.append({"name": a["COMPANY_NAME"], "segment": a["SEGMENT"],
                                  "region": a["REGION"], "verdict": "—", "risk_signals": 0,
                                  "renewal_in_days": None, "reason": ""})
        elif a["STATUS"] == "churned":
            churned.append({"name": a["COMPANY_NAME"]})
        else:                                            # prospect — carry its open-deal status so the
            open_opps = [o for o in data.get_opportunities(a["ACCOUNT_ID"]) if not o["WON_LOST_REASON"]]
            top = max(open_opps, key=lambda o: o.get("DAYS_IN_STAGE") or 0, default=None)  # most-stalled deal
            prospects.append({
                "name": a["COMPANY_NAME"], "industry": a["INDUSTRY"], "region": a["REGION"],
                "open_deals": len(open_opps),
                "top_deal": ({"stage": top["STAGE"], "days_in_stage": top["DAYS_IN_STAGE"],
                              "amount_eur": top["AMOUNT_EUR"]} if top else None),
            })
    # rank for "what should I prep": worst verdict first, then MOST signals converging, then SOONEST
    # renewal — so Onyx Partners (RED, 4 signals, 18d) leads a lone-signal account, and within a tier the
    # soonest renewal wins; not alphabetical.
    customers.sort(key=lambda c: (_VERDICT_RANK.get(c["verdict"], 3),
                                  -c.get("risk_signals", 0),
                                  c["renewal_in_days"] if c["renewal_in_days"] is not None else 9999))
    # prospects: those with a deal first, most-stalled (highest days-in-stage) on top — a real ranking
    # signal, so "which prospect to prep" has a grounded answer instead of an arbitrary pick
    prospects.sort(key=lambda p: (p["top_deal"] is None,
                                  -(p["top_deal"]["days_in_stage"] if p["top_deal"] else 0)))
    counts = {v: sum(1 for c in customers if c["verdict"] == v) for v in ("RED", "AMBER", "GREEN")}
    counts.update(prospects=len(prospects),
                  prospects_with_deals=sum(1 for p in prospects if p["top_deal"]), churned=len(churned),
                  # exact count of customers with an open renewal — so "how many renewals" isn't the
                  # model counting a 24-row list by eye (it truncates/miscounts); it reads this number
                  open_renewals=sum(1 for c in customers if c.get("renewal_in_days") is not None))
    return {"book_owner": BOOK_AE, "counts": counts,
            "customers": customers, "prospects": prospects, "churned": churned}


def list_customers() -> dict:
    """The CUSTOMER side of the book — every customer with its RED/AMBER/GREEN verdict and renewal
    timing, ranked worst-first. For 'what should I prep / top retention risks / which renewals are
    coming up / how many renewals'. Returns customers only (no prospects), so the answer grounds on
    exactly what it's about."""
    b = _book()
    keep = ("RED", "AMBER", "GREEN", "open_renewals", "churned")
    return {"counts": {k: b["counts"][k] for k in keep if k in b["counts"]}, "customers": b["customers"]}


def list_prospects() -> dict:
    """The PROSPECT side of the book — every prospect with its open-deal status (stage, days-in-stage,
    amount), most-stalled first. For 'which prospect needs attention / how many prospects / my
    pipeline'. Returns prospects only (no customers)."""
    b = _book()
    return {"counts": {k: b["counts"][k] for k in ("prospects", "prospects_with_deals")},
            "prospects": b["prospects"]}


_STALL_DAYS = 45   # a prospect deal sitting this long in one stage is a "stuck deal" — actively slipping


def todays_priorities(limit: int = 5) -> dict:
    """One 'what should I actually do today' list ACROSS both customers and prospects — because a call
    today can be a renewal OR a discovery. It merges the two sides with a TRANSPARENT priority rule
    (returned as `rule`, so the AE can judge it — it's a heuristic, not a calibrated score):
      1. RED renewals   — existing revenue at risk with a deadline (soonest renewal first)
      2. stuck deals    — a prospect deal >45 days in one stage, actively slipping (most-stuck first)
      3. AMBER renewals — a softer churn watch with a deadline (soonest renewal first)
    Returns up to `limit` items; it never pads the list with low-priority (GREEN / un-stuck)
    accounts — "say less and be right"."""
    b = _book()
    items = []
    for c in b["customers"]:                                   # at-risk renewals (RED tier 0, AMBER tier 2)
        if c["renewal_in_days"] is not None and c["verdict"] in ("RED", "AMBER"):
            items.append({"type": "renewal", "tag": c["verdict"], "name": c["name"],
                          "when": f'renewal in {c["renewal_in_days"]} days', "why": c["reason"],
                          "signals": c.get("signals", []),     # for the compact footer "why"
                          "_tier": 0 if c["verdict"] == "RED" else 2, "_sort": c["renewal_in_days"]})
    for p in b["prospects"]:                                   # stuck deals (tier 1, between RED and AMBER)
        td = p["top_deal"]
        if td and td["days_in_stage"] >= _STALL_DAYS:
            items.append({"type": "discovery", "tag": "STUCK", "name": p["name"],
                          "when": f'{td["stage"]}, {td["days_in_stage"]}d in stage',
                          "why": f'deal stuck {td["days_in_stage"]}d (€{td["amount_eur"]:,.0f})',
                          "amount_eur": td["amount_eur"],      # for the compact footer "why"
                          "_tier": 1, "_sort": -td["days_in_stage"]})
    items.sort(key=lambda x: (x["_tier"], x["_sort"]))         # tier, then time pressure within tier
    return {"priorities": [{k: v for k, v in it.items() if not k.startswith("_")} for it in items[:limit]],
            "rule": "RED renewals first, then prospect deals stuck >45d, then AMBER renewals"}


# --- EXPANSION-briefing tools ---------------------------------------------------

EXPANSION_GROWTH_PCT = 0.15   # material MAU growth (mirrors signals.USAGE_DECLINE_PCT's magnitude,
                              # positive direction) — a real upsell signal, not noise


def _usage_growth_pct(usage: list[dict]) -> float | None:
    """MAU trend, growth direction (mirrors signals._usage_decline's valid-value filtering,
    without the recovery/suppression logic that only makes sense for a DECLINE read)."""
    valid = [r["MONTHLY_ACTIVE_USERS"] for r in usage
             if r["MONTHLY_ACTIVE_USERS"] and r["MONTHLY_ACTIVE_USERS"] > 0]
    if len(valid) < 2:
        return None
    return (valid[-1] - valid[0]) / valid[0]


def list_expansion_candidates() -> dict:
    """The EXPANSION side of the book — customers with a real, grounded upsell/cross-sell signal:
    an open expansion deal already in flight (signals.py), usage GROWING at least
    EXPANSION_GROWTH_PCT, or a module not yet active (whitespace) WITH a warm senior stakeholder to
    raise it with. Ranked: in-flight deal first, then growth magnitude, then whitespace-with-coverage.
    Customers only, and only real candidates — an account with no signal is left off, not padded in."""
    candidates = []
    for a in data.accounts_by_owner(BOOK_AE):
        if a["STATUS"] != "customer":
            continue
        aid = a["ACCOUNT_ID"]
        res = signals.compute_signals(aid)
        if not res.get("assessable"):
            continue
        usage = data.get_usage(aid)
        contacts = data.get_contacts(aid)
        expansion_sig = next((s for s in res["signals"] if s["signal"] == "expansion_in_flight"), None)
        growth = _usage_growth_pct(usage)
        latest = usage[-1] if usage else None
        whitespace = []
        if latest:
            if not latest.get("AUTOMATION_MODULE_ACTIVE"):
                whitespace.append("Automation")
            if not latest.get("ANALYTICS_MODULE_ACTIVE"):
                whitespace.append("Analytics")
        warm_senior = any(
            c["PERSONA_TYPE"] in signals.SENIOR_PERSONAS
            and signals._age_or(c["LAST_INTERACTION"]) <= signals.CONTACT_WARM_DAYS
            for c in contacts
        )
        has_growth = growth is not None and growth >= EXPANSION_GROWTH_PCT
        whitespace_play = bool(whitespace) and warm_senior
        if not (expansion_sig or has_growth or whitespace_play):
            continue                                       # no real signal — don't pad the list
        reasons = []
        if expansion_sig:
            reasons.append(f"open expansion in flight ({expansion_sig['value']})")
        if has_growth:
            reasons.append(f"usage growing {round(growth * 100)}%")
        if whitespace_play:
            reasons.append(f"{'/'.join(whitespace)} not active, warm senior stakeholder to raise it with")
        candidates.append({
            "name": a["COMPANY_NAME"], "segment": a["SEGMENT"], "region": a["REGION"],
            "expansion_in_flight": bool(expansion_sig),
            "expansion_deal": expansion_sig["value"] if expansion_sig else None,
            "usage_growth_pct": round(growth * 100) if growth is not None else None,
            "whitespace_modules": whitespace,
            "warm_senior_stakeholder": warm_senior,
            "reason": "; ".join(reasons),
        })
    candidates.sort(key=lambda c: (not c["expansion_in_flight"],
                                   -(c["usage_growth_pct"] if c["usage_growth_pct"] is not None else -999),
                                   not (c["whitespace_modules"] and c["warm_senior_stakeholder"])))
    return {"counts": {"candidates": len(candidates)}, "candidates": candidates}


def icp_fit(name: str) -> dict:
    """ICP-fit read (size band, vertical, region) for one account against the sweet spot —
    used both for a prospect (fit for the pitch) and an expansion candidate (how credible a
    bigger ask is). Thin wrapper over docs.icp_fit."""
    acc, err = _resolve(name)
    if err:
        return err
    acc = data.get_account(acc["ACCOUNT_ID"])
    fit = docs.icp_fit(acc)
    return {"account": acc["COMPANY_NAME"], "notes": fit["notes"], "citation": fit["citation"]}


# --- PROSPECT-briefing tools -----------------------------------------------------

def discovery_brief(name: str) -> dict:
    """Discovery prep for one PROSPECT (or a short win-back note for a churned account):
    ICP fit, matched cited case study, the open deal's stage/momentum, and a competitor
    battlecard if one is named. Thin wrapper over the same _discovery_brief assess_account
    uses for a non-customer — exposed standalone so the PROSPECT briefing doesn't need the
    customer-framed assess_account tool at all."""
    acc, err = _resolve(name)
    if err:
        return err
    acc = data.get_account(acc["ACCOUNT_ID"])
    opps = data.get_opportunities(acc["ACCOUNT_ID"])
    out = {"account": acc["COMPANY_NAME"], "status": acc["STATUS"],
           "segment": acc["SEGMENT"], "region": acc["REGION"]}
    return _discovery_brief(acc, opps, out)


def discovery_pack(name: str | None = None) -> dict:
    """Call-prep for a discovery call: questions to ask (verbatim battlecard questions if the
    prospect has a named competitor, else the general playbook §3) + the MEDDIC qualification
    checklist. Call with an account name to auto-detect a named competitor from their activity
    log, or with no name for the general pack. Thin wrapper over docs.discovery_pack."""
    account, competitor = None, None
    if name:
        acc, err = _resolve(name)
        if err:
            return err
        aid = acc["ACCOUNT_ID"]
        account = acc["COMPANY_NAME"]
        comp = signals._competitor_mention(data.get_activities(aid, 40))
        competitor = comp["competitor"] if comp else None
    out = docs.discovery_pack(competitor)
    if account:
        out["account"] = account
    return out


def match_case_study(name: str) -> dict:
    """The single closest customer proof point for a prospect's firmographics (industry first,
    then region), cited and verbatim — or an honest 'no reference on file'. Thin wrapper over
    docs.match_case_study + docs.retrieve_case_study."""
    acc, err = _resolve(name)
    if err:
        return err
    acc = data.get_account(acc["ACCOUNT_ID"])
    match = docs.match_case_study(acc["INDUSTRY"], acc["REGION"])
    if not match:
        return {"account": acc["COMPANY_NAME"], "case_study": None,
                "note": f"No close reference on file for {acc['INDUSTRY']} — ping #customer-references."}
    return {"account": acc["COMPANY_NAME"], "case_study": docs.retrieve_case_study(match)}


# --- tool schemas + dispatch, grouped into briefings -----------------------------

ALL_TOOL_SCHEMAS: dict[str, dict] = {
    "assess_account": {"type": "function", "function": {
        "name": "assess_account",
        "description": "Pre-call brief for an account — use this FIRST for any account. Customer: a "
                       "RED/AMBER/GREEN verdict + fired risk signals with evidence + playbook recommendations "
                       "+ renewal/expansion timing. Prospect: a discovery brief (ICP fit, a matched cited case "
                       "study, the open deal's stage, and a competitor battlecard if one is named).",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}},
    "account_details": {"type": "function", "function": {
        "name": "account_details",
        "description": "Drill into one dimension of an account for a follow-up.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"},
            "dimension": {"type": "string", "enum": ["usage", "tickets", "contacts", "opportunities", "activities"]}},
            "required": ["name", "dimension"]}}},
    "search_playbook": {"type": "function", "function": {
        "name": "search_playbook",
        "description": "Search the sales-enablement docs for a general question (playbook, battlecards, ICP, "
                       "objection handling, pricing, case studies).",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    "list_customers": {"type": "function", "function": {
        "name": "list_customers",
        "description": "The AE's CUSTOMERS with a one-glance RED/AMBER/GREEN status + renewal timing, ranked "
                       "worst-first. Use for 'what should I prep', 'top retention risks', 'which renewals are "
                       "coming up', 'how many renewals' — no account name needed. Returns customers only.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    "list_prospects": {"type": "function", "function": {
        "name": "list_prospects",
        "description": "The AE's PROSPECTS with their open-deal status (stage, days-in-stage, amount), most-"
                       "stalled first. Use for 'which prospect needs attention', 'how many prospects', 'my "
                       "pipeline'. Returns prospects only.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    "todays_priorities": {"type": "function", "function": {
        "name": "todays_priorities",
        "description": "The AE's cross-book attention shortlist — merges at-risk renewals (customers) AND stuck "
                       "prospect deals into ONE ranked list with the priority rule used. Use for 'what should I do "
                       "today', 'top calls today', 'my priorities', 'my day'. NOTE: it ranks by account risk + "
                       "deal momentum, NOT by a calendar — it doesn't know which calls are actually scheduled today.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    "list_expansion_candidates": {"type": "function", "function": {
        "name": "list_expansion_candidates",
        "description": "The AE's customers with a real, grounded expansion signal (open expansion deal, usage "
                       "growth, or whitespace module + warm stakeholder), ranked. Use for 'where can I upsell', "
                       "'who's ready to expand', 'cross-sell opportunities'. Returns customers only.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    "icp_fit": {"type": "function", "function": {
        "name": "icp_fit",
        "description": "ICP-fit read (size band, vertical, region) for one account against the sweet spot, cited.",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}},
    "discovery_brief": {"type": "function", "function": {
        "name": "discovery_brief",
        "description": "Discovery prep for one prospect (or a short win-back note if churned): ICP fit, matched "
                       "cited case study, open-deal stage/momentum, and a competitor battlecard if named. Call "
                       "this FIRST for any named prospect.",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}},
    "discovery_pack": {"type": "function", "function": {
        "name": "discovery_pack",
        "description": "Call-prep questions + the MEDDIC qualification checklist for a discovery call, tuned to a "
                       "named competitor if the account (by name) has one, else the general playbook. Omit the "
                       "name for the general pack.",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": []}}},
    "match_case_study": {"type": "function", "function": {
        "name": "match_case_study",
        "description": "The single closest customer proof point for a prospect's firmographics (industry, "
                       "region), cited and verbatim — or an honest 'no reference on file'.",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}},
    "switch_briefing": {"type": "function", "function": {
        "name": "switch_briefing",
        "description": "Switch the active briefing when the AE's ask has clearly moved to a different job than "
                       "the one you're currently briefed for.",
        "parameters": {"type": "object", "properties": {
            "briefing": {"type": "string", "enum": ["retention", "expansion", "prospect"]}},
            "required": ["briefing"]}}},
}

ALL_TOOL_FUNCS = {
    "assess_account": assess_account, "account_details": account_details,
    "search_playbook": search_playbook, "list_customers": list_customers,
    "list_prospects": list_prospects, "todays_priorities": todays_priorities,
    "list_expansion_candidates": list_expansion_candidates, "icp_fit": icp_fit,
    "discovery_brief": discovery_brief, "discovery_pack": discovery_pack,
    "match_case_study": match_case_study,
}


def _make_switch_tool(session_state: dict):
    """switch_briefing needs to mutate the CONVERSATION's state (not just return data), so it's
    built per-session rather than living as a plain stateless function like the other tools."""
    def switch_briefing(briefing: str) -> dict:
        key = briefings.canon(briefing)
        if not key:
            return {"error": f"Unknown briefing '{briefing}'. Choose retention, expansion, or prospect."}
        session_state["briefing"] = key
        return {"switched_to": key, "label": briefings.BRIEFINGS[key]["label"]}
    return switch_briefing


def get_toolset(briefing_key: str, session_state: dict) -> tuple[list, dict]:
    """The tool SCHEMAS + DISPATCH for one active briefing (its own tools + the always-available
    switch_briefing) — this is what makes the briefing's scoping real: only these tools are ever
    passed to the model for a turn on this briefing."""
    names = briefings.BRIEFINGS[briefing_key]["tools"]
    tools = [ALL_TOOL_SCHEMAS[n] for n in names] + [ALL_TOOL_SCHEMAS["switch_briefing"]]
    dispatch = {n: ALL_TOOL_FUNCS[n] for n in names}
    dispatch["switch_briefing"] = _make_switch_tool(session_state)
    return tools, dispatch


# backward-compat defaults (RETENTION briefing) — used when a caller (the web demo, or a test
# that predates the briefing split) invokes run_turn without an explicit toolset.
_DEFAULT_STATE = {"briefing": briefings.DEFAULT_BRIEFING}
DEFAULT_TOOLS, DEFAULT_DISPATCH = get_toolset(briefings.DEFAULT_BRIEFING, _DEFAULT_STATE)
SYSTEM = briefings.system_prompt(briefings.DEFAULT_BRIEFING)  # BRAND-aware; see brand.py


# --- the tool-use loop (ours) -------------------------------------------------

def _first_line(text: str) -> str:
    """First substantive body line of a doc section (skipping the section header itself) —
    a short verbatim quote for the citation."""
    for ln in text.splitlines()[1:]:  # skip the section header line
        s = ln.strip().lstrip("#-*> ").replace("**", "").strip()
        if len(s) > 20 and not s.startswith(("Internal", "For AE")):
            return s
    return text.strip()[:120]


def _excerpt(text: str, n: int = 4) -> list:
    """First n substantive lines of a doc section (skip the header + blanks) — a short verbatim
    snippet so any figure the model quotes from a search hit (e.g. a discount %, a €threshold) is
    FROZEN in the footer, not merely cited by section name."""
    out = []
    for ln in text.splitlines()[1:]:                 # skip the section header line
        s = ln.strip().replace("**", "")
        if s:
            out.append(s)
        if len(out) >= n:
            break
    return out


def _details_evidence(dim: str, rows: list) -> list:
    """Turn an account_details drill-in into verbatim footer lines, so the SPECIFIC numbers
    the AE hears on a drill-in (the exact MAU series, ticket dates, contact recency) are
    frozen from code too — not just the top-level risk brief."""
    if dim == "usage":
        return ["USAGE.MONTHLY_ACTIVE_USERS: " + "→".join(str(r["MONTHLY_ACTIVE_USERS"]) for r in rows)]
    if dim == "tickets":
        return [f'{r["PRIORITY"]} "{r["SUBJECT"]}" ({str(r["CREATED_DATE"])[:10]}, {r["STATUS"]})' for r in rows]
    if dim == "contacts":
        return [f'{r["FULL_NAME"]} — {r["PERSONA_TYPE"]} (last {str(r["LAST_INTERACTION"])[:10]})' for r in rows]
    if dim == "opportunities":
        return [f'{r["NAME"]}: {r["TYPE"]}/{r["STAGE"]}, close {str(r["CLOSE_DATE"])[:10]}' for r in rows]
    if dim == "activities":
        return [f'{str(r["ACTIVITY_DATE"])[:10]}: {r["SUMMARY"]}' for r in rows[:6]]
    return []


def _grounding(results: list) -> str:
    """Deterministic 'retrieved-and-frozen' footer, built by CODE (not the LLM) from the
    turn's tool results — so the load-bearing numbers, quotes and sources cannot drift."""
    verdict, ev, srcs = None, [], []
    for r in results:
        if not isinstance(r, dict):
            continue
        if "verdict" in r:                                    # the code-computed RED/AMBER/GREEN
            verdict = r["verdict"]
        if "signals" in r:                                    # assess_account (customer / retention)
            ev += [s["evidence"] for s in r["signals"]]
            # freeze the load-bearing COMMERCIAL numbers too — the system prompt tells the model
            # to lead with the renewal timing / ARR, so those must sit in the verbatim block, not
            # just the risk-signal evidence (otherwise "€76k, closes in 18 days" could drift).
            if r.get("arr_eur") is not None:
                ev.append(f'ACCOUNT.ARR_EUR: €{r["arr_eur"]:,.0f}')
            if r.get("renewal_closes_in_days") is not None:
                ev.append(f'RENEWAL: closes in {r["renewal_closes_in_days"]} days')
            if r.get("expansion_closes_in_days") is not None:
                ev.append(f'EXPANSION: closes in {r["expansion_closes_in_days"]} days')
            srcs += [f'{rec["cite"]}: "{_first_line(rec["playbook_text"])}"'
                     for rec in r.get("recommendations", [])]
        elif r.get("mode") in ("discovery", "churned"):       # assess_account / discovery_brief (prospect / churned)
            ev += r.get("icp_fit", {}).get("notes", [])
            for o in r.get("open_opportunities", []):          # freeze the deal figures spoken in discovery
                amt = f'€{o["amount_eur"]:,.0f}' if o.get("amount_eur") is not None else "n/a"
                ev.append(f'OPPORTUNITY {o["name"]}: {o["type"]}/{o["stage"]}, {amt}, {o["days_in_stage"]}d in stage')
            for block in (r.get("case_study"), r.get("icp_fit")):
                if block and block.get("citation"):
                    srcs.append(block["citation"])
            comp = r.get("competitor")
            if comp:
                srcs.append(f'{comp["cite"]}: "{_first_line(comp["playbook_text"])}"')
            disc = r.get("discovery")                          # freeze the qualification checklist + cite the questions
            if disc:
                # the snapshot doesn't say which MEDDIC fields are already covered, so this is a
                # checklist to VERIFY on the call — not an assertion that these are open gaps
                ev.append("QUALIFICATION CHECKLIST (MEDDIC — status not in snapshot, verify on call): "
                          + ", ".join(disc["qualification"]["checklist"]))
                srcs.append(disc["questions"]["citation"])
                srcs.append(disc["qualification"]["citation"])
        elif "customers" in r and "counts" in r:              # list_customers (retention triage) — customers only
            c = r["counts"]
            ev.append(f'CUSTOMERS: {len(r["customers"])} — {c.get("RED", 0)} RED / {c.get("AMBER", 0)} AMBER / '
                      f'{c.get("GREEN", 0)} GREEN ({c.get("open_renewals", 0)} with an open renewal)')
            # freeze the top-3 ranked customers WITH their levers — the load-bearing claims a
            # "top retention risks" answer leads with (verdict, signal count, renewal timing)
            for i, x in enumerate([cu for cu in r["customers"] if cu["verdict"] in ("RED", "AMBER")][:3], 1):
                rn = f'renewal in {x["renewal_in_days"]}d' if x.get("renewal_in_days") is not None else "no open renewal"
                ev.append(f'#{i} {x["name"]} — {x["verdict"]}, {x["risk_signals"]} signal(s), {rn}')
        elif "prospects" in r and "counts" in r:              # list_prospects (pipeline triage) — prospects only
            c = r["counts"]
            ev.append(f'PROSPECTS: {c.get("prospects", 0)} ({c.get("prospects_with_deals", 0)} with open deals)')
            for p in [x for x in r["prospects"] if x.get("top_deal")][:3]:   # top-3 most-stalled, already sorted
                td = p["top_deal"]
                ev.append(f'PROSPECT {p["name"]} ({p["industry"]}/{p["region"]}): '
                          f'{td["stage"]}, {td["days_in_stage"]}d in stage, €{td["amount_eur"]:,.0f}')
        elif "priorities" in r:                               # todays_priorities (merged day plan)
            short = {"usage_decline": "usage", "critical_ticket": "P1 ticket",
                     "competitor": "competitor", "stakeholder_gap": "stakeholder gap"}
            for it in r["priorities"]:
                line = f'{it["tag"]} {it["name"]} — {it["when"]}'
                if it.get("signals"):                         # renewals: freeze the converging signals
                    n = len(it["signals"])
                    line += f' ({n} signal{"s" if n != 1 else ""}: ' + \
                            ", ".join(short.get(s, s) for s in it["signals"]) + ")"
                elif it.get("amount_eur") is not None:        # stuck deals: freeze the deal size
                    line += f' (€{it["amount_eur"]:,.0f})'
                ev.append(line)
            if r.get("rule"):
                ev.append(f'PRIORITY RULE: {r["rule"]}')
        elif "candidates" in r and "counts" in r:             # list_expansion_candidates
            c = r["counts"]
            ev.append(f'EXPANSION CANDIDATES: {c.get("candidates", 0)}')
            for x in r["candidates"][:3]:
                ev.append(f'{x["name"]} — {x["reason"]}')
        elif "case_study" in r and "account" in r:            # match_case_study (standalone tool)
            cs = r["case_study"]
            if cs:
                srcs.append(cs["citation"])
                ev.append(f'{r["account"]}: matched case study — {cs["citation"]}')
            elif r.get("note"):
                ev.append(f'{r["account"]}: {r["note"]}')
        elif "notes" in r and "citation" in r and "account" in r:  # icp_fit (standalone tool)
            ev += [f'{r["account"]} ICP: {n}' for n in r["notes"]]
            srcs.append(r["citation"])
        elif "questions" in r and "qualification" in r:       # discovery_pack (standalone tool)
            ev.append("QUALIFICATION CHECKLIST (MEDDIC — status not in snapshot, verify on call): "
                      + ", ".join(r["qualification"]["checklist"]))
            srcs.append(r["questions"]["citation"])
            srcs.append(r["qualification"]["citation"])
        elif "results" in r:                                  # search_playbook
            for s in r["results"]:
                srcs.append(f'{s["file"]} · {s["header"]}')
                # freeze a short verbatim excerpt so quoted figures (discount %, €thresholds) are visible
                for ln in _excerpt(s["text"]):
                    ev.append(f'{s["header"]}: {ln}')
        elif "account" in r and len(r) == 2:                  # account_details {account, <dim>} (drill-in)
            dim = next(k for k in r if k != "account")
            ev += _details_evidence(dim, r[dim])              # freeze the drilled-in figures too
            srcs.append(f'{r["account"]} · {dim} (snapshot)')
    if not (verdict or ev or srcs):
        return ""
    lines = ["", "— grounded in (verbatim) —"]
    if verdict:  # the verdict is code-decided, so we show it frozen too
        lines.append(f'  ▸ verdict: {verdict["level"]}  (deterministic · {verdict["risk_signal_count"]} risk signals)')
    lines += [f"  • {e}" for e in dict.fromkeys(ev)]          # dedup, preserve order
    lines += [f"  ↳ {s}" for s in dict.fromkeys(srcs)]
    return "\n".join(lines)


MAX_TOOL_ROUNDS = 6  # a brief needs 1-4 tool calls; this caps runaway loops (cost/latency safety)


def run_turn(messages: list, client, tools: list | None = None, dispatch: dict | None = None) -> str:
    """Run one user turn to completion: call the model, execute any tool calls, loop until
    a final text answer. Appends a deterministic verbatim grounding footer. Mutates `messages`.

    `tools`/`dispatch` scope this turn to ONE active briefing's tools (see get_toolset) — the
    model is never offered tools outside the briefing currently in play. Omitting them falls
    back to the RETENTION briefing's toolset, for callers that don't route (web demo, legacy
    single-briefing tests)."""
    tools = DEFAULT_TOOLS if tools is None else tools
    dispatch = DEFAULT_DISPATCH if dispatch is None else dispatch
    turn_results = []
    for round_i in range(MAX_TOOL_ROUNDS + 1):
        # on the last allowed round we forbid tools ("none"), forcing a final text answer —
        # so a model that keeps requesting tools can never loop against the API forever
        force_final = round_i == MAX_TOOL_ROUNDS
        try:
            resp = client.chat.completions.create(
                model=MODEL, messages=messages, tools=tools,
                tool_choice="none" if force_final else "auto", temperature=0)
        except Exception as e:
            # if the LLM is unreachable (outage / rate limit / no network) don't crash the
            # chat — point to the deterministic offline brief, which needs no LLM at all
            return (f"(LLM unavailable: {e}\n Try the deterministic brief: "
                    f"python src/agent.py --offline <account>)")
        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))
        if not msg.tool_calls:
            return (msg.content or "") + _grounding(turn_results)
        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments or "{}")
            try:
                result = dispatch[tc.function.name](**args)
            except Exception as e:  # never crash the chat on a tool error
                result = {"error": str(e)}
            turn_results.append(result)
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": json.dumps(result, default=str)})
    return "I couldn't complete that — too many steps."  # safety net (unreachable in practice)


def _offline_render(out: dict) -> str:
    """Render a brief from assess_account WITHOUT any LLM — the demo fallback if OpenAI is
    down. It's the same deterministic content (verdict + signals + cited recommendations, or
    the discovery brief), just formatted by code instead of phrased by the model."""
    if any(k in out for k in ("error", "out_of_scope", "ambiguous")):
        return str(next(iter(out.values())))
    lines = [f'━ {out["account"]}  ({out["status"]} · {out["segment"]}/{out["region"]}) ━']
    if out.get("verdict"):                                    # customer / retention
        lines.append(f'{out["verdict"]["level"]} — renewal risk')
        lines += [f'  • {s["summary"]}' for s in out.get("signals", [])]
        lines += [f'  → {rec["cite"]}' for rec in out.get("recommendations", [])]
        if out.get("renewal_closes_in_days") is not None:
            lines.append(f'  renewal closes in {out["renewal_closes_in_days"]} days')
    else:                                                     # prospect / churned
        lines.append(out.get("note", ""))
        lines += [f'  · {n}' for n in out.get("icp_fit", {}).get("notes", [])]
        cs = out.get("case_study", {})
        lines.append("  proof point: " + (cs.get("citation") or cs.get("text", "")))
        disc = out.get("discovery")
        if disc:                                              # prospect: questions to ask + MEDDIC to verify on call
            qs = [ln.strip().lstrip("-").strip() for ln in disc["questions"]["text"].splitlines()
                  if ln.strip().startswith("-")][:3]
            if qs:
                lines.append("  ask on this call:")
                lines += [f"    – {q}" for q in qs]
            lines.append("  verify on this call (MEDDIC — status not in snapshot): "
                         + ", ".join(disc["qualification"]["checklist"]))
    return "\n".join(lines) + _grounding([out])


# --- briefing routing (turn-level) ------------------------------------------------

def route_turn(user_text: str, current_briefing: str | None, client=None) -> tuple[str, bool]:
    """Decide the active briefing for this user turn. Thin pass-through to briefings.route —
    kept here too so callers only need `import agent`."""
    return briefings.route(user_text, current_briefing, client)


def handle_message(user_text: str, client, session_state: dict, messages: list) -> tuple[str, bool, str]:
    """Route one user turn to a briefing, (re)point the system prompt + toolset at it, run it,
    and return (briefing, changed, reply). `session_state` and `messages` persist across turns
    (REPL-style); `session_state["briefing"]` may also be updated by a switch_briefing tool call
    made by the model mid-turn, so it — not the pre-call `briefing` — is the source of truth
    once run_turn returns."""
    briefing, changed = briefings.route(user_text, session_state.get("briefing"), client)
    session_state["briefing"] = briefing
    tools, dispatch = get_toolset(briefing, session_state)
    sys_msg = {"role": "system", "content": briefings.system_prompt(briefing)}
    if messages and messages[0].get("role") == "system":
        messages[0] = sys_msg
    else:
        messages.insert(0, sys_msg)
    messages.append({"role": "user", "content": user_text})
    reply = run_turn(messages, client, tools, dispatch)
    return briefing, changed, reply


def chat():
    """Terminal REPL. Routes every turn to a briefing (RETENTION / EXPANSION / PROSPECT),
    exposes only that briefing's tools, and announces a briefing change with one short line."""
    from openai import OpenAI
    client = OpenAI()
    session_state = {"briefing": None}
    messages = []
    print(f"{BRAND['company']} AE prep assistant · model {MODEL} · "
          f"type an account or question ('exit' to quit)\n")
    while True:
        try:
            user = input("you › ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if user.lower() in ("exit", "quit", ""):
            break
        briefing_before = session_state.get("briefing")
        briefing, changed, reply = handle_message(user, client, session_state, messages)
        # a mid-turn switch_briefing tool call may have moved session_state past `briefing`
        final_briefing = session_state.get("briefing", briefing)
        prefix = ""
        if changed or final_briefing != briefing_before:
            prefix = f"[briefing: {briefings.BRIEFINGS[final_briefing]['label']}]\n"
        print("\n" + prefix + reply + "\n")


if __name__ == "__main__":
    # `python src/agent.py --offline <account>` prints the deterministic brief with NO LLM
    # (the demo safety net); `--route <text>` prints which briefing the router picks for a
    # message, also with NO LLM (keyword-first router needs no model); otherwise start the
    # conversational REPL.
    if len(sys.argv) >= 3 and sys.argv[1] == "--offline":
        print(_offline_render(assess_account(" ".join(sys.argv[2:]))))
    elif len(sys.argv) >= 3 and sys.argv[1] == "--route":
        text = " ".join(sys.argv[2:])
        key, changed = briefings.route(text, None, client=None)
        print(f'"{text}" -> briefing: {briefings.BRIEFINGS[key]["label"]} '
              f'(tools: {", ".join(briefings.BRIEFINGS[key]["tools"])})')
    else:
        chat()
