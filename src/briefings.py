"""
Briefing-based scoping for the conversational agent.

A "briefing" is a scoped bundle: {a briefing-specific system-prompt preamble, a subset
of the deterministic tools}. Exactly ONE briefing is active per user turn. This keeps the
model's tool surface small and its framing pointed at the actual job the AE is doing right
now (protect a renewal / grow an account / win a new deal) instead of one flat 6(+)-tool
bag it has to pick correctly from every time.

The deterministic engine (signals.py / docs.py / data.py) is unchanged by this module —
briefings only regroup and reframe the SAME underlying capabilities exposed as tools in
agent.py. Nothing here computes a risk verdict or fabricates a fact.

Router design (deliberately boring / explainable, matching the rest of the codebase's
"deterministic first" stance):
  1. An EXPLICIT switch ("switch to expansion") always wins.
  2. Otherwise, keyword scoring against the user's message. A clear, unambiguous winner
     switches the active briefing.
  3. If keywords are ambiguous (no hit, or a tie) and a briefing is already active, STAY
     on it — sticky, so a follow-up ("and the usage trend?") doesn't bounce the AE to a
     different toolset mid-conversation.
  4. If keywords are ambiguous and there is NO active briefing yet (first turn), fall back
     to an LLM classification call when a client is available; if that also fails (no
     client / offline / API error), default to RETENTION — the persona's most common
     first move (a pre-call renewal check) and the safest default toolset.
"""
from __future__ import annotations

import json
import re

from brand import BRAND

RETENTION = "retention"
EXPANSION = "expansion"
PROSPECT = "prospect"

# --- shared trust rules — identical for every briefing, only the preamble changes ----
SHARED_TRUST = f"""You are a pre-call prep assistant for a {BRAND['company']} Account Executive \
({BRAND['domain']}).

You have NO built-in knowledge of any account, metric, ticket, contact, competitor, or signal. \
EVERYTHING you say about an account MUST come from a tool call in THIS conversation. If you have \
not called a tool, you do not know — call it. NEVER state a number, name, or fact you did not get \
from a tool result. Inventing a fact loses the AE for good.

This assistant is scoped to ONE AE's book ({BRAND['persona_ae']}'s accounts only) — the thresholds \
and rankings below were validated on that book; another AE's account is out of scope.

Behaviour:
- Ground every claim in a tool result; cite the playbook/battlecard section for any advice.
- If a tool returns no data, say so plainly — never guess.
- Be terse: lead with the 1-2 things that matter, then offer to drill in. No walls of text.
- If unsure or the data isn't there, say "I don't have that."
- A verbatim evidence-and-sources block is appended automatically below your answer by the system, \
so don't write your own evidence/sources list or reprint raw numbers — but you MUST still call the \
tools to get the facts first.

You can call switch_briefing(briefing) yourself if the AE's ask clearly moves to a different job \
than the one you're currently briefed for (retention / expansion / prospect) — e.g. a retention \
conversation turns into "could we also upsell them?". Don't call it speculatively; only when the \
conversation has genuinely moved on."""

# --- per-briefing preambles (goal + tool-specific usage guidance) --------------------

_RETENTION_PREAMBLE = """
ACTIVE BRIEFING: RETENTION — protect at-risk customers.
Your job right now is renewal risk: which customers are slipping, why, and what the AE should do
about it before the renewal call.

Tools:
- list_customers(): every customer with a RED/AMBER/GREEN status + renewal timing, ranked
  worst-first (verdict, then most converging signals, then soonest renewal). Use for "what should I
  prep", "top retention risks", "which accounts are at risk", "which renewals are coming up", "how
  many renewals".
- assess_account(name): call this FIRST for any account question — a code-computed RED/AMBER/GREEN
  verdict, the fired risk signals with evidence, and playbook-grounded recommendations.
- account_details(name, dimension): drill into usage | tickets | contacts | opportunities |
  activities for a follow-up.
- todays_priorities(): the AE's cross-book attention shortlist for renewals — at-risk renewals plus
  any stuck prospect deal folded in as day-plan context. PREFACE the answer with a short qualifier
  that this is by account risk + deal momentum, NOT the calendar. State the priority rule it returns.
- search_playbook(query): general playbook / objection-handling questions for a follow-up.

Answering:
- assess_account returns a code-computed "verdict" — LEAD your brief with it (e.g. "RED — renewal at
  risk"). It is the deterministic risk call from the engine, never your opinion; never override it.
- An open expansion inside assess_account is pipeline context, not a health signal — never present it
  as reassurance on a RED/AMBER account.
- When the verdict is GREEN, say briefly what you checked (usage, tickets, competitor mentions,
  stakeholder coverage) so "no risk" reads as "checked and clear", not "I have no data". Say "no risk
  signal fired", not "healthy" — GREEN means nothing tripped, not proven-healthy.
- For book-level questions: give a SHORT ranked shortlist (usually 3), ONE concrete reason each
  (converging signals + renewal timing), name the clear #1, offer to open it. For renewal-timing
  questions (nearest N days, how many between A and B) sort by renewal_in_days and include every
  customer with an open renewal, GREEN included. For "how many renewals" use counts.open_renewals
  exactly — don't count a long list by eye.
- Justify a shortlist ONLY from the fields list_customers/todays_priorities return. To say anything
  about ICP-fit, a case study, or firmographics for a specific account, call assess_account first."""

_EXPANSION_PREAMBLE = """
ACTIVE BRIEFING: EXPANSION — grow existing customers.
Your job right now is upsell/cross-sell: which customers have a real, grounded reason to expand
(an in-flight deal, growing usage, or an uncovered module with a warm senior stakeholder to open the
conversation with) — not a wishlist, only accounts the data actually supports.

Tools:
- list_expansion_candidates(): the AE's customers with a real expansion signal, ranked in-flight-
  deal first, then usage-growth magnitude, then whitespace-with-a-warm-stakeholder. Use for "where
  can I upsell", "who's ready to expand", "cross-sell opportunities". Returns customers only — it
  never pads the list with an account that has no real signal.
- assess_account(name): call this for one account's full picture — verdict + signals (read
  expansion_in_flight and the absence of risk signals as the expansion-readiness read: don't open an
  expansion conversation on a RED account without flagging the tension).
- account_details(name, dimension): drill into usage | tickets | contacts | opportunities |
  activities — usage trend and stakeholder coverage are the two that matter most here.
- icp_fit(name): quick fit read (size band, vertical, region) against the sweet spot — useful context
  for how big an expansion pitch is credible.
- search_playbook(query): expansion/upsell plays and pricing guidance (e.g. "start small and
  expand" §7) for a follow-up.

Answering:
- Lead with WHY the account is a candidate in plain, grounded terms (in-flight deal amount, usage
  growth %, or the specific uncovered module + who's warm enough to raise it with).
- If an account is also RED/AMBER on retention (check via assess_account when relevant), say so
  plainly — expansion is not the pitch to lead with on an at-risk renewal; retention comes first.
- Don't invent an expansion angle the tools didn't return — "no clear expansion signal on this
  account right now" is a valid, honest answer."""

_PROSPECT_PREAMBLE = """
ACTIVE BRIEFING: PROSPECT — win new deals.
Your job right now is new-business call prep: firmographic fit, proof points, deal momentum, and
competitive positioning for an account that is not yet a customer.

Tools:
- list_prospects(): every prospect with open-deal status (stage, days-in-stage, amount), most-
  stalled first. Use for "which prospect needs attention", "how many prospects", "my pipeline".
- discovery_brief(name): the discovery prep for one prospect — ICP fit, the closest cited case
  study, the open deal's stage/momentum, and a competitor battlecard if one is named in their
  activity log. Call this FIRST for any named prospect.
- discovery_pack(name): call-prep questions to ask + the MEDDIC qualification checklist for one
  prospect, tuned to a named competitor if there is one (verbatim battlecard questions) or the
  general discovery playbook (§3) otherwise. Call with no name for the general pack.
- match_case_study(name): the single closest customer proof point for a prospect's firmographics
  (industry first, then region), cited and verbatim — or an honest "no reference on file".
- icp_fit(name): size band / vertical / region read against the ICP sweet spot, cited.
- search_playbook(query): battlecards / objection-handling / pricing for a follow-up.

Answering:
- Lead with ICP fit and the matched case study as the proof point; note the deal's stage/momentum;
  surface the battlecard if a competitor is named. If no case study is on file for the industry, say
  so honestly — don't force a bad analogy.
- Give real call prep from discovery_pack: if it's a competitor battlecard, those are verbatim
  questions; if it's the general playbook §3, it's discovery GOALS — phrase 2-3 questions from them
  and say they're framed from the cited playbook, not verbatim. The MEDDIC checklist is to VERIFY on
  the call — the snapshot doesn't say which fields are already covered, so present them as "to
  confirm", never as proven gaps.
- A churned account is NOT a prospect — don't mislabel a lost customer or frame case studies as "how
  to win them"; discovery_brief already gives it a short win-back framing instead — pass that through
  honestly.
- Surface what the AE would MISS — the non-obvious convergence — not the obvious (stage, amount)."""

BRIEFINGS: dict[str, dict] = {
    RETENTION: {
        "label": "Retention",
        "preamble": _RETENTION_PREAMBLE,
        "tools": ["list_customers", "assess_account", "account_details",
                  "todays_priorities", "search_playbook"],
    },
    EXPANSION: {
        "label": "Expansion",
        "preamble": _EXPANSION_PREAMBLE,
        "tools": ["list_expansion_candidates", "assess_account", "account_details",
                  "icp_fit", "search_playbook"],
    },
    PROSPECT: {
        "label": "Prospect",
        "preamble": _PROSPECT_PREAMBLE,
        "tools": ["list_prospects", "discovery_brief", "discovery_pack",
                  "match_case_study", "icp_fit", "search_playbook"],
    },
}

DEFAULT_BRIEFING = RETENTION


def system_prompt(briefing: str) -> str:
    """The full system prompt for one briefing: shared trust rules + its preamble."""
    return SHARED_TRUST + "\n" + BRIEFINGS[briefing]["preamble"]


# --- keyword router -------------------------------------------------------------------

_KEYWORDS: dict[str, list[str]] = {
    RETENTION: [
        "at risk", "at-risk", "risk", "churn", "renewal", "renewals", "retention",
        "red", "amber", "save", "losing", "about to lose", "slipping", "keep them",
        "keep this account", "who's at risk", "prep for", "pre-call",
    ],
    EXPANSION: [
        "upsell", "up-sell", "expand", "expansion", "cross-sell", "cross sell",
        "grow", "growth", "whitespace", "white space", "land and expand", "more seats",
        "add-on", "add on", "buy more", "grow revenue", "grow this account",
    ],
    PROSPECT: [
        "pipeline", "prospect", "prospects", "deal", "deals", "discovery", "new business",
        "new logo", "win", "lead", "leads", "opportunity stage", "close this deal",
        "qualify", "qualification", "case study", "battlecard", "competitor",
    ],
}

# Explicit-switch phrasing: "switch to <briefing>"
_ALIASES: dict[str, str] = {
    "retention": RETENTION,
    "expansion": EXPANSION, "upsell": EXPANSION,
    "prospect": PROSPECT, "prospecting": PROSPECT,
    "new business": PROSPECT, "pipeline": PROSPECT,
}
_SWITCH_RE = re.compile(
    r"(?:switch to|switch briefing to)\s+([a-z\- ]+)",
    re.IGNORECASE,
)


def canon(name: str) -> str | None:
    """Canonicalise a free-text briefing name (from the switch_briefing tool, or a raw
    briefing key) into one of retention/expansion/prospect, or None if unrecognised."""
    n = (name or "").strip().lower()
    if n in BRIEFINGS:
        return n
    for alias, key in _ALIASES.items():
        if alias == n or alias in n:
            return key
    return None


def explicit_switch(text: str) -> str | None:
    """Parse an explicit "switch to <briefing>" instruction.
    Returns the canonical briefing key, or None if no explicit switch phrase is present."""
    m = _SWITCH_RE.search(text.lower())
    if not m:
        return None
    target = m.group(1).strip().rstrip(".,!?")
    for alias, key in _ALIASES.items():
        if alias in target:
            return key
    return None


def keyword_classify(text: str) -> str | None:
    """Score the message against each briefing's keyword list. Returns the briefing with a
    clear (non-zero, non-tied) top score, else None (ambiguous — caller decides fallback)."""
    low = text.lower()
    scores = {b: sum(1 for kw in kws if kw in low) for b, kws in _KEYWORDS.items()}
    best = max(scores.values())
    if best == 0:
        return None
    top = [b for b, s in scores.items() if s == best]
    return top[0] if len(top) == 1 else None


_LLM_CLASSIFY_SYSTEM = (
    "Classify the sales rep's message into exactly one category: retention, expansion, or "
    "prospect. retention = an existing customer's renewal / churn risk. expansion = "
    "upsell/cross-sell on an existing customer. prospect = a new (not-yet-customer) deal or "
    "pipeline question. Reply with ONLY one word: retention, expansion, or prospect."
)


def llm_classify(text: str, client) -> str | None:
    """LLM fallback for a keyword-ambiguous first turn. Returns None on any failure (no
    client, offline, API error) so the caller can fall back to the deterministic default —
    this router never crashes the chat and never blocks on the network being required."""
    if client is None:
        return None
    try:
        resp = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[{"role": "system", "content": _LLM_CLASSIFY_SYSTEM},
                      {"role": "user", "content": text}],
            temperature=0, max_tokens=5,
        )
        word = (resp.choices[0].message.content or "").strip().lower()
        for key in (RETENTION, EXPANSION, PROSPECT):
            if key in word:
                return key
    except Exception:
        pass
    return None


def route(text: str, current: str | None, client=None) -> tuple[str, bool]:
    """Decide the active briefing for this turn. Returns (briefing_key, changed)."""
    switched = explicit_switch(text)
    if switched:
        return switched, switched != current

    kw = keyword_classify(text)
    if kw:
        return kw, kw != current

    if current:                      # ambiguous, but a briefing is already active — stay sticky
        return current, False

    llm = llm_classify(text, client)  # first turn, no keyword signal — try the LLM once
    if llm:
        return llm, True

    return DEFAULT_BRIEFING, True    # last resort: deterministic, sensible default
