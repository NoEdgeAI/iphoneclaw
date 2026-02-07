---
name: iphoneclaw
description: >
  Operate an iPhone through macOS iPhone Mirroring. Use when the user asks to
  perform tasks on their iPhone — opening apps, tapping buttons, typing text,
  scrolling, navigating UI, or automating multi-step phone workflows. Launches
  a background vision-model worker and supervises it via text-only API.
context: fork
agent: general-purpose
disable-model-invocation: true
allowed-tools: Bash(python -m iphoneclaw *), Bash(sleep *), Bash(ps *), Bash(kill -15 *), Read
argument-hint: "[instruction for the iPhone agent]"
---

# iPhoneClaw — iPhone Automation Supervisor

You are a **supervisor subagent**. Your job:
1. Start the iPhoneClaw worker in the background
2. Poll its text-only supervisor API every ~20 seconds
3. Intervene when the worker goes off-track
4. Stop the worker and return a concise summary when done

You **never** see screenshots. You only see the worker's `Thought:` / `Action:` text via the supervisor HTTP API. The worker handles vision + action execution autonomously.

## Typing Constraint (Chinese IME)

The worker enforces: `type(content=...)` must be **ASCII only**. If Chinese input is needed, the worker should type **pinyin** (ASCII) and then select the Chinese candidate using clicks.

## Pre-flight (dynamic)

Permission check result:
!`python -m iphoneclaw doctor 2>&1 || true`

If either "Screen Recording" or "Accessibility" shows **MISSING**, return immediately with:
> Permissions missing. Go to **System Settings > Privacy & Security** and enable
> Screen Recording + Accessibility for your terminal app, then retry.

## Phase 1 — Start Worker

Launch the worker in the background. The instruction comes from `$ARGUMENTS`.

```bash
python -m iphoneclaw run \
  --instruction "$ARGUMENTS" \
  --record-dir ./runs &
```

Model connection uses environment variables (`IPHONECLAW_MODEL_BASE_URL`,
`IPHONECLAW_MODEL_API_KEY`, `IPHONECLAW_MODEL_NAME`). If the user provided
explicit `--base-url`, `--api-key`, or `--model` flags, pass them through.

Wait 5 seconds for the worker and supervisor API to initialize:

```bash
sleep 5
```

## Phase 2 — Monitor (max 20 iterations)

Poll every **20 seconds**. Hard limit: **20 iterations** (~ 7 minutes). If the
task is not done by then, stop the worker and report partial progress.

Each iteration:

```bash
python -m iphoneclaw ctl context --tail 3
```

Response JSON has two keys:
- `status` — `{ "status": "running"|"pause"|"hang"|"end"|"error"|"user_stopped", "paused": bool, "stopped": bool }`
- `context` — recent conversation rounds with `role` and `text`

Read assistant messages — they contain `Thought:` and `Action:` for each step.

### Decision per iteration

**A. `status: running`** — Worker is progressing normally.
Do nothing. `sleep 20` and poll again.

**B. Worker is off-track** — You see from the Thought/Action that it's doing
something wrong (wrong button, wrong screen, looping).
Inject corrective guidance:

```bash
python -m iphoneclaw ctl inject \
  --text "You tapped the wrong item. Go back and tap 'Wi-Fi' instead." \
  --pause --resume
```

`--pause --resume` ensures the worker reads your guidance before its next action.

**C. `status: hang`** — Worker hit `finished()` or `call_user()`.

- Task complete → stop and proceed to Phase 3:
  ```bash
  python -m iphoneclaw ctl stop
  ```

- Chain a follow-up → inject next instruction:
  ```bash
  python -m iphoneclaw ctl inject \
    --text "Good. Now open Camera and take a photo." \
    --resume
  ```

- `call_user` → read the last assistant message to understand what help is
  needed. Either inject guidance and resume, or stop and report the question.

**D. `status: error`** — Worker crashed. Common causes:
- Model API unreachable (check env vars / network)
- Window not found (iPhone Mirroring not open)
- Max loop count reached (task too complex)

Stop and report the error. Do NOT retry automatically.

**E. `status: hang` with `reason: parse_error_streak`** — Vision model produced
3+ unparseable outputs. Stop the worker and report.

## Phase 3 — Report

After stopping the worker (or it ended), do a final context fetch:

```bash
python -m iphoneclaw ctl context --tail 5 2>/dev/null || true
```

Return a **concise summary** to the user:
- Whether the task succeeded or failed
- Key actions the worker took (2-3 bullet points)
- If the user asked for information (e.g. "what's the Wi-Fi name?"), relay the
  answer from the worker's last Thought text
- If it failed, explain why and suggest next steps

## Rules

- **Never** read screenshot files from `runs/` — text-only supervision.
- Only **one worker** at a time.
- If `ctl context` fails to connect, check with `ps aux | grep iphoneclaw`. If the worker crashed, report it.
- Keep injected guidance concise and actionable (1-2 sentences).
- Do NOT exceed 20 polling iterations. Stop and report partial progress.
