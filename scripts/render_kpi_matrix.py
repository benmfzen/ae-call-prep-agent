#!/usr/bin/env python3
"""Render every retention KPI for Mara's whole book into a colour-coded HTML matrix.
Colour is driven purely by signals.compute_signals (the engine); this only paints it.
Timing is split three ways — Close (any deal) / Renewal / Expansion — so a slipping
renewal is unambiguous (the type-blind single "Close" hid whether it was a renewal)."""
import sys, sqlite3, html, pathlib
from datetime import date

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import data, signals  # noqa: E402

AE = "mara.lindqvist@nimbus.example"
OUT = ROOT / "reports" / "mara-kpi-matrix.html"
OUT.parent.mkdir(exist_ok=True)

# risk-signal columns only; the positive "expansion in flight" signal is carried by the
# green Expansion timing column instead (avoids two columns both headed "Expansion").
KPIS = [("usage_decline", "MAU trend"), ("critical_ticket", "Ticket"), ("competitor", "Competitor"),
        ("stakeholder_gap", "Stakeholder")]

conn = sqlite3.connect(f"file:{data.DB_PATH}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
accts = conn.execute(
    "SELECT ACCOUNT_ID, COMPANY_NAME, SEGMENT, STATUS, ARR_EUR FROM ACCOUNTS WHERE OWNER_AE=?", (AE,)
).fetchall()


def days_to(opps, typ=None):
    """Days until the soonest OPEN opp closes (optionally of a given TYPE). None if none open."""
    pool = [o for o in opps if not o["WON_LOST_REASON"] and o["CLOSE_DATE"]
            and (typ is None or o["TYPE"] == typ)]
    if not pool:
        return None
    return (date.fromisoformat(min(o["CLOSE_DATE"] for o in pool)[:10]) - data.ASOF).days


rows = []
for a in accts:
    res = signals.compute_signals(a["ACCOUNT_ID"])
    fired = {s["signal"]: s for s in res["signals"]} if res["assessable"] else {}
    opps = data.get_opportunities(a["ACCOUNT_ID"])
    n_risk = sum(1 for s in fired.values() if s["severity"] in ("strong", "moderate"))
    rows.append({"a": a, "ok": res["assessable"], "fired": fired, "n_risk": n_risk,
                 "renewal": days_to(opps, "Renewal"), "expansion": days_to(opps, "Expansion")})

order = {"customer": 0, "prospect": 1, "churned": 2}
rows.sort(key=lambda r: (order.get(r["a"]["STATUS"], 9), -r["n_risk"], r["a"]["COMPANY_NAME"]))

n_cust = sum(r["a"]["STATUS"] == "customer" for r in rows)
n_conv = sum(r["n_risk"] >= 2 for r in rows)
n_watch = sum(r["n_risk"] == 1 for r in rows)
n_slip = sum(r["renewal"] is not None and r["n_risk"] >= 1 for r in rows)


def cell(r, key):
    if not r["ok"]:
        return '<td class="na">n/a</td>'
    s = r["fired"].get(key)
    if not s:
        return '<td class="ok">·</td>'
    val = html.escape(str(s.get("value", "✓")))
    tip = html.escape(s["summary"])
    return f'<td class="{s["severity"]}" title="{tip}">{val}</td>'


def dcell(d, cls="num"):
    return f'<td class="{cls}">{d}d</td>' if d is not None else f'<td class="{cls} none">—</td>'


def arr(a):
    return f'€{a["ARR_EUR"]/1000:.0f}k' if a["ARR_EUR"] else "—"


trs = []
for r in rows:
    a = r["a"]
    badge = f'<span class="conv c{min(r["n_risk"],3)}">{r["n_risk"]}</span>' if r["ok"] else '<span class="conv na2">–</span>'
    # a renewal that is approaching AND the account has risk = the "slipping renewal" — highlight it
    ren_cls = "num ren"
    if r["renewal"] is not None and r["n_risk"] >= 2:
        ren_cls = "num ren slip"
    elif r["renewal"] is not None and r["n_risk"] == 1:
        ren_cls = "num ren watch"
    # Expansion is pipeline context, not a health signal: paint it green ONLY when the account
    # has no risk. On a RED/AMBER account (e.g. Gale, -55% MAU + open expansion) it's shown
    # neutral, so a green cell can never read as "this account is fine".
    exp_sig = r["fired"].get("expansion_in_flight")
    if r["expansion"] is not None:
        cls = "num exp" if r["n_risk"] == 0 else "num"
        exp_cell = f'<td class="{cls}" title="{html.escape(exp_sig["summary"]) if exp_sig else ""}">{r["expansion"]}d</td>'
    else:
        exp_cell = '<td class="num none">—</td>'
    tds = "".join(cell(r, k) for k, _ in KPIS)
    trs.append(
        f'<tr><td class="name">{html.escape(a["COMPANY_NAME"])}</td>'
        f'<td>{a["SEGMENT"]}</td><td class="st {a["STATUS"]}">{a["STATUS"]}</td>'
        f'<td class="num">{arr(a)}</td>'
        f'{dcell(r["renewal"], ren_cls)}{exp_cell}'
        f'{tds}<td class="num">{badge}</td></tr>'
    )

kpi_headers = "".join(f"<th>{lbl}</th>" for _, lbl in KPIS)
html_doc = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Mara — Retention KPI Matrix</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 14px/1.4 -apple-system, Segoe UI, Roboto, sans-serif; margin: 32px; background:#0f1115; color:#e6e6e6; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .sub {{ color:#9aa0aa; margin:0 0 18px; }}
  .stats {{ display:flex; gap:24px; margin:0 0 18px; }}
  .stat b {{ font-size:22px; display:block; }}
  table {{ border-collapse: collapse; width:100%; font-variant-numeric: tabular-nums; }}
  th, td {{ padding:6px 9px; text-align:center; border:1px solid #23262e; white-space:nowrap; }}
  th {{ background:#1a1d24; position:sticky; top:0; font-weight:600; }}
  td.name {{ text-align:left; font-weight:600; }}
  td.num {{ text-align:right; }}
  td.none {{ color:#3d434f; }}
  td.st {{ font-size:11px; text-transform:uppercase; letter-spacing:.4px; }}
  .customer {{ color:#8fd6a0; }} .prospect {{ color:#8fb3ff; }} .churned {{ color:#8a8f99; }}
  /* timing: renewal is the hero dimension */
  th:nth-child(5), td.ren {{ background:#141821; }}
  td.ren {{ color:#e6c07a; font-weight:600; }}
  td.ren.slip {{ background:#3a1b18; color:#ff9b8a; font-weight:800; }}   /* approaching renewal + converging risk = slipping */
  td.ren.watch {{ background:#2a2413; color:#e6c07a; }}
  td.exp {{ background:#122b1f; color:#6fce9b; font-weight:600; }}   /* open expansion = positive */
  /* signal colours */
  td.strong  {{ background:#c0392b; color:#fff; font-weight:700; }}
  td.moderate{{ background:#d4a017; color:#111; font-weight:700; }}
  td.positive{{ background:#1f7a4d; color:#fff; }}
  td.ok      {{ background:#161922; color:#3d434f; }}
  td.na      {{ background:#000; color:#4a4f5a; font-size:11px; }}
  .conv {{ display:inline-block; min-width:20px; padding:1px 6px; border-radius:10px; font-weight:700; }}
  .conv.c0 {{ background:#161922; color:#3d434f; }} .conv.c1 {{ background:#d4a017; color:#111; }}
  .conv.c2, .conv.c3 {{ background:#c0392b; color:#fff; }} .conv.na2 {{ background:#000; color:#4a4f5a; }}
  .legend {{ margin-top:16px; color:#9aa0aa; font-size:12px; }}
  .legend span {{ display:inline-block; padding:2px 8px; border-radius:4px; margin-right:6px; }}
</style></head><body>
<h1>Retention KPI Matrix — Mara Lindqvist's book</h1>
<p class="sub">All {len(rows)} accounts · engine: <code>signals.compute_signals</code> · as-of {data.ASOF} · colour = per-KPI signal (aggregate verdict intentionally not shown). Renewal / Expansion = days until that open deal type closes.</p>
<div class="stats">
  <div class="stat"><b>{n_cust}</b>customers (scored)</div>
  <div class="stat"><b style="color:#e0685a">{n_conv}</b>≥2 risk signals (converging)</div>
  <div class="stat"><b style="color:#e6b800">{n_watch}</b>1 risk signal (watch)</div>
  <div class="stat"><b style="color:#ff9b8a">{n_slip}</b>renewals slipping (approaching + risk)</div>
</div>
<table>
<thead><tr><th style="text-align:left">Account</th><th>Seg</th><th>Status</th><th>ARR</th><th>Renewal</th><th>Expansion</th>{kpi_headers}<th>#risk</th></tr></thead>
<tbody>
{chr(10).join(trs)}
</tbody></table>
<div class="legend">
  <span class="strong" style="background:#c0392b;color:#fff">strong risk</span>
  <span class="moderate" style="background:#d4a017;color:#111">moderate risk</span>
  <span style="background:#122b1f;color:#6fce9b">Expansion open (positive)</span>
  <span style="background:#161922;color:#3d434f">· no signal</span>
  <span style="background:#3a1b18;color:#ff9b8a">renewal slipping (approaching + risk)</span>
  <span style="background:#000;color:#6a6f7a">n/a (prospect / churned)</span>
  <br>Renewal / Expansion = days until that open deal closes ("—" = none open). Hover a coloured KPI cell for the evidence. #risk = strong+moderate signals (convergence, not an official verdict).
</div>
</body></html>"""

OUT.write_text(html_doc)
print(f"→ {OUT}")
print(f"  {len(rows)} accounts · {n_cust} customers · {n_conv} converging · {n_watch} watch · {n_slip} renewals slipping")
