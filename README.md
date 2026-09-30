# AI Usage Widget

A compact Windows desktop widget that shows your **Claude**, **ChatGPT / Codex**, and **OpenCode** usage at a glance — live limit rings, reset countdowns, weekly/monthly token counts, and OpenCode spend tracking, all in a tiny taskbar bar that expands on hover.

![Widget preview](https://github.com/Aldathor/claude-chatgpt-usage-widget/assets/preview.png)

---

## Features

- **Mini taskbar bar** — always-on-top 3-row bar (Claude % · Codex % · OpenCode $) that stays out of your way
- **Hover to expand** — flies up to show full usage rings, reset timers, and token/cost stats across all three providers
- **Claude limits** — session and weekly limits pulled from Claude Code's local OAuth token (covers all Claude apps: chat, Cowork, Code, CLI)
- **Codex limits** — weekly usage read from the Codex CLI's local logs
- **OpenCode spend** — monthly cost ring (vs a configurable budget) + weekly/monthly token counts, read from OpenCode's local SQLite DB (`~/.local/share/opencode/opencode.db`)
- **Token counts** — weekly (7-day) and monthly (30-day) token totals per provider, sourced from local session logs
- **Auto-hide chrome** — header fades and collapses after 10 s of inactivity; reappears on any interaction
- **Drag to reposition** — click-drag the compact bar anywhere along the taskbar edge
- **System tray icon** — show/hide or quit from the tray

---

## Requirements

- Windows 10/11 (WebView2 runtime — included with Windows 11; install from Microsoft if missing on Win 10)
- [Claude Code](https://claude.ai/code) installed and signed in (for Claude limits)
- [Codex CLI](https://github.com/openai/codex) installed and signed in (for Codex limits)
- [OpenCode](https://opencode.ai) installed and used at least once (for OpenCode spend tracking — optional)

---

## Usage

Download `AIUsage.exe` from the [latest release](https://github.com/Aldathor/claude-chatgpt-usage-widget/releases) and run it. No install needed.

- **Hover** the compact bar to expand the full view
- **Click** the compact bar (without dragging) to open the settings panel
- **Drag** the bar to reposition it
- **Right-click the tray icon** to show/hide or quit

---

## Build from source

```bash
pip install pywebview pystray pillow
pyinstaller --onefile --windowed --name AIUsage --icon source/app.ico \
  --hidden-import=webview --hidden-import=pystray \
  --hidden-import=PIL --hidden-import=PIL.Image --hidden-import=sqlite3 \
  source/usage_monitor.py
```

To adjust the OpenCode monthly budget ceiling (default $50), edit `OPENCODE_MONTHLY_BUDGET` near the top of `usage_monitor.py`.

Single file, no external assets needed — all HTML/CSS/JS is embedded in `usage_monitor.py`.

---

## Data & privacy

All data stays on your machine. The only outbound call is to Claude's usage endpoint using **your own OAuth token** (the same one Claude Code uses). No telemetry, no third-party services.

---

## License

MIT
