---
name: iphoneclaw-action-scripts
description: Record, register, and invoke iPhoneClaw action scripts to reduce VLM tokens and make automations repeatable. Use when you want the model/agent to call `run_script(name=..., vars=...)`, when updating `action_scripts/registry.json`, when exporting scripts from `runs/*/events.jsonl`, or when triggering scripts remotely via `python -m iphoneclaw ctl run-script` while the worker is paused.
---

# iPhoneClaw Action Scripts

Use the local script registry (`action_scripts/registry.json`) to give the model a low-token primitive:
`run_script(name=..., ...)` expands into a pre-recorded `.txt` action script and executes the concrete actions.

Key files:
- `action_scripts/registry.json`: short name -> script path
- `action_scripts/common/*.txt`: curated scripts
- `action_scripts/recorded/*.txt`: recordings/exports

## Preferred Model Output (Low Token)

When a stable flow exists, output a single action:
```text
Action: run_script(name='open_app_spotlight', APP='bilibili')
```

Notes:
- Vars can be passed as `vars={...}` or as keyword sugar (`APP='bilibili'`).
- Prefer `name=...` (registry) over `path=...` (arbitrary file) for safety and portability.

## Run Scripts Manually (Local)

Run a script file directly:
```bash
python -m iphoneclaw script run --file action_scripts/common/open_app_spotlight.txt --var APP=bilibili
```

## Run Scripts Remotely (Supervisor API, Worker Paused)

Requirement: the worker must be `paused` and supervisor exec must be enabled (`enable_supervisor_exec`).

Use `ctl`:
```bash
python -m iphoneclaw ctl run-script --name open_app_spotlight --var APP=bilibili
```

This hits supervisor endpoint `POST /v1/agent/script/run`.

## Record And Register A New Script

1. Create or record the script.
```bash
# quick record from stdin (Ctrl-D to finish)
python -m iphoneclaw script record --out action_scripts/recorded/my_flow.txt
```

Or export from a previous run:
```bash
python -m iphoneclaw script from-run --run-dir runs/<run_id> --out action_scripts/recorded/<run_id>.txt
```

2. Add a registry entry in `action_scripts/registry.json`:
```json
{
  "my_flow": "recorded/my_flow.txt"
}
```

3. Invoke it via model action:
```text
Action: run_script(name='my_flow')
```

## Registry Path Resolution

Default registry path is `./action_scripts/registry.json`.

If running from a different working directory, set:
- `IPHONECLAW_SCRIPT_REGISTRY=/absolute/path/to/action_scripts/registry.json`
