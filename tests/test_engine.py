"""
Eval harness — "how do you know it's good enough".

This pins the engine's behaviour to a GOLDEN TRUTH TABLE: a handful of accounts whose
correct outcome we're confident about (data + narrative agree), plus the specific
false-positive traps a prior adversarial review found and we fixed. It runs in seconds,
uses NO LLM (only the deterministic engine), and turns "an adversarial review read it and it was
right" into a green test run anyone can reproduce.

What "good" means here (the trust bar): precision over recall — never cry wolf on a healthy
account. So the tests check both that the real risk fires (Onyx Partners) AND that the scary-
but-healthy accounts stay silent (Timberline Services, Fjord Construction), and that the known
false positives are dead (the short-competitor-token-inside-"usage" substring bug, the
templated-Vantive boilerplate).

Run:  .venv/bin/python -m pytest tests/ -q      (or: .venv/bin/python tests/test_engine.py)
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
import data      # noqa: E402
import docs      # noqa: E402
import signals   # noqa: E402
import agent     # noqa: E402

MARA = "mara.lindqvist@nimbus.example"


# --- helpers ------------------------------------------------------------------

def _account_id(name):
    m = data.resolve_account(name, owner=MARA)
    assert m, f"no account resolved for {name!r} in Mara's book"
    return m[0]["ACCOUNT_ID"]


def fired(name):
    """The set of RISK signal names (strong+moderate) that fire for a customer account."""
    r = signals.compute_signals(_account_id(name))
    assert r["assessable"], f"{name} is not assessable (prospect/churned?)"
    return {s["signal"] for s in r["signals"] if s["severity"] in ("strong", "moderate")}


def verdict(name):
    r = signals.compute_signals(_account_id(name))
    return signals.band(r["signals"])["level"]


# --- the golden truth: right calls on accounts we're confident about ----------

def test_onyx_is_the_hero_red():
    # the silently-slipping renewal: four independent signals converge
    assert fired("Onyx Partners") == {"usage_decline", "critical_ticket", "competitor", "stakeholder_gap"}
    assert verdict("Onyx Partners") == "RED"


def test_fjord_construction_is_green():
    # healthy: usage rising, only a harmless P2, warm influencers — nothing should fire
    assert fired("Fjord Construction") == set()
    assert verdict("Fjord Construction") == "GREEN"


def test_timberline_trap_stays_green():
    # the trap: a big €278k deal WITH an SSO P1 ticket looks scary, but usage is flat and it's
    # fully multi-threaded — a naive scorer flags it, ours must not
    assert fired("Timberline Services") == set()
    assert verdict("Timberline Services") == "GREEN"


def test_solace_is_amber_usage_suppressed_by_recovery():
    # Solace cratered then rebounds (…4→17), so the recovery guard suppresses usage_decline;
    # only the cold-stakeholder gap remains -> a watch, not a fire alarm
    assert fired("Solace Logistics") == {"stakeholder_gap"}
    assert verdict("Solace Logistics") == "AMBER"


def test_umbra_holding_is_amber_on_sustained_decline():
    # Umbra Holding ran 29→6 then recovered to 13 and PLATEAUED (…13,13) — 55% below where it
    # started. The recovery guard only suppresses an account still climbing on its last step; a
    # plateau this far down is a real, sustained depression -> one strong signal -> AMBER (a
    # watch, not RED).
    assert fired("Umbra Holding") == {"usage_decline"}
    assert verdict("Umbra Holding") == "AMBER"


def test_competitor_outbound_alone_does_not_make_red():
    # Rune Retail's ONLY competitor mention is "competitor (kestrel) cold-emailed champion" —
    # the rival's OUTBOUND, not the customer evaluating (and a template across ~8 accounts). It
    # must NOT alone make it RED (precision over recall: override on customer intent, not
    # competitor spam).
    assert "competitor" not in fired("Rune Retail")
    assert verdict("Rune Retail") != "RED"


def test_competitor_fires_on_customer_intent_not_outbound():
    # the discriminator, isolated: "champion mentioned Kestrel" (customer intent) fires;
    # "competitor cold-emailed the champion" (outbound) does not
    intent = [{"ACTIVITY_DATE": "2026-05-10", "SUMMARY": "champion mentioned Kestrel in the QBR"}]
    outbound = [{"ACTIVITY_DATE": "2026-05-10", "SUMMARY": "competitor (Kestrel) cold-emailed champion"}]
    assert signals._competitor_mention(intent) is not None
    assert signals._competitor_mention(outbound) is None


# --- the false positives the adversarial review found and we fixed (must stay dead) -------

def test_no_competitor_substring_false_positive():
    # a short competitor token must NOT substring-match inside an unrelated word (the historical
    # bug: a legacy vendor's short name matching inside "usage review"); word-boundary matching
    # must NOT fire competitor
    for name in ("Cedarline Logistics", "Lark Healthcare"):
        assert "competitor" not in fired(name), f"{name}: phantom competitor came back"


def test_vantive_boilerplate_suppressed():
    # Solace's only "Vantive" mention is the templated "shared the Vantive comparison battlecard"
    assert "competitor" not in fired("Solace Logistics")


# --- prospects route to discovery, not fake retention -------------------------

def test_prospect_not_assessable_for_retention():
    r = signals.compute_signals(_account_id("Tide Professional Services"))
    assert r["assessable"] is False


def test_discovery_case_study_match():
    # firmographic match on industry (+region); honest None when no reference exists
    assert docs.match_case_study("Logistics", "DACH")["header"].startswith("## Cascade")
    assert docs.match_case_study("Hospitality", "France")["header"].startswith("## Sable")
    assert docs.match_case_study("Software", "UKI")["header"].startswith("## Vela")  # DB 'Software' <-> doc 'Tech'
    assert docs.match_case_study("Financial Services", "Iberia") is None  # no FS reference on file


def test_discovery_pack_gives_cited_questions_and_meddic():
    # prospect call prep: concrete questions (competitor-specific when named) + the MEDDIC checklist
    kestrel = docs.discovery_pack("Kestrel")
    assert "Discovery questions" in kestrel["questions"]["citation"]
    assert kestrel["questions"]["text"].strip()                 # verbatim questions retrieved
    assert kestrel["qualification"]["checklist"] == docs.MEDDIC  # all 6 fields
    assert "§3" in docs.discovery_pack(None)["questions"]["citation"]   # no competitor -> general §3


def test_expansion_never_flips_a_risk_account_green():
    # an open expansion is pipeline context, not health — it must never turn a risk account GREEN
    for name in ("Umbra Holding", "Solace Logistics", "Copperfield Labs"):
        assert verdict(name) != "GREEN", f"{name} carries a risk signal but banded GREEN"


# --- scope + grounding integrity (cheap guards the adversarial review asked for) ----------

def test_single_ae_scope_enforced():
    # another AE's account must be refused, not briefed with Mara's thresholds
    _, err = agent._resolve("Driftwood Professional")   # Marcus's account
    assert "out_of_scope" in err
    acc, err = agent._resolve("Onyx Partners")
    assert acc and err is None


def test_grounding_footer_freezes_the_load_bearing_numbers():
    # the trust claim is "numbers the model leads with can't drift" — so the commercial figures
    # the system prompt tells it to lead with (ARR, renewal timing) must appear in the CODE-built
    # verbatim footer, not just the risk evidence. (Regression lock for a real gap the adversarial review found.)
    out = agent.assess_account("Onyx Partners B.V.")
    footer = agent._grounding([out])
    assert f'€{out["arr_eur"]:,.0f}' in footer, "ARR spoken but not frozen in footer"
    assert f'{out["renewal_closes_in_days"]} days' in footer, "renewal timing spoken but not frozen"
    # drill-in figures too: the MAU series must survive into a follow-up's footer
    assert "187→168" in agent._grounding([agent.account_details("Onyx Partners B.V.", "usage")])


def test_discovery_footer_freezes_deal_figures():
    # a prospect's spoken deal amount/stage must also be frozen, not left to the LLM's prose
    out = agent.assess_account("Vault Studios Ltd")
    if out.get("open_opportunities"):                 # only assert when there is a deal to freeze
        footer = agent._grounding([out])
        assert "OPPORTUNITY" in footer and "in stage" in footer


def test_list_customers_triages_customers_only():
    # ranking by (verdict, most converging signals, soonest renewal) -> Onyx Partners (RED, 4
    # signals, 18d) leads on signals then renewal, not alphabetical. And the customer tool
    # returns NO prospects, so a retention answer grounds on customers only (the split that
    # keeps the footer on-topic).
    cust = agent.list_customers()
    reds = {c["name"] for c in cust["customers"] if c["verdict"] == "RED"}
    assert "Onyx Partners B.V." in reds                       # the single, unassailable RED
    assert not any("Rune Retail" in n for n in reds)          # Rune Retail's cold-email outbound is not RED
    top = cust["customers"][0]
    assert top["name"] == "Onyx Partners B.V."                # ranked #1
    assert top["risk_signals"] == 4 and top["renewal_in_days"] == 18
    assert cust["counts"]["open_renewals"] == 12             # exact count, not the model eyeballing rows
    assert "prospects" not in cust                           # customers only — no prospect leakage


def test_todays_priorities_merges_renewals_and_stuck_deals():
    # 'top calls today' spans the whole day: RED renewals first, then prospect deals stuck >45d,
    # then AMBER renewals. So Onyx Partners (RED) leads and Meadowlark Hospitality (prospect, 94d
    # stuck) is merged in ABOVE the AMBER renewals — a mixed customer+prospect day, with a
    # transparent rule.
    out = agent.todays_priorities()
    p = out["priorities"]
    assert "rule" in out                                     # ranking is stated, not opaque
    assert p[0]["name"] == "Onyx Partners B.V." and p[0]["tag"] == "RED"
    stuck = [i for i, x in enumerate(p) if x["tag"] == "STUCK"]
    amber = [i for i, x in enumerate(p) if x["tag"] == "AMBER"]
    assert stuck and any("Meadowlark" in x["name"] for x in p)  # a prospect made today's plan
    if amber:
        assert min(stuck) < min(amber)                       # a stuck deal outranks an AMBER renewal


def test_list_prospects_triages_prospects_only():
    # prospects carry open-deal status (so the model can't hallucinate 'only X has data') and rank
    # most-stalled first; the tool returns NO customers.
    prosp = agent.list_prospects()
    assert prosp["counts"]["prospects"] >= 1
    assert prosp["counts"]["prospects_with_deals"] >= 5
    assert prosp["prospects"][0]["top_deal"] is not None     # most-stalled prospect leads, has a deal
    assert "customers" not in prosp                          # prospects only — no customer leakage


def test_offline_render_is_deterministic_and_llm_free():
    # the demo safety net: a full brief with NO model call, leading with the code verdict
    brief = agent._offline_render(agent.assess_account("Onyx Partners B.V."))
    assert brief.startswith("━") and "RED" in brief and "grounded in (verbatim)" in brief


def test_every_doc_citation_resolves():
    # guards against silent grounding loss if a playbook header is renamed/renumbered:
    # every registered retrieval key must resolve to non-empty verbatim text
    for key in docs.REGISTRY:
        assert docs.retrieve(key)["text"].strip(), f"retrieval key {key!r} resolved to empty text"
    # discovery citations too: every case study must resolve to non-empty verbatim text
    for cs in docs.CASE_STUDIES:
        assert docs.retrieve_case_study(cs)["text"].strip(), f"case study {cs['header']!r} resolved empty"


# --- allow running directly without pytest ------------------------------------
if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
    print(f"\n{passed}/{len(tests)} passed")
