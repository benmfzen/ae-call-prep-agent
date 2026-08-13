"""
Unstructured-doc retrieval — resolves each fired KPI into the exact, citable
enablement-doc section that tells the AE what to DO about it.

Section-based, deterministic, no embeddings: the mapping (signal → doc section) is
fixed and encoded here. That keeps retrieval
defensible — the agent quotes a real playbook paragraph with a citation, it does not
paraphrase or guess. `search()` is the general, query-driven path for follow-ups.
"""
from __future__ import annotations

from pathlib import Path

DOCS_DIR = Path(__file__).resolve().parent.parent / "case-materials" / "unstructured_data"

# retrieval_key -> (filename, [section header lines], human-readable citation label)
REGISTRY: dict[str, tuple[str, list[str], str]] = {
    "playbook_renewals":       ("01_sales_playbook.md",     ["## 6. Renewals"],                    "Sales Playbook §6 — Renewals"),
    "playbook_multithreading": ("01_sales_playbook.md",     ["## 5. Multi-threading"],             "Sales Playbook §5 — Multi-threading"),
    "playbook_escalation":     ("01_sales_playbook.md",     ["## 9. When to escalate"],            "Sales Playbook §9 — When to escalate"),
    "icp_persona_cfo":         ("02_icp.md",                ["### CFO / Head of Finance"],         "ICP — CFO / Head of Finance persona"),
    "battlecard_kestrel":      ("04_battlecard_kestrel.md", ["## When we win", "## Our counter"],  "Kestrel battlecard — When we win / Our counter"),
    "battlecard_vantive":      ("03_battlecard_vantive.md", ["## When we win", "## Our counter"],  "Vantive battlecard — When we win / Our counter"),
    "pricing_expand":          ("07_pricing_cheatsheet.md", ["### Customer wants to start small and expand"], "Pricing — start small & expand"),
    # fallback for a competitor we detect but have no dedicated battlecard for (e.g. Tandem):
    # the generic "we're also looking at [competitor]" objection track — a real, cited answer
    # instead of pointing the AE at a battlecard that doesn't exist.
    "objection_competitor":    ("06_objection_handling.md", ["also looking at [competitor]"], "Objection handling — 'We're also looking at [competitor]'"),
    # discovery-call support (prospect path): the questions to ask + the qualification framework
    "playbook_discovery":      ("01_sales_playbook.md",     ["### Discovery"],                     "Sales Playbook §3 — Discovery"),
    "playbook_meddic":         ("01_sales_playbook.md",     ["## 2. The MEDDIC framework"],        "Sales Playbook §2 — MEDDIC (qualification)"),
    "discovery_q_kestrel":     ("04_battlecard_kestrel.md", ["## Discovery questions"],            "Kestrel battlecard — Discovery questions"),
    "discovery_q_vantive":     ("03_battlecard_vantive.md", ["## Discovery questions to ask early"], "Vantive battlecard — Discovery questions"),
}

# fired-signal name -> retrieval_key  (competitor is value-dependent, see below)
SIGNAL_KEYS = {
    "usage_decline":       "playbook_renewals",
    "critical_ticket":     "icp_persona_cfo",
    "stakeholder_gap":     "playbook_multithreading",
    "expansion_in_flight": "pricing_expand",
}
COMPETITOR_KEYS = {"Kestrel": "battlecard_kestrel", "Vantive": "battlecard_vantive"}


def _section(text: str, header: str) -> str | None:
    """Return the markdown section beginning at `header`, up to the next header of
    the same or a higher level (i.e. the same-or-fewer number of #)."""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("#") and header.lower() in ln.lower():
            level = len(ln) - len(ln.lstrip("#"))
            out = [ln]
            for nxt in lines[i + 1:]:
                if nxt.lstrip().startswith("#") and (len(nxt) - len(nxt.lstrip("#"))) <= level:
                    break
                out.append(nxt)
            return "\n".join(out).strip()
    return None


def retrieve(key: str) -> dict:
    """Resolve a retrieval_key into its doc section(s) + citation."""
    fname, headers, label = REGISTRY[key]
    text = (DOCS_DIR / fname).read_text()
    parts = [s for s in (_section(text, h) for h in headers) if s]
    return {"key": key, "citation": label, "source_file": fname, "text": "\n\n".join(parts)}


def recommend_for_signal(sig: dict) -> dict | None:
    """Given a fired signal (from signals.compute_signals), return the matching
    playbook-grounded action recommendation, or None if the signal has no mapping."""
    name = sig.get("signal")
    if name == "competitor":
        # a named competitor with a battlecard -> that battlecard; otherwise the generic
        # competitor objection track (so Tandem etc. still gets a real, cited answer)
        key = COMPETITOR_KEYS.get(sig.get("competitor") or sig.get("value"), "objection_competitor")
    else:
        key = SIGNAL_KEYS.get(name)
    if not key:
        return None
    return {"signal": name, "severity": sig.get("severity"),
            "trigger": sig.get("summary"), "action": retrieve(key)}


def recommendations(signals: list[dict]) -> list[dict]:
    """Action recommendations for a list of fired signals (skips unmapped)."""
    return [r for r in (recommend_for_signal(s) for s in signals) if r]


# --- discovery side: firmographics -> case study + ICP fit ----------------------
# For the persona's SECOND call (a hot prospect), a prospect has no usage/tickets — so the
# match is on FIRMOGRAPHICS. Each case study in 08_customer_case_studies.md is tagged by
# industry/region/size; we match a prospect to the closest proof-point, deterministically.

CASE_STUDIES = [
    {"industry": "Retail",       "region": "DACH",    "header": "## Lumen Retail Group",   "won": True},
    {"industry": "Hospitality",  "region": "Iberia",  "header": "## Sable Hospitality",    "won": True},
    {"industry": "Logistics",    "region": "Nordics", "header": "## Cascade Logistics",    "won": True},
    {"industry": "Software",     "region": "UKI",     "header": "## Vela Software",        "won": True},   # 'Tech' in the doc
    {"industry": "Manufacturing","region": "DACH",    "header": "## Lost deal: Petrichor", "won": False},  # cautionary
]

# industries the ICP doc names as the sweet spot (02_icp.md); anything else = extra scrutiny
ICP_SWEETSPOT_INDUSTRIES = {"retail", "hospitality", "logistics", "professional services",
                            "manufacturing", "tech", "software"}


def match_case_study(industry: str | None, region: str | None) -> dict | None:
    """Best case study for a prospect's firmographics: same industry first (won studies
    ahead of lost), then same region. Returns None if NO industry matches — so the agent
    can honestly say 'no close reference on file' instead of forcing a bad analogy."""
    matches = [c for c in CASE_STUDIES if c["industry"].lower() == (industry or "").lower()]
    if not matches:
        return None
    matches.sort(key=lambda c: (not c["won"], c["region"].lower() != (region or "").lower()))
    return matches[0]


def retrieve_case_study(match: dict) -> dict:
    """Verbatim case-study section + citation (same retrieved-and-frozen pattern as retrieve)."""
    text = (DOCS_DIR / "08_customer_case_studies.md").read_text()
    label = match["header"].lstrip("# ").split("(")[0].replace("Lost deal:", "").strip()
    return {"citation": f"Case study — {label}" + ("" if match["won"] else " (LOST deal — cautionary)"),
            "won": match["won"], "text": _section(text, match["header"]) or ""}


# MEDDIC field names (Sales Playbook §2) — the qualification checklist a discovery call should close
MEDDIC = ["Metrics", "Economic Buyer", "Decision Criteria", "Decision Process", "Identify Pain", "Champion"]
DISCOVERY_Q_KEYS = {"Kestrel": "discovery_q_kestrel", "Vantive": "discovery_q_vantive"}


def discovery_pack(competitor: str | None = None) -> dict:
    """Discovery-call support for a prospect: the QUESTIONS to ask + the MEDDIC qualification
    checklist — both cited to a doc section, never fabricated. Honest nuance: a named competitor's
    battlecard has *verbatim* discovery questions; the general path (§3) is discovery GOALS the caller
    turns into questions (still cited to §3, not verbatim). The checklist is MEDDIC (§2) — a list to
    VERIFY on the call, since the snapshot doesn't say which fields are already covered."""
    qkey = DISCOVERY_Q_KEYS.get(competitor, "playbook_discovery")
    questions = retrieve(qkey)                       # {citation, text} — verbatim, cited
    return {"questions": {"citation": questions["citation"], "text": questions["text"]},
            "qualification": {"citation": "Sales Playbook §2 — MEDDIC", "checklist": MEDDIC}}


def icp_fit(acc: dict) -> dict:
    """Quick ICP-fit read of a prospect against the sweet spot (02_icp.md): size band,
    industry vertical, region — computed, with the ICP doc cited."""
    emp = acc.get("EMPLOYEE_COUNT") or 0
    if 250 <= emp <= 2500:
        size = f"{emp} employees — in the ICP sweet spot (250–2,500)"
    elif 120 <= emp <= 6000:
        size = f"{emp} employees — at the ICP edge (120–6,000)"
    else:
        size = f"{emp} employees — outside the ICP band"
    ind = acc.get("INDUSTRY") or "?"
    ind_note = (f"{ind} — a sweet-spot vertical" if ind.lower() in ICP_SWEETSPOT_INDUSTRIES
                else f"{ind} — outside the core verticals (extra scrutiny; regulated sectors especially)")
    return {"notes": [size, ind_note, f"region {acc.get('REGION')}"], "citation": "ICP — Sweet spot"}


# --- general query-driven retrieval (for the agent's follow-ups) ----------------

def _all_sections() -> list[dict]:
    out = []
    for fname in sorted(p.name for p in DOCS_DIR.glob("*.md")):
        lines = (DOCS_DIR / fname).read_text().splitlines()
        cur = None
        for ln in lines:
            if ln.lstrip().startswith("#"):
                if cur:
                    out.append(cur)
                cur = {"file": fname, "header": ln.strip("# ").strip(), "body": [ln]}
            elif cur:
                cur["body"].append(ln)
        if cur:
            out.append(cur)
    return [{"file": s["file"], "header": s["header"], "text": "\n".join(s["body"]).strip()} for s in out]


def search(query: str, limit: int = 3) -> list[dict]:
    """Simple keyword/section search over all docs — the on-demand path for follow-up
    questions ("what does the playbook say about a stalled qualification?")."""
    terms = [t for t in query.lower().split() if len(t) > 2]
    scored = []
    for s in _all_sections():
        hay = (s["header"] + " " + s["text"]).lower()
        score = sum(hay.count(t) for t in terms)
        if score:
            scored.append((score, s))
    scored.sort(key=lambda x: -x[0])
    return [s for _, s in scored[:limit]]
