# AE Pre-Call Prep Agent

[![CI](https://github.com/benmfzen/ae-call-prep-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/benmfzen/ae-call-prep-agent/actions/workflows/ci.yml)

A pre-call prep assistant for a mid-market Account Executive, built as an explainable decision engine with an LLM on top. The RED/AMBER/GREEN renewal-risk verdict is computed in deterministic, tested Python. The model only picks which tool to call and turns the result into a sentence, and every number or quote it states is read from code and printed verbatim underneath, so if the prose ever drifts from the data you see the drift next to its source instead of trusting it.

> A self-directed portfolio project. The company ("Nimbus"), domain, competitors and data are fictional, and all data is synthetic.

```
you › Onyx Partners renewal — what should I know?

RED — renewal at risk. Active users are down 50% (187→93) and a recent P1 workflow error
(14 records processed with incorrect values) hurt trust. The champion mentioned Kestrel, and
there's no warm senior stakeholder — only IT. Renewal closes in 18 days.
  • re-anchor the renewal on value/adoption (Playbook §6)
  • face the trust hit with the CFO (ICP CFO persona)
  • counter Kestrel on workflow depth (Kestrel battlecard) — the incident is your wedge
  • multi-thread to a senior buyer now (Playbook §5)
— grounded in (verbatim) —
  ▸ verdict: RED  (deterministic · 4 risk signals)
  • PRODUCT.USAGE.MONTHLY_ACTIVE_USERS: 187→168→149→130→112→93
  • SUPPORT.TICKETS: P1 — Workflow error: 14 records miscalculated in March cycle …
  ↳ Kestrel battlecard — When we win / Our counter: "Customer has real workflow complexity …"
```

The AE asks about an account, the agent hands back a short grounded brief and answers the follow-ups, drawing on structured CRM and product data alongside unstructured sales-enablement docs.

## What this demonstrates

- **The verdict is deterministic and tested on its own.** `signals.band()` computes RED/AMBER/GREEN in Python, and `tests/test_engine.py` pins it down with no model in the loop.
- **The false positives are named and guarded.** A systemic outage shared across accounts, a competitor name that matches inside an unrelated word, an account that is already recovering: each one has a specific guard and a test that fails if the guard ever regresses.
- **The eval needs no LLM and runs in under a second.** All 47 tests run with no API key and no network call (`python -m pytest tests/ -q`).

## What I'd own in production

- **Threshold governance.** The signal thresholds in `signals.py` are anchored to benchmarks, not fitted to labels. In production they need an owner, a review cadence and a documented change process so they do not quietly go stale.
- **Backtesting against real outcomes.** Validate the signals against actual renewal win/loss history and report precision and recall, instead of the "does it separate the two seed accounts" bar this eval currently holds.
- **Escalation on RED.** A RED verdict should open a CSM or manager escalation path (Playbook §9 already lists the triggers), not just surface in a chat reply.
- **Room to grow the briefings.** Today the router scopes tools to one of three briefings per turn. The same pattern extends to more account states and more data sources, say support sentiment or contract terms, behind one scoped-tool surface.

## Quickstart

```bash
python3 -m venv .venv && . .venv/bin/activate      # or: uv venv .venv
pip install -r requirements.txt                    # openai + pytest
export OPENAI_API_KEY=sk-...                        # any OpenAI key; used only by the chat loop
python src/agent.py                                 # start the terminal chat
```

The data runs fully local. The agent queries a SQLite snapshot built from the CSVs in `data/` (`data/crm.db`), so there is no live warehouse or network dependency for the data. Only the chat loop itself calls the OpenAI API; the `--offline <account>` brief needs no model at all.

## Try it (demo script)

```
what should I do today?                        # → cross-book triage: the primary entry point
top 3 retention risks right now                # → ranked customers, Onyx Partners #1 (4 converging signals)
which prospect needs my attention?              # → pipeline triage: the most-stalled deal, cited
Onyx Partners renewal — what should I know?     # → the RED brief (call #1); then drill in:
why is usage dropping, and who are the contacts?    # → drill-in
how should I handle the Kestrel threat?         # → the Kestrel battlecard, cited
prep me for a discovery call with a new prospect    # → discovery briefing: ICP fit + case study + discovery Qs + MEDDIC
is a healthy account at risk?                   # → GREEN, checked & clear: no risk signal fired (no cry-wolf)
```

## Architecture

```
data.py       one read-only SQL surface over the local snapshot (every fact routes through here)
   ↓
signals.py    deterministic retention KPIs + band() → RED/AMBER/GREEN verdict   ← the intelligence
docs.py       each fired signal → the exact playbook section (verbatim + cited); case-study match
   ↓
briefings.py  three scoped BRIEFINGS — Retention / Expansion / Prospect — each a bundle of
              {system-prompt preamble, subset of tools}; a router picks exactly ONE per turn
   ↓
agent.py      own OpenAI tool-use loop, only the active briefing's tools exposed, plus
              a CODE-built verbatim grounding footer
```

**Why it is shaped this way** (the trust story, and what the code walkthrough shows):

- The LLM is told it has no built-in knowledge. Every account fact has to come from a tool call.
- The risk call ("is this renewal RED?") is computed in `signals.band()`, not by the model.
- The grounding footer under each answer is assembled by code, so the numbers, quotes and the verdict are shown verbatim from the source and cannot drift in the model's prose.
- Signals are de-noised against real false positives. A systemic outage shared by several accounts is suppressed. A short competitor token matching inside "usage" is gone thanks to word-boundary matching. An account that is bouncing back is not flagged.
- The briefing router scopes the tool surface per turn (retention vs. expansion vs. prospect intent) instead of exposing one flat tool bag for every question. That gives the model a smaller, better-matched set of tools per task, and it can still ask to switch mid-conversation.

## How I know it's good enough

```bash
python -m pytest tests/ -q        # engine + briefing-router + agent-loop checks, LLM-free/fake-client, <1s
```

The eval pins the discrimination bar, and it favours precision over recall with zero false alarms on healthy accounts. Onyx Partners fires 4 signals and lands on RED. Scary-but-healthy accounts (a big deal plus a technical P1) stay GREEN. The known false positives stay dead, prospects route to discovery, and every doc citation resolves. In production, "good" would be a backtest of these signals against real renewal win/loss, reported as precision and recall.

## What's synthetic / what's cut

- **Synthetic data.** `data/crm.db` is a fully synthetic CRM snapshot: 6 tables, roughly 1,500 rows, fictional companies and contacts. No real customers, no real product.
- **Scope is one AE's book (Mara).** Her book carries by far the most open renewals, and the clearest RED account (Onyx Partners, with four converging signals) is hers, so she is where the churn pain is provable. The engine is AE-agnostic; scoping to Mara is a data-driven choice enforced in code.
- **Deliberate cuts.** The stuck-deal signal is left out as a retention signal, because deal stagnation is not customer health (it is used only for prospect prioritisation). A handful of textbook KPIs that cannot be built honestly from this snapshot are left out too (no seat counts, no NPS/CSAT/NRR). And there is no production UI: this is an engine prototype, and the intended surface is a Slack or CRM panel.

## Repo layout

```
src/            agent.py · briefings.py · signals.py · docs.py · data.py · brand.py   (the build)
tests/          test_engine.py · test_briefings.py · test_agent_loop.py               (the eval)
web/            serve.py                                                               (local demo UI)
data/           crm.db + CSVs + MANIFEST                                              (local snapshot)
case-materials/ unstructured_data/ — sales enablement docs (playbook, ICP, battlecards,
                pricing, case studies) the agent retrieves and cites verbatim
scripts/        rebuild-from-CSV · KPI-matrix renderer
```

## Secrets

`OPENAI_API_KEY` lives in `~/.config/nimbus-case/.env` (chmod 600) or in the environment, never in the repo (`.gitignore`).
