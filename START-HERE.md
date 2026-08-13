# START HERE: run it in 2 minutes

> A self-directed portfolio project. The company, domain, competitors, and data are fictional;
> all data is synthetic.

## What it is

A pre-call prep assistant for a mid-market Account Executive. It tells the AE where to look across their book: which renewal is quietly slipping, which prospect deal is stuck, and then lets them drill in.

Every answer is grounded in local CRM data and sales-enablement documents. The RED / AMBER / GREEN risk verdict is computed in deterministic code, not by the LLM.

## 0. What you need

Python 3.10 or newer:

```bash
python3 --version
```

The account data is included as a local SQLite snapshot. No credentials or network connection are needed to query it.

> Installing the Python dependencies normally requires an internet connection.

## 1. Set up once

Run these commands from the repository folder.

### macOS / Linux

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

The commands below use `.venv/bin/python` on macOS/Linux. On Windows, replace it with:

```powershell
.\.venv\Scripts\python.exe
```

## 2. See it work, no API key needed

### Run the tests

This checks the deterministic engine and its regression cases without calling an LLM:

```bash
.venv/bin/python -m pytest tests/ -q
```

Expected result:

```text
47 passed
```

### Generate an offline account brief

```bash
.venv/bin/python src/agent.py --offline "Onyx Partners"
```

This prints the RED brief with its fired signals, source evidence and cited playbook guidance, all without calling a model.

## 3. Talk to it live, OpenAI API key required

Set your API key for the current terminal session.

### macOS / Linux

```bash
export OPENAI_API_KEY="sk-..."
```

### Windows PowerShell

```powershell
$env:OPENAI_API_KEY = "sk-..."
```

Then choose either interface:

```bash
.venv/bin/python src/agent.py
```

Or start the local web demo:

```bash
.venv/bin/python web/serve.py
```

Then open [http://localhost:8000](http://localhost:8000).

## Try these prompts

1. `What should I do today?`
   Cross-book prioritisation, the primary entry point.

2. `Prep me for my Onyx Partners call.`
   The RED renewal brief with four converging signals.

3. `How should I handle the Kestrel threat?`
   A follow-up that retrieves the relevant battlecard.

4. `Which prospect needs my attention?`
   Prospect prioritisation based on deal momentum.

5. `Prep me for a discovery call with a new prospect.`
   Discovery prep with ICP fit, a matched case study and qualification prompts.

## If something breaks

- **`python3: command not found`**
  Install Python 3.10+ from [python.org](https://www.python.org/) or your package manager.

- **The live chat reports a missing or invalid API key**
  The tests and `--offline` brief still work without an API key.

- **You edited a CSV in `data/`**
  On the next start, the app automatically rebuilds the local SQLite snapshot from the CSV files. No network access is required.

## Read more

- [`README.md`](README.md): architecture, walkthrough and demo script.
