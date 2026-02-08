# iphoneclaw

[English](README.md) | [中文](README.zh.md)

**iPhone + AI，开源版 Apple Intelligence：让 Agent 接管你的 iPhone。**

![demo](assets/demo.gif)

![iphoneclaw mascot](assets/iphoneclaw-brand-mascot.png)

完整演示视频: [assets/iphoneclaw.mp4](assets/iphoneclaw.mp4)

官网: https://iphoneclaw.com

`iphoneclaw` 是一个 **macOS-only** 的 Python CLI Worker：通过 **iPhone 镜像 / iPhone Mirroring** 窗口，让 VLM（Vision Language Model）以 UI-TARS 风格的 `Thought:` / `Action:` 循环来操控你的 iPhone。

核心流程:

1. 截取 iPhone 镜像窗口截图（Quartz CGWindowList）
2. 调用 OpenAI-compatible 的多模态接口
3. 解析 `Thought:` / `Action:`
4. 用 Quartz CGEvent 执行鼠标/键盘操作
5. 记录每一步到 `runs/`

同时提供 **本地 Supervisor API**（仅文本 + SSE），便于外部 Agent 框架监督运行：拉取最近 N 轮对话、订阅实时事件，并通过 `pause/resume/stop/inject` 进行干预。设计目标是可以接入 **Claude Code / Codex** 等编排框架，让“老板 Agent”监管这个 UI Worker。

同时支持“持续学习”：supervisor 可以把经验教训记录在 `WORKER_DIARY.md`，并在每次新任务开始前先查一查，让 worker 越用越熟练。

社区日记仓库（需用户同意后再提交 PR）：https://github.com/NoEdgeAI/awesome-iphoneclaw-diary

## 设备与系统要求

- 一台 Mac（Mac mini / MacBook）+ 一台 iPhone
- 支持 iPhone 镜像:
  - Mac 升级到 **macOS Sequoia（macOS 15）** 或更高
  - iPhone 升级到 **iOS 18** 或更高
  - Mac 和 iPhone 使用 **同一 Apple ID** 登录
- Python >= 3.9
- 终端需要授予 Screen Recording（屏幕录制）与 Accessibility（辅助功能）权限

## 安装

```bash
git clone https://github.com/NoEdgeAI/iphoneclaw.git
cd iphoneclaw

# pip
pip install -e .

# 或 uv
uv pip install -e .
```

包含开发依赖（可选）:

```bash
pip install -e ".[dev]"
# 或
uv pip install -e ".[dev]"
```

检查权限:

```bash
iphoneclaw doctor
```

如果 Screen Recording 或 Accessibility 显示 **MISSING**，到 **System Settings > Privacy & Security** 给你的终端程序授权。

## 推荐模型

iphoneclaw 支持任意 OpenAI-compatible 的视觉模型接口。以下是常见选项:

### 选项 A: UI-TARS + vLLM（自建）

UI-TARS 是字节系 GUI agent 模型，天然输出 iphoneclaw 需要的 Action 格式。

```bash
python -m vllm.entrypoints.openai.api_server \
  --served-model-name ui-tars \
  --model ByteDance-Seed/UI-TARS-1.5-7B \
  --limit-mm-per-prompt image=5 \
  -tp 1
```

运行:

```bash
python -m iphoneclaw run \
  --instruction "打开设置并开启 Wi-Fi" \
  --base-url http://127.0.0.1:8000/v1 \
  --model ui-tars
```

### 选项 B: 火山 Ark（Doubao）

```bash
export IPHONECLAW_MODEL_BASE_URL="https://ark.cn-beijing.volces.com/api/v3"
export IPHONECLAW_MODEL_API_KEY="your-ark-api-key"
export IPHONECLAW_MODEL_NAME="doubao-1-5-ui-tars-250428"

python -m iphoneclaw run \
  --instruction "打开设置并开启 Wi-Fi"
```

### 选项 C: Qwen2.5-VL + vLLM（自建，可选）

Qwen2.5-VL 也能很好地完成屏幕理解与 UI 操作（只要输出符合 `Thought:` / `Action:` 格式）。

```bash
pip install vllm

vllm serve Qwen/Qwen2.5-VL-7B-Instruct \
  --host 0.0.0.0 --port 8000 \
  --served-model-name qwen-vl \
  --limit-mm-per-prompt '{"image":2,"video":0}'
```

运行:

```bash
python -m iphoneclaw run \
  --instruction "打开设置并开启 Wi-Fi" \
  --base-url http://127.0.0.1:8000/v1 \
  --model qwen-vl
```

## 快速开始

```bash
# 1) 权限检查
python -m iphoneclaw doctor

# 2) 启动并验证窗口识别
python -m iphoneclaw launch --app 'iPhone镜像'

# 3) 测试截图（包含自动裁剪白边校准）
python -m iphoneclaw screenshot --out /tmp/shot.jpg

# 4) 运行 worker
python -m iphoneclaw run \
  --instruction "打开设置并开启 Wi-Fi"
```

## CLI 命令一览

```
iphoneclaw doctor          检查 macOS 权限
iphoneclaw launch          启动目标 App 并输出窗口 bounds
iphoneclaw bounds          输出窗口 bounds (x y w h)
iphoneclaw screenshot      截图（窗口 -> JPEG）
iphoneclaw calibrate       截图 + 坐标映射信息
iphoneclaw windows         枚举可见窗口（调试用）
iphoneclaw run             运行 agent loop + supervisor API
iphoneclaw serve           只启动 supervisor API（不跑 worker）
iphoneclaw ctl             通过 supervisor API 控制/查看 worker
```

## Supervisor API

worker 默认在 `127.0.0.1:17334` 暴露 HTTP API，用于监控与干预（只返回文本，不返回截图）:

```bash
# 查看最近 N 轮上下文（文本）
python -m iphoneclaw ctl context --tail 5

# 暂停 / 继续 / 停止
python -m iphoneclaw ctl pause
python -m iphoneclaw ctl resume
python -m iphoneclaw ctl stop

# 注入指导上下文（下一次模型调用会带上）
python -m iphoneclaw ctl inject --text "只打开 Wi-Fi，不要修改其他设置。" --resume
```

SSE 事件流: `GET /v1/agent/events`

## macOS 打字（AppleScript）

如果 CGEvent/剪贴板打字不稳定，可以启用 **System Events** AppleScript。默认使用 in-process `NSAppleScript`（权限归因到当前终端/python 进程）。

```bash
export IPHONECLAW_APPLESCRIPT_MODE=native    # 默认
export IPHONECLAW_APPLESCRIPT_MODE=osascript # 通过 /usr/bin/osascript fallback
```

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `IPHONECLAW_MODEL_BASE_URL` | 模型 API base URL | `http://localhost:8000/v1` |
| `IPHONECLAW_MODEL_API_KEY` | 模型 API key | (空) |
| `IPHONECLAW_MODEL_NAME` | 模型名 | `doubao-1-5-ui-tars-250428` |
| `IPHONECLAW_TARGET_APP` | 要控制的 macOS 应用名 | `iPhone Mirroring` |
| `IPHONECLAW_WINDOW_CONTAINS` | 窗口匹配子串 | (空) |
| `IPHONECLAW_SUPERVISOR_HOST` | Supervisor host | `127.0.0.1` |
| `IPHONECLAW_SUPERVISOR_PORT` | Supervisor port | `17334` |
| `IPHONECLAW_SUPERVISOR_TOKEN` | Supervisor bearer token | (空) |
| `IPHONECLAW_RECORD_DIR` | 运行记录目录 | `./runs` |
| `IPHONECLAW_APPLESCRIPT_MODE` | 打字模式: native/osascript | `native` |
| `IPHONECLAW_RESTORE_CURSOR` | 每步操作后恢复鼠标位置（1/0） | `0` |
| `IPHONECLAW_AUTO_PAUSE_ON_USER_INPUT` | 用户触碰鼠标/键盘时自动暂停（1/0） | `0` |
| `IPHONECLAW_TYPE_ASCII_ONLY` | 禁止在 `type(content=...)` 里输出中文（用拼音 + 输入法候选）(1/0) | `1` |
| `IPHONECLAW_SCROLL_INVERT_Y` | 反转竖向滚轮方向（1/0） | `0` |
| `IPHONECLAW_SCROLL_FOCUS_CLICK` | 滚动前点击聚焦（风险：可能点进视频/条目）(1/0) | `0` |

## Claude Code 集成

iphoneclaw 自带 [Claude Code skill](https://code.claude.com/docs/en/skills)，可以让 Claude 作为“老板 Agent”自动监督 worker：

1. 后台启动 iphoneclaw worker
2. 定时轮询 Supervisor API（只读文本）
3. 发现偏航时 `pause/resume/inject` 干预
4. 完成后给出简洁总结

Skill 默认在 `.claude/skills/iphoneclaw/SKILL.md`，Claude Code 打开本项目会自动发现。跨项目使用可复制到用户目录：

```bash
mkdir -p ~/.claude/skills/iphoneclaw
cp .claude/skills/iphoneclaw/SKILL.md ~/.claude/skills/iphoneclaw/SKILL.md
```

用法示例：

```
/iphoneclaw 打开设置并开启 Wi-Fi
/iphoneclaw 查看电量并汇报
/iphoneclaw 打开 Safari 访问 example.com
```

确保先配置模型环境变量（`IPHONECLAW_MODEL_BASE_URL`, `IPHONECLAW_MODEL_API_KEY`, `IPHONECLAW_MODEL_NAME`）。

## 资料

- 架构/实现计划: `PLAN.md`
- Claude Code skill: `.claude/skills/iphoneclaw/SKILL.md`

## 致谢

- [UI-TARS](https://github.com/bytedance/UI-TARS)

## 许可证

Apache-2.0
