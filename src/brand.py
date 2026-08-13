"""
Single source of truth for the fictional company this demo is dressed as.

The underlying CRM data, playbook, battlecards and case studies in case-materials/ are
fictionalized in step with this module (see the docs under
case-materials/unstructured_data/) — this module controls what the CONVERSATIONAL
AGENT calls itself in prompts and user-facing text. Change BRAND once here to re-skin the
whole assistant.
"""
from __future__ import annotations

BRAND = {
    "company": "Nimbus",                                  # what the assistant calls "we" / "our"
    "persona_ae": "Mara",                                 # the AE persona this assistant is scoped to
    "domain": "work-operations SaaS (mid-market, EMEA)",  # one-line market context for the system prompt
}
