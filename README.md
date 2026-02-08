# iphoneclaw

[English](README.md) | [中文](README.zh.md)

**iPhone + AI. Open-source Apple Intelligence: let agents take over your iPhone.**

![demo](assets/demo.gif)

![iphoneclaw mascot](assets/iphoneclaw-brand-mascot.png)

Full demo video: [assets/iphoneclaw.mp4](assets/iphoneclaw.mp4)

Official site: https://iphoneclaw.com

macOS-only Python CLI worker that controls the **iPhone Mirroring / iPhone镜像** window using a VLM (Vision Language Model) agent loop:

1. Capture window screenshot (Quartz CGWindowList)
2. Call an OpenAI-compatible vision chat endpoint
3. Parse `Thought:` / `Action:`
4. Execute actions via Quartz CGEvent (mouse / keyboard)
5. Record each step to `runs/`

It also exposes a **local Supervisor API** (text-only + SSE) so external agent frameworks can supervise the run:
poll the latest conversation (tail N rounds), subscribe to live events, and intervene with `pause/resume/stop/inject`.
This is designed to plug into orchestrators like **Claude Code** / **Codex** as a “boss agent” supervising a UI worker.

It can also **improve over time**: supervisors can record “lessons learned” in `WORKER_DIARY.md`, and consult it before starting new tasks.

Community diary repo (opt-in PRs): https://github.com/NoEdgeAI/awesome-iphoneclaw-diary

## Prerequisites

- A Mac (Mac mini / MacBook) + an iPhone
- iPhone Mirroring supported:
  - Mac: **macOS Sequoia (macOS 15)** or newer
  - iPhone: **iOS 18** or newer
  - Both devices signed in with the **same Apple ID**
- Python >= 3.9
- Screen Recording & Accessibility permissions granted to your terminal

## Installation

```bash
git clone https://github.com/user/iphoneclaw.git
cd iphoneclaw

# pip
pip install -e .

# or uvall -e .
```

To include dev dependencies (pytest):

```bash
pip install -e ".[dev]"
# or
uv pip install -e ".[dev]"
```

Verify the installation and check macOS permissions:

```bash
iphoneclaw doctor
```

If Screen Recording or Accessibility shows **MISSING**, go to **System Settings > Privacy & Security** and grant permissions to your terminal app.

## Supported Models

iphoneclaw works with any OpenAI-compatible vision model endpoint. Below are three recommended options.

### Option A: UI-TARS via vLLM (Self-hosted)

[UI-TARS](https://github.com/bytedance/UI-TARS) is a GUI agent model by ByteDance, purpose-built for screen interaction. It outputs structured `Thought:` / `Action:` in the format iphoneclaw expects natively.

**Available models on HuggingFace:**

| Model | Size | Notes |
|-------|------|-------|
| `ByteDance-Seed/UI-TARS-1.5-7B` | ~8B | Latest & recommended |
| `ByteDance-Seed/UI-TARS-7B-DPO` | 7B | v1, DPO-tuned |
| `ByteDance-Seed/UI-TARS-72B-DPO` | 72B | v1, best quality, needs 4x A100 |
| `ByteDance-Seed/UI-TARS-2B-SFT` | 2B | Lightweight, 8 GB VRAM |

**Deploy with vLLM:**

```bash
pip install vllm

python -m vllm.entrypoints.openai.api_server \
  --served-model-name ui-tars \
  --model ByteDance-Seed/UI-TARS-1.5-7B \
  --limit-mm-per-prompt image=5 \
  -tp 1
```

For the 72B model use `-tp 4` (4 GPUs with tensor parallelism).

**Run iphoneclaw:**

```bash
python -m iphoneclaw run \
  --instruction "Open Settings and enable Wi-Fi" \
  --base-url http://127.0.0.1:8000/v1 \
  --model ui-tars
```

### Option B: Qwen2.5-VL via vLLM (Self-hosted)

[Qwen2.5-VL](https://huggingface.co/collections/Qwen/qwen25-vl) by Alibaba has strong vision-agent capabilities out of the box, including screen understanding and UI interaction.

**Available models on HuggingFace:**

| Model | Size | Notes |
|-------|------|-------|
| `Qwen/Qwen2.5-VL-7B-Instruct` | ~8B | Good balance |
| `Qwen/Qwen2.5-VL-32B-Instruct` | ~33B | Strong |
| `Qwen/Qwen2.5-VL-72B-Instruct` | ~73B | Best quality |
| `Qwen/Qwen2.5-VL-3B-Instruct` | ~4B | Lightweight |

**Deploy with vLLM:**

```bash
pip install vllm

vllm serve Qwen/Qwen2.5-VL-7B-Instruct \
  --host 0.0.0.0 --port 8000 \
  --served-model-name qwen-vl \
  --limit-mm-per-prompt '{"image":2,"video":0}'
```

For the 72B model add `--tensor-parallel-size 4`.

**Run iphoneclaw:**

```bash
python -m iphoneclaw run \
  --instruction "Open Settings and enable Wi-Fi" \
  --base-url http://127.0.0.1:8000/v1 \
  --model qwen-vl
```

### Option C: Volcengine Doubao UI-TARS (Cloud API)

[Volcengine Ark (火山引擎方舟)](https://www.volcengine.com/product/doubao) hosts Doubao vision models as a managed cloud API. No GPU required -- just an API key.

**Available models:**

| Model ID | Description |
|----------|-------------|
| `doubao-1-5-ui-tars-250428` | Vision model (recommended) |

**Setup:**

1. Register at [console.volcengine.com](https://console.volcengine.com) and complete real-name authentication
2. Create an API key in the Ark console

**Run iphoneclaw:**

```bash
export IPHONECLAW_MODEL_BASE_URL="https://ark.cn-beijing.volces.com/api/v3"
export IPHONECLAW_MODEL_API_KEY="your-ark-api-key"
export IPHONECLAW_MODEL_NAME="doubao-1-5-ui-tars-250428"

python -m iphoneclaw run \
  --instruction "Open Settings and enable Wi-Fi"
```

Or pass inline:

```bash
python -m iphoneclaw run \
  --instruction "Open Settings and enable Wi-Fi" \
  --base-url "https://ark.cn-beijing.volces.com/api/v3" \
  --api-key "$ARK_API_KEY" \
  --model "doubao-1-5-ui-tars-250428"
```

## Quick Start

```bash
# 1. Check permissions
python -m iphoneclaw doctor

# 2. Launch iPhone Mirroring and verify window detection
python -m iphoneclaw launch

# 3. Take a test screenshot
python -m iphoneclaw screenshot --out /tmp/shot.jpg

# 4. Run the agent (pick one of the model options above)
python -m iphoneclaw run \
  --instruction "Open Settings and enable Wi-Fi" \
  --base-url http://127.0.0.1:8000/v1 \
  --model ui-tars
```

## CLI Reference

```
iphoneclaw doctor          Check macOS permissions
iphoneclaw launch          Launch target app, print window bounds
iphoneclaw bounds          Print window bounds (x y w h)
iphoneclaw screenshot      Capture target window to JPEG
iphoneclaw calibrate       Screenshot + coordinate mapping info
iphoneclaw windows         List visible windows (debug)
iphoneclaw run             Run the agent loop + supervisor API
iphoneclaw serve           Start supervisor API only (no worker)
iphoneclaw ctl             Control a running worker via supervisor
```

## Supervisor API

The worker exposes an HTTP API on `127.0.0.1:17334` for monitoring and control:

```bash
# View recent conversation context
python -m iphoneclaw ctl context --tail 5

# Pause / resume / stop
python -m iphoneclaw ctl pause
python -m iphoneclaw ctl resume
python -m iphoneclaw ctl stop

# Inject guidance into the agent's context
python -m iphoneclaw ctl inject --text "Only toggle Wi-Fi; do not change other settings." --resume
```

SSE event stream: `GET /v1/agent/events`

## Typing on macOS (AppleScript)

If CGEvent/clipboard typing is unreliable, iphoneclaw can type via **System Events** AppleScript. By default it uses in-process `NSAppleScript` (permission attribution follows your terminal/python process).

```bash
export IPHONECLAW_APPLESCRIPT_MODE=native    # default
export IPHONECLAW_APPLESCRIPT_MODE=osascript # fallback via /usr/bin/osascript
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `IPHONECLAW_MODEL_BASE_URL` | Model API base URL | `http://localhost:8000/v1` |
| `IPHONECLAW_MODEL_API_KEY` | Model API key | (empty) |
| `IPHONECLAW_MODEL_NAME` | Model name | `doubao-1-5-ui-tars-250428` |
| `IPHONECLAW_TARGET_APP` | macOS app to control | `iPhone Mirroring` |
| `IPHONECLAW_WINDOW_CONTAINS` | Window match substring | (empty) |
| `IPHONECLAW_SUPERVISOR_HOST` | Supervisor bind host | `127.0.0.1` |
| `IPHONECLAW_SUPERVISOR_PORT` | Supervisor bind port | `17334` |
| `IPHONECLAW_SUPERVISOR_TOKEN` | Supervisor bearer token | (empty) |
| `IPHONECLAW_RECORD_DIR` | Run recording directory | `./runs` |
| `IPHONECLAW_APPLESCRIPT_MODE` | Typing mode: native/osascript | `native` |
| `IPHONECLAW_RESTORE_CURSOR` | Restore mouse cursor position after each action (1/0) | `0` |
| `IPHONECLAW_AUTO_PAUSE_ON_USER_INPUT` | Auto-pause when user touches mouse/keyboard (1/0) | `0` |
| `IPHONECLAW_TYPE_ASCII_ONLY` | Reject non-ASCII `type(content=...)` (use pinyin + IME for Chinese) (1/0) | `1` |
| `IPHONECLAW_SCROLL_INVERT_Y` | Invert vertical wheel scroll direction (1/0) | `0` |
| `IPHONECLAW_SCROLL_FOCUS_CLICK` | Click to focus before wheel scroll (risk: opens items under cursor) (1/0) | `0` |

## Claude Code Integration

iphoneclaw ships with a [Claude Code skill](https://code.claude.com/docs/en/skills) that lets Claude supervise the worker autonomously. When invoked, Claude:

1. Starts the iphoneclaw worker in the background
2. Polls the Supervisor API every ~20 seconds (text only, no screenshots)
3. Intervenes if the worker goes off-track
4. Returns a concise summary when done

The skill uses `context: fork` to run in an isolated subagent — polling noise stays out of your main conversation.

**Setup:** The skill is auto-discovered from `.claude/skills/iphoneclaw/SKILL.md` when you open this project in Claude Code. For cross-project use, copy to your home directory:

```bash
mkdir -p ~/.claude/skills/iphoneclaw
cp .claude/skills/iphoneclaw/SKILL.md ~/.claude/skills/iphoneclaw/SKILL.md
```

**Usage:**

```
/iphoneclaw Open Settings and enable Wi-Fi
/iphoneclaw Check battery percentage and report back
/iphoneclaw Open Safari, go to example.com, and take a screenshot
```

Ensure model environment variables are set before invoking (`IPHONECLAW_MODEL_BASE_URL`, `IPHONECLAW_MODEL_API_KEY`, `IPHONECLAW_MODEL_NAME`).

## Docs

- Architecture / implementation plan: `PLAN.md`
- Claude Code skill: `.claude/skills/iphoneclaw/SKILL.md`

## Thanks

- [UI-TARS](https://github.com/bytedance/UI-TARS)

## License

Apache-2.0
