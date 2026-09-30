# AI Usage Widget (Claude + ChatGPT/Codex)

Portable Windows widget showing usage limits for **Claude** (5h session + weekly) and **ChatGPT/Codex** (weekly) at a glance, plus local token counts.

Built on [halimadmech/ai-usage-monitor](https://github.com/halimadmech/ai-usage-monitor) (MIT). This repo carries a custom build of `AIUsage.exe`:

- **Custom ring dashboard** — light UI with a donut ring per service (`% left`, reset countdown), plan badge, and a "Stay productive" footer
- **Pinned always-on-top** (`ALWAYS_ON_TOP = True`) — the main window stays in front of all other apps while open
- **Frameless window with custom header** — drag it by the header; gear = settings panel (tokens, refresh, sign out); `—` minimizes; `✕` closes to the tray
- Taskbar mini strip is unchanged (two lines, hover to expand)

## Setup on a new computer

1. Clone this repo
2. Make sure you're signed in to:
   - **Claude Code** (or have the Claude desktop app) — required for the Claude numbers
   - **Codex CLI** — required for the ChatGPT numbers
3. Double-click `AIUsage.exe`
   - On first run, if the Claude ring asks, click **Connect Claude** from that machine
   - Windows may show "Windows protected your PC" → More info → Run anyway (unsigned binary, source is public)
4. Drag the window by its header to where you want it; the taskbar strip can be dragged to a free spot and locked from the tray icon

## Notes

- The big ring shows the **5h session** for Claude and the **weekly** window for ChatGPT; Claude's weekly is the small bar under the ring
- Reads usage only; no API credits are consumed
- Data stays local — the only network calls go to Anthropic/OpenAI for your own usage numbers
- The ChatGPT account is on the "prolite" plan, which has **no 5-hour Codex window** — only the weekly ring will populate

## Rebuilding from source

`source/usage_monitor.py` is the full patched app (single file). With Python 3 + the build tools:

```
py -m pip install pyinstaller pywebview pystray pillow
py -m PyInstaller --onefile --windowed --name AIUsage --icon app.ico --add-data "app.ico;." --hidden-import pystray._win32 usage_monitor.py
```

Upstream MIT license included as `source/LICENSE-upstream`.
