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
