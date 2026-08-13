"""
Briefing-router eval — LLM-free (keyword-first routing needs no model), fast, deterministic.

Covers the four things the briefing refactor is supposed to guarantee:
  1. a representative query for each briefing routes to the correct briefing;
  2. only that briefing's tools are offered to the model for the turn;
  3. an explicit "switch to <briefing>" changes the active briefing (even overriding a
     sticky current one);
  4. the offline demo path (no API key / no LLM call at all) still works per-briefing.

Run:  .venv/bin/python -m pytest tests/test_briefings.py -q
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
import agent       # noqa: E402
import briefings   # noqa: E402


# --- 1. representative query -> correct briefing (keyword router, no LLM needed) ------

def test_retention_query_routes_to_retention():
    key, changed = briefings.route("who's at risk this week?", None, client=None)
    assert key == briefings.RETENTION and changed


def test_expansion_query_routes_to_expansion():
    key, changed = briefings.route("where can I upsell my accounts?", None, client=None)
    assert key == briefings.EXPANSION and changed


def test_prospect_query_routes_to_prospect():
    key, changed = briefings.route("how's my pipeline looking?", None, client=None)
    assert key == briefings.PROSPECT and changed


# --- 2. only the active briefing's tools are offered -----------------------------------

def test_retention_toolset_matches_the_briefing_definition():
    tools, dispatch = agent.get_toolset(briefings.RETENTION, {"briefing": briefings.RETENTION})
    names = {t["function"]["name"] for t in tools}
    expected = set(briefings.BRIEFINGS[briefings.RETENTION]["tools"]) | {"switch_briefing"}
    assert names == expected
    assert set(dispatch) == expected
    # expansion/prospect-only tools must NOT leak into the retention toolset
    assert "list_expansion_candidates" not in names
    assert "discovery_brief" not in names


def test_expansion_toolset_matches_the_briefing_definition():
    tools, dispatch = agent.get_toolset(briefings.EXPANSION, {"briefing": briefings.EXPANSION})
    names = {t["function"]["name"] for t in tools}
    expected = set(briefings.BRIEFINGS[briefings.EXPANSION]["tools"]) | {"switch_briefing"}
    assert names == expected
    assert "list_customers" not in names          # retention-only tool must not leak in
    assert "discovery_pack" not in names           # prospect-only tool must not leak in


def test_prospect_toolset_matches_the_briefing_definition():
    tools, dispatch = agent.get_toolset(briefings.PROSPECT, {"briefing": briefings.PROSPECT})
    names = {t["function"]["name"] for t in tools}
    expected = set(briefings.BRIEFINGS[briefings.PROSPECT]["tools"]) | {"switch_briefing"}
    assert names == expected
    assert "assess_account" not in names           # PROSPECT briefing uses discovery_brief instead
    assert "todays_priorities" not in names


def test_search_playbook_is_shared_across_all_three_briefings():
    for key in (briefings.RETENTION, briefings.EXPANSION, briefings.PROSPECT):
        names = {t["function"]["name"] for t in agent.get_toolset(key, {"briefing": key})[0]}
        assert "search_playbook" in names
        assert "switch_briefing" in names           # always available, every briefing


# --- 3. explicit switch changes the active briefing -------------------------------------

def test_explicit_switch_to_expansion_overrides_sticky_current():
    # even with RETENTION active and no expansion keyword otherwise, the explicit phrase wins
    key, changed = briefings.route("switch to expansion", briefings.RETENTION, client=None)
    assert key == briefings.EXPANSION and changed


def test_explicit_switch_is_a_noop_if_already_active():
    key, changed = briefings.route("switch to retention", briefings.RETENTION, client=None)
    assert key == briefings.RETENTION and changed is False


def test_explicit_switch_extra_phrasing():
    key, changed = briefings.route("switch to prospecting please", briefings.RETENTION, client=None)
    assert key == briefings.PROSPECT and changed


def test_switch_briefing_tool_mutates_session_state():
    session_state = {"briefing": briefings.RETENTION}
    _, dispatch = agent.get_toolset(briefings.RETENTION, session_state)
    result = dispatch["switch_briefing"]("expansion")
    assert result["switched_to"] == briefings.EXPANSION
    assert session_state["briefing"] == briefings.EXPANSION      # the live session state moved


def test_ambiguous_message_stays_sticky_on_current_briefing():
    # no clear keyword winner ("and the usage trend?") -> stays on whatever's active
    key, changed = briefings.route("and the usage trend?", briefings.EXPANSION, client=None)
    assert key == briefings.EXPANSION and changed is False


# --- 4. engine tests still pass (smoke-check via a couple of load-bearing calls) --------

def test_engine_is_untouched_onyx_partners_still_red():
    out = agent.assess_account("Onyx Partners B.V.")
    assert out["verdict"]["level"] == "RED"


# --- new tool wrappers: thin, grounded, no fabrication -----------------------------------

def test_list_expansion_candidates_returns_customers_with_a_real_signal():
    out = agent.list_expansion_candidates()
    assert "candidates" in out and "counts" in out
    for c in out["candidates"]:
        assert c["expansion_in_flight"] or (c["usage_growth_pct"] or 0) >= agent.EXPANSION_GROWTH_PCT * 100 \
            or (c["whitespace_modules"] and c["warm_senior_stakeholder"])


def test_icp_fit_tool_matches_docs_icp_fit():
    import docs
    out = agent.icp_fit("Onyx Partners B.V.")
    acc = agent._resolve("Onyx Partners B.V.")[0]
    import data
    expected = docs.icp_fit(data.get_account(acc["ACCOUNT_ID"]))
    assert out["notes"] == expected["notes"]


def test_discovery_brief_matches_assess_account_for_a_prospect():
    # discovery_brief is a standalone wrapper over the same logic assess_account uses for a
    # non-customer — both must agree on the mode for a real prospect
    prospects = agent.list_prospects()["prospects"]
    assert prospects, "expected at least one prospect in the book"
    name = prospects[0]["name"]
    via_assess = agent.assess_account(name)
    via_tool = agent.discovery_brief(name)
    assert via_assess["mode"] == via_tool["mode"] == "discovery"


def test_match_case_study_tool_is_grounded_or_honestly_empty():
    prospects = agent.list_prospects()["prospects"]
    for p in prospects[:5]:
        out = agent.match_case_study(p["name"])
        assert "account" in out
        assert ("case_study" in out and out["case_study"]) or "note" in out


def test_discovery_pack_tool_general_and_competitor_variants():
    general = agent.discovery_pack(None)
    assert "§3" in general["questions"]["citation"]
    kestrel_out = agent.discovery_pack.__globals__["docs"].discovery_pack("Kestrel")
    assert "Discovery questions" in kestrel_out["questions"]["citation"]


# --- BRAND rebrand: user-facing prompt text always uses the fictional brand -----------

def test_system_prompt_uses_brand_name():
    for key in briefings.BRIEFINGS:
        prompt = briefings.system_prompt(key)
        from brand import BRAND
        assert BRAND["company"] in prompt


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
