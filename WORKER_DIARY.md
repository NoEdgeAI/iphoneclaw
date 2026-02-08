# iphoneclaw Worker Diary

This is a lightweight, text-only "lessons learned" log written by the supervisor agent (Claude Code/Codex).
Goal: make the worker more reliable over time by recording recurring failure modes and the fixes.

Rules:
- Do NOT paste screenshots or base64.
- Do NOT paste secrets (API keys, tokens).
- Keep entries short and actionable.
- Prefer: symptom -> cause -> fix -> how to prevent.

## Log Format (grep-friendly, append-only)

All new diary entries MUST be a single line starting with `DIARY|` so supervisors can retrieve relevant lessons using `grep`.

Required fields:
- `app`
- `task`
- `reflection`

Recommended fields:
- `ts` (RFC3339 local time)
- `tags` (comma-separated keywords)
- `run` (optional run id like `runs/20260208_070745`)

Template:
`DIARY|ts=YYYY-MM-DDTHH:MM:SS±HH:MM|app=<AppName>|task=<ShortTask>|reflection=<OneLine>|tags=<k1,k2>|run=<runs/...>`

Encoding rules:
- One line only. Replace newlines with `; `.
- Avoid `|` inside values. If needed, replace with `/`.
- No screenshots/base64, no secrets.

## Example Entries (sanitized)

These are intentionally generic examples (no personal data, no screenshots).

DIARY|ts=2026-02-08T00:00:00+00:00|app=VideoPlayer|task=Pause video quickly|reflection=Player controls auto-hide; use double_click at center or click;sleep(ms=50);click sequence; avoid slow reasoning between taps|tags=video,double-click,timing
DIARY|ts=2026-02-08T00:00:10+00:00|app=Settings|task=Toggle a single switch safely|reflection=Avoid extra navigation after goal reached; confirm switch state before finished(); if looping, trim context and restate goal|tags=settings,switch,finish-too-early,context-clear
DIARY|ts=2026-02-08T00:00:20+00:00|app=iPhone Home|task=Scroll without opening apps|reflection=Do not click-to-focus before wheel scroll; keep cursor near safe area (above dock) to avoid accidental opens|tags=home,scroll,wheel,no-focus-click

## Common Lessons

- Scrolling:
  - Prefer `scroll(direction='down'|'up', ...)` (wheel), not vertical `drag(...)`.
  - Avoid "click to focus" before scrolling. Clicking may open a video/item under the cursor.
  - On iPhone Home/App Library, scroll/swipe slightly above the bottom nav/dock, not mid-screen.

- Typing:
  - `type(content=...)` must be ASCII only. For Chinese, type pinyin (ASCII) and click IME candidates.
  - Avoid iPhone Home Screen search/Spotlight for launching apps; typing there is often unreliable.

- iPhone gestures:
  - Long-press-like drags can trigger icon rearrange; keep swipe gestures fast (no long hold).

## Legacy Template (deprecated)

Date: YYYY-MM-DD
Task: <short>
Symptom: <what went wrong>
Cause: <why>
Fix: <what to do next time>
Prevention: <prompt/guideline/config change>

DIARY|ts=2026-02-08T11:30:00+08:00|app=YouTube|task=Search latest Linux video and read comments|reflection=Worker kept calling finished() prematurely after opening video without reading description/comments; double_click() for pausing video worked well; YouTube comment section needs to be clicked/tapped to expand before scrolling; worker crashed (max loop) before completing comment reading; supervisor needs to be more aggressive about preventing early finished() calls; also YouTube Shorts trap - avoid clicking short-form videos as navigation out is difficult|tags=youtube,linux,pause,double-click,comments,finished-too-early,shorts-trap,max-loop
DIARY|ts=2026-02-08T12:00:00+08:00|app=YouTube|task=YouTube video pause and exit navigation|reflection=YouTube video player: double-click CENTER of video to pause/play; double-click TOP-LEFT corner to exit/go back; single click on video shows controls briefly then hides; worker must use left_double at center (500,230) to pause, left_double at top-left (80,130) to exit; also worker calls finished() way too early - must explicitly list ALL required info in Thought before finishing|tags=youtube,double-click,pause,exit,back-button,center-click,top-left-exit
