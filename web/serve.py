#!/usr/bin/env python3
"""
Minimal local web frontend for the AE pre-call prep agent — a presentable DEMO shell.

This is a demo surface only (the production surface would be a Slack/CRM panel, per the
one-pager). It talks to the exact same `agent.run_turn()` as the terminal REPL — NO logic is
duplicated here. The page just renders the model's answer and the code-built grounding footer
nicely: a colour-coded RED / AMBER / GREEN verdict badge, the frozen evidence, and the cited
sources. State is a single server-side conversation, so multi-turn drill-ins work; "New call"
resets it.

Run:  .venv/bin/python web/serve.py      then open  http://localhost:8000
"""
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import agent  # noqa: E402
from openai import OpenAI  # noqa: E402

PORT = 8000
GROUNDING_MARKER = "— grounded in (verbatim) —"

_client = OpenAI()
_messages = [{"role": "system", "content": agent.SYSTEM}]  # one shared conversation (demo)
_lock = threading.Lock()                                   # serialise turns (LLM call is blocking)


def _split(reply: str):
    """Split the agent's reply into (prose, grounding-footer) and sniff the verdict colour,
    so the page can render them as separate, styled blocks instead of one wall of text."""
    if GROUNDING_MARKER in reply:
        prose, footer = reply.split(GROUNDING_MARKER, 1)
        footer = footer.strip()
    else:
        prose, footer = reply, ""
    m = re.search(r"verdict:\s*(RED|AMBER|GREEN)", footer)
    return prose.strip(), footer, (m.group(1) if m else None)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except ValueError:
            payload = {}

        if self.path == "/reset":
            with _lock:
                del _messages[1:]                          # keep the system prompt, drop the chat
            self._send(200, json.dumps({"ok": True}))
            return

        if self.path == "/chat":
            msg = (payload.get("message") or "").strip()
            if not msg:
                self._send(400, json.dumps({"error": "empty message"}))
                return
            with _lock:
                _messages.append({"role": "user", "content": msg})
                try:
                    reply = agent.run_turn(_messages, _client)
                except Exception as e:                     # never 500 the demo
                    reply = f"(error: {e})"
            prose, footer, verdict = _split(reply)
            self._send(200, json.dumps({"answer": prose, "grounding": footer, "verdict": verdict}))
            return

        self._send(404, json.dumps({"error": "not found"}))

    def log_message(self, *_):                             # keep the console quiet
        pass


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AE Pre-Call Prep</title>
<style>
  :root{
    --bg:#f6f3ef; --panel:#fffdfb; --ink:#2b2724; --muted:#8a827a; --line:#e7e0d8;
    --user:#3a5a78; --red:#c0392b; --amber:#c9821a; --green:#1e8449; --accent:#7a5c3e;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
    font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
  header{padding:18px 22px;border-bottom:1px solid var(--line);background:var(--panel);
    position:sticky;top:0;z-index:5}
  header h1{margin:0;font-size:17px;font-weight:650;letter-spacing:.2px}
  header .sub{color:var(--muted);font-size:12.5px;margin-top:2px}
  .wrap{max-width:820px;margin:0 auto;padding:0 16px}
  #log{padding:22px 0 140px}
  .msg{display:flex;margin:14px 0}
  .msg.user{justify-content:flex-end}
  .bubble{max-width:82%;padding:11px 15px;border-radius:14px;white-space:pre-wrap;word-wrap:break-word}
  .user .bubble{background:var(--user);color:#fff;border-bottom-right-radius:4px}
  .assistant .bubble{background:var(--panel);border:1px solid var(--line);border-bottom-left-radius:4px;
    white-space:normal}
  .assistant .bubble p{margin:0 0 9px} .assistant .bubble p:last-child{margin:0}
  .assistant .bubble ul,.assistant .bubble ol{margin:6px 0 9px;padding-left:20px}
  .assistant .bubble li{margin:4px 0}
  .assistant .bubble strong{font-weight:650}
  .assistant .bubble code{font:12.5px ui-monospace,Menlo,Consolas,monospace;background:#f1ebe3;
    padding:1px 5px;border-radius:4px}
  .grounded{max-width:82%;margin:6px 0 2px;border:1px solid var(--line);border-left:4px solid var(--accent);
    border-radius:10px;background:var(--panel);overflow:hidden}
  .grounded.RED{border-left-color:var(--red)} .grounded.AMBER{border-left-color:var(--amber)}
  .grounded.GREEN{border-left-color:var(--green)}
  .grounded .head{display:flex;align-items:center;gap:10px;padding:9px 13px;border-bottom:1px solid var(--line);
    cursor:pointer;user-select:none}
  .grounded:has(.gbody.collapsed) .head{border-bottom:none}
  .gbody.collapsed{display:none}
  .chev{margin-left:auto;color:var(--muted);font-size:12px}
  .badge{font-weight:700;font-size:12px;letter-spacing:.6px;padding:3px 9px;border-radius:6px;color:#fff}
  .badge.RED{background:var(--red)} .badge.AMBER{background:var(--amber)} .badge.GREEN{background:var(--green)}
  .badge.NONE{background:var(--muted)}
  .grounded .head .label{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.5px}
  .gbody{padding:8px 14px 13px}
  .gtitle{font-size:10.5px;text-transform:uppercase;letter-spacing:.8px;color:var(--muted);
    font-weight:700;margin:13px 0 7px}
  .gtitle:first-child{margin-top:4px}
  .erow{margin:8px 0}
  .etag{display:inline-block;font:11px/1.4 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    color:var(--accent);background:#f1ebe3;padding:1px 7px;border-radius:5px}
  .eval{margin-top:3px;font-size:13px;color:#403b36;line-height:1.5;word-wrap:break-word}
  .srow{margin:10px 0}
  .scite{font-weight:650;font-size:12.5px;color:var(--accent)}
  .squote{font-style:italic;font-size:13px;color:#6b645c;margin-top:2px;line-height:1.5}
  .dots{color:var(--muted);font-style:italic;padding:4px 2px}
  .composer{position:fixed;bottom:0;left:0;right:0;background:linear-gradient(180deg,rgba(246,243,239,0),var(--bg) 32%);
    padding:14px 16px 18px}
  .composer .inner{max-width:820px;margin:0 auto}
  .chips{display:flex;flex-wrap:wrap;gap:7px;margin-bottom:9px}
  .chip{font-size:12.5px;padding:5px 11px;border:1px solid var(--line);background:var(--panel);border-radius:20px;
    cursor:pointer;color:var(--accent)}
  .chip:hover{border-color:var(--accent)}
  .row{display:flex;gap:8px}
  #box{flex:1;padding:12px 14px;border:1px solid var(--line);border-radius:12px;background:var(--panel);
    font:15px inherit;color:var(--ink);resize:none;min-height:46px;max-height:150px}
  #box:focus{outline:none;border-color:var(--accent)}
  button{border:none;border-radius:12px;padding:0 18px;font-weight:600;cursor:pointer;font-size:14px}
  #send{background:var(--accent);color:#fff} #send:disabled{opacity:.5;cursor:default}
  #reset{background:var(--panel);border:1px solid var(--line);color:var(--muted)}
</style>
</head>
<body>
<header><div class="wrap"><h1>AE Pre-Call Prep</h1>
  <div class="sub">internal demo · scoped to Mara's book · the risk verdict is computed in code, not by the model</div>
</div></header>

<div class="wrap"><div id="log"></div></div>

<div class="composer"><div class="inner">
  <div class="chips" id="chips"></div>
  <div class="row">
    <textarea id="box" placeholder="Ask about an account…  e.g. prep me for my Onyx Partners call"></textarea>
    <button id="send">Send</button>
    <button id="reset" title="Start a fresh conversation">New</button>
  </div>
</div></div>

<script>
/* ---------------------------------------------------------------------------
   WHAT THIS SCRIPT DOES (plain-language overview)
   The page is a chat window. When you type a question and hit Send, the browser
   POSTs it to the Python server (/chat), which runs the SAME agent as the terminal
   and answers with three parts: the model's prose ("answer"), the code-built
   evidence block ("grounding"), and the risk colour ("verdict"). This script then:
     1. draws your question as a bubble on the right,
     2. shows a "…" while it waits,
     3. draws the answer as a bubble on the left (formatting bullet lists nicely),
     4. draws the evidence as a coloured, collapsible card underneath.
   No logic/decisions live here — the server does all the thinking; this only paints it.
--------------------------------------------------------------------------- */

// grab the three page elements we interact with (the message log, the input box, the Send button)
const log = document.getElementById('log');
const box = document.getElementById('box');
const send = document.getElementById('send');

// the little suggestion pills above the input — clicking one just fills the box for you
const CHIPS = ["What are my top 3 retention risks right now?",
               "Which prospect needs my attention?"];
const chips = document.getElementById('chips');
CHIPS.forEach(t=>{const c=document.createElement('span');c.className='chip';c.textContent=t;
  c.onclick=()=>{box.value=t;box.focus()};chips.appendChild(c)});

// esc() neutralises any <, >, & in text so it's shown as characters, never run as HTML (XSS safety)
function esc(s){ return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
// inline() escapes first, then turns **x** into bold and `x` into a code chip
function inline(s){ return esc(s).replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>')
                                 .replace(/`([^`]+)`/g,'<code>$1</code>'); }
// renderMd() turns the model's plain text into tidy HTML: blank lines → paragraphs,
// "-"/"*"/"•" lines → bullet lists, "1." lines → numbered lists, **x** → bold. That's why
// the brief reads as clean bullets instead of raw dashes.
function renderMd(text){
  const lines=(text||'').split('\n'); let html='', list=null, para=[];
  const flushP=()=>{ if(para.length){ html+='<p>'+para.join('<br>')+'</p>'; para=[]; } };
  const flushL=()=>{ if(list){ html+='</'+list+'>'; list=null; } };
  for(const raw of lines){
    const line=raw.replace(/\s+$/,'');
    const b=line.match(/^\s*[-*•]\s+(.*)/), n=line.match(/^\s*\d+[.)]\s+(.*)/);
    if(b){ flushP(); if(list!=='ul'){flushL();html+='<ul>';list='ul';} html+='<li>'+inline(b[1])+'</li>'; }
    else if(n){ flushP(); if(list!=='ol'){flushL();html+='<ol>';list='ol';} html+='<li>'+inline(n[1])+'</li>'; }
    else if(line.trim()===''){ flushP(); flushL(); }
    else { flushL(); para.push(inline(line.trim())); }
  }
  flushP(); flushL(); return html || '<p></p>';
}
// add() draws one chat bubble. cls is 'user' (right, plain text) or 'assistant' (left, markdown),
// then it scrolls to the bottom so the newest message is visible. Returns the bubble (so we can
// later remove the "…" placeholder).
function add(cls, text){
  const m=document.createElement('div'); m.className='msg '+cls;
  const b=document.createElement('div'); b.className='bubble';
  if(cls==='assistant') b.innerHTML=renderMd(text); else b.textContent=text;   // user text stays literal
  m.appendChild(b); log.appendChild(m); window.scrollTo(0,document.body.scrollHeight); return m;
}
// splitFirst() cuts "label: value" into [label, value] at the first ": " (used for the evidence rows)
function splitFirst(s){ const i=s.indexOf(': '); return i<0?[null,s]:[s.slice(0,i),s.slice(i+2)]; }

// addGrounding() draws the coloured evidence card under an answer. It reads the server's plain-text
// footer, sorts the lines into Evidence (data, "•") and Sources (citations, "↳"), and lays them out.
// The card starts collapsed (see body.collapsed) so the brief stays short; the header toggles it.
function addGrounding(footer, verdict){
  if(!footer) return;
  const card=document.createElement('div'); card.className='grounded '+(verdict||'');
  const head=document.createElement('div'); head.className='head';
  const badge=document.createElement('span'); badge.className='badge '+(verdict||'NONE');
  badge.textContent = verdict || 'GROUNDED';
  const label=document.createElement('span'); label.className='label'; label.textContent='grounded in (verbatim)';
  const chev=document.createElement('span'); chev.className='chev'; chev.textContent='▸ show evidence';
  head.appendChild(badge); head.appendChild(label); head.appendChild(chev); card.appendChild(head);

  // split the code-built footer into Evidence (• data) and Sources (↳ citations); dedup both
  const ev0=[], sr0=[];
  footer.split('\n').forEach(raw=>{ const l=raw.trim();
    if(l.startsWith('•')) ev0.push(l.slice(1).trim());
    else if(l.startsWith('↳')) sr0.push(l.slice(1).trim()); });
  const evidence=[...new Set(ev0)], sources=[...new Set(sr0)];

  const body=document.createElement('div'); body.className='gbody collapsed';   // primary brief first; evidence on demand
  if(evidence.length){
    const t=document.createElement('div'); t.className='gtitle'; t.textContent='Evidence'; body.appendChild(t);
    evidence.forEach(e=>{ const [tag,val]=splitFirst(e);
      const row=document.createElement('div'); row.className='erow';
      if(tag){ const g=document.createElement('span'); g.className='etag'; g.textContent=tag; row.appendChild(g); }
      const v=document.createElement('div'); v.className='eval'; v.textContent=val; row.appendChild(v);
      body.appendChild(row); });
  }
  if(sources.length){
    const t=document.createElement('div'); t.className='gtitle'; t.textContent='Sources'; body.appendChild(t);
    sources.forEach(s=>{
      // only a genuine  cite: "quote"  splits; a header that merely contains a colon
      // (e.g. "Battlecard: Nimbus vs Vantive") stays whole as the citation
      const m=s.match(/^(.*?): "(.*)"$/);
      const cite=m?m[1]:s, quote=m?m[2]:null;
      const row=document.createElement('div'); row.className='srow';
      const c=document.createElement('div'); c.className='scite'; c.textContent=cite; row.appendChild(c);
      if(quote){ const q=document.createElement('div'); q.className='squote'; q.textContent=quote; row.appendChild(q); }
      body.appendChild(row); });
  }
  card.appendChild(body);
  head.onclick=()=>{ const collapsed=body.classList.toggle('collapsed');
    chev.textContent = collapsed ? '▸ show evidence' : '▾ hide evidence'; };
  const wrap=document.createElement('div'); wrap.className='msg assistant';
  wrap.appendChild(card); log.appendChild(wrap); window.scrollTo(0,document.body.scrollHeight);
}
// ask() is the round-trip: show the question, show "…", send it to the server, then replace the
// "…" with the real answer + evidence card. Send is disabled while waiting so you can't double-fire.
async function ask(text){
  add('user', text);
  const thinking=add('assistant','…'); thinking.querySelector('.bubble').classList.add('dots');
  send.disabled=true;
  try{
    const r=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:text})});   // POST the message to the Python server
    const d=await r.json();                    // d = {answer, grounding, verdict}
    thinking.remove();
    add('assistant', d.answer || d.error || '(no answer)');
    addGrounding(d.grounding, d.verdict);
  }catch(e){ thinking.remove(); add('assistant','(network error: '+e+')'); }
  send.disabled=false; box.focus();
}
// wiring: Send button and Enter both submit; Shift+Enter makes a new line; "New" resets the chat
function submit(){ const t=box.value.trim(); if(!t) return; box.value=''; ask(t); }
send.onclick=submit;
box.addEventListener('keydown',e=>{ if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();submit();} });
document.getElementById('reset').onclick=async()=>{ await fetch('/reset',{method:'POST'}); log.innerHTML=''; box.focus(); };
box.focus();
</script>
</body>
</html>"""


if __name__ == "__main__":
    print(f"AE prep demo UI  →  http://localhost:{PORT}   (Ctrl-C to stop)")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
