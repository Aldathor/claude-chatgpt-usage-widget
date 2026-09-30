"""
AI Usage Monitor
================
A compact desktop widget showing Claude Code and Codex CLI usage in one window:
live limit bars per provider plus tokens used for Today, Yesterday, and the
Last 30 Days.

Data sources (all local / your own account):
- Codex limits + tokens : read from Codex CLI's local logs.
- Claude limits (%)      : read from Claude Code's own usage endpoint using the
                          OAuth token that `claude` stores after you log in.
                          This is account-wide: it covers ALL Claude usage
                          (chat, Cowork, Claude Code, CLI), not just the CLI.
- Claude tokens         : read from Claude Code's local session logs (this PC
                          only; Claude chat usage is never logged locally).

Nothing is sent anywhere except your own authenticated request to Anthropic's
usage endpoint, exactly as Claude Code itself does.

Run:           python usage_monitor.py
Build:         see build_exe.bat (standalone AIUsage.exe)
Diagnose:      AIUsage.exe --test-claude   (writes a report you can read)
"""

import base64
import glob
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
import webbrowser
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# --------------------------------------------------------------------------
# CONFIG  --  edit if you like
# --------------------------------------------------------------------------

REFRESH_SECONDS = 15          # how often the window re-reads local data
PREFERRED_PORT = 8765

WINDOW_WIDTH = 640            # snug around the card (the card fills this minus a small gap)
WINDOW_HEIGHT = 700           # fallback only; at launch this is set from WINDOW_FRACTION
WINDOW_FRACTION = 0.667       # widget height as a fraction of the screen height (~2/3)
WINDOW_MIN_HEIGHT = 300
WINDOW_MAX_HEIGHT = 2000
ALWAYS_ON_TOP = True          # True pins the window above others, widget-style

# Taskbar mini gadget (always-on-top; a 2-line bar that expands on hover).
# MINI_BAR_HEIGHT is a fixed CSS height that comfortably fits the two lines; the
# window is sized to it (× DPI scale) and centered on the taskbar, so the layout
# never depends on measuring the taskbar height at runtime.
MINI_WIDTH = 184
MINI_BAR_HEIGHT = 40
MINI_EXPANDED_WIDTH = 380
MINI_EXPANDED_HEIGHT = 520

# Claude usage endpoint (the same one Claude Code uses). The User-Agent header
# is REQUIRED; without it the endpoint hard rate-limits. Poll no faster than
# ~180s. Edit CLAUDE_UA if a future Claude Code version rejects this one.
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_BETA = "oauth-2025-04-20"
CLAUDE_UA = "claude-code/2.1.114"
CLAUDE_POLL_SECONDS = 300
CLAUDE_CREDS = Path.home() / ".claude" / ".credentials.json"

CLAUDE_LOG_DIR = Path.home() / ".claude" / "projects"
CODEX_LOG_DIRS = [Path.home() / ".codex" / "sessions", Path.home() / ".codex"]
CODEX_AUTH = Path.home() / ".codex" / "auth.json"
# Codex limits can be read LIVE (no model call, no quota) by driving the official
# `codex app-server`'s `account/rateLimits/read` RPC. Polled gently; falls back to
# the last log snapshot if the codex binary isn't installed.
CODEX_POLL_SECONDS = 120
STATE_FILE = Path.home() / ".usage_monitor_state.json"  # welcome marker only


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def parse_ts(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = value / 1000.0 if value > 1e12 else value
        try:
            return datetime.fromtimestamp(v, tz=timezone.utc)
        except Exception:
            return None
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fmt_reset(seconds):
    if seconds is None:
        return None
    try:
        s = int(seconds)
    except Exception:
        return None
    if s < 0:
        s = 0
    d, h, m = s // 86400, (s % 86400) // 3600, (s % 3600) // 60
    if d > 0:
        return f"{d}d {h}h"
    if h > 0:
        return f"{h}h {m}m" if m else f"{h}h"
    return f"{m}m"


def fmt_ago(seconds):
    """'as of' phrasing for a past timestamp, e.g. '5m ago', '3d ago'."""
    if seconds is None:
        return None
    try:
        s = max(0, int(seconds))
    except Exception:
        return None
    if s < 60:
        return "just now"
    d, h, m = s // 86400, (s % 86400) // 3600, (s % 3600) // 60
    if d > 0:
        return f"{d}d ago"
    if h > 0:
        return f"{h}h ago"
    return f"{m}m ago"


# --------------------------------------------------------------------------
# local log parsers (token rows; Codex limits)
# --------------------------------------------------------------------------

def parse_claude():
    events = []
    if not CLAUDE_LOG_DIR.exists():
        return events
    seen = set()
    for path in CLAUDE_LOG_DIR.rglob("*.jsonl"):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    msg = row.get("message") or {}
                    usage = msg.get("usage") or {}
                    if not usage:
                        continue
                    key = (msg.get("id"), row.get("requestId"))
                    if key != (None, None) and key in seen:
                        continue
                    seen.add(key)
                    inp = int(usage.get("input_tokens", 0) or 0)
                    out = int(usage.get("output_tokens", 0) or 0)
                    cw = int(usage.get("cache_creation_input_tokens", 0) or 0)
                    cr = int(usage.get("cache_read_input_tokens", 0) or 0)
                    if inp == out == cw == cr == 0:
                        continue
                    events.append({
                        "ts": parse_ts(row.get("timestamp")),
                        "input": inp, "output": out, "cache_w": cw, "cache_r": cr,
                    })
        except Exception:
            continue
    return events


def _codex_info(row):
    if isinstance(row.get("info"), dict):
        return row["info"]
    p = row.get("payload")
    if isinstance(p, dict) and isinstance(p.get("info"), dict):
        return p["info"]
    if isinstance(p, dict):
        return p
    return row


def _codex_rate_limits(row):
    """Find a `rate_limits` block regardless of Codex CLI log schema version.

    Older logs nested it inside `info` (so `_codex_info()` happened to surface
    it); current logs put it as a sibling of `info` under `payload`, which
    `_codex_info()` no longer reaches. Check every plausible spot directly
    rather than relying on `_codex_info()`'s single guess.
    """
    if not isinstance(row, dict):
        return None
    for holder in (row, row.get("info"), row.get("payload"),
                   (row.get("payload") or {}).get("info") if isinstance(row.get("payload"), dict) else None):
        if isinstance(holder, dict) and isinstance(holder.get("rate_limits"), dict):
            return holder["rate_limits"]
    return None


def parse_codex():
    events = []
    files = []
    for d in CODEX_LOG_DIRS:
        if d.exists():
            files.extend(d.rglob("*.jsonl"))
    for path in set(files):
        try:
            last_cumulative = None
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    info = _codex_info(row)
                    block = field = None
                    for f in ("last_token_usage", "token_usage",
                              "total_token_usage", "usage"):
                        b = info.get(f) if isinstance(info, dict) else None
                        if isinstance(b, dict):
                            block, field = b, f
                            break
                    if not block:
                        continue
                    inp = int(block.get("input_tokens", 0) or 0)
                    out = int(block.get("output_tokens", 0) or 0)
                    cr = int(block.get("cached_input_tokens",
                             block.get("cache_read_input_tokens", 0)) or 0)
                    ts = parse_ts(row.get("timestamp") or row.get("ts"))
                    if field == "total_token_usage":
                        if last_cumulative is None:
                            d_in, d_out, d_cr = inp, out, cr
                        else:
                            d_in = max(0, inp - last_cumulative[0])
                            d_out = max(0, out - last_cumulative[1])
                            d_cr = max(0, cr - last_cumulative[2])
                        last_cumulative = (inp, out, cr)
                        inp, out, cr = d_in, d_out, d_cr
                    if inp == out == cr == 0:
                        continue
                    events.append({
                        "ts": ts, "input": inp, "output": out,
                        "cache_w": 0, "cache_r": cr,
                    })
        except Exception:
            continue
    return events


def _scan_codex_rate_limits():
    latest, latest_ts = None, None
    files = []
    for d in CODEX_LOG_DIRS:
        if d.exists():
            files.extend(d.rglob("*.jsonl"))
    for path in set(files):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    rl = _codex_rate_limits(row)
                    if not isinstance(rl, dict):
                        continue
                    ts = parse_ts(row.get("timestamp") or row.get("ts"))
                    if latest_ts is None or (ts and ts > latest_ts):
                        latest, latest_ts = rl, ts
        except Exception:
            continue
    return latest, latest_ts


def codex_limit_bars(rl):
    """Codex's own logs give an absolute `resets_at` (unix epoch), not a
    countdown, so it's converted to seconds-from-now here. If that moment has
    already passed, the local snapshot is too old to say anything useful about
    the reset, so leave it blank rather than show a misleading "0m"."""
    if not isinstance(rl, dict):
        return []
    out = []
    now = time.time()
    for key in ("primary", "secondary"):
        b = rl.get(key)
        if not isinstance(b, dict):
            continue
        used = b.get("used_percent")
        if used is None:
            continue
        win = b.get("window_minutes") or 0
        label = "Session" if (win and win <= 600) else ("Weekly" if win else key.title())
        secs = b.get("resets_in_seconds")
        if secs is None and b.get("resets_at") is not None:
            try:
                secs = float(b["resets_at"]) - now
            except Exception:
                secs = None
        out.append({"label": label,
                    "percent_left": max(0, min(100, round(100 - float(used)))),
                    "resets": fmt_reset(secs) if (secs is not None and secs >= 0) else None})
    return out


def read_codex_plan():
    """Friendly Codex plan label like 'ChatGPT Plus', read locally from the
    id_token in ~/.codex/auth.json. Only the plan-type claim is used."""
    try:
        data = json.loads(CODEX_AUTH.read_text(encoding="utf-8"))
        tok = (data.get("tokens") or {}).get("id_token") or ""
        payload = tok.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        auth = claims.get("https://api.openai.com/auth") or {}
        plan = (auth.get("chatgpt_plan_type") or "").lower()
    except Exception:
        return None
    label = _CODEX_PLAN_LABELS.get(plan, plan.title() if plan else "")
    return ("ChatGPT " + label) if label else None


_CODEX_PLAN_LABELS = {"plus": "Plus", "pro": "Pro", "team": "Team",
                      "business": "Business", "enterprise": "Enterprise",
                      "edu": "Edu", "free": "Free", "go": "Go"}


def find_codex_binary():
    """Locate the official `codex` executable (needed for the live limits read)."""
    exe = shutil.which("codex")
    if exe:
        return exe
    patterns = []
    for base in (os.environ.get("LOCALAPPDATA"), os.environ.get("APPDATA")):
        if base:
            patterns.append(os.path.join(base, "OpenAI", "Codex", "bin", "codex.exe"))
    patterns.append(str(Path.home() / ".codex" / "bin" / "codex.exe"))
    for p in patterns:
        if os.path.exists(p):
            return p
    return None


def fetch_codex_usage(timeout=25):
    """Read LIVE Codex rate limits via the official `codex app-server` RPC
    `account/rateLimits/read` — the same call the Codex desktop app makes. This
    is an account read, not a model turn, so it costs no quota. Returns
    (snapshot_in_scan_format, plan_label, error)."""
    exe = find_codex_binary()
    if not exe:
        return None, None, "no-codex"
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        proc = subprocess.Popen(
            [exe, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1, **kwargs)
    except Exception:
        return None, None, "spawn-failed"

    result = {"box": None}
    def drive():
        try:
            def send(obj):
                proc.stdin.write(json.dumps(obj) + "\n")
                proc.stdin.flush()
            send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"clientInfo": {"name": "ai-usage-monitor",
                                            "title": None, "version": "1.0"},
                             "capabilities": None}})
            asked = False
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                if msg.get("id") == 1 and "result" in msg and not asked:
                    asked = True
                    send({"jsonrpc": "2.0", "id": 2,
                          "method": "account/rateLimits/read"})
                elif msg.get("id") == 2:
                    result["box"] = msg
                    return
        except Exception:
            pass

    t = threading.Thread(target=drive, daemon=True)
    t.start()
    t.join(timeout)
    try:
        proc.terminate()
    except Exception:
        pass

    msg = result["box"]
    if not msg or "result" not in msg:
        return None, None, "no-response"
    rl = (msg["result"] or {}).get("rateLimits") or {}

    def win(w):
        if not isinstance(w, dict):
            return None
        return {"used_percent": w.get("usedPercent"),
                "window_minutes": w.get("windowDurationMins"),
                "resets_at": w.get("resetsAt")}

    snap = {"primary": win(rl.get("primary")), "secondary": win(rl.get("secondary"))}
    plan = rl.get("planType")
    plan_label = None
    if plan:
        lbl = _CODEX_PLAN_LABELS.get(str(plan).lower(), str(plan).title())
        plan_label = "ChatGPT " + lbl
    return snap, plan_label, None


# --------------------------------------------------------------------------
# Claude limits via the OAuth usage endpoint
# --------------------------------------------------------------------------

_claude_cache = {"limits": [], "status": "init", "fetched": 0.0}
_claude_lock = threading.Lock()


def read_claude_token():
    tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if tok:
        return tok.strip(), "env"
    try:
        data = json.loads(CLAUDE_CREDS.read_text(encoding="utf-8"))
        oauth = data.get("claudeAiOauth") or data
        tok = oauth.get("accessToken") or oauth.get("access_token")
        if tok:
            return tok, "file"
    except Exception:
        pass
    return None, None


def read_claude_plan():
    """Return a friendly plan label like 'Max (5x)' from the credentials file."""
    try:
        data = json.loads(CLAUDE_CREDS.read_text(encoding="utf-8"))
        oauth = data.get("claudeAiOauth") or data
    except Exception:
        return None
    sub = (oauth.get("subscriptionType") or "").lower()
    tier = (oauth.get("rateLimitTier") or "").lower()
    name = {"max": "Max", "pro": "Pro", "team": "Team",
            "enterprise": "Enterprise", "free": "Free"}.get(sub, sub.title() if sub else "")
    mult = ""
    for m in ("20x", "5x", "1x"):
        if m in tier:
            mult = m
            break
    if name and mult:
        return f"{name} ({mult})"
    return name or None


def fetch_claude_usage():
    token, _ = read_claude_token()
    if not token:
        return None, "no-login"
    req = urllib.request.Request(CLAUDE_USAGE_URL, method="GET")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("anthropic-beta", CLAUDE_BETA)
    req.add_header("User-Agent", CLAUDE_UA)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        return None, {401: "expired", 403: "expired", 429: "rate-limited"}.get(e.code, f"http-{e.code}")
    except Exception:
        return None, "network"


def claude_usage_to_bars(usage):
    out = []
    mapping = [("five_hour", "Session"), ("seven_day", "Weekly"),
               ("seven_day_sonnet", "Weekly (Sonnet)"), ("seven_day_opus", "Weekly (Opus)")]
    for key, label in mapping:
        w = usage.get(key)
        if not isinstance(w, dict):
            continue
        util = w.get("utilization")
        if util is None:
            continue
        secs = None
        if w.get("resets_at"):
            ts = parse_ts(w["resets_at"])
            if ts:
                secs = max(0, (ts - datetime.now(timezone.utc)).total_seconds())
        out.append({"label": label,
                    "percent_left": max(0, min(100, round(100 - float(util)))),
                    "resets": fmt_reset(secs)})
    return out


def refresh_claude_usage():
    usage, err = fetch_claude_usage()
    with _claude_lock:
        _claude_cache["fetched"] = time.time()
        if usage is not None:
            _claude_cache["limits"] = claude_usage_to_bars(usage)
            _claude_cache["status"] = "ok"
        else:
            _claude_cache["status"] = err or "error"
            if err in ("no-login", "expired"):
                _claude_cache["limits"] = []


def claude_usage_loop():
    # Poll the local token file cheaply (every few seconds, no network) and only
    # call the usage endpoint when the token first appears / changes (e.g. right
    # after sign-in or a refresh) or on the normal slow cadence. This keeps us
    # well under the endpoint's rate limit while still showing the bars within a
    # few seconds of the user signing in.
    last_tok = None
    last_fetch = 0.0
    while True:
        tok, _ = read_claude_token()
        now = time.time()
        if (tok and tok != last_tok) or (now - last_fetch >= CLAUDE_POLL_SECONDS):
            try:
                refresh_claude_usage()
            except Exception:
                pass
            last_tok = tok
            last_fetch = now
        time.sleep(5)


def get_claude_limits():
    with _claude_lock:
        return list(_claude_cache["limits"]), _claude_cache["status"]


# --------------------------------------------------------------------------
# one-click sign-in (drives the OFFICIAL `claude` binary, no terminal needed)
# --------------------------------------------------------------------------

def find_claude_binary():
    """Locate a real `claude` executable: PATH first, then the binary that the
    Claude desktop app bundles, then the standard CLI install path."""
    exe = shutil.which("claude")
    if exe:
        return exe
    patterns = []
    for base in (os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA")):
        if base:
            patterns.append(os.path.join(base, "Claude", "claude-code", "*", "claude.exe"))
    patterns.append(str(Path.home() / ".local" / "bin" / "claude.exe"))
    patterns.append(str(Path.home() / ".local" / "bin" / "claude"))
    found = [c for p in patterns for c in glob.glob(p) if os.path.exists(c)]
    found.sort(key=os.path.getmtime, reverse=True)  # newest version first
    return found[0] if found else None


def start_claude_login():
    """Launch the official `claude auth login` flow in its own window. It opens
    the browser, the user signs in to their own account, and it writes the
    standard credentials file that this app already reads. Returns (ok, error)."""
    exe = find_claude_binary()
    if not exe:
        return False, "no-claude"
    try:
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000010  # CREATE_NEW_CONSOLE
        subprocess.Popen([exe, "auth", "login"], **kwargs)
        return True, None
    except Exception:
        return False, "spawn-failed"


def run_claude_logout():
    """Sign out via the official `claude auth logout` (clears the local creds).
    Runs hidden and waits, then refreshes so the bars clear immediately."""
    exe = find_claude_binary()
    if not exe:
        return False, "no-claude"
    try:
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        subprocess.run([exe, "auth", "logout"], timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
    except Exception:
        return False, "spawn-failed"
    try:
        refresh_claude_usage()  # reflect the signed-out state without waiting
    except Exception:
        pass
    return True, None


# --------------------------------------------------------------------------
# welcome marker
# --------------------------------------------------------------------------

def read_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_state(**changes):
    state = read_state()
    state.update(changes)
    try:
        STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass


def is_welcomed():
    return bool(read_state().get("welcomed"))


def set_welcomed():
    write_state(welcomed=True)


def get_mini_settings():
    s = read_state()
    return {"enabled": bool(s.get("mini_enabled", True)),   # taskbar widget on by default
            "locked": bool(s.get("mini_locked", False)),
            "x": s.get("mini_x"), "y": s.get("mini_y")}


# --------------------------------------------------------------------------
# Codex limits: live via app-server, with a local-log-snapshot fallback
# --------------------------------------------------------------------------

_codex_cache = {"bars": [], "plan": None, "note": "from Codex CLI logs",
                "hint": "", "fetched": 0.0}
_codex_lock = threading.Lock()


def _codex_from_logs():
    """Fallback view built from the last rate_limits snapshot in the local logs."""
    rl, ts = _scan_codex_rate_limits()
    bars = codex_limit_bars(rl)
    if bars and ts:
        age = fmt_ago((datetime.now(timezone.utc) - ts).total_seconds())
        return bars, f"as of last Codex run · {age}", ""
    if bars:
        return bars, "from Codex CLI logs", ""
    return [], "from Codex CLI logs", "no-data"


def refresh_codex_usage(prefer_live=True):
    bars = note = hint = plan = None
    if prefer_live:
        snap, plan_label, err = fetch_codex_usage()
        if err is None and snap:
            bars = codex_limit_bars(snap)
            if bars:
                note, hint, plan = "live · updated just now", "", plan_label
    if bars is None:                       # no binary / failed → local logs
        bars, note, hint = _codex_from_logs()
    if plan is None:
        plan = read_codex_plan()
    with _codex_lock:
        _codex_cache.update(bars=bars, note=note, hint=hint, plan=plan,
                            fetched=time.time())


def codex_usage_loop():
    # seed instantly from local logs so the card isn't empty while the first
    # (slower) live read spins up, then poll live limits gently.
    try:
        refresh_codex_usage(prefer_live=False)
    except Exception:
        pass
    while True:
        try:
            refresh_codex_usage(prefer_live=True)
        except Exception:
            pass
        time.sleep(CODEX_POLL_SECONDS)


def get_codex_view():
    """(bars, note, hint, plan) for the Codex card. Uses the background cache
    once populated; otherwise computes a quick local-log view synchronously so
    direct callers (tests, first paint) still work without spawning anything."""
    with _codex_lock:
        if _codex_cache["fetched"]:
            c = dict(_codex_cache)
            return c["bars"], c["note"], c["hint"], c["plan"]
    bars, note, hint = _codex_from_logs()
    return bars, note, hint, read_codex_plan()


# --------------------------------------------------------------------------
# build cards
# --------------------------------------------------------------------------

def _usage_rows(events, now):
    today = now.astimezone().date()
    yest = today - timedelta(days=1)
    cutoff30 = now - timedelta(days=30)

    def bucket(pred):
        toks = 0
        for e in events:
            if pred(e["ts"]):
                toks += e["input"] + e["output"] + e["cache_w"] + e["cache_r"]
        return {"tokens": toks}

    return {
        "Today": bucket(lambda ts: ts is not None and ts.astimezone().date() == today),
        "Yesterday": bucket(lambda ts: ts is not None and ts.astimezone().date() == yest),
        "Last 30 Days": bucket(lambda ts: ts is not None and ts >= cutoff30),
    }


def build_cards():
    now = datetime.now(timezone.utc)
    claude = parse_claude()
    codex = parse_codex()
    climits, cstatus = get_claude_limits()

    # Codex bars are read LIVE via the app-server when available (see
    # get_codex_view / codex_usage_loop), falling back to the last local-log
    # snapshot (labelled with its age) when the codex binary isn't installed.
    codex_bars, codex_note, codex_hint, codex_plan = get_codex_view()

    cards = [
        {"name": "Claude", "glyph": "claude", "found": CLAUDE_LOG_DIR.exists(),
         "plan": read_claude_plan(), "signed_in": bool(read_claude_token()[0]),
         "limits": climits, "hint": cstatus, "usage": _usage_rows(claude, now),
         "limit_note": "all Claude apps · chat, Cowork, Code, CLI",
         "token_note": "Claude Code on this PC only"},
        {"name": "Codex CLI", "glyph": "codex",
         "found": any(d.exists() for d in CODEX_LOG_DIRS), "plan": codex_plan,
         "limits": codex_bars, "hint": codex_hint,
         "usage": _usage_rows(codex, now),
         "limit_note": codex_note, "token_note": "this PC only"},
    ]
    with _claude_lock:
        fetched = _claude_cache["fetched"]
    next_secs = int(max(0, fetched + CLAUDE_POLL_SECONDS - time.time())) if fetched else CLAUDE_POLL_SECONDS
    return {"generated": now.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            "claude_next": next_secs,
            "first_run": not is_welcomed(), "cards": cards}


# --------------------------------------------------------------------------
# web view (light-theme widget)
# --------------------------------------------------------------------------

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<title>AI Usage</title>
<style>
 :root{
   --bg:#f6f7f9;--white:#ffffff;--ink:#0f172a;--soft:#475569;--muted:#64748b;--faint:#94a3b8;
   --line:#e9ecf1;--track:#e8eaee;--blue:#3b82f6;--green:#12b886;--claude:#d97757;
   --cbg:#fcebe4;--cfg:#c26a45;--gbg:#ddf3ec;--gfg:#0e8a67;
 }
 *{box-sizing:border-box}
 html,body{height:100%;overflow:hidden}
 body{margin:0;background:var(--bg);color:var(--ink);
      font-family:'Segoe UI',system-ui,-apple-system,Arial,sans-serif;
      font-size:14px;-webkit-user-select:none;user-select:none}
 .win{height:100vh;display:grid;grid-template-rows:56px minmax(0,1fr) 58px}
 header{display:flex;align-items:center;gap:6px;padding:0 12px 0 18px;background:var(--bg)}
 .brand{display:flex;align-items:center;gap:10px;padding:6px 4px;cursor:default}
 .brand svg{display:block}
 .brand .t{font-size:19px;font-weight:700;letter-spacing:-.2px}
 .dragspace{flex:1;align-self:stretch}
 .iconbtn{width:36px;height:36px;border:0;background:transparent;border-radius:9px;
          display:grid;place-items:center;color:var(--muted);cursor:pointer;padding:0}
 .iconbtn:hover{background:#e9edf2;color:var(--soft)}
 main{background:var(--white);display:flex;min-height:0;border-top:1px solid var(--line);
      border-bottom:1px solid var(--line)}
 .col{flex:1 1 50%;min-width:0;padding:0 20px 16px;display:flex;flex-direction:column;
      align-items:center;container-type:size}
 .col+.col{border-left:1px solid var(--line)}
 .chead{display:flex;align-items:center;gap:9px;padding-top:22px;margin-bottom:12px}
 .cbody{width:100%;flex:1;min-height:0;display:flex;flex-direction:column;
        align-items:center;justify-content:safe center}
 .chead .logo{display:grid;place-items:center}
 .logo svg{width:clamp(22px,9.4cqw,36px);height:auto;display:block}
 .chead .nm{font-size:clamp(15px,6.4cqw,23px);font-weight:700;letter-spacing:-.2px}
 .badge{font-size:clamp(9.5px,3.4cqw,13px);font-weight:600;border-radius:999px;padding:3px 10px}
 .badge.claude{background:var(--cbg);color:var(--cfg)}
 .badge.gpt{background:var(--gbg);color:var(--gfg)}
 .ringwrap{position:relative;width:min(62cqw,52cqh,300px);aspect-ratio:1/1;margin-top:2px}
 .ringwrap svg{display:block;width:100%;height:100%}
 .rcenter{position:absolute;inset:0;display:flex;flex-direction:column;
          align-items:center;justify-content:center;gap:2px}
 .pct{font-size:clamp(20px,14.5cqw,56px);font-weight:700;letter-spacing:-1.5px;line-height:1}
 .left{font-size:clamp(10px,4.7cqw,18px);color:var(--muted)}
 .caption{margin-top:14px;font-size:clamp(10px,3.9cqw,15px);color:var(--faint)}
 .reset{text-align:center;color:var(--muted);font-size:clamp(11px,4.4cqw,17px);margin-top:3px;line-height:1.35}
 .reset b{display:block;color:var(--ink);font-size:clamp(13px,5.6cqw,22px);font-weight:700;letter-spacing:-.2px}
 .sec{margin-top:15px;width:min(60cqw,260px)}
 .sec .sbar{height:5px;border-radius:4px;background:var(--track);overflow:hidden}
 .sec .sfill{height:100%;border-radius:4px}
 .sec .st{margin-top:6px;text-align:center;font-size:clamp(9.5px,3.6cqw,14px);color:var(--muted)}
 .msg{padding:26px 12px;text-align:center;color:var(--muted);line-height:1.5;font-size:clamp(11.5px,4.2cqw,16px)}
 .cbtn{margin-top:12px;background:var(--blue);border:0;color:#fff;font-size:clamp(11px,4.2cqw,15px);
       font-weight:600;border-radius:10px;padding:9px 18px;cursor:pointer}
 .cbtn:disabled{opacity:.7;cursor:default}
 footer{display:flex;align-items:center;justify-content:space-between;
        padding:0 20px;color:var(--muted);font-size:14.5px;background:var(--bg)}
 .upd{display:flex;align-items:center;gap:9px}
 .upd b{color:var(--soft);font-weight:600}
 .sp{display:flex;align-items:center;gap:8px}
 .panel{position:fixed;top:60px;right:12px;width:335px;background:#fff;border:1px solid #e6eaf0;
        border-radius:14px;box-shadow:0 18px 40px rgba(15,23,42,.16);padding:15px 16px;z-index:60;
        display:none}
 .panel.open{display:block}
 .ptitle{font-size:15px;font-weight:700;margin-bottom:10px;display:flex;
         justify-content:space-between;align-items:center}
 .prow{display:flex;justify-content:space-between;gap:10px;padding:5px 0;font-size:12.5px;color:var(--soft)}
 .prow .k{color:var(--muted)}
 .psep{height:1px;background:var(--line);margin:10px 0}
 .pbtn{border:0;border-radius:9px;padding:8px 12px;font-size:12.5px;font-weight:600;cursor:pointer}
 .pbtn.blue{background:#e8f0fe;color:#2563eb}
 .pbtn.blue:hover{background:#dbe7fd}
 .pbtn.red{background:#fdecea;color:#c0392b}
 .pbtn.red:hover{background:#fbdcd8}
 .pbtn.grey{background:#eef1f5;color:var(--soft)}
 .pbtn.grey:hover{background:#e4e8ee}
 .foot2{margin-top:10px;color:var(--faint);font-size:11px;line-height:1.4}
 .welcome{position:fixed;inset:0;background:rgba(246,247,249,.97);z-index:80;
          display:none;align-items:center;justify-content:center;padding:22px}
 .welcome.open{display:flex}
 .wcard{background:#fff;border:1px solid #e9ecf1;border-radius:16px;
        box-shadow:0 18px 44px rgba(15,23,42,.12);padding:22px 22px 18px;max-width:460px}
 .wcard .wt{font-size:17px;font-weight:700;margin-bottom:9px}
 .wcard ul{margin:0 0 13px;padding-left:17px}
 .wcard li{margin-bottom:6px;font-size:13px;line-height:1.45;color:var(--soft)}
 .wcard .cbtn{width:100%;margin-top:2px}
</style></head><body>
<div class="win">
 <header>
  <div class="brand pywebview-drag-region">
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none"><g fill="#64748b">
      <rect x="3.2" y="13.6" width="4.4" height="8.4" rx="2.2"/>
      <rect x="9.8" y="8.8" width="4.4" height="13.2" rx="2.2"/>
      <rect x="16.4" y="3.2" width="4.4" height="18.8" rx="2.2"/></g></svg>
    <span class="t">AI Usage</span>
  </div>
  <div class="dragspace pywebview-drag-region"></div>
  <button class="iconbtn" id="bgear" title="Settings">
    <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
  </button>
  <button class="iconbtn" id="bmin" title="Minimize">
    <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M5 12h14"/></svg>
  </button>
  <button class="iconbtn" id="bclose" title="Close to tray">
    <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>
  </button>
 </header>
 <main id="cols">
   <div class="col" style="justify-content:center"><div class="msg">Loading…</div></div>
 </main>
 <footer>
  <div class="upd">
    <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 7.5v4.8l3.2 1.9"/></svg>
    <span>Updated <b id="gen">—</b></span>
  </div>
  <div class="sp"><span>Stay productive</span>
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2.6c2.6 2.1 4 5.6 4 9.1 0 2.3-.7 4.5-1.9 6.2h-4.2C8.7 16.2 8 14 8 11.7c0-3.5 1.4-7 4-9.1z"/><circle cx="12" cy="10.2" r="1.7"/><path d="M8.3 15.2 5.6 18.9h2.8M15.7 15.2l2.7 3.7h-2.8"/><path d="M12 18.4v2.6"/></svg>
  </div>
 </footer>
 <div class="panel" id="panel"></div>
 <div class="welcome" id="welcome"></div>
</div>
<script>
 const ftok=n=>n>=1e9?(n/1e9).toFixed(1)+'B':n>=1e6?(n/1e6).toFixed(1)+'M':n>=1e3?(n/1e3).toFixed(1)+'k':''+n;
 const NOTE={'no-login':'Sign in to show your Claude usage limits.',
   'expired':'Claude session expired — connect again to refresh it.',
   'rate-limited':'Usage check is rate-limited; it will retry shortly.',
   'network':'Could not reach the usage service.',
   'init':'Loading limits…','ok':'No active usage window right now.',
   'no-data':'No usage snapshot yet — run a Codex session, then this fills in.'};
 const LOGINERR={'no-claude':'Claude not found. Install the Claude desktop app, then click Connect again.',
   'spawn-failed':'Could not start sign-in. Please try again.'};
 const CLAUDE_SVG='<svg width="30" height="30" viewBox="0 0 24 24" fill="#d97757"><path d="m4.7144 15.9555 4.7174-2.6471.079-.2307-.079-.1275h-.2307l-.7893-.0486-2.6956-.0729-2.3375-.0971-2.2646-.1214-.5707-.1215-.5343-.7042.0546-.3522.4797-.3218.686.0608 1.5179.1032 2.2767.1578 1.6514.0972 2.4468.255h.3886l.0546-.1579-.1336-.0971-.1032-.0972L6.973 9.8356l-2.55-1.6879-1.3356-.9714-.7225-.4918-.3643-.4614-.1578-1.0078.6557-.7225.8803.0607.2246.0607.8925.686 1.9064 1.4754 2.4893 1.8336.3643.3035.1457-.1032.0182-.0728-.164-.2733-1.3539-2.4467-1.445-2.4893-.6435-1.032-.17-.6194c-.0607-.255-.1032-.4674-.1032-.7285L6.287.1335 6.6997 0l.9957.1336.419.3642.6192 1.4147 1.0018 2.2282 1.5543 3.0296.4553.8985.2429.8318.091.255h.1579v-.1457l.1275-1.706.2368-2.0947.2307-2.6957.0789-.7589.3764-.9107.7468-.4918.5828.2793.4797.686-.0668.4433-.2853 1.8517-.5586 2.9021-.3643 1.9429h.2125l.2429-.2429.9835-1.3053 1.6514-2.0643.7286-.8196.85-.9046.5464-.4311h1.0321l.759 1.1293-.34 1.1657-1.0625 1.3478-.8804 1.1414-1.2628 1.7-.7893 1.36.0729.1093.1882-.0183 2.8535-.607 1.5421-.2794 1.8396-.3157.8318.3886.091.3946-.3278.8075-1.967.4857-2.3072.4614-3.4364.8136-.0425.0304.0486.0607 1.5482.1457.6618.0364h1.621l3.0175.2247.7892.522.4736.6376-.079.4857-1.2142.6193-1.6393-.3886-3.825-.9107-1.3113-.3279h-.1822v.1093l1.0929 1.0686 2.0035 1.8092 2.5075 2.3314.1275.5768-.3218.4554-.34-.0486-2.2039-1.6575-.85-.7468-1.9246-1.621h-.1275v.17l.4432.6496 2.3436 3.5214.1214 1.0807-.17.3521-.6071.2125-.6679-.1214-1.3721-1.9246L14.38 17.959l-1.1414-1.9428-.1397.079-.674 7.2552-.3156.3703-.7286.2793-.6071-.4614-.3218-.7468.3218-1.4753.3886-1.9246.3157-1.53.2853-1.9004.17-.6314-.0121-.0425-.1397.0182-1.4328 1.9672-2.1796 2.9446-1.7243 1.8456-.4128.164-.7164-.3704.0667-.6618.4008-.5889 2.386-3.0357 1.4389-1.882.929-1.0868-.0062-.1579h-.0546l-6.3385 4.1164-1.1293.1457-.4857-.4554.0608-.7467.2307-.2429 1.9064-1.3114Z"/></svg>';
 const OPENAI_SVG='<svg width="31" height="31" viewBox="0 0 256 260" fill="#10a37f"><path d="M239.183914,106.202783 C245.054304,88.5242096 243.02228,69.1733805 233.607599,53.0998864 C219.451678,28.4588021 190.999703,15.7836129 163.213007,21.739505 C147.554077,4.32145883 123.794909,-3.42398554 100.87901,1.41873898 C77.9631105,6.26146349 59.3690093,22.9572536 52.0959621,45.2214219 C33.8436494,48.9644867 18.0901721,60.392749 8.86672513,76.5818033 C-5.443491,101.182962 -2.19544431,132.215255 16.8986662,153.320094 C11.0060865,170.990656 13.0197283,190.343991 22.4238231,206.422991 C36.5975553,231.072344 65.0680342,243.746566 92.8695738,237.783372 C105.235639,251.708249 123.001113,259.630942 141.623968,259.52692 C170.105359,259.552169 195.337611,241.165718 204.037777,214.045661 C222.28734,210.296356 238.038489,198.869783 247.267014,182.68528 C261.404453,158.127515 258.142494,127.262775 239.183914,106.202783 L239.183914,106.202783 Z M141.623968,242.541207 C130.255682,242.559177 119.243876,238.574642 110.519381,231.286197 L112.054146,230.416496 L163.724595,200.590881 C166.340648,199.056444 167.954321,196.256818 167.970781,193.224005 L167.970781,120.373788 L189.815614,133.010026 C190.034132,133.121423 190.186235,133.330564 190.224885,133.572774 L190.224885,193.940229 C190.168603,220.758427 168.442166,242.484864 141.623968,242.541207 Z M37.1575749,197.93062 C31.456498,188.086359 29.4094818,176.546984 31.3766237,165.342426 L32.9113895,166.263285 L84.6329973,196.088901 C87.2389349,197.618207 90.4682717,197.618207 93.0742093,196.088901 L156.255402,159.663793 L156.255402,184.885111 C156.243557,185.149771 156.111725,185.394602 155.89729,185.550176 L103.561776,215.733903 C80.3054953,229.131632 50.5924954,221.165435 37.1575749,197.93062 Z M23.5493181,85.3811273 C29.2899861,75.4733097 38.3511911,67.9162648 49.1287482,64.0478825 L49.1287482,125.438515 C49.0891492,128.459425 50.6965386,131.262556 53.3237748,132.754232 L116.198014,169.025864 L94.3531808,181.662102 C94.1132325,181.789434 93.8257461,181.789434 93.5857979,181.662102 L41.3526015,151.529534 C18.1419426,138.076098 10.1817681,108.385562 23.5493181,85.125333 L23.5493181,85.3811273 Z M203.0146,127.075598 L139.935725,90.4458545 L161.7294,77.8607748 C161.969348,77.7334434 162.256834,77.7334434 162.496783,77.8607748 L214.729979,108.044502 C231.032329,117.451747 240.437294,135.426109 238.871504,154.182739 C237.305714,172.939368 225.050719,189.105572 207.414262,195.67963 L207.414262,134.288998 C207.322521,131.276867 205.650697,128.535853 203.0146,127.075598 Z M224.757116,94.3850867 L223.22235,93.4642272 L171.60306,63.3828173 C168.981293,61.8443751 165.732456,61.8443751 163.110689,63.3828173 L99.9806554,99.8079259 L99.9806554,74.5866077 C99.9533004,74.3254088 100.071095,74.0701869 100.287609,73.9215426 L152.520805,43.7889738 C168.863098,34.3743518 189.174256,35.2529043 204.642579,46.0434841 C220.110903,56.8340638 227.949269,75.5923959 224.757116,94.1804513 L224.757116,94.3850867 Z M88.0606409,139.097931 L66.2158076,126.512851 C65.9950399,126.379091 65.8450965,126.154176 65.8065367,125.898945 L65.8065367,65.684966 C65.8314495,46.8285367 76.7500605,29.6846032 93.8270852,21.6883055 C110.90411,13.6920079 131.063833,16.2835462 145.5632,28.338998 L144.028434,29.2086986 L92.3579852,59.0343142 C89.7419327,60.5687513 88.1282597,63.3683767 88.1117998,66.4011901 L88.0606409,139.097931 Z M99.9294965,113.5185 L128.06687,97.3011417 L156.255402,113.5185 L156.255402,145.953218 L128.169187,162.170577 L99.9806554,145.953218 L99.9294965,113.5185 Z"/></svg>';
 const C=2*Math.PI*86;
 let lastData=null,nextSecs=null,sized=false;
 const $=id=>document.getElementById(id);
 const panel=$('panel'),welcome=$('welcome');
 function fmtTime(s){const m=String(s||'').match(/(\d{1,2}:\d{2})(?::\d{2})?\s*$/);return m?m[1]:'—';}
 function fmtCd(s){s=Math.max(0,s);const m=Math.floor(s/60),ss=s%60;return m>0?(m+'m '+ss+'s'):(ss+'s');}
 function ringSVG(pct,color){
   const off=C*(1-Math.max(0,Math.min(100,pct))/100);
   return '<div class="ringwrap"><svg viewBox="0 0 200 200">'+
     '<circle cx="100" cy="100" r="86" fill="none" stroke="#e8eaee" stroke-width="19"/>'+
     '<circle cx="100" cy="100" r="86" fill="none" stroke="'+color+'" stroke-width="19" stroke-linecap="round" '+
     'stroke-dasharray="'+C.toFixed(2)+'" stroke-dashoffset="'+off.toFixed(2)+'" transform="rotate(-90 100 100)"/>'+
     '</svg><div class="rcenter"><div class="pct">'+pct+'%</div><div class="left">left</div></div></div>';
 }
 function secRow(l,color){
   return '<div class="sec"><div class="sbar"><div class="sfill" style="width:'+l.percent_left+'%;background:'+color+'"></div></div>'+
     '<div class="st">'+l.label+' · '+l.percent_left+'% left'+(l.resets?(' · '+l.resets):'')+'</div></div>';
 }
 function colHTML(c){
   const isClaude=c.glyph==='claude';
   const color=isClaude?'#3b82f6':'#12b886';
   const name=isClaude?'Claude':'ChatGPT';
   let badge=c.plan||'';if(!isClaude)badge=badge.replace(/^ChatGPT\s+/i,'');
   let h='<div class="col"><div class="chead">'+(isClaude?CLAUDE_SVG:OPENAI_SVG)+
     '<span class="nm">'+name+'</span>'+(badge?('<span class="badge '+(isClaude?'claude':'gpt')+'">'+badge+'</span>'):'')+'</div><div class="cbody">';
   const ls=c.limits||[];
   let main=null,second=null;
   if(isClaude){main=ls.find(l=>/session/i.test(l.label))||ls[0]||null;
     if(main)second=ls.find(l=>l!==main)||null;}
   else{main=ls[0]||null;second=ls[1]||null;}
   if(main){
     h+=ringSVG(main.percent_left,color);
     h+='<div class="caption">'+(isClaude?'5h session':'weekly')+'</div>';
     h+='<div class="reset">Resets in<b>'+(main.resets||'—')+'</b></div>';
     if(second)h+=secRow(second,color);
   }else{
     h+='<div class="msg">'+(NOTE[c.hint]||'Limits unavailable.')+'</div>';
     if(isClaude&&(c.hint==='no-login'||c.hint==='expired')){
       h+='<button class="cbtn" onclick="connectClaude(this)">Connect Claude</button>';
     }
   }
   return h+'</div></div>';
 }
 async function connectClaude(btn){
   btn.disabled=true;btn.textContent='Opening sign-in…';
   try{
     const r=await (await fetch('/login')).json();
     if(r.ok){btn.textContent='Finish in the window that opened — limits appear here automatically.';}
     else{btn.disabled=false;btn.textContent=(LOGINERR[r.error]||'Could not start sign-in. Try again.');}
   }catch(e){btn.disabled=false;btn.textContent='Could not start sign-in. Try again.';}
 }
 async function logoutClaude(el){
   el.textContent='Signing out…';
   try{await fetch('/logout');}catch(e){}
   load();
 }
 function openWelcome(){
   welcome.innerHTML='<div class="wcard"><div class="wt">Welcome to AI Usage</div>'+
     '<ul><li><b>Data stays on this computer.</b> The only network call is your own usage check.</li>'+
     '<li>Claude limits cover <b>all</b> your Claude usage — chat, Cowork, Code and CLI.</li>'+
     '<li>Token counts come from local logs on this PC only.</li></ul>'+
     '<button class="cbtn" onclick="dismissWelcome()">Got it</button></div>';
   welcome.classList.add('open');
 }
 async function dismissWelcome(){
   try{await fetch('/seen');}catch(e){}
   welcome.classList.remove('open');load();
 }
 function panelHTML(){
   if(!lastData)return '';
   let h='<div class="ptitle">Settings<button class="pbtn grey" onclick="closePanel()">Close</button></div>';
   h+='<div class="prow"><span class="k">Last update</span><span>'+fmtTime(lastData.generated)+'</span></div>';
   h+='<div class="prow"><span class="k">Next refresh in</span><span id="pcd">'+(nextSecs!=null?fmtCd(nextSecs):'—')+'</span></div>';
   h+='<div class="prow" style="justify-content:flex-start"><button class="pbtn blue" onclick="load()">Refresh now</button></div>';
   h+='<div class="psep"></div>';
   h+='<div class="prow"><span class="k" style="font-weight:600">Tokens used (this PC)</span></div>';
   for(const c of lastData.cards){
     const nm=c.glyph==='claude'?'Claude':'ChatGPT';const u=c.usage||{};
     const part=k=>{const x=u[k];return x?(x.tokens>0?ftok(x.tokens):'—'):'—';};
     h+='<div class="prow"><span class="k">'+nm+'</span><span>'+part('Today')+' today · '+part('Yesterday')+' yest · '+part('Last 30 Days')+' 30d</span></div>';
   }
   h+='<div class="psep"></div>';
   const cl=lastData.cards.find(c=>c.glyph==='claude');
   if(cl&&cl.signed_in)h+='<button class="pbtn red" onclick="logoutClaude(this)">Sign out of Claude</button>';
   h+='<div class="foot2">Only your own usage checks go online — everything else stays on this computer.</div>';
   return h;
 }
 function renderPanel(){panel.innerHTML=panelHTML();}
 function closePanel(){panel.classList.remove('open');}
 function togglePanel(){panel.classList.toggle('open');if(panel.classList.contains('open'))renderPanel();}
 function tickCd(){
   if(nextSecs!=null){nextSecs=Math.max(0,nextSecs-1);
     const el=$('pcd');if(el)el.textContent=fmtCd(nextSecs);}
 }
 function fitWidget(){
   try{
     const api=window.pywebview&&window.pywebview.api;
     if(api&&api.set_height&&!sized){sized=true;api.set_height(556);}
   }catch(e){}
 }
 async function load(){
   try{
     const d=await (await fetch('/data')).json();
     lastData=d;
     $('gen').textContent=fmtTime(d.generated);
     if(typeof d.claude_next==='number')nextSecs=d.claude_next;
     if(d.first_run){openWelcome();}
     else{$('cols').innerHTML=d.cards.map(colHTML).join('');}
     if(panel.classList.contains('open'))renderPanel();
     setTimeout(fitWidget,60);
   }catch(e){}
 }
 $('bmin').onclick=()=>{try{window.pywebview.api.minimize_win();}catch(e){}};
 $('bclose').onclick=()=>{try{window.pywebview.api.hide_win();}catch(e){}};
 $('bgear').onclick=togglePanel;
 document.addEventListener('mousedown',e=>{
   if(panel.classList.contains('open')&&!panel.contains(e.target)&&!e.target.closest('#bgear'))closePanel();
 });
 window.addEventListener('pywebviewready',function(){setTimeout(fitWidget,60);});
 load();setInterval(load,__REFRESH__000);setInterval(tickCd,1000);
</script></body></html>"""


# Compact always-on-top gadget: two lines (Claude% / Codex% session) that expand
# to the full view on hover, with drag + lock. Talks to a MiniController js_api.
PAGE_MINI = r"""<!doctype html><html><head><meta charset="utf-8">
<title>mini</title>
<style>
 :root{--bg:#eceef1;--card:#f5f6f8;--ink:#1f2330;--muted:#8a93a2;
       --reset:#a98b8b;--track:#d7dbe2;--fill:#3b82f6;--line:#e6e8ec}
 *{box-sizing:border-box}
 html,body{margin:0;height:100vh;overflow:hidden;background:transparent;
   font-family:'Segoe UI',system-ui,Arial,sans-serif;user-select:none}
 body{position:relative}
 /* full flyout panel - opens ABOVE the bar on hover (bottom set to bar height in JS) */
 #full{position:absolute;left:0;right:0;top:0;bottom:__BARH__px;overflow:auto;display:none;
   background:var(--bg);color:var(--ink);border-radius:10px 10px 0 0;padding:8px 10px;
   box-shadow:0 -2px 14px rgba(0,0,0,.28)}
 body.open #full{display:block}
 .cap{font-weight:600;font-size:10px;color:var(--muted);text-transform:uppercase;
   letter-spacing:.3px;margin:0 0 4px}
 .prov{margin-bottom:8px}
 .ptitle{display:flex;align-items:center;gap:6px;padding:2px 2px 5px}
 .pname{font-weight:700;font-size:13px;color:var(--ink)}
 .plan{background:#e2e8f5;color:#4b5b78;font-size:9px;font-weight:600;padding:1px 6px;border-radius:8px}
 .card{background:var(--card);border-radius:10px;padding:8px 10px}
 .limit{margin-bottom:6px}
 .ltitle{font-weight:600;font-size:11px;margin-bottom:2px}
 .bar{height:5px;background:var(--track);border-radius:5px;overflow:hidden}
 .fill{height:100%;background:var(--fill)}
 .lmeta{display:flex;justify-content:space-between;margin-top:2px;font-size:10px}
 .lreset{color:var(--reset)}
 .sep{height:1px;background:var(--line);margin:6px 0}
 .urow{display:flex;justify-content:space-between;font-size:11px;padding:1px 0}
 .uval{color:var(--muted)}
 .note{color:var(--muted);font-size:10px;padding:2px 0}
 /* compact bar - fills the whole window at rest (so it stays visible whatever
    height Windows actually gives us); shrinks to the bottom strip when open */
 #compact{position:absolute;left:0;right:0;top:0;bottom:0;
   background:rgba(30,32,37,.94);color:#fff;border-radius:6px;
   padding:2px 8px;display:flex;flex-direction:column;justify-content:center;gap:2px;
   cursor:move;box-shadow:0 1px 6px rgba(0,0,0,.35)}
 body.open #compact{top:auto;height:__BARH__px}
 #compact.locked{cursor:default}
 .mrow{display:flex;align-items:center;gap:6px;font-size:10px;font-weight:600;line-height:1.15}
 .mname{width:42px;color:#aeb6c2}
 .mbar{flex:1;height:4px;background:rgba(255,255,255,.16);border-radius:4px;overflow:hidden}
 .mfill{height:100%;background:#4c8dff}
 .mpct{width:30px;text-align:right;color:#fff}
</style></head><body>
 <div id="full"></div>
 <div id="compact">
   <div class="mrow"><span class="mname">Claude</span>
     <span class="mbar"><span class="mfill" id="cf" style="width:0%"></span></span>
     <span class="mpct" id="cp">--</span></div>
   <div class="mrow"><span class="mname">Codex</span>
     <span class="mbar"><span class="mfill" id="xf" style="width:0%"></span></span>
     <span class="mpct" id="xp">--</span></div>
 </div>
<script>
 const ftok=n=>n>=1e9?(n/1e9).toFixed(1)+'B':n>=1e6?(n/1e6).toFixed(1)+'M':n>=1e3?(n/1e3).toFixed(1)+'k':''+n;
 const NOTE={'no-login':'Sign in (open the main window).','expired':'Session expired.',
   'rate-limited':'Rate-limited; retrying.','network':'No connection.','init':'Loading...',
   'ok':'No active window.','no-data':'Run a session to fill this in.'};
 function api(){ return (window.pywebview && window.pywebview.api) || null; }
 let expanded=false, dragging=false, sx=0, startLeft=0, dpr=1, pending=null, raf=0;
 function sessionPct(card){
   if(!card||!card.limits) return null;
   const s=card.limits.find(l=>/session/i.test(l.label));
   return s?s.percent_left:null;
 }
 function fullCard(c){
   let h='<div class="prov"><div class="ptitle"><span class="pname">'+c.name+'</span>'+
     (c.plan?('<span class="plan">'+c.plan+'</span>'):'')+'</div><div class="card">';
   if(c.limits&&c.limits.length){
     h+='<div class="cap">Usage limit</div>';
     for(const l of c.limits){
       h+='<div class="limit"><div class="ltitle">'+l.label+'</div>'+
          '<div class="bar"><div class="fill" style="width:'+l.percent_left+'%"></div></div>'+
          '<div class="lmeta"><span>'+l.percent_left+'% left</span><span class="lreset">'+
          (l.resets?('Resets in '+l.resets):'')+'</span></div></div>';
     }
     h+='<div class="sep"></div>';
   } else if(c.hint){ h+='<div class="note">'+(NOTE[c.hint]||'')+'</div>'; }
   h+='<div class="cap">Tokens used</div>';
   for(const k of ['Today','Yesterday','Last 30 Days']){
     const u=c.usage&&c.usage[k]; if(!u) continue;
     const v=(u.tokens>0)?(ftok(u.tokens)+' tokens'):'—';
     h+='<div class="urow"><span>'+k+'</span><span class="uval">'+v+'</span></div>';
   }
   return h+'</div></div>';
 }
 let lastData=null;
 async function load(){
   try{ lastData=await (await fetch('/data')).json(); }catch(e){ return; }
   const c=lastData.cards&&lastData.cards[0], x=lastData.cards&&lastData.cards[1];
   const cp=sessionPct(c), xp=sessionPct(x);
   document.getElementById('cp').textContent=cp==null?'--':cp+'%';
   document.getElementById('xp').textContent=xp==null?'--':xp+'%';
   document.getElementById('cf').style.width=(cp==null?0:cp)+'%';
   document.getElementById('xf').style.width=(xp==null?0:xp)+'%';
   if(expanded) document.getElementById('full').innerHTML=lastData.cards.map(fullCard).join('');
 }
 // The native side resizes this window on hover (cursor-driven); the page just
 // reacts to its own height to show/hide the full panel. No mouse events needed.
 function applySize(){
   const big = window.innerHeight > 100;
   if(big && !expanded){
     expanded=true;
     if(lastData) document.getElementById('full').innerHTML=lastData.cards.map(fullCard).join('');
     document.body.classList.add('open');
   } else if(!big && expanded){
     expanded=false;
     document.body.classList.remove('open');
   }
 }
 window.addEventListener('resize', applySize);
 setInterval(applySize, 150);
 const compact=document.getElementById('compact');
 compact.addEventListener('mousedown', async e=>{
   if(e.button!==0) return;
   const a=api(); if(!a) return;
   let r; try{ r=await a.begin_drag(); }catch(err){ return; }
   if(!r || r[2]) return;                 // locked (via tray) -> no drag
   expanded=false; document.body.classList.remove('open');
   dragging=true; sx=e.screenX; startLeft=r[0]; dpr=r[1]||1;
 });
 function flush(){ raf=0; if(pending!=null){ const a=api(); if(a) a.drag_to(pending); } }
 window.addEventListener('mousemove', e=>{
   if(!dragging) return;
   pending=startLeft+(e.screenX-sx)*dpr;
   if(!raf) raf=requestAnimationFrame(flush);
 });
 window.addEventListener('mouseup', e=>{
   if(!dragging) return; dragging=false;
   const a=api(); const x=startLeft+(e.screenX-sx)*dpr;
   if(a){ try{ a.end_drag(x); }catch(err){} }
 });
 window.addEventListener('pywebviewready', load);
 load(); setInterval(load,__REFRESH__000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/seen"):
            set_welcomed()
            body, ctype = b'{"ok":true}', "application/json"
        elif self.path.startswith("/login"):
            ok, err = start_claude_login()
            body = json.dumps({"ok": ok, "error": err}).encode("utf-8")
            ctype = "application/json"
        elif self.path.startswith("/logout"):
            ok, err = run_claude_logout()
            body = json.dumps({"ok": ok, "error": err}).encode("utf-8")
            ctype = "application/json"
        elif self.path.startswith("/data"):
            body = json.dumps(build_cards()).encode("utf-8")
            ctype = "application/json"
        elif self.path.startswith("/mini"):
            body = (PAGE_MINI.replace("__REFRESH__", str(REFRESH_SECONDS))
                             .replace("__BARH__", str(MINI_BAR_HEIGHT))).encode("utf-8")
            ctype = "text/html; charset=utf-8"
        else:
            body = (PAGE.replace("__REFRESH__", str(REFRESH_SECONDS))
                        .replace("__FRACTION__", str(WINDOW_FRACTION))).encode("utf-8")
            ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def find_port(start):
    for p in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return start


def run_claude_test():
    """Write a diagnostic report for the Claude usage connection."""
    token, src = read_claude_token()
    lines = ["AI Usage Monitor - Claude connection test",
             "=" * 44,
             f"Credentials file : {CLAUDE_CREDS}",
             f"File exists      : {CLAUDE_CREDS.exists()}",
             f"Token found      : {'yes (' + src + ')' if token else 'NO'}",
             f"Endpoint         : {CLAUDE_USAGE_URL}",
             f"User-Agent       : {CLAUDE_UA}", ""]
    if token:
        usage, err = fetch_claude_usage()
        if usage is not None:
            lines.append("RESULT: success. Raw response:")
            lines.append(json.dumps(usage, indent=2))
            lines.append("")
            lines.append("Parsed bars:")
            lines.append(json.dumps(claude_usage_to_bars(usage), indent=2))
        else:
            lines.append(f"RESULT: failed ({err}).")
            if err == "expired":
                lines.append("Your token is stale. Open Claude Code and send a message, then retry.")
            elif err == "rate-limited":
                lines.append("Rate-limited. Wait a few minutes and retry; do not run this repeatedly.")
    else:
        lines.append("RESULT: no token. Run `claude` once and log in, then retry.")
    report = "\n".join(lines)
    out = Path.home() / "usage_monitor_claude_test.txt"
    try:
        out.write_text(report, encoding="utf-8")
    except Exception:
        pass
    print(report)
    try:
        os.startfile(str(out))  # noqa
    except Exception:
        pass


def primary_screen_height():
    """Primary screen height in logical pixels (~CSS px). 0 if it can't be read."""
    return primary_screen_size()[1]


def primary_screen_size():
    """(width, height) of the primary screen in logical px, or (0, 0)."""
    try:
        import ctypes
        u = ctypes.windll.user32
        return int(u.GetSystemMetrics(0)), int(u.GetSystemMetrics(1))
    except Exception:
        return 0, 0


def resource_path(name):
    """Path to a bundled resource, working both from source and the PyInstaller exe."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


MINI_TITLE = "AI Usage (mini)"


# --------------------------------------------------------------------------
# Win32 layer for docking the mini gadget onto the taskbar (all PHYSICAL px, so
# it's DPI-correct: we work in the same pixel space as the taskbar itself and
# never mix with pywebview's logical coordinates).
# --------------------------------------------------------------------------

_WIN = None
if os.name == "nt":
    try:
        import ctypes
        from ctypes import wintypes
        _WIN = ctypes.WinDLL("user32", use_last_error=True)
        _WIN.FindWindowW.restype = wintypes.HWND
        _WIN.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        _WIN.GetWindowRect.restype = wintypes.BOOL
        _WIN.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        _WIN.SetWindowPos.restype = wintypes.BOOL
        _WIN.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        _LONG_PTR = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long
        _GETL = getattr(_WIN, "GetWindowLongPtrW", _WIN.GetWindowLongW)
        _SETL = getattr(_WIN, "SetWindowLongPtrW", _WIN.SetWindowLongW)
        _GETL.restype = _LONG_PTR
        _GETL.argtypes = [wintypes.HWND, ctypes.c_int]
        _SETL.restype = _LONG_PTR
        _SETL.argtypes = [wintypes.HWND, ctypes.c_int, _LONG_PTR]
        _WIN.SetWindowTextW.restype = wintypes.BOOL
        _WIN.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
        _WIN.GetCursorPos.restype = wintypes.BOOL
        _WIN.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        _WIN.ShowWindow.restype = wintypes.BOOL
        _WIN.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        try:
            _WIN.GetDpiForWindow.restype = wintypes.UINT
            _WIN.GetDpiForWindow.argtypes = [wintypes.HWND]
        except Exception:
            pass
    except Exception:
        _WIN = None

# ---- single instance (Windows): a second launch must not start a duplicate
# app/tray icon — it pokes the running instance to show its window, then exits.
_K32 = None
_single_mutex = None      # held (never closed) for the whole process lifetime
_show_event = None
if os.name == "nt":
    try:
        import ctypes
        _K32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _K32.CreateMutexW.restype = ctypes.c_void_p
        _K32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        _K32.CreateEventW.restype = ctypes.c_void_p
        _K32.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_wchar_p]
        _K32.OpenEventW.restype = ctypes.c_void_p
        _K32.OpenEventW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
        _K32.SetEvent.restype = ctypes.c_int
        _K32.SetEvent.argtypes = [ctypes.c_void_p]
        _K32.WaitForSingleObject.restype = ctypes.c_uint32
        _K32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        _K32.CloseHandle.restype = ctypes.c_int
        _K32.CloseHandle.argtypes = [ctypes.c_void_p]
    except Exception:
        _K32 = None

_MUTEX_NAME = "Local\\AIUsageMonitor_SingleInstance"
_SHOW_EVENT_NAME = "Local\\AIUsageMonitor_ShowMain"
_ERROR_ALREADY_EXISTS = 183
_EVENT_MODIFY_STATE = 0x0002


def other_instance_running():
    """First launch claims a named mutex and creates the show-event, returning
    False. A later launch finds the mutex taken, signals the running instance
    to show its main window, and returns True (caller should just exit)."""
    global _single_mutex, _show_event
    if not _K32:
        return False
    _single_mutex = _K32.CreateMutexW(None, 0, _MUTEX_NAME)
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        ev = _K32.OpenEventW(_EVENT_MODIFY_STATE, 0, _SHOW_EVENT_NAME)
        if ev:
            _K32.SetEvent(ev)
            _K32.CloseHandle(ev)
        return True
    _show_event = _K32.CreateEventW(None, 0, 0, _SHOW_EVENT_NAME)  # auto-reset
    return False


def show_request_watcher(window):
    """Daemon thread in the running instance: each time a second launch signals
    the show-event, bring the main window back (same as tray -> Open)."""
    while _K32 and _show_event:
        _K32.WaitForSingleObject(_show_event, 0xFFFFFFFF)
        if _quitting:
            return
        try:
            window.show()
            window.restore()
        except Exception:
            pass


_HWND_TOPMOST = -1
_SWP_NOACTIVATE, _SWP_SHOWWINDOW = 0x0010, 0x0040
_SWP_FRAMECHANGED, _SWP_NOMOVE, _SWP_NOSIZE, _SWP_NOZORDER = 0x0020, 0x0002, 0x0001, 0x0004
_GWL_STYLE, _GWL_EXSTYLE = -16, -20
_WS_EX_TOOLWINDOW, _WS_EX_TOPMOST = 0x00000080, 0x00000008
# WinForms sets WS_EX_APPWINDOW (ShowInTaskbar default) — it FORCES a taskbar
# button and beats WS_EX_TOOLWINDOW, so it must be cleared, not just outvoted.
_WS_EX_APPWINDOW = 0x00040000
# frame bits to strip so the widget is truly borderless (pywebview frameless can
# be ignored by the WebView2 backend, leaving a title bar + a minimum size)
_WS_CAPTION, _WS_THICKFRAME = 0x00C00000, 0x00040000
_WS_MINIMIZEBOX, _WS_MAXIMIZEBOX, _WS_SYSMENU = 0x00020000, 0x00010000, 0x00080000


def _tb_rect():
    if not _WIN:
        return None
    h = _WIN.FindWindowW("Shell_TrayWnd", None)
    if not h:
        return None
    r = wintypes.RECT()
    if not _WIN.GetWindowRect(h, ctypes.byref(r)):
        return None
    return (r.left, r.top, r.right, r.bottom)


def _mini_hwnd():
    return _WIN.FindWindowW(None, MINI_TITLE) if _WIN else None


def _hwnd_rect(hwnd):
    r = wintypes.RECT()
    if _WIN and hwnd and _WIN.GetWindowRect(hwnd, ctypes.byref(r)):
        return (r.left, r.top, r.right, r.bottom)
    return None


def _dpi_scale(hwnd):
    try:
        return (_WIN.GetDpiForWindow(hwnd) or 96) / 96.0
    except Exception:
        return 1.0


def _cursor():
    if not _WIN:
        return None
    p = wintypes.POINT()
    return (p.x, p.y) if _WIN.GetCursorPos(ctypes.byref(p)) else None


def _pt_in(pt, g):
    return bool(pt and g and g[0] <= pt[0] < g[0] + g[2] and g[1] <= pt[1] < g[1] + g[3])


def _pt_in_rect(pt, r):
    return bool(pt and r and r[0] <= pt[0] < r[2] and r[1] <= pt[1] < r[3])


def _place(hwnd, x, y, w, h):
    if _WIN and hwnd:
        _WIN.SetWindowPos(hwnd, _HWND_TOPMOST, int(x), int(y), int(w), int(h),
                          _SWP_NOACTIVATE | _SWP_SHOWWINDOW)


def _style_widget(hwnd):
    """Force the mini window borderless + no taskbar button/thumbnail (Win32),
    since the WebView2 backend may ignore pywebview's frameless flag and always
    sets WS_EX_APPWINDOW. Called every dock tick: it no-ops when the styles are
    already right, so WinForms can't quietly re-apply its own."""
    if not (_WIN and hwnd):
        return
    st = _GETL(hwnd, _GWL_STYLE)
    want_st = st & ~(_WS_CAPTION | _WS_THICKFRAME | _WS_MINIMIZEBOX
                     | _WS_MAXIMIZEBOX | _WS_SYSMENU)
    ex = _GETL(hwnd, _GWL_EXSTYLE)
    want_ex = (ex | _WS_EX_TOOLWINDOW | _WS_EX_TOPMOST) & ~_WS_EX_APPWINDOW
    if st == want_st and ex == want_ex:
        return
    _SETL(hwnd, _GWL_STYLE, want_st)
    # The taskbar only re-reads the TOOLWINDOW/APPWINDOW flags on a
    # hidden->visible transition, so the style change must be wrapped in a
    # hide/show or the widget keeps its taskbar button/thumbnail.
    _WIN.ShowWindow(hwnd, 0)              # SW_HIDE
    _SETL(hwnd, _GWL_EXSTYLE, want_ex)
    try:
        _WIN.SetWindowTextW(hwnd, "")     # drop the pointless "AI Usage (mini)" title
    except Exception:
        pass
    _WIN.SetWindowPos(hwnd, _HWND_TOPMOST, 0, 0, 0, 0,
                      _SWP_FRAMECHANGED | _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE)
    _WIN.ShowWindow(hwnd, 4)              # SW_SHOWNOACTIVATE


def taskbar_dock_loop(mini):
    """Keep the gadget styled + docked, and drive hover expand/collapse from the
    real cursor position (JS mouseenter/leave is unreliable for a topmost,
    frameless window). Python is the single source of truth for the geometry."""
    while True:
        try:
            if mini and mini.visible:
                hwnd = mini.hwnd()
                if hwnd:
                    _style_widget(hwnd)   # no-op unless the styles drifted
                    mini.tick(hwnd)
        except Exception:
            pass
        time.sleep(0.2)


def round_main_window():
    """Frameless main window: add an invisible resize border (WS_THICKFRAME)
    so the user can drag its edges, and rounded corners (Windows 11)."""
    if not _WIN:
        return
    try:
        _DWM = ctypes.WinDLL("dwmapi", use_last_error=True)
        _DWM.DwmSetWindowAttribute.restype = ctypes.c_long
        _DWM.DwmSetWindowAttribute.argtypes = [wintypes.HWND, ctypes.c_uint,
                                              ctypes.c_void_p, ctypes.c_uint]
        h = None
        for _ in range(240):
            h = _WIN.FindWindowW(None, "AI Usage Monitor")
            if h:
                break
            time.sleep(0.25)
        if not h:
            return
        for _ in range(20):   # WinForms may rebuild the handle after we touch
            st = _GETL(h, _GWL_STYLE)          # the styles, so re-assert a bit
            if not (st & _WS_THICKFRAME):
                _SETL(h, _GWL_STYLE, st | _WS_THICKFRAME)
                _WIN.SetWindowPos(h, 0, 0, 0, 0, 0,
                                  _SWP_FRAMECHANGED | _SWP_NOMOVE | _SWP_NOSIZE
                                  | _SWP_NOZORDER | _SWP_NOACTIVATE)
                pref = ctypes.c_int(2)   # DWMWCP_ROUND
                _DWM.DwmSetWindowAttribute(h, 33, ctypes.byref(pref),
                                           ctypes.sizeof(pref))
            time.sleep(0.5)
    except Exception:
        pass


class WinApi:
    """Exposed to the page so it can set the widget to ~half the screen height.
    The window is non-resizable; the page scales its own content to fit."""

    def __init__(self):
        self._window = None

    def bind(self, window):
        self._window = window

    def set_height(self, height):
        try:
            h = max(WINDOW_MIN_HEIGHT, min(int(round(float(height))), WINDOW_MAX_HEIGHT))
            if self._window is not None:
                self._window.resize(WINDOW_WIDTH, h)
        except Exception:
            pass
        return True

    def minimize_win(self):
        try:
            if self._window is not None:
                self._window.minimize()
        except Exception:
            pass
        return True

    def hide_win(self):
        try:
            if self._window is not None:
                self._window.hide()
        except Exception:
            pass
        return True


class MiniController:
    """js_api for the mini gadget AND its show/hide controller for the tray."""

    def __init__(self):
        self._window = None       # pywebview window (used only for show/hide)
        self._visible = False
        self._x = None            # desired compact LEFT in physical px (None = default)
        self._expanded = False
        self._dragging = False
        self._hwnd = None         # cached native handle (title is cleared later)

    def bind(self, window, visible, desired_x):
        self._window = window
        self._visible = visible
        self._x = desired_x

    def hwnd(self):
        """Native window handle, found once by title then cached (the title is
        cleared afterwards, so we must not rely on FindWindow again)."""
        if not self._hwnd:
            self._hwnd = _mini_hwnd()
        return self._hwnd

    @property
    def visible(self):
        return self._visible

    @property
    def busy(self):               # dock loop pauses while expanded/dragging
        return self._expanded or self._dragging

    # --- tray-side controls ---
    def show(self):
        if self._window:
            try:
                self._window.show()
                self._visible = True
                write_state(mini_enabled=True)
            except Exception:
                pass

    def hide(self):
        if self._window:
            try:
                self._window.hide()
                self._visible = False
                write_state(mini_enabled=False)
            except Exception:
                pass

    def toggle(self):
        self.hide() if self._visible else self.show()

    # --- geometry helpers (physical px, taskbar coordinate space) ---
    def _default_x(self, tb, scale):
        return tb[0] + int(80 * scale)

    def _compact_geom(self, hwnd, tb):
        """(x, y, w, h) in physical px for the compact bar, centered on the taskbar."""
        scale = _dpi_scale(hwnd)
        w = int(MINI_WIDTH * scale)
        barH = min(int(MINI_BAR_HEIGHT * scale), tb[3] - tb[1])
        x = self._x if self._x is not None else self._default_x(tb, scale)
        x = max(tb[0], min(int(x), tb[2] - w))
        y = tb[1] + max(0, ((tb[3] - tb[1]) - barH) // 2)
        return x, y, w, barH

    def _expand_geom(self, hwnd, tb):
        """(x, y, w, h) physical px for the expanded flyout (panel above the bar)."""
        scale = _dpi_scale(hwnd)
        cx, cy, _, barH = self._compact_geom(hwnd, tb)
        w = int(MINI_EXPANDED_WIDTH * scale)
        panel = int(MINI_EXPANDED_HEIGHT * scale)
        x = max(0, min(cx, tb[2] - w))
        y = max(0, cy - panel)                 # grow upward; bar's bottom unchanged
        return x, y, w, panel + barH

    def tick(self, hwnd):
        """Runs ~5x/sec: keep the bar docked, and expand/collapse based on whether
        the real cursor is over it. The page shows/hides its panel by watching its
        own window height, so there's no Python<->JS state to get out of sync."""
        if self._dragging:
            return
        tb = _tb_rect()
        if not tb:
            return
        cur = _cursor()
        if not self._expanded:
            comp = self._compact_geom(hwnd, tb)
            _place(hwnd, *comp)
            if _pt_in(cur, comp):
                self._expanded = True
                _place(hwnd, *self._expand_geom(hwnd, tb))
        else:
            if not _pt_in_rect(cur, _hwnd_rect(hwnd)):
                self._expanded = False
                _place(hwnd, *self._compact_geom(hwnd, tb))

    # --- js bridge (mini page) ---
    def begin_drag(self):
        # Lock is owned by the tray menu; check it here so it's always current.
        if get_mini_settings()["locked"]:
            return [0, 1.0, True]     # [_, _, locked]
        self._dragging = True
        self._expanded = False        # dragging always operates on the compact bar
        hwnd = self.hwnd()
        r = _hwnd_rect(hwnd)
        return [r[0] if r else 0, _dpi_scale(hwnd), False]

    def drag_to(self, x_phys):
        hwnd, tb = self.hwnd(), _tb_rect()
        if hwnd and tb:
            scale = _dpi_scale(hwnd)
            w = int(MINI_WIDTH * scale)
            self._x = max(tb[0], min(int(x_phys), tb[2] - w))
            _place(hwnd, *self._compact_geom(hwnd, tb))
        return True

    def end_drag(self, x_phys):
        self.drag_to(x_phys)
        self._dragging = False
        if self._x is not None:
            write_state(mini_x=int(self._x))
        return True


def build_tray(main_window, mini):
    """System-tray icon: Open / toggle Mini widget / Exit."""
    import pystray
    from PIL import Image
    try:
        image = Image.open(resource_path("app.ico"))
    except Exception:
        image = Image.new("RGBA", (64, 64), (59, 130, 246, 255))

    def on_open(icon, item):
        try:
            main_window.show()
            main_window.restore()
        except Exception:
            pass

    def on_toggle(icon, item):
        mini.toggle()

    def on_lock(icon, item):
        write_state(mini_locked=not get_mini_settings()["locked"])

    def on_exit(icon, item):
        request_exit(icon, main_window, mini)

    menu = pystray.Menu(
        pystray.MenuItem("Open", on_open, default=True),
        pystray.MenuItem("Taskbar widget", on_toggle, checked=lambda item: mini.visible),
        pystray.MenuItem("Lock widget position", on_lock,
                         checked=lambda item: get_mini_settings()["locked"]),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", on_exit),
    )
    return pystray.Icon("ai_usage_monitor", image, "AI Usage Monitor", menu)


_quitting = False
_tray_active = False


def request_exit(icon, main_window, mini):
    global _quitting
    _quitting = True
    try:
        if icon:
            icon.stop()
    except Exception:
        pass
    for w in (mini._window if mini else None, main_window):
        try:
            if w:
                w.destroy()
        except Exception:
            pass


def main():
    if "--test-claude" in sys.argv:
        run_claude_test()
        return

    if other_instance_running():
        return    # the running instance was told to show its window instead

    threading.Thread(target=claude_usage_loop, daemon=True).start()
    threading.Thread(target=codex_usage_loop, daemon=True).start()

    port = find_port(PREFERRED_PORT)
    url = f"http://localhost:{port}"
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        import webview
    except Exception:
        webview = None
    if webview is not None:
        try:
            sw, sh = primary_screen_size()
            init_h = (max(WINDOW_MIN_HEIGHT, min(int(sh * WINDOW_FRACTION), WINDOW_MAX_HEIGHT))
                      if sh else WINDOW_HEIGHT)
            global _tray_active
            api = WinApi()
            window = webview.create_window("AI Usage Monitor", url,
                                           width=WINDOW_WIDTH, height=init_h,
                                           resizable=True, on_top=ALWAYS_ON_TOP,
                                           frameless=True, easy_drag=False,
                                           background_color="#f6f7f9",
                                           js_api=api)
            api.bind(window)

            # taskbar-overlay mini gadget. Its final geometry is driven by the
            # Win32 dock loop (physical px, DPI-correct); the create_window values
            # are just a small initial placeholder that gets snapped within ~1s.
            ms = get_mini_settings()
            mini = MiniController()
            mini_window = webview.create_window("AI Usage (mini)", url + "/mini",
                                                width=MINI_WIDTH, height=44,
                                                x=120, y=120, frameless=True,
                                                on_top=True, resizable=False,
                                                easy_drag=False, focus=False,
                                                # pywebview's default min_size (200x100 logical) is
                                                # enforced by Windows even against raw SetWindowPos,
                                                # so the compact bar could never reach its real size
                                                min_size=(1, 1),
                                                background_color="#1e2025",
                                                hidden=(not ms["enabled"]), js_api=mini)
            mini.bind(mini_window, ms["enabled"], ms["x"])
            threading.Thread(target=taskbar_dock_loop, args=(mini,), daemon=True).start()
            threading.Thread(target=show_request_watcher, args=(window,),
                             daemon=True).start()

            # X on the main window minimizes to the tray. But if the tray failed
            # to start, closing must FULLY quit (destroy the hidden mini too) so
            # the app can never get stuck running with no visible window.
            def on_closing():
                global _quitting
                if _quitting:
                    return True
                if _tray_active:
                    try:
                        window.hide()
                    except Exception:
                        pass
                    return False
                _quitting = True
                try:
                    mini_window.destroy()
                except Exception:
                    pass
                return True
            try:
                window.events.closing += on_closing
            except Exception:
                pass

            icon = None
            try:
                icon = build_tray(window, mini)
                icon.run_detached()
                _tray_active = True
            except Exception:
                icon = None

            threading.Thread(target=round_main_window, daemon=True).start()
            webview.start()
            try:
                if icon:
                    icon.stop()
            except Exception:
                pass
            os._exit(0)
        except Exception:
            pass
    print("Opening the dashboard in your browser:", url)
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
