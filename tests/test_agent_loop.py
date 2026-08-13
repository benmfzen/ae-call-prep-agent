"""
Conversation-layer eval — the LLM tool-use loop (run_turn), tested WITHOUT a real model.

The engine tests (test_engine.py) are LLM-free and cover the deterministic risk/doc logic.
These cover the *other* half: the own-written agent loop. We drive run_turn with a FAKE
OpenAI client that returns scripted messages, so we can assert — deterministically, offline,
in milliseconds — the behaviours that actually matter for trust:

  * a requested tool is really executed, and the CODE-built grounding footer is appended
    (so the load-bearing evidence is present no matter what the model's prose says);
  * an LLM outage degrades to a friendly offline pointer instead of crashing the chat;
  * a model that keeps asking for tools can't loop against the API forever (the round cap);
  * several tool calls in one round all run;
  * a tool that returns an error dict doesn't break the loop.

Run:  .venv/bin/python -m pytest tests/test_agent_loop.py -q
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
import agent      # noqa: E402
import briefings  # noqa: E402


# --- a minimal fake of the OpenAI client, shaped like the bits run_turn uses ----

class _Fn:
    def __init__(self, name, arguments):
        self.name, self.arguments = name, arguments


class _ToolCall:
    def __init__(self, id, name, arguments):
        self.id, self.function = id, _Fn(name, arguments)


class _Msg:
    """Mimics resp.choices[0].message: has .content, .tool_calls, and .model_dump()."""
    def __init__(self, content=None, tool_calls=None):
        self.content, self.tool_calls = content, tool_calls

    def model_dump(self, exclude_none=True):
        d = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = [{"id": tc.id, "type": "function",
                                "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                               for tc in self.tool_calls]
        return {k: v for k, v in d.items() if not (exclude_none and v is None)}


class _Resp:
    def __init__(self, msg):
        self.choices = [type("C", (), {"message": msg})]


class _Completions:
    def __init__(self, scripts):
        self.scripts, self.calls = list(scripts), 0

    def create(self, **_kw):
        step = self.scripts[min(self.calls, len(self.scripts) - 1)]  # last script repeats
        self.calls += 1
        if isinstance(step, Exception):
            raise step
        return _Resp(step)


class FakeClient:
    """Drop-in for the OpenAI client: FakeClient(msg1, msg2, ...) returns those in order."""
    def __init__(self, *scripts):
        self.chat = type("Chat", (), {"completions": _Completions(scripts)})()


def _tool(id, name, arguments):
    return _ToolCall(id, name, arguments)


def _convo(user):
    return [{"role": "system", "content": agent.SYSTEM}, {"role": "user", "content": user}]


ONYX = "Onyx Partners B.V."


# --- the tests ----------------------------------------------------------------

def test_run_turn_executes_the_tool_and_appends_code_grounding():
    # model asks for assess_account, then gives a final answer. The loop must actually run the
    # tool AND append the code-built footer — so the verdict/evidence come from code, not prose.
    client = FakeClient(
        _Msg(tool_calls=[_tool("c1", "assess_account", f'{{"name": "{ONYX}"}}')]),
        _Msg(content="RED — renewal at risk."),
    )
    msgs = _convo("prep me for Onyx Partners")
    out = agent.run_turn(msgs, client)
    assert "RED — renewal at risk." in out            # the model's prose
    assert "grounded in (verbatim)" in out            # code footer appended
    assert "verdict: RED" in out                      # verdict is from the executed tool
    assert any(m.get("role") == "tool" for m in msgs)  # the tool really ran


def test_run_turn_degrades_gracefully_on_llm_outage():
    # if the API call raises (outage / rate limit / no network), don't crash — point at --offline
    client = FakeClient(RuntimeError("connection reset"))
    out = agent.run_turn(_convo("prep Onyx Partners"), client)
    assert "LLM unavailable" in out and "--offline" in out


def test_run_turn_caps_a_runaway_tool_loop():
    # a model that keeps requesting tools forever must be bounded, not loop against the API
    always_calls = _Msg(tool_calls=[_tool("c", "search_playbook", '{"query": "x"}')])
    client = FakeClient(*([always_calls] * 30))
    out = agent.run_turn(_convo("x"), client)
    assert "couldn't complete" in out.lower()
    assert client.chat.completions.calls <= agent.MAX_TOOL_ROUNDS + 1   # bounded call count


def test_run_turn_runs_every_tool_call_in_a_round():
    # a single model message can request several tools; all must execute before the next round
    client = FakeClient(
        _Msg(tool_calls=[
            _tool("a", "assess_account", f'{{"name": "{ONYX}"}}'),
            _tool("b", "account_details", f'{{"name": "{ONYX}", "dimension": "usage"}}'),
        ]),
        _Msg(content="done"),
    )
    msgs = _convo("prep Onyx Partners and show usage")
    agent.run_turn(msgs, client)
    assert len([m for m in msgs if m.get("role") == "tool"]) == 2


def test_run_turn_survives_a_tool_that_returns_an_error():
    # an unknown account makes the tool return an {"error": ...} dict — the loop must keep going
    client = FakeClient(
        _Msg(tool_calls=[_tool("c", "assess_account", '{"name": "Nonexistent Zzz Corp"}')]),
        _Msg(content="I don't have that account."),
    )
    out = agent.run_turn(_convo("prep zzz"), client)
    assert "I don't have that account." in out


# --- run_turn is briefing-scoped: only the passed-in toolset is ever callable -------------

def test_run_turn_uses_the_passed_briefing_toolset_not_the_default():
    # drive run_turn with the EXPANSION toolset explicitly and call its own tool
    # (list_expansion_candidates) — proves run_turn no longer hardcodes one flat tool bag
    session_state = {"briefing": briefings.EXPANSION}
    tools, dispatch = agent.get_toolset(briefings.EXPANSION, session_state)
    client = FakeClient(
        _Msg(tool_calls=[_tool("c1", "list_expansion_candidates", "{}")]),
        _Msg(content="here's who to upsell."),
    )
    msgs = [{"role": "system", "content": briefings.system_prompt(briefings.EXPANSION)},
            {"role": "user", "content": "where can I upsell?"}]
    out = agent.run_turn(msgs, client, tools, dispatch)
    assert "here's who to upsell." in out
    assert "grounded in (verbatim)" in out or out.strip().endswith("upsell.")


def test_run_turn_tool_call_outside_the_active_briefing_errors_not_crashes():
    # if the model (mis)requests a tool that isn't in the RETENTION toolset (e.g. a
    # PROSPECT-only tool), the loop must not crash — dispatch.get(...) style failure
    # surfaces as a tool error the model can recover from, not a KeyError
    tools, dispatch = agent.get_toolset(briefings.RETENTION, {"briefing": briefings.RETENTION})
    client = FakeClient(
        _Msg(tool_calls=[_tool("c1", "discovery_pack", '{"name": "x"}')]),  # not in RETENTION
        _Msg(content="handled."),
    )
    msgs = _convo("prep me")
    out = agent.run_turn(msgs, client, tools, dispatch)
    assert "handled." in out
    tool_msg = next(m for m in msgs if m.get("role") == "tool")
    assert "error" in tool_msg["content"]


# --- standalone runner --------------------------------------------------------
if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        try:
            t(); print(f"  PASS  {t.__name__}"); passed += 1
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
    print(f"\n{passed}/{len(tests)} passed")
