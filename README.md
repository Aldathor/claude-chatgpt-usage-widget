# AI Usage Widget (Claude + ChatGPT/Codex)

Portable Windows widget showing usage limits for **Claude** (5h session + weekly) and **ChatGPT/Codex** (weekly) at a glance, plus local token counts.

Built on [halimadmech/ai-usage-monitor](https://github.com/halimadmech/ai-usage-monitor) (MIT). This repo carries a custom build of `AIUsage.exe`:
- **Pinned always-on-top** (`ALWAYS_ON_TOP = True` in the source) — the main window stays in front of all other apps while open

## Setup on a new computer

1. Clone this repo
2. Make sure you're signed in to:
   - **Claude Code** (or have the Claude desktop app) — required for the Claude bars
   - **Codex CLI** — required for the ChatGPT bars
3. Double-click `AIUsage.exe`
   - On first run, if the Claude card asks, click **Connect Claude** from that machine
   - Windows may show "Windows protected your PC" → More info → Run anyway (unsigned binary, source is public)
4. The widget sits on the taskbar: drag it to a free spot, hover to expand, right-click the tray icon to lock position or exit

## Notes

- Reads usage only; no API credits are consumed
- Data stays local — the only network calls go to Anthropic/OpenAI for your own usage numbers
- The ChatGPT account is on the "prolite" plan, which has **no 5-hour Codex window** — only the weekly bar will populate
