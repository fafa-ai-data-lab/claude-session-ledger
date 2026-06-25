#!/usr/bin/env python3
"""
Session Ledger — a single-file, zero-dependency browser for your Claude Code sessions.

Scans Claude Code's session transcripts (~/.claude/projects/**/*.jsonl) and bakes a
self-contained HTML page showing, for every session:
  - the session's auto-generated title (Claude Code's `ai-title`; falls back to the first query)
  - its session id (click to copy a ready-to-run `claude --resume` command)
  - the first N and last N real user queries (slash commands / tool output / injected
    context are filtered out)
  - query count, time range, project, and a `bridge` tag for chat-bridge-driven sessions

Four themes ship inside every generated file; switch live from the header (remembered
via localStorage). No data leaves your machine — open the HTML locally.

Usage:
  python3 session_ledger.py                 # scan all projects, default theme, write sessions.html
  python3 session_ledger.py --open          # ...and open it in your browser
  python3 session_ledger.py --serve --open  # serve locally so the in-page ↻ Update button live-rescans
  python3 session_ledger.py --project myapp  # only projects whose dir contains "myapp"
  python3 session_ledger.py --theme terminal # first-open theme: brutalist|terminal|blueprint|ledger
  python3 session_ledger.py --queries 8      # show first/last 8 instead of 5
  python3 session_ledger.py --demo -o examples/demo.html   # build from bundled sample data
  python3 session_ledger.py --projects-dir /path/to/projects   # custom transcripts location

Requires Python 3.8+. No third-party packages.
"""
import argparse
import glob
import json
import os
import re
import sys
import webbrowser
from datetime import datetime

DEFAULT_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
HOME = os.path.expanduser("~")
# How Claude Code encodes a cwd into a project-dir name: leading "/" kept as "-", every
# other "/" and "." turned into "-". We can't perfectly reverse it (literal hyphens are
# ambiguous), so we only strip the current user's home prefix for a friendly label.
HOME_ENC = "-" + HOME.lstrip("/").replace("/", "-")

# User messages whose text starts with one of these are not real queries (slash commands,
# local command output, the paste caveat banner).
NOISE_PREFIXES = (
    "<local-command-caveat>",
    "<local-command-stdout>",
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "Caveat: The messages below",
)

# Injected blocks stripped out of a query (keeping any genuine text around them):
#  - system-reminder / task-notification: pure harness injection -> usually leaves nothing
#  - bridge_context / quoted_message: external chat-bridge metadata -> keeps the real text
#  - local-command / command: slash-command wrappers
STRIP_BLOCK_RE = re.compile(
    r"<system-reminder>.*?</system-reminder>"
    r"|<task-notification>.*?</task-notification>"
    r"|<bridge_context>.*?</bridge_context>"
    r"|<quoted_message[^>]*>.*?</quoted_message>"
    r"|<local-command-[^>]*>.*?</local-command-[^>]*>"
    r"|<command-[^>]*>.*?</command-[^>]*>",
    re.DOTALL,
)


def clean_text(text):
    return STRIP_BLOCK_RE.sub("", text).strip()


def extract_query(d):
    """Pull the real user-typed text out of a `user` record, or None if it isn't a query."""
    if d.get("isMeta") or d.get("isSidechain"):
        return None
    msg = d.get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        # Skip tool_result turns (tool output, not user input).
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        content = "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    if not isinstance(content, str):
        return None
    if any(content.lstrip().startswith(p) for p in NOISE_PREFIXES):
        return None
    text = clean_text(content)
    if not text or text == "[Request interrupted by user]":
        return None
    return text


def parse_session(path, maxq):
    """Parse one .jsonl transcript. Returns a session dict, or None if it has no real query."""
    session_id = os.path.splitext(os.path.basename(path))[0]
    title = None
    queries = []
    first_ts = last_ts = None
    is_bridge = False

    with open(path, "r", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            # Cheap pre-filter: only parse lines that can possibly matter.
            if not ('"type":"user"' in line or '"aiTitle"' in line):
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = d.get("type")
            if t == "ai-title":
                if d.get("aiTitle"):
                    title = d["aiTitle"]          # keep the most recent title
            elif t == "user":
                if "<bridge_context>" in line:
                    is_bridge = True
                ts = d.get("timestamp")
                if ts:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts
                q = extract_query(d)
                if q:
                    queries.append(q)

    if not queries:
        return None
    if not title:
        title = (queries[0].strip().splitlines()[0][:60]) or "(untitled)"

    return {
        "id": session_id,
        "title": title,
        "first": queries[:maxq],
        "last": queries[-maxq:],
        "n_query": len(queries),
        "first_ts": first_ts,
        "last_ts": last_ts,
        "bridge": is_bridge,
    }


def extract_assistant(d):
    """Pull the assistant's spoken text out of an `assistant` record (no tool_use/thinking)."""
    msg = d.get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        content = "\n".join(p for p in parts if p)
    if not isinstance(content, str):
        return None
    text = content.strip()
    return text or None


def parse_conversation(path):
    """Parse one .jsonl into the full ordered turn list: user + assistant, noise filtered.

    Returns [{role: 'user'|'assistant', text, ts}] in transcript order. Used on demand by
    the /api/session/<id> endpoint to power the chat-modal view (not baked into the HTML).
    """
    turns = []
    with open(path, "r", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("isMeta") or d.get("isSidechain"):
                continue
            t = d.get("type")
            ts = d.get("timestamp")
            if t == "user":
                text = extract_query(d)
                role = "user"
            elif t == "assistant":
                text = extract_assistant(d)
                role = "assistant"
            else:
                continue
            if text:
                turns.append({"role": role, "text": text, "ts": ts})
    return turns


def find_session_file(projects_dir, sid):
    """Locate <sid>.jsonl under any project dir. Rejects ids that aren't plain tokens
    (guards against path traversal in the served /api/session/<id> route)."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", sid or ""):
        return None
    hits = glob.glob(os.path.join(projects_dir, "*", sid + ".jsonl"))
    return hits[0] if hits else None


def pretty_project(dirname):
    """Turn an encoded project-dir name into a friendly label (best effort)."""
    name = dirname
    if name.startswith(HOME_ENC + "-"):
        name = name[len(HOME_ENC) + 1:]
    elif name == HOME_ENC:
        return "home"
    name = name.lstrip("-") or "(root)"
    return name.replace("--claude-worktrees-", " ⟶ wt/").replace("-claude-worktrees-", " ⟶ wt/")


def scan(projects_dir, project_kw, maxq):
    """Scan every project dir under projects_dir; optionally filter by substring."""
    sessions = []
    if not os.path.isdir(projects_dir):
        sys.exit(f"error: projects dir not found: {projects_dir}\n"
                 f"hint: run Claude Code at least once, or pass --projects-dir.")
    project_dirs = sorted(d for d in glob.glob(os.path.join(projects_dir, "*")) if os.path.isdir(d))
    for pdir in project_dirs:
        dirname = os.path.basename(pdir)
        if project_kw and project_kw.lower() not in dirname.lower():
            continue
        proj = pretty_project(dirname)
        count = 0
        for path in sorted(glob.glob(os.path.join(pdir, "*.jsonl"))):
            try:
                s = parse_session(path, maxq)
            except Exception as e:
                print(f"  warn: {path} -> {e}", file=sys.stderr)
                continue
            if s:
                s["project"] = proj
                sessions.append(s)
                count += 1
        if count:
            print(f"  {proj}: {count} sessions", file=sys.stderr)
    # Dedupe by session id: one transcript can show up under two project dirs when a
    # workspace is reachable by two paths (e.g. a symlinked alias), which would list the
    # same session twice. Keep the most complete copy (most queries; ties keep the first).
    best = {}
    for s in sessions:
        cur = best.get(s["id"])
        if cur is None or s["n_query"] > cur["n_query"]:
            best[s["id"]] = s
    if len(best) < len(sessions):
        print(f"  (deduped {len(sessions) - len(best)} sessions seen under multiple paths)",
              file=sys.stderr)
    sessions = list(best.values())
    sessions.sort(key=lambda s: s.get("last_ts") or "", reverse=True)
    return sessions


# ────────────────────────────────────────────────────────────────────────────
# Bundled sample data (used by --demo) so the tool can be demoed / screenshotted
# without exposing any real transcripts.
# ────────────────────────────────────────────────────────────────────────────
DEMO_SESSIONS = [
    {"id": "a1b2c3d4-1111-4aaa-9001-000000000001", "project": "my-web-app", "bridge": False,
     "n_query": 14, "first_ts": "2026-06-08T09:12:00Z", "last_ts": "2026-06-08T11:48:00Z",
     "title": "Add dark mode toggle with system preference",
     "first": ["add a dark mode toggle to the settings page",
               "it should default to the OS preference via prefers-color-scheme",
               "persist the choice in localStorage",
               "the toggle isn't updating the chart colors — fix that too",
               "great, now write a test for the persistence logic"],
     "last": ["the test is flaky on CI, can you stabilize it",
              "use a deterministic clock instead of Date.now()",
              "ship it",
              "actually rename the css var --bg to --surface everywhere",
              "perfect, thanks"]},
    {"id": "a1b2c3d4-2222-4aaa-9002-000000000002", "project": "my-web-app", "bridge": False,
     "n_query": 6, "first_ts": "2026-06-07T14:03:00Z", "last_ts": "2026-06-07T14:51:00Z",
     "title": "Debug 500 on /api/checkout",
     "first": ["users report a 500 on checkout, find the root cause",
               "here's the stack trace: NullPointer in CartService.total()",
               "the cart can be empty when a coupon is applied — handle that"],
     "last": ["add a regression test for the empty-cart-with-coupon case",
              "confirm the fix against the staging logs",
              "open a PR titled 'fix: guard empty cart in checkout total'"]},
    {"id": "b1b2c3d4-3333-4bbb-9003-000000000003", "project": "infra-scripts", "bridge": False,
     "n_query": 23, "first_ts": "2026-06-06T08:20:00Z", "last_ts": "2026-06-06T19:05:00Z",
     "title": "Migrate Terraform state to S3 backend",
     "first": ["I want to move our terraform state from local to an S3 backend with locking",
               "use a DynamoDB table for the lock",
               "write the backend config but don't apply yet, show me the plan first",
               "the plan wants to recreate the VPC — that's wrong, investigate",
               "ah it's a provider version bump, pin it to 5.x"],
     "last": ["now run the state migration",
              "verify the lock works by running two applies concurrently",
              "document the bootstrap steps in the README",
              "add a make target `make tf-init`",
              "looks good, merge"]},
    {"id": "b1b2c3d4-4444-4bbb-9004-000000000004", "project": "infra-scripts", "bridge": True,
     "n_query": 3, "first_ts": "2026-06-05T22:10:00Z", "last_ts": "2026-06-05T22:31:00Z",
     "title": "Hotfix: rotate leaked API key",
     "first": ["a key got committed to git history, rotate it and scrub the history",
               "use git-filter-repo, not filter-branch"],
     "last": ["force-push to all branches and notify the team",
              "add a pre-commit hook with gitleaks so this can't happen again"]},
    {"id": "c1b2c3d4-5555-4ccc-9005-000000000005", "project": "data-pipeline", "bridge": False,
     "n_query": 31, "first_ts": "2026-06-04T10:00:00Z", "last_ts": "2026-06-04T17:42:00Z",
     "title": "Build incremental daily aggregation job",
     "first": ["design an incremental daily rollup for the events table",
               "it should be idempotent — re-running a day must not double count",
               "partition by date, dedupe on event_id within each partition",
               "here's the schema: event_id, user_id, ts, type, payload(json)",
               "extract type from payload.kind, not the top-level column (it's stale)"],
     "last": ["backfill the last 90 days",
              "the backfill is slow — can we parallelize per-partition",
              "add data-quality checks: row count vs source, null rate on user_id",
              "wire it into the scheduler at 02:00 UTC",
              "write a runbook for when a partition fails"]},
    {"id": "c1b2c3d4-6666-4ccc-9006-000000000006", "project": "data-pipeline", "bridge": True,
     "n_query": 8, "first_ts": "2026-06-03T13:15:00Z", "last_ts": "2026-06-03T15:50:00Z",
     "title": "Why did yesterday's DAU drop 12%?",
     "first": ["DAU dropped 12% day-over-day, figure out why",
               "it's not a real drop — check for a tracking regression first",
               "the iOS app shipped a new SDK version yesterday, correlate with that"],
     "last": ["confirmed: the SDK dropped a session_start event on cold start",
              "quantify the affected slice so we can annotate the dashboard",
              "draft a short writeup: cause, impact, fix owner",
              "post it to the analytics channel"]},
    {"id": "d1b2c3d4-7777-4ddd-9007-000000000007", "project": "ml-experiments", "bridge": False,
     "n_query": 19, "first_ts": "2026-06-02T11:30:00Z", "last_ts": "2026-06-02T20:14:00Z",
     "title": "A/B test analysis for new ranking model",
     "first": ["analyze the A/B test for ranking-v2 vs control",
               "primary metric is click-through rate, guardrail is latency p95",
               "compute significance — CTR with a two-proportion z-test",
               "treatment CTR +3.1% (p=0.004), latency p95 +8ms — acceptable?",
               "yes 8ms is within budget; check for novelty effect over the 14 days"],
     "last": ["segment by new vs returning users",
              "new users drive most of the lift, returning is flat — note that",
              "write the recommendation: ship to 100% with latency monitoring",
              "make the CTR-over-time chart, annotate the ramp dates",
              "finalize the doc"]},
    {"id": "d1b2c3d4-8888-4ddd-9008-000000000008", "project": "ml-experiments", "bridge": False,
     "n_query": 4, "first_ts": "2026-06-01T16:40:00Z", "last_ts": "2026-06-01T17:20:00Z",
     "title": "Set up experiment tracking with MLflow",
     "first": ["set up MLflow tracking for our training runs",
               "log params, metrics, and the model artifact"],
     "last": ["point the tracking server at the team S3 bucket",
              "add an example notebook showing how to query past runs"]},
    {"id": "e1b2c3d4-9999-4eee-9009-000000000009", "project": "docs-site", "bridge": False,
     "n_query": 11, "first_ts": "2026-05-31T09:05:00Z", "last_ts": "2026-05-31T12:30:00Z",
     "title": "Migrate docs from Docusaurus to Astro Starlight",
     "first": ["evaluate moving our docs from Docusaurus to Astro Starlight",
               "what breaks in the migration — MDX components, search, versioning?",
               "do a proof of concept with the getting-started section",
               "the sidebar config differs — map our categories over"],
     "last": ["set up Pagefind for search",
              "preserve the old URLs with redirects so we don't break inbound links",
              "deploy a preview and compare Lighthouse scores",
              "scores are better — proceed with the full migration"]},
]

# A full synthetic conversation for one demo session, so `--serve --demo` can showcase
# the chat-modal view (both sides + rendered markdown) without touching real transcripts.
DEMO_CONVOS = {
    "a1b2c3d4-1111-4aaa-9001-000000000001": [
        {"role": "user", "text": "add a dark mode toggle to the settings page", "ts": "2026-06-08T09:12:00Z"},
        {"role": "assistant", "text": "Here's the plan:\n\n1. Add a toggle `<button>` to the settings page\n2. Read the saved choice from `localStorage`\n3. Fall back to the OS setting via `prefers-color-scheme`\n\nI'll flip a `data-theme` attribute on `<html>` so the existing CSS variables switch instantly — no per-component changes needed.", "ts": "2026-06-08T09:14:00Z"},
        {"role": "user", "text": "it should default to the OS preference via prefers-color-scheme", "ts": "2026-06-08T09:20:00Z"},
        {"role": "assistant", "text": "Done. The resolution order is now:\n\n| source | wins when |\n|---|---|\n| `localStorage` | the user has toggled before |\n| `prefers-color-scheme` | first visit, no saved choice |\n\n```js\nconst saved = localStorage.getItem('theme');\nconst os = matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';\ndocument.documentElement.dataset.theme = saved ?? os;\n```", "ts": "2026-06-08T09:25:00Z"},
        {"role": "user", "text": "the toggle isn't updating the chart colors — fix that too", "ts": "2026-06-08T10:02:00Z"},
        {"role": "assistant", "text": "The charts read their colors **once at init**, so they miss the switch. Two ways to fix it:\n\n- **Re-render on toggle** — listen for the theme change and call `chart.update()`\n- **Drive the chart from CSS variables** so it follows `--surface` automatically\n\nI went with the first — one line, and the chart config stays declarative.", "ts": "2026-06-08T10:08:00Z"},
        {"role": "user", "text": "great, now write a test for the persistence logic", "ts": "2026-06-08T11:40:00Z"},
        {"role": "assistant", "text": "Added `theme.test.js`:\n\n```js\ntest('persists the choice across reloads', () => {\n  setTheme('dark');\n  expect(localStorage.getItem('theme')).toBe('dark');\n});\n```\n\nAll green — the toggle, the OS fallback, and persistence are covered.", "ts": "2026-06-08T11:48:00Z"},
    ],
}


# ────────────────────────────────────────────────────────────────────────────
# Theme system: one structural stylesheet (driven by CSS variables) plus four
# theme blocks. `__SEL__` is replaced per theme with html[data-theme="<name>"],
# so all four ship in every file and can be switched live from the header.
# ────────────────────────────────────────────────────────────────────────────
ALL_FONTS = ('<link href="https://fonts.googleapis.com/css2?'
             'family=Archivo:wght@400;500;600;700;800;900&'
             'family=Chakra+Petch:wght@400;500;600;700&'
             'family=Fraunces:ital,opsz,wght@0,9..144,400;0,9..144,500;0,9..144,600;1,9..144,400&'
             'family=IBM+Plex+Mono:wght@400;500;600;700&'
             'family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">')

SHARED_CSS = r"""
  *{box-sizing:border-box;}
  html{scroll-behavior:smooth;}
  body{margin:0; color:var(--ink); background:var(--bg); font-family:var(--body); -webkit-font-smoothing:antialiased;
    transition:background .25s, color .25s;}
  .page{position:relative; z-index:1;}
  header{padding:28px 40px 0;}
  .masthead{display:flex; justify-content:space-between; align-items:flex-end; gap:24px; flex-wrap:wrap;}
  .brand .kicker{font-family:var(--mono); font-size:11px; letter-spacing:.32em; text-transform:uppercase; color:var(--accent); margin-bottom:6px;}
  .brand h1{font-family:var(--display); margin:0; line-height:.95; font-optical-sizing:auto;
    font-size:clamp(34px,5vw,56px); font-weight:var(--h1-weight,600);
    letter-spacing:var(--h1-ls,-.02em); text-transform:var(--h1-transform,none);}
  .brand h1 em{font-style:italic; color:var(--accent);}
  .dateline{font-family:var(--mono); font-size:11px; color:var(--ink2); text-align:right; line-height:1.9; letter-spacing:.02em;}
  .dateline b{color:var(--ink); font-weight:700;}
  .controls{display:flex; gap:24px; align-items:baseline; flex-wrap:wrap; margin-top:22px; padding:14px 0;
    border-top:1px solid var(--line); position:sticky; top:0; z-index:5; background:var(--bg);}
  .field{display:flex; align-items:baseline; gap:8px;}
  .field label{font-family:var(--mono); font-size:10px; letter-spacing:.18em; text-transform:uppercase; color:var(--muted);}
  .field input, .field select{font-family:var(--mono); font-size:13px; color:var(--ink); background:transparent;
    border:none; border-bottom:1px solid var(--line2); padding:4px 2px; outline:none; transition:border-color .2s;}
  .field input:focus, .field select:focus{border-color:var(--accent);}
  .field select option{color:#111; background:#fff;}
  .field.search{flex:1; min-width:200px;}
  .field.search input{width:100%;}
  .field input::placeholder{color:var(--muted);}
  .field.theme select{color:var(--accent); font-weight:600;}
  .refreshbtn{font-family:var(--mono); font-size:11px; letter-spacing:.12em; text-transform:uppercase;
    color:var(--accent); background:transparent; border:1px solid var(--accent2); border-radius:var(--radius,2px);
    padding:6px 13px; cursor:pointer; white-space:nowrap; transition:background .2s,color .2s,border-color .2s;}
  .refreshbtn:hover:not(:disabled){background:var(--accent); color:var(--bg); border-color:var(--accent);}
  .refreshbtn:disabled{opacity:.55; cursor:default;}
  .wrap{padding:30px 40px 10px; display:grid; grid-template-columns:repeat(auto-fill,minmax(540px,1fr)); gap:26px;}
  @media(max-width:620px){ .wrap{grid-template-columns:1fr; padding:24px 18px;} }
  @keyframes rise{from{opacity:0; transform:translateY(14px);} to{opacity:1; transform:none;}}
  .entry{position:relative; background:var(--panel); border:var(--border-w,1px) solid var(--line2);
    border-radius:var(--radius,2px); padding:22px 24px 24px; display:flex; flex-direction:column; gap:14px;
    box-shadow:var(--shadow,none); animation:rise .5s cubic-bezier(.2,.7,.2,1) backwards;
    animation-delay:calc(var(--i,0) * 45ms);
    transition:transform .25s cubic-bezier(.2,.7,.2,1), box-shadow .25s, border-color .25s, background .25s, color .25s;}
  .entry:hover{transform:translateY(-3px); border-color:var(--accent); box-shadow:var(--shadow-hover,none);}
  .entry::before{content:"№ " attr(data-no); position:absolute; top:0; left:0;
    font-family:var(--mono); font-size:10px; letter-spacing:.12em; color:var(--muted);
    background:var(--tagbg,transparent); border-right:1px solid var(--line); border-bottom:1px solid var(--line);
    padding:3px 9px; border-radius:var(--radius,2px) 0 6px 0;}
  .head{display:flex; justify-content:space-between; align-items:flex-start; gap:14px; margin-top:8px;}
  .title{font-family:var(--display); font-weight:var(--title-weight,500); font-size:20px; line-height:1.25;
    margin:0; letter-spacing:-.01em; text-transform:var(--title-transform,none);}
  .tags{display:flex; flex-direction:column; gap:6px; align-items:flex-end; flex:none;}
  .proj{font-family:var(--mono); font-size:10px; letter-spacing:.06em; color:var(--accent);
    border:1px solid var(--accent2); border-radius:var(--pill,20px); padding:3px 10px; white-space:nowrap; background:var(--proj-bg,transparent);}
  .tag-bridge{font-family:var(--mono); font-size:10px; letter-spacing:.08em; color:var(--bridge);
    border:1px solid var(--bridge); border-radius:var(--pill,20px); padding:3px 10px; white-space:nowrap;
    display:inline-flex; align-items:center; gap:5px; background:var(--bridge-bg,transparent);}
  .tag-bridge::before{content:""; width:6px; height:6px; border-radius:50%; background:var(--bridge);}
  .metaline{display:flex; flex-wrap:wrap; align-items:center; gap:10px; font-family:var(--mono); font-size:11px;
    color:var(--ink2); padding-bottom:12px; border-bottom:1px solid var(--line);}
  .sid{cursor:pointer; color:var(--muted); border:none; background:none; font:inherit; padding:0;
    border-bottom:1px dotted var(--line2); transition:color .2s, border-color .2s;}
  .sid:hover{color:var(--accent); border-color:var(--accent);}
  .dot{color:var(--line2);}
  .qn{color:var(--ink); font-weight:500;}
  .cols{display:grid; grid-template-columns:1fr 1fr; gap:0;}
  .cols > section:first-child{border-right:1px solid var(--line); padding-right:18px;}
  .cols > section:last-child{padding-left:18px;}
  @media(max-width:560px){ .cols{grid-template-columns:1fr;}
    .cols > section:first-child{border-right:none; border-bottom:1px solid var(--line); padding:0 0 14px;}
    .cols > section:last-child{padding:14px 0 0;} }
  .lbl{font-family:var(--mono); font-size:10px; letter-spacing:.14em; text-transform:uppercase;
    margin-bottom:12px; display:flex; align-items:center; gap:7px;}
  .lbl .tick{width:18px; height:1.5px;}
  .lbl.first{color:var(--first);} .lbl.first .tick{background:var(--first);}
  .lbl.last{color:var(--last);}  .lbl.last .tick{background:var(--last);}
  ol.q{list-style:none; margin:0; padding:0; display:flex; flex-direction:column; gap:11px; counter-reset:q;}
  ol.q li{counter-increment:q; display:grid; grid-template-columns:22px 1fr; gap:8px;}
  ol.q li::before{content:counter(q,decimal-leading-zero); font-family:var(--mono); font-size:10px; color:var(--muted); padding-top:2px;}
  .first ol.q li::before{color:var(--first);}
  .last  ol.q li::before{color:var(--last);}
  .qtxt{font-size:12.5px; line-height:1.5; color:var(--ink2); white-space:pre-wrap; word-break:break-word;
    max-height:4.5em; overflow:hidden; position:relative; cursor:pointer; transition:color .15s;}
  .qtxt:hover{color:var(--ink);}
  .qtxt.expanded{max-height:none;}
  .qtxt.clamped:not(.expanded)::after{content:"＋"; position:absolute; right:0; bottom:0; padding-left:22px;
    background:linear-gradient(90deg,transparent,var(--panel) 55%); color:var(--accent); font-weight:700;}
  .qempty{font-size:12px; color:var(--muted); font-style:italic;}
  .empty{grid-column:1/-1; text-align:center; padding:90px 20px; color:var(--muted); font-family:var(--display); font-size:22px;}
  footer{text-align:center; padding:34px; font-family:var(--mono); font-size:11px; color:var(--muted);
    letter-spacing:.04em; border-top:1px solid var(--line); margin-top:20px;}
  #toast{position:fixed; bottom:28px; left:50%; transform:translateX(-50%) translateY(20px);
    background:var(--ink); color:var(--bg); font-family:var(--mono); font-size:12px; padding:9px 18px;
    border-radius:var(--radius,2px); opacity:0; pointer-events:none; transition:.25s; z-index:20; letter-spacing:.06em;}
  #toast.show{opacity:1; transform:translateX(-50%) translateY(0);}
  /* Chat modal — click a session to read its full conversation */
  .modal[hidden]{display:none;}
  .modal{position:fixed; inset:0; z-index:50; display:flex; align-items:center; justify-content:center; padding:4vh 16px;}
  .modal-backdrop{position:absolute; inset:0; background:rgba(0,0,0,.55); -webkit-backdrop-filter:blur(2px); backdrop-filter:blur(2px);}
  .modal-box{position:relative; z-index:1; width:min(860px,100%); max-height:90vh; display:flex; flex-direction:column;
    background:var(--bg); color:var(--ink); border:var(--border-w,1px) solid var(--line2);
    border-radius:var(--radius,2px); box-shadow:0 30px 80px -20px rgba(0,0,0,.6); overflow:hidden;
    animation:rise .3s cubic-bezier(.2,.7,.2,1);}
  .modal-head{display:flex; align-items:flex-start; gap:16px; padding:16px 20px; border-bottom:1px solid var(--line); background:var(--panel);}
  .modal-titles{flex:1; min-width:0;}
  .modal-title{font-family:var(--display); font-weight:var(--title-weight,600); font-size:18px; line-height:1.3; word-break:break-word;}
  .modal-meta{font-family:var(--mono); font-size:11px; color:var(--muted); margin-top:5px; word-break:break-all;}
  .modal-close{flex:none; font-family:var(--mono); font-size:15px; line-height:1; color:var(--ink2); background:transparent;
    border:1px solid var(--line2); border-radius:var(--radius,2px); width:30px; height:30px; cursor:pointer; transition:.2s;}
  .modal-close:hover{color:var(--bg); background:var(--accent); border-color:var(--accent);}
  .chat{padding:22px; overflow-y:auto; display:flex; flex-direction:column; gap:14px; background:var(--bg);}
  .chat-loading{text-align:center; color:var(--muted); font-family:var(--mono); font-size:13px; padding:46px 12px; line-height:1.7;}
  .bubble{max-width:82%; display:flex; flex-direction:column; gap:5px;}
  .bubble.user{align-self:flex-end; align-items:flex-end;}
  .bubble.assistant{align-self:flex-start; align-items:flex-start;}
  .bubble .who{font-family:var(--mono); font-size:9.5px; letter-spacing:.14em; text-transform:uppercase; color:var(--muted);}
  .bubble.user .who{color:var(--accent);}
  .bubble.assistant .who{color:var(--first);}
  .btxt{font-size:13px; line-height:1.55; word-break:break-word; overflow-wrap:anywhere; padding:11px 14px; border:1px solid var(--line2); border-radius:12px;}
  .btxt.plain{white-space:pre-wrap;}
  .bubble.user .btxt{background:color-mix(in srgb, var(--accent) 16%, var(--bg)); border-color:var(--accent); border-bottom-right-radius:3px;}
  .bubble.assistant .btxt{background:var(--panel); border-color:var(--line2); border-bottom-left-radius:3px;}
  /* rendered-markdown styling inside an assistant bubble */
  .btxt.md > :first-child{margin-top:0;} .btxt.md > :last-child{margin-bottom:0;}
  .btxt.md p{margin:.5em 0;}
  .btxt.md h1,.btxt.md h2,.btxt.md h3,.btxt.md h4{margin:.7em 0 .35em; line-height:1.25; font-family:var(--display);}
  .btxt.md h1{font-size:1.3em;} .btxt.md h2{font-size:1.18em;} .btxt.md h3{font-size:1.07em;} .btxt.md h4{font-size:1em;}
  .btxt.md ul,.btxt.md ol{margin:.45em 0; padding-left:1.4em;}
  .btxt.md li{margin:.18em 0;}
  .btxt.md code{font-family:var(--mono); font-size:.85em; background:rgba(128,128,128,.18); padding:.1em .36em; border-radius:4px;}
  .btxt.md pre{background:rgba(128,128,128,.14); border:1px solid var(--line2); border-radius:6px; padding:10px 12px; overflow-x:auto; margin:.55em 0;}
  .btxt.md pre code{background:none; padding:0; font-size:.84em; line-height:1.45;}
  .btxt.md a{color:var(--accent); text-decoration:underline;}
  .btxt.md blockquote{margin:.5em 0; padding:.15em .9em; border-left:3px solid var(--accent2); color:var(--ink2);}
  .btxt.md hr{border:none; border-top:1px solid var(--line2); margin:.75em 0;}
  .btxt.md table{border-collapse:collapse; margin:.55em 0; font-size:.92em; display:block; max-width:100%; overflow-x:auto;}
  .btxt.md th,.btxt.md td{border:1px solid var(--line2); padding:5px 9px; text-align:left;}
  .btxt.md th{background:rgba(128,128,128,.13); font-weight:600;}
  .title.chat-open{cursor:pointer;}
  .title.chat-open:hover{color:var(--accent);}
  .viewchat{font-family:var(--mono); font-size:10px; letter-spacing:.06em; color:var(--accent); background:none;
    border:none; padding:0; cursor:pointer; border-bottom:1px dotted var(--line2); transition:.2s;}
  .viewchat:hover{border-color:var(--accent);}
  @media(max-width:560px){ .bubble{max-width:94%;} }
"""

NOISE_SVG = ("url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'%3E"
             "%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='3' stitchTiles='stitch'/%3E"
             "%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E\")")

THEMES = {
    "brutalist": {
        "label": "Swiss Brutalist (light · bold)",
        "css": r"""
  __SEL__{
    --bg:#FCFCFA; --panel:#FFFFFF; --ink:#0A0A0A; --ink2:#262626; --muted:#888;
    --line:#0A0A0A; --line2:#0A0A0A; --accent:#FF2D16; --accent2:#0A0A0A;
    --first:#0A0A0A; --last:#FF2D16; --bridge:#0A0A0A;
    --display:'Archivo','PingFang SC',sans-serif;
    --mono:'JetBrains Mono',ui-monospace,monospace;
    --body:'Archivo','PingFang SC',sans-serif;
    --radius:0; --pill:0; --border-w:2px; --tagbg:#FFFFFF; --proj-bg:#FFFFFF; --bridge-bg:#FFFFFF;
    --shadow:none; --shadow-hover:7px 7px 0 var(--accent);
    --h1-weight:900; --h1-transform:uppercase; --h1-ls:-.035em; --title-weight:700;
  }
  __SEL__ header{border-bottom:3px solid #0A0A0A; background:var(--bg);}
  __SEL__ .controls{border-top:3px solid #0A0A0A;}
  __SEL__ .brand .kicker{color:#0A0A0A;}
  __SEL__ .entry:hover{transform:translate(-3px,-3px);}
  __SEL__ .proj{font-weight:600;}
  __SEL__ .lbl{font-weight:700;}
""",
    },
    "terminal": {
        "label": "Terminal Phosphor (dark · mono)",
        "css": r"""
  __SEL__{
    --bg:#070B07; --panel:#0E140E; --ink:#A8E6A8; --ink2:#7FB87F; --muted:#506B50;
    --line:#1A281A; --line2:#2A3E2A; --accent:#E8B24A; --accent2:#6B8A3A;
    --first:#5BD98A; --last:#E8B24A; --bridge:#52C7D6;
    --display:'IBM Plex Mono',ui-monospace,monospace;
    --mono:'IBM Plex Mono',ui-monospace,monospace;
    --body:'IBM Plex Mono',ui-monospace,'PingFang SC',monospace;
    --radius:0; --pill:0; --tagbg:#0E140E; --proj-bg:transparent; --bridge-bg:rgba(82,199,214,.07);
    --shadow:inset 0 0 0 1px rgba(40,60,40,.4); --shadow-hover:0 0 28px -6px rgba(91,217,138,.45);
    --h1-weight:700; --h1-ls:.01em; --title-weight:600;
  }
  __SEL__ header{border-bottom:1px solid var(--line2); background:#0A0F0A;}
  __SEL__ .brand h1{text-shadow:0 0 14px rgba(91,217,138,.4);}
  __SEL__ .brand .kicker::before{content:"$ ";}
  __SEL__ body::before{content:""; position:fixed; inset:0; z-index:0; pointer-events:none; opacity:.55;
    background:repeating-linear-gradient(0deg, rgba(0,0,0,0) 0, rgba(0,0,0,0) 2px, rgba(0,18,0,.5) 3px);}
""",
    },
    "blueprint": {
        "label": "Blueprint (dark · technical)",
        "css": r"""
  __SEL__{
    --bg:#0B1E2D; --panel:#102A3C; --ink:#D6E8F2; --ink2:#9DC0D6; --muted:#5C7E92;
    --line:#1B3A4F; --line2:#27506B; --accent:#46B6E6; --accent2:#2E6F93;
    --first:#5BD0BE; --last:#E0A64A; --bridge:#C78BE0;
    --display:'Chakra Petch','PingFang SC',sans-serif;
    --mono:'IBM Plex Mono',ui-monospace,monospace;
    --body:'Chakra Petch','PingFang SC',sans-serif;
    --radius:0; --pill:2px; --tagbg:#0B1E2D; --proj-bg:rgba(70,182,230,.08); --bridge-bg:rgba(199,139,224,.1);
    --shadow:inset 0 0 0 1px rgba(70,182,230,.06); --shadow-hover:0 18px 40px -24px rgba(70,182,230,.5);
    --h1-weight:700; --h1-transform:uppercase; --h1-ls:.02em; --title-weight:600;
  }
  __SEL__ header{border-bottom:1px solid var(--line2); background:linear-gradient(#0D2333,#0B1E2D);}
  __SEL__ body::before{content:""; position:fixed; inset:0; z-index:0; pointer-events:none; opacity:.5;
    background-image:linear-gradient(rgba(70,182,230,.06) 1px,transparent 1px),
      linear-gradient(90deg, rgba(70,182,230,.06) 1px,transparent 1px); background-size:30px 30px;}
""",
    },
    "ledger": {
        "label": "Archive Ledger (warm · serif)",
        "css": r"""
  __SEL__{
    --bg:#E7E0D0; --panel:#FBF7EC; --ink:#211C14; --ink2:#4C4536; --muted:#8C8270;
    --line:#D8CFBA; --line2:#C2B79C; --accent:#9E3B26; --accent2:#B5654A;
    --first:#3C6149; --last:#A8542A; --bridge:#2C6E8A;
    --display:'Fraunces','Songti SC','Noto Serif CJK SC',Georgia,serif;
    --mono:'JetBrains Mono',ui-monospace,SFMono-Regular,Menlo,monospace;
    --body:-apple-system,BlinkMacSystemFont,'PingFang SC','Segoe UI',sans-serif;
    --radius:2px; --pill:20px; --tagbg:#F6F1E6; --proj-bg:rgba(158,59,38,.05); --bridge-bg:rgba(44,110,138,.08);
    --shadow:0 1px 0 #D8CFBA, 0 14px 28px -22px rgba(33,28,20,.5);
    --shadow-hover:0 1px 0 #D8CFBA, 0 26px 40px -24px rgba(158,59,38,.45);
    --h1-weight:600;
  }
  __SEL__ header{border-bottom:3px double var(--line2); background:linear-gradient(#F6F1E6,#E7E0D0);}
  __SEL__ footer{border-top:3px double var(--line2);}
  __SEL__ .empty{font-style:italic;}
  __SEL__ body::before{content:""; position:fixed; inset:0; z-index:0; pointer-events:none; opacity:.05; background-image:%NOISE%;}
""".replace("%NOISE%", NOISE_SVG),
    },
}

THEME_ORDER = ["brutalist", "terminal", "blueprint", "ledger"]


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en" data-theme="__DEFTHEME__">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Session Ledger · Claude Code session archive</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
__FONTS__
<script>try{var _t=localStorage.getItem('sb-theme'); if(_t) document.documentElement.setAttribute('data-theme',_t);}catch(e){}</script>
<style>
__CSS__
</style>
</head>
<body>
<div class="page">
<header>
  <div class="masthead">
    <div class="brand">
      <div class="kicker">Claude Code · Session Archive</div>
      <h1>Session <em>Ledger</em></h1>
    </div>
    <div class="dateline" id="dateline"></div>
  </div>
  <div class="controls">
    <div class="field">
      <button id="refresh" class="refreshbtn" title="Rescan ~/.claude/projects and reload the list (live when served via --serve)">↻ Update</button>
    </div>
    <div class="field search">
      <label>Search</label>
      <input id="search" type="text" placeholder="title / session id / query text / project / bridge …">
    </div>
    <div class="field theme">
      <label>Theme</label>
      <select id="theme">__THEMEOPTS__</select>
    </div>
    <div class="field" id="projField">
      <label>Project</label>
      <select id="project"></select>
    </div>
    <div class="field">
      <label>Sort</label>
      <select id="sort">
        <option value="recent">Recent activity</option>
        <option value="oldest">Oldest first</option>
        <option value="queries">Query count</option>
        <option value="title">Title</option>
      </select>
    </div>
  </div>
</header>

<div class="wrap" id="wrap"></div>
<footer id="foot"></footer>
</div>
<div id="toast"></div>

<div id="chatModal" class="modal" hidden>
  <div class="modal-backdrop"></div>
  <div class="modal-box" role="dialog" aria-modal="true">
    <div class="modal-head">
      <div class="modal-titles">
        <div class="modal-title" id="chatTitle"></div>
        <div class="modal-meta" id="chatMeta"></div>
      </div>
      <button class="modal-close" id="chatClose" title="Close (Esc)">✕</button>
    </div>
    <div class="chat" id="chatBody"></div>
  </div>
</div>

<script id="data" type="application/json">__DATA__</script>
<script>
let RAW = JSON.parse(document.getElementById('data').textContent);
let GEN_AT = "__GENAT__";
let SCOPE = "__SCOPE__";
let PROJDIR = "__PROJDIR__";
const REFRESH_CMD = __REFRESHCMD__;
const $ = s => document.querySelector(s);
const wrap=$('#wrap'), searchEl=$('#search'), projEl=$('#project'), sortEl=$('#sort'), themeEl=$('#theme'), refreshEl=$('#refresh');

function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function fmtTs(ts){ if(!ts) return '—'; const d=new Date(ts); if(isNaN(d)) return '—';
  return d.toLocaleString(undefined,{year:'2-digit',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}); }

// Live theme switch (remembered via localStorage)
themeEl.value = document.documentElement.getAttribute('data-theme');
themeEl.addEventListener('change',()=>{
  document.documentElement.setAttribute('data-theme', themeEl.value);
  try{ localStorage.setItem('sb-theme', themeEl.value); }catch(e){}
});

// Project filter (hidden when there's only one project) — rebuilt on every refresh
function buildProjects(){
  const projects=[...new Set(RAW.map(s=>s.project))].sort();
  $('#projField').style.display = projects.length<=1 ? 'none' : '';
  const cur = projEl.value;
  projEl.innerHTML='<option value="">All ('+RAW.length+')</option>'+
    projects.map(p=>'<option value="'+esc(p)+'">'+esc(p)+' ('+RAW.filter(s=>s.project===p).length+')</option>').join('');
  if(cur && projects.includes(cur)) projEl.value = cur;   // keep selection across a refresh
}
function updateDateline(){
  $('#dateline').innerHTML='Scope <b>'+esc(SCOPE)+'</b><br><b>'+RAW.length+'</b> sessions · built <b>'+GEN_AT+'</b><br>'+esc(PROJDIR);
}
buildProjects();
updateDateline();

function qlist(arr){
  if(!arr||!arr.length) return '<div class="qempty">(none)</div>';
  return '<ol class="q">'+arr.map(q=>'<li><div class="qtxt">'+esc(q)+'</div></li>').join('')+'</ol>';
}

function render(){
  const kw=searchEl.value.trim().toLowerCase(), proj=projEl.value, sort=sortEl.value;
  let list=RAW.filter(s=>{
    if(proj && s.project!==proj) return false;
    if(!kw) return true;
    if(kw==='bridge' && s.bridge) return true;
    if(s.title.toLowerCase().includes(kw)||s.id.toLowerCase().includes(kw)||s.project.toLowerCase().includes(kw)) return true;
    return [...s.first,...s.last].some(q=>q.toLowerCase().includes(kw));
  });
  if(sort==='recent') list.sort((a,b)=>(b.last_ts||'').localeCompare(a.last_ts||''));
  else if(sort==='oldest') list.sort((a,b)=>(a.first_ts||'').localeCompare(b.first_ts||''));
  else if(sort==='queries') list.sort((a,b)=>b.n_query-a.n_query);
  else if(sort==='title') list.sort((a,b)=>a.title.localeCompare(b.title));

  if(!list.length){ wrap.innerHTML='<div class="empty">No matching sessions</div>'; return; }

  wrap.innerHTML=list.map((s,i)=>`
    <article class="entry" data-no="${String(i+1).padStart(3,'0')}" style="--i:${Math.min(i,14)}">
      <div class="head">
        <h2 class="title chat-open" data-id="${esc(s.id)}" data-title="${esc(s.title)}" title="Open full conversation">${esc(s.title)}</h2>
        <div class="tags">
          <span class="proj">${esc(s.project)}</span>
          ${s.bridge ? '<span class="tag-bridge">bridge</span>' : ''}
        </div>
      </div>
      <div class="metaline">
        <button class="sid" data-id="${esc(s.id)}" title="click to copy resume command">${esc(s.id)}</button>
        <span class="dot">·</span><span class="qn">${s.n_query}</span> queries
        <span class="dot">·</span>${fmtTs(s.first_ts)} → ${fmtTs(s.last_ts)}
        <span class="dot">·</span><button class="viewchat chat-open" data-id="${esc(s.id)}" data-title="${esc(s.title)}">View conversation →</button>
      </div>
      <div class="cols">
        <section>
          <div class="lbl first"><span class="tick"></span>First ${s.first.length}</div>
          ${qlist(s.first)}
        </section>
        <section>
          <div class="lbl last"><span class="tick"></span>Last ${s.last.length}</div>
          ${qlist(s.last)}
        </section>
      </div>
    </article>`).join('');

  wrap.querySelectorAll('.qtxt').forEach(el=>{
    if(el.scrollHeight>el.clientHeight+2) el.classList.add('clamped');
    el.addEventListener('click',()=>el.classList.toggle('expanded'));
  });
  wrap.querySelectorAll('.sid').forEach(b=>b.addEventListener('click',()=>copyId(b.dataset.id)));
  wrap.querySelectorAll('.chat-open').forEach(el=>el.addEventListener('click',()=>openChat(el.dataset.id, el.dataset.title)));
}

// Click a session id -> copy a ready-to-run resume command
const RESUME_CMD = id => '__RESUMEPREFIX__' + id;
let toastT;
function toast(msg){ const t=$('#toast'); t.textContent=msg; t.classList.add('show');
  clearTimeout(toastT); toastT=setTimeout(()=>t.classList.remove('show'),2600); }
function copyText(txt){
  return (navigator.clipboard?navigator.clipboard.writeText(txt):Promise.reject()).catch(()=>{
    const ta=document.createElement('textarea'); ta.value=txt; document.body.appendChild(ta);
    ta.select(); try{document.execCommand('copy');}catch(e){} ta.remove();
  });
}
function copyId(id){ const cmd=RESUME_CMD(id); copyText(cmd).then(()=>toast('Copied · '+cmd)); }

// Click a session (title or "View conversation") -> fetch its full transcript, show as a chat modal.
const chatModal=$('#chatModal'), chatBody=$('#chatBody'), chatTitleEl=$('#chatTitle'), chatMetaEl=$('#chatMeta');
// One AI response spans many transcript events (text fragments interleaved with tool calls);
// merge consecutive same-role turns into a single bubble so it reads like a real chat.
function coalesce(turns){
  const out=[];
  for(const t of turns){
    const last=out[out.length-1];
    if(last && last.role===t.role) last.text += '\n\n' + t.text;
    else out.push({role:t.role, text:t.text});
  }
  return out;
}
// Minimal, dependency-free markdown -> HTML for AI replies. Escapes first (safe), then renders.
function mdInline(t){
  return t
    .replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+)\*/g,'$1<em>$2</em>')
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>');
}
function mdRender(src){
  let s = esc(src);
  const blocks=[], inl=[];
  s = s.replace(/```[^\n]*\n([\s\S]*?)```/g,(m,c)=>{ blocks.push(c.replace(/\n+$/,'')); return ''+(blocks.length-1)+''; });
  s = s.replace(/`([^`\n]+)`/g,(m,c)=>{ inl.push(c); return ''+(inl.length-1)+''; });
  const L=s.split('\n'); let out='', i=0;
  const isList=x=>/^\s*([-*+]|\d+\.)\s+/.test(x);
  const special=x=>/^\s*$/.test(x)||/^#{1,6}\s/.test(x)||isList(x)||/^>\s?/.test(x)||/^(\-\-\-+|\*\*\*+|___+)\s*$/.test(x);
  const cells=r=>r.replace(/^\s*\|/,'').replace(/\|\s*$/,'').split('|').map(c=>c.trim());
  while(i<L.length){
    const line=L[i];
    const h=line.match(/^(#{1,6})\s+(.*)$/);
    if(h){ out+='<h'+h[1].length+'>'+mdInline(h[2])+'</h'+h[1].length+'>'; i++; continue; }
    if(/^(\-\-\-+|\*\*\*+|___+)\s*$/.test(line)){ out+='<hr>'; i++; continue; }
    if(line.includes('|') && i+1<L.length && L[i+1].includes('-') && /^\s*\|?[\s:|-]*-[\s:|-]*$/.test(L[i+1])){
      const hd=cells(line); i+=2; const rows=[];
      while(i<L.length && L[i].includes('|') && !/^\s*$/.test(L[i])){ rows.push(cells(L[i])); i++; }
      out+='<table><thead><tr>'+hd.map(c=>'<th>'+mdInline(c)+'</th>').join('')+'</tr></thead><tbody>'+
        rows.map(r=>'<tr>'+r.map(c=>'<td>'+mdInline(c)+'</td>').join('')+'</tr>').join('')+'</tbody></table>'; continue;
    }
    if(/^>\s?/.test(line)){ const b=[]; while(i<L.length&&/^>\s?/.test(L[i])){ b.push(L[i].replace(/^>\s?/,'')); i++; } out+='<blockquote>'+mdInline(b.join('<br>'))+'</blockquote>'; continue; }
    if(/^\s*[-*+]\s+/.test(line)){ const b=[]; while(i<L.length&&/^\s*[-*+]\s+/.test(L[i])){ b.push('<li>'+mdInline(L[i].replace(/^\s*[-*+]\s+/,''))+'</li>'); i++; } out+='<ul>'+b.join('')+'</ul>'; continue; }
    if(/^\s*\d+\.\s+/.test(line)){ const b=[]; while(i<L.length&&/^\s*\d+\.\s+/.test(L[i])){ b.push('<li>'+mdInline(L[i].replace(/^\s*\d+\.\s+/,''))+'</li>'); i++; } out+='<ol>'+b.join('')+'</ol>'; continue; }
    if(/^\s*$/.test(line)){ i++; continue; }
    const p=[]; while(i<L.length && !special(L[i])){ p.push(L[i]); i++; } out+='<p>'+mdInline(p.join('<br>'))+'</p>';
  }
  out = out.replace(/(\d+)/g,(m,n)=>'<code>'+inl[+n]+'</code>');
  out = out.replace(/(\d+)/g,(m,n)=>'<pre><code>'+blocks[+n]+'</code></pre>');
  return out;
}
function renderTurns(turns){
  return turns.map(t=>{
    const user = t.role==='user';
    const body = user ? '<div class="btxt plain">'+esc(t.text)+'</div>'
                      : '<div class="btxt md">'+mdRender(t.text)+'</div>';
    return '<div class="bubble '+(user?'user':'assistant')+'"><div class="who">'+(user?'You':'Claude')+'</div>'+body+'</div>';
  }).join('');
}
function openChat(id, title){
  chatTitleEl.textContent = title || id;
  chatMetaEl.textContent = id;
  chatBody.innerHTML = '<div class="chat-loading">Loading conversation…</div>';
  chatModal.hidden = false;
  document.body.style.overflow = 'hidden';
  fetch('api/session/'+encodeURIComponent(id), {cache:'no-store'})
    .then(r=>{ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
    .then(d=>{
      const turns = coalesce(d.turns || []);
      if(!turns.length){ chatBody.innerHTML='<div class="chat-loading">(no messages found in this session)</div>'; return; }
      chatMetaEl.textContent = id+' · '+turns.length+' messages';
      chatBody.innerHTML = renderTurns(turns);
      chatBody.scrollTop = 0;
    })
    .catch(err=>{
      const cmd = REFRESH_CMD.replace('--open','--serve --open');
      chatBody.innerHTML = '<div class="chat-loading">Full conversation needs serve mode '+
        '— a static file can\'t read the transcript.<br>Run: <b>'+esc(cmd)+'</b><br>then open the served page.'+
        '<br><br>('+esc(err.message)+')</div>';
    });
}
function closeChat(){ chatModal.hidden = true; document.body.style.overflow=''; }
$('#chatClose').addEventListener('click', closeChat);
chatModal.querySelector('.modal-backdrop').addEventListener('click', closeChat);
document.addEventListener('keydown', e=>{ if(e.key==='Escape' && !chatModal.hidden) closeChat(); });

// ↻ Update: live-rescan when served (python3 session_ledger.py --serve). A static
// file:// page can't shell out, so there it degrades to copying the regenerate command.
async function refresh(){
  const orig = refreshEl.textContent;
  refreshEl.disabled = true; refreshEl.textContent = '↻ Updating…';
  try{
    const r = await fetch('api/sessions', {cache:'no-store'});
    if(!r.ok) throw new Error('HTTP '+r.status);
    const d = await r.json();
    RAW = d.sessions; GEN_AT = d.gen_at; SCOPE = d.scope; PROJDIR = d.projdir;
    buildProjects(); updateDateline(); render(); updateFoot();
    toast('Updated · '+RAW.length+' sessions · '+GEN_AT);
  }catch(e){
    await copyText(REFRESH_CMD);
    toast('Static file — run to refresh (copied): '+REFRESH_CMD);
  }finally{
    refreshEl.disabled = false; refreshEl.textContent = orig;
  }
}
function updateFoot(){ $('#foot').textContent = RAW.length+' sessions · '+SCOPE+' · built '+GEN_AT+' · click ↻ Update to rescan'; }

searchEl.addEventListener('input',render);
projEl.addEventListener('change',render);
sortEl.addEventListener('change',render);
refreshEl.addEventListener('click',refresh);
render();
updateFoot();

// Deep link: ?chat=<id> (or #chat=<id>) auto-opens that conversation — handy for sharing.
(function(){
  const m = location.search.match(/[?&]chat=([^&]+)/) || location.hash.match(/chat=([^&]+)/);
  if(m){ const id=decodeURIComponent(m[1]); const s=RAW.find(x=>x.id===id); openChat(id, s&&s.title); }
})();
</script>
</body>
</html>
"""


def build_html(sessions, gen_at, scope, default_theme, projdir_display, skip_permissions):
    theme_css = "\n".join(
        THEMES[name]["css"].replace("__SEL__", f'html[data-theme="{name}"]')
        for name in THEME_ORDER
    )
    theme_opts = "".join(
        f'<option value="{name}">{THEMES[name]["label"]}</option>'
        for name in THEME_ORDER
    )
    resume_prefix = ("claude --dangerously-skip-permissions --resume "
                     if skip_permissions else "claude --resume ")
    # Command the Refresh button copies when opened as a static file (can't live-rescan).
    refresh_cmd = json.dumps(f"python3 {os.path.abspath(__file__)} --open")
    data_json = json.dumps(sessions, ensure_ascii=False).replace("</", "<\\/")
    out = HTML_TEMPLATE.replace("__FONTS__", ALL_FONTS)
    out = out.replace("__CSS__", theme_css + SHARED_CSS)
    out = out.replace("__THEMEOPTS__", theme_opts)
    out = out.replace("__DEFTHEME__", default_theme)
    out = out.replace("__RESUMEPREFIX__", resume_prefix)
    out = out.replace("__REFRESHCMD__", refresh_cmd)
    out = out.replace("__DATA__", data_json)
    out = out.replace("__GENAT__", gen_at)
    out = out.replace("__SCOPE__", scope)
    out = out.replace("__PROJDIR__", projdir_display)
    return out


def _collect(args):
    """Return (sessions, scope, projdir_display) for the current args — demo or live scan."""
    if args.demo:
        sessions = [dict(s, first=s["first"][:args.queries], last=s["last"][-args.queries:])
                    for s in DEMO_SESSIONS]
        return sessions, "demo", "(bundled sample data)"
    scope = args.project if args.project else "all projects"
    projdir_display = args.projects_dir.replace(HOME, "~")
    sessions = scan(args.projects_dir, args.project, args.queries)
    return sessions, scope, projdir_display


def serve(args):
    """Run a tiny stdlib HTTP server so the in-page ↻ Update button can re-scan live.

    GET /              -> freshly built HTML
    GET /api/sessions  -> rescans transcripts, returns {sessions, gen_at, scope, projdir} JSON
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass  # keep the terminal quiet

        def _send(self, text, ctype, code=200):
            data = text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                sessions, scope, projdir = _collect(args)
                gen_at = datetime.now().strftime("%Y-%m-%d %H:%M")
                self._send(build_html(sessions, gen_at, scope, args.theme, projdir,
                                      args.skip_permissions), "text/html; charset=utf-8")
            elif path == "/api/sessions":
                sessions, scope, projdir = _collect(args)
                gen_at = datetime.now().strftime("%Y-%m-%d %H:%M")
                self._send(json.dumps({"sessions": sessions, "gen_at": gen_at,
                                       "scope": scope, "projdir": projdir}, ensure_ascii=False),
                           "application/json; charset=utf-8")
            elif path.startswith("/api/session/"):
                from urllib.parse import unquote
                sid = unquote(path[len("/api/session/"):])
                turns = None
                if args.demo:
                    if sid in DEMO_CONVOS:
                        turns = DEMO_CONVOS[sid]
                    else:
                        for s in DEMO_SESSIONS:
                            if s["id"] == sid:
                                seen = []
                                for q in s["first"] + s["last"]:
                                    if q not in seen:
                                        seen.append(q)
                                turns = [{"role": "user", "text": q, "ts": None} for q in seen]
                                break
                else:
                    f = find_session_file(args.projects_dir, sid)
                    if f:
                        turns = parse_conversation(f)
                if turns is None:
                    self._send(json.dumps({"error": "session not found", "id": sid}),
                               "application/json; charset=utf-8", 404)
                else:
                    self._send(json.dumps({"id": sid, "turns": turns}, ensure_ascii=False),
                               "application/json; charset=utf-8")
            else:
                self.send_response(404)
                self.end_headers()

    httpd = None
    for p in range(args.serve, args.serve + 10):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            break
        except OSError:
            print(f"  port {p} busy, trying {p + 1} …", file=sys.stderr)
    if httpd is None:
        sys.exit(f"error: no free port in {args.serve}–{args.serve + 9}")
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"\n✓ Session Ledger live at {url}", file=sys.stderr)
    print("  the ↻ Update button now re-scans on click · Ctrl-C to stop", file=sys.stderr)
    if args.open:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.", file=sys.stderr)
        httpd.shutdown()


def main():
    ap = argparse.ArgumentParser(
        description="Build a self-contained HTML browser for your Claude Code sessions.")
    ap.add_argument("--project", help="only include projects whose dir name contains this substring")
    ap.add_argument("--projects-dir", default=DEFAULT_PROJECTS_DIR,
                    help="where Claude Code stores transcripts (default: ~/.claude/projects)")
    ap.add_argument("--theme", default="brutalist", choices=THEME_ORDER,
                    help="first-open theme; all four ship in the file and switch live (default: brutalist)")
    ap.add_argument("--queries", type=int, default=5, metavar="N",
                    help="how many earliest / latest queries to show per session (default: 5)")
    ap.add_argument("--demo", action="store_true",
                    help="build from bundled sample data instead of scanning (safe to share)")
    ap.add_argument("--skip-permissions", action="store_true",
                    help="copied resume command includes --dangerously-skip-permissions (opt-in)")
    ap.add_argument("--open", action="store_true", help="open the result in your browser when done")
    ap.add_argument("--serve", nargs="?", const=8765, type=int, default=None, metavar="PORT",
                    help="serve the page locally so the in-page ↻ Update button can live-rescan "
                         "(default port 8765; long-running, Ctrl-C to stop)")
    ap.add_argument("-o", "--output",
                    default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions.html"))
    args = ap.parse_args()

    if args.serve is not None:
        serve(args)
        return

    if not args.demo:
        scope_lbl = args.project if args.project else "all projects"
        print(f"scanning {args.projects_dir} ({scope_lbl}) · default theme {args.theme} …", file=sys.stderr)
    sessions, scope, projdir_display = _collect(args)

    gen_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    out_path = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as fh:
        fh.write(build_html(sessions, gen_at, scope, args.theme, projdir_display, args.skip_permissions))
    print(f"\n✓ {len(sessions)} sessions · {args.theme} default · 4 switchable themes → {out_path}",
          file=sys.stderr)

    if args.open:
        webbrowser.open("file://" + out_path)


if __name__ == "__main__":
    main()
