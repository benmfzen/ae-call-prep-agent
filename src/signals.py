"""
Retention KPIs — deterministic, playbook-grounded signal computation.

This module has two clearly-separated parts, on purpose:
  * compute_signals(account) — the pure per-account KPI computation: which signals
    fired, each a self-contained citable result (value + evidence + playbook anchor).
  * band(signals) — a transparent convergence heuristic that rolls those fired signals
    into a single RED / AMBER / GREEN verdict.

Why keep them apart: the KPIs stay independently testable, and — crucially — the RISK
CALL ("this renewal is at risk") is made HERE, in deterministic code, never by the LLM.
band() is a heuristic, not a calibrated model: with no real churn labels to fit weights,
we score by convergence (how many independent signals fire) rather than pretend to a
tuned probability. That is honest and, more importantly, explainable line-by-line.

Design stance (the trust story):
  * Signals are computed here in plain Python, never by the LLM. Every fired signal
    cites a data field AND a rule (sales playbook / industry benchmark).
  * Only signals that DISCRIMINATE the churn account (Onyx Partners) from the healthy one
    (Fjord Construction) are computed; signals that fire on both — raw "no champion", close-
    date proximity, activity gap, module flags — are excluded because they fire on the healthy
    account too and so don't separate churn from health.
  * Thresholds are benchmark-anchored; the reasoning for each is in the inline note beside the constant.
"""
from __future__ import annotations

import re
from datetime import date

import data

# --- thresholds (explicit; benchmark-anchored — see retention-kpi-decisions.md) ---
USAGE_DECLINE_PCT = -0.15      # material MAU decline (industry 15-20% cut); separates Onyx Partners/Cobalt from noise
USAGE_STRONG_PCT = -0.25       # magnitude note: below this is a steep drop
USAGE_RECOVERY_RETRACE = 0.5   # recovery guard: only suppress a decline if usage climbed back
                               # at least this share of the drop (trough→peak); a lone +1 tick fails it
TICKET_RECENT_DAYS = 90        # a recent P1/P2 still matters; older incidents have healed
TICKET_RARITY_MAX = 2          # subject on <=2 accounts = bespoke; systemic (SSO, 7 accts) is noise
CONTACT_WARM_DAYS = 60         # a stakeholder touched within 60d is "warm" (Playbook §8 uses a 60-day lens)

# "senior" stakeholders whose warm engagement counts as real renewal coverage. A warm
# Technical/User contact does NOT count — an IT-only warm thread on a renewal is
# the gap, not the cover (this is exactly Onyx Partners' failure mode).
SENIOR_PERSONAS = ("Champion", "Economic Buyer", "Influencer")

# Rivals a customer would churn TO. Legacy incumbents (Meridian/Corelink/ADP) are deliberately
# excluded — they are systems customers migrate FROM, not rivals they leave to. Matching is
# word-boundary (see below), which also guards against a short competitor name substring-
# matching inside an unrelated word (the historical bug: a 4-letter legacy-vendor token
# matching inside the word "usage").
COMPETITORS = ["Kestrel", "Vantive", "Tandem"]
COMPETITOR_RECENT_DAYS = 90    # a competitor mention older than this is stale (mirrors the ticket window)
# templated free-text that mentions a competitor but is NOT a real signal
BOILERPLATE = ["battlecard", "comparison", "roi calculator"]
# competitor OUTBOUND, not the customer evaluating — a rival cold-emailing our champion is the rival's
# activity, not buyer intent. It's noise (and here a template across ~8 accounts), so it must NOT alone
# make an account RED. Precision over recall: we override only on customer-side interest, not spam.
COMPETITOR_OUTBOUND = ["cold-email", "cold email", "cold outreach", "reached out", "cold-called", "cold call"]


def _d(s: str | None) -> date | None:
    return date.fromisoformat(str(s)[:10]) if s else None


def _age(s: str | None) -> int | None:
    d = _d(s)
    return (data.ASOF - d).days if d else None


def _age_or(s: str | None, default: int = 999) -> int:
    """Age in days, or `default` if missing. Explicit None-guard (age 0 = touched today
    must NOT be treated as missing — `_age(s) or 999` would wrongly bucket it as stale)."""
    a = _age(s)
    return default if a is None else a


# --- individual KPIs. Each returns a fired-signal dict, or None -----------------

def _usage_decline(usage: list[dict]) -> dict | None:
    """MAU trend (Playbook §6 adoption anchor). Guards: drop non-positive baselines
    (Vector MAU -4) and interior-zero months (Cobalt Mar=0) — trend over valid positives."""
    valid = [r["MONTHLY_ACTIVE_USERS"] for r in usage
             if r["MONTHLY_ACTIVE_USERS"] and r["MONTHLY_ACTIVE_USERS"] > 0]
    if len(valid) < 2:
        return None
    pct = (valid[-1] - valid[0]) / valid[0]
    if pct > USAGE_DECLINE_PCT:
        return None
    # recovery guard: don't cry wolf on an account that is GENUINELY bouncing back — but a
    # single +1 tick on a still-cratered account is not a recovery. Suppress only when usage
    # is BOTH rising on the last month AND has retraced at least half the drop from its low
    # back toward its peak. Cobalt (…4→17) retraced ~59% → a real rebound, stays quiet; a
    # -50% account that merely ticks 92→93 retraced ~1% → still the story, so it fires.
    peak, trough = max(valid), min(valid)
    rising = valid[-1] > valid[-2]
    retraced = (valid[-1] - trough) / (peak - trough) if peak > trough else 0.0
    if rising and retraced >= USAGE_RECOVERY_RETRACE:
        return None
    series = "→".join(str(r["MONTHLY_ACTIVE_USERS"]) for r in usage)
    logins = [r["LOGINS"] for r in usage if r["LOGINS"] is not None]
    depth_note = ""
    if len(logins) >= 2 and logins[-1] < logins[0]:
        depth_note = f" Logins also falling ({logins[0]}→{logins[-1]}) — corroborates."
    return {
        "signal": "usage_decline",
        "severity": "strong",
        "value": f"{round(pct*100)}%",
        "summary": f"Active users down {round(pct*100)}% ({valid[0]}→{valid[-1]})"
                   f"{' — steep' if pct <= USAGE_STRONG_PCT else ''}.{depth_note}",
        "evidence": f"PRODUCT.USAGE.MONTHLY_ACTIVE_USERS: {series}",
        "anchor": "Playbook §6: renewal anchor #1 is adoption & impact (industry: 15-20% drop = churn watch).",
    }


def _bespoke_ticket(tickets: list[dict], rarity: dict[str, int]) -> dict | None:
    """Critical-incident signal: severity x rarity x recency (Playbook §8; ChurnZero
    'severity of cases'). A bespoke P1/P2 fires; a systemic outage (subject shared
    across many accounts) is suppressed."""
    for t in tickets:
        if t["PRIORITY"] not in ("P1", "P2"):
            continue
        if rarity.get(t["SUBJECT"], 99) > TICKET_RARITY_MAX:
            continue  # systemic / templated → noise
        age = _age(t["CREATED_DATE"])
        if age is None or age > TICKET_RECENT_DAYS:
            continue
        state = "open" if not t["RESOLVED_DATE"] else "resolved, but trust lingers"
        return {
            "signal": "critical_ticket",
            "severity": "strong",
            "value": t["PRIORITY"],
            "summary": f'{t["PRIORITY"]} “{t["SUBJECT"]}” ({t["CREATED_DATE"][:10]}, {state}).',
            "evidence": f'SUPPORT.TICKETS: {t["PRIORITY"]} — {t["SUMMARY"]}',
            "anchor": "Playbook §8 (recent P1s) — account-specific incident, not a systemic wave.",
        }
    return None


def _competitor_mention(activities: list[dict]) -> dict | None:
    """Genuine competitor mention in activity free-text → bridges to the battlecard.
    De-noised: the templated 'shared the Vantive comparison battlecard' boilerplate is
    excluded, so only real, bespoke mentions fire (quote-only, never inferred)."""
    for a in activities:
        if _age_or(a["ACTIVITY_DATE"]) > COMPETITOR_RECENT_DAYS:
            continue  # stale mention (>90d) — not a live threat
        summary = a["SUMMARY"] or ""
        low = summary.lower()
        if any(b in low for b in BOILERPLATE):
            continue
        if any(o in low for o in COMPETITOR_OUTBOUND):        # competitor's outreach ≠ customer intent
            continue
        for comp in COMPETITORS:
            # word-boundary match, so a short competitor token no longer fires inside "usage"
            if re.search(rf"\b{re.escape(comp.lower())}\b", low):
                return {
                    "signal": "competitor",
                    "severity": "strong",
                    "value": comp,
                    "summary": f'Competitor named: {comp} — “{summary}” ({a["ACTIVITY_DATE"][:10]}).',
                    "evidence": f'CRM.ACTIVITIES.SUMMARY: {summary}',
                    "anchor": f"Pull the {comp} battlecard (customer is evaluating alternatives).",
                    "competitor": comp,
                }
    return None


def _stakeholder_gap(contacts: list[dict], opps: list[dict]) -> dict | None:
    """Decision-maker warmth (refined multithreading, Playbook §5). Fires when an open
    renewal has NO warm (<=60d) SENIOR stakeholder (Champion / Economic Buyer / Influencer).
    A warm Technical/User contact does NOT count — an IT-only warm thread on a renewal
    is the gap, not the cover (Onyx Partners). This discriminates: Fjord Construction has warm
    Influencers and Timberline Services has a warm Economic Buyer, so both correctly stay
    silent, while Onyx Partners (warm only through IT) and Solace Logistics (all cold) fire."""
    open_renewal = any(o["TYPE"] == "Renewal" and not o["WON_LOST_REASON"] for o in opps)
    if not open_renewal:
        return None
    warm_senior = any(
        c["PERSONA_TYPE"] in SENIOR_PERSONAS
        and _age_or(c["LAST_INTERACTION"]) <= CONTACT_WARM_DAYS
        for c in contacts
    )
    if warm_senior:
        return None
    warm_junior = sorted({c["PERSONA_TYPE"] for c in contacts
                          if _age_or(c["LAST_INTERACTION"]) <= CONTACT_WARM_DAYS})
    cover = f"{', '.join(warm_junior)} only" if warm_junior else "no one"
    return {
        "signal": "stakeholder_gap",
        "severity": "moderate",
        "value": cover,
        "summary": f"Open renewal with no warm senior stakeholder (Champion/EB/Influencer) — "
                   f"warm coverage: {cover}.",
        "evidence": f"CRM.CONTACTS: no Champion/Economic Buyer/Influencer touched in the last "
                    f"{CONTACT_WARM_DAYS} days.",
        "anchor": "Playbook §5: engage a senior buyer — an IT-only or cold renewal stalls.",
    }


# NOTE: a "stuck deal" (days-in-stage) signal is deliberately NOT a retention signal here. Deal
# stagnation is a deal-velocity / new-business concept, not customer health, and it fires on ZERO
# of Mara's customer renewals. So it never touches a renewal verdict — but it IS the right signal
# for the new-business job, so it lives on the prospect side (agent.todays_priorities uses a
# >45-day stall to prioritise slipping deals). A context-dependent signal decision, not a blanket cut.


def _expansion_in_flight(opps: list[dict]) -> dict | None:
    """The one health-POSITIVE signal (green offset). A customer actively buying more is
    investing further — the inverse of churn (OpenView/Bessemer). Absence is not penalised."""
    for o in opps:
        if o["TYPE"] == "Expansion" and not o["WON_LOST_REASON"]:
            return {
                "signal": "expansion_in_flight",
                "severity": "positive",
                "value": f'€{o["AMOUNT_EUR"]/1000:.0f}k',
                "summary": f'Open expansion in flight: “{o["NAME"]}” (€{o["AMOUNT_EUR"]:.0f}, {o["STAGE"]}).',
                "evidence": f'CRM.OPPORTUNITIES: Expansion, {o["STAGE"]}, close {o["CLOSE_DATE"][:10]}.',
                "anchor": "Expansion = health-positive (OpenView/Bessemer): the customer is investing more.",
            }
    return None


# --- orchestration: compute signals only (NO verdict / banding) -----------------

# signals that are decisive on their own on an open renewal: a customer actively naming a
# competitor, or a bespoke trust-damaging support incident. Either alone -> RED.
HARD_OVERRIDE_SIGNALS = ("competitor", "critical_ticket")


def band(signals: list[dict]) -> dict:
    """Roll the fired signals into a single RED / AMBER / GREEN verdict — the one-glance
    'is this renewal at risk?' an AE wants before a call. This is the RISK CALL, and it is
    made deterministically in code (not by the LLM).

    The rule, by CONVERGENCE not a tuned score:
      RED   = >=2 strong signals fire together, OR a hard-override fires
              (a competitor mention or a bespoke trust-ticket is decisive on its own)
      AMBER = at least one NON-override risk signal fired, but not enough to converge to RED
              (e.g. a lone usage_decline, or usage_decline + the moderate stakeholder gap) —
              a "glance at this", not a fire alarm. (A lone competitor or bespoke ticket is a
              hard-override and skips AMBER straight to RED.)
      GREEN = no risk signal fired
    Expansion is a POSITIVE signal and never counts toward risk, so it can't turn a
    declining account green.
    """
    # keep only the risk-bearing signals (strong or moderate); drop positives like expansion
    risk = [s for s in signals if s["severity"] in ("strong", "moderate")]
    strong = [s for s in risk if s["severity"] == "strong"]
    override = [s for s in risk if s["signal"] in HARD_OVERRIDE_SIGNALS]

    if len(strong) >= 2 or override:          # convergence, or a decisive single signal
        level = "RED"
    elif risk:                                # one signal worth a look
        level = "AMBER"
    else:                                     # nothing fired
        level = "GREEN"

    # the reason is just the fired risk signals' own summaries — nothing invented
    reason = "; ".join(s["summary"] for s in risk) or "no risk signals fired"
    return {"level": level, "reason": reason, "risk_signal_count": len(risk)}


def compute_signals(account_id: str) -> dict:
    """Compute the retention KPIs for a CUSTOMER account. Returns the fired signals
    (each individually citable) plus light context — but NO aggregated verdict; the
    RED/AMBER/GREEN banding lives elsewhere and is intentionally not decided here.

    Prospects/churned have no usage/tickets → returns assessable=False so the caller
    routes to the discovery path instead of fabricating retention signals."""
    acc = data.get_account(account_id)
    usage = data.get_usage(account_id)
    opps = data.get_opportunities(account_id)

    if acc["STATUS"] != "customer" or not usage:
        return {"assessable": False, "status": acc["STATUS"],
                "reason": "No product-usage/support data (not an active customer) — "
                          "retention KPIs don't apply; use the discovery context instead."}

    contacts = data.get_contacts(account_id)
    tickets = data.get_tickets(account_id)
    activities = data.get_activities(account_id, limit=40)
    rarity = data.ticket_subject_rarity()

    signals = [s for s in (
        _usage_decline(usage),
        _bespoke_ticket(tickets, rarity),
        _competitor_mention(activities),
        _stakeholder_gap(contacts, opps),
        _expansion_in_flight(opps),
    ) if s]

    # close-date proximity: context/urgency for ordering — NOT a risk signal
    open_opps = [o for o in opps if not o["WON_LOST_REASON"] and o["CLOSE_DATE"]]
    soonest = min(open_opps, key=lambda o: o["CLOSE_DATE"], default=None)
    context = None
    if soonest:
        context = {"next_opp": soonest["NAME"], "type": soonest["TYPE"],
                   "close_date": soonest["CLOSE_DATE"][:10],
                   "days_to_close": -(_age(soonest["CLOSE_DATE"]) or 0)}

    return {"assessable": True, "signals": signals, "context": context}
