from __future__ import annotations

import sys
import time
from collections import deque
from typing import Any, Dict, List, Optional

from iphoneclaw.agent.conversation import ConversationStore
from iphoneclaw.agent.executor import execute_action
from iphoneclaw.agent.recorder import RunRecorder
from iphoneclaw.config import Config
from iphoneclaw.macos.capture import ScreenCapture
from iphoneclaw.macos.user_input_monitor import UserInputMonitor
from iphoneclaw.macos.window import WindowFinder
from iphoneclaw.model.client import OpenAICompatClient, invoke_model
from iphoneclaw.model.image import data_url_from_jpeg_base64, resize_jpeg_base64, smart_resize
from iphoneclaw.model.prompt_v15 import system_prompt_v15
from iphoneclaw.parse.action_parser import parse_predictions
from iphoneclaw.supervisor.hub import SupervisorHub
from iphoneclaw.supervisor.state import WorkerControl
from iphoneclaw.automation.router import L0Router
from iphoneclaw.automation.action_script import expand_special_predictions
from iphoneclaw.types import StatusEnum


class Worker:
    def __init__(
        self,
        cfg: Config,
        *,
        hub: Optional[SupervisorHub] = None,
        control: Optional[WorkerControl] = None,
        recorder: Optional[RunRecorder] = None,
        conversation: Optional[ConversationStore] = None,
    ) -> None:
        self.cfg = cfg
        self.hub = hub or SupervisorHub()
        self.control = control or WorkerControl()
        self.recorder = recorder or RunRecorder(cfg)
        self.conversation = conversation or ConversationStore()

        self.wf = WindowFinder(app_name=cfg.target_app, window_contains=cfg.window_contains)
        self.cap = ScreenCapture(self.wf)
        self._monitor: Optional[UserInputMonitor] = None

        self.client = OpenAICompatClient(cfg.model_base_url, cfg.model_api_key, cfg.model_name)
        self.system = system_prompt_v15(cfg.language)
        self._vision_image_url_as_string = ("volces.com" in cfg.model_base_url.lower()) or (
            "doubao" in cfg.model_name.lower()
        )
        self._sent_type_ascii_guidance = False

        # L0 automation: in-run memoization
        self._l0: Optional[L0Router] = None
        if cfg.automation_enable and cfg.automation_l0_enable:
            self._l0 = L0Router(
                hash_threshold=cfg.automation_hash_threshold,
                max_reuse=cfg.automation_max_reuse,
            )

    def _publish_conv(self, role: str, text: str) -> None:
        self.hub.publish("conversation", {"role": role, "text": text})

    def _vision_msg(self, instruction: str, image_b64: str) -> Dict[str, Any]:
        img_url = data_url_from_jpeg_base64(image_b64)
        if self._vision_image_url_as_string:
            return {
                "role": "user",
                "content": [
                    {"type": "text", "text": instruction},
                    {"type": "image_url", "image_url": img_url},
                ],
            }
        return {
            "role": "user",
            "content": [
                {"type": "text", "text": instruction},
                {"type": "image_url", "image_url": {"url": img_url}},
            ],
        }

    def run(self, instruction: str) -> None:
        self.control.set_status(StatusEnum.RUNNING)
        self.hub.set_status(self.control.snapshot()["status"])

        if getattr(self.cfg, "auto_pause_on_user_input", False):
            def _on_act(a) -> None:
                snap = self.control.snapshot()
                if snap.get("paused") or snap.get("stopped"):
                    return
                if snap.get("status") != StatusEnum.RUNNING.value:
                    return
                self.control.pause()
                snap2 = self.control.snapshot()
                kind = getattr(a, "kind", "")
                pos = getattr(a, "pos", None)
                payload = {"reason": "user_input", "kind": kind, "pos": pos}

                # 1) Print (so the local operator immediately understands what happened).
                try:
                    print(
                        f"[iphoneclaw] auto-paused due to user input (kind={kind} pos={pos}). "
                        f"Use `python -m iphoneclaw ctl resume` to continue.",
                        file=sys.stderr,
                        flush=True,
                    )
                except Exception:
                    pass

                # 2) Persist to run logs (runs/.../events.jsonl).
                try:
                    self.recorder.log_event("auto_pause", payload)
                except Exception:
                    pass

                # 3) Publish to SSE.
                self.hub.set_status(snap2["status"])
                self.hub.publish("auto_pause", payload)

            self._monitor = UserInputMonitor(on_activity=_on_act)
            self._monitor.start()

        # Top-level guard: never crash silently.
        try:
            self.wf.launch_app()
        except Exception as e:
            self.control.set_status(StatusEnum.ERROR)
            self.hub.set_status(self.control.snapshot()["status"], error=str(e))
            self.hub.publish("error", {"where": "launch_app", "error": str(e)})
            if self._monitor:
                self._monitor.stop()
            return

        self.conversation.add("system", self.system)
        self.recorder.log_conversation("system", self.system)

        self.conversation.add("user", instruction)
        self.recorder.log_conversation("user", instruction)
        self._publish_conv("user", instruction)

        step = 0
        parse_err_streak = 0
        recent_sigs: "deque[str]" = deque(maxlen=16)
        repeat_streak = 0
        last_sig = ""
        while True:
            try:
                if self.control.snapshot()["stopped"]:
                    self.control.set_status(StatusEnum.USER_STOPPED)
                    self.hub.set_status(self.control.snapshot()["status"])
                    if self._monitor:
                        self._monitor.stop()
                    return

                # Pause / Hang gate at step boundaries
                while self.control.snapshot()["paused"]:
                    time.sleep(0.2)
                    if self.control.snapshot()["stopped"]:
                        self.control.set_status(StatusEnum.USER_STOPPED)
                        self.hub.set_status(self.control.snapshot()["status"])
                        if self._monitor:
                            self._monitor.stop()
                        return

                injected = self.control.pop_injected()
                if injected:
                    txt = "[Supervisor Guidance]\n" + injected
                    self.conversation.add("user", txt, injected=True)
                    self.recorder.log_conversation("user", txt, injected=True)
                    self._publish_conv("user", txt)

                step += 1
                if step > int(self.cfg.max_loop_count):
                    self.control.set_status(StatusEnum.ERROR)
                    self.hub.set_status(self.control.snapshot()["status"], error="max_loop_count")
                    if self._monitor:
                        self._monitor.stop()
                    return

                shot = self.cap.capture()
                self.recorder.write_step(step, screenshot=shot)

                # ---- L0 automation: in-run memoization ----
                pre_fp: Optional[int] = None
                if self._l0 is not None:
                    pre_fp = self._l0.fingerprint(shot.base64)
                    l0_entry = self._l0.try_cache(pre_fp, step)
                    if l0_entry is not None:
                        self.recorder.log_event("automation_hit", {
                            "step": step,
                            "fingerprint": pre_fp,
                            "cached_fingerprint": l0_entry.fingerprint,
                            "hit_count": l0_entry.hit_count,
                        })

                        cached_action_strs = [p.raw_action for p in l0_entry.actions]
                        if self.cfg.automation_verbose:
                            print(
                                f"[iphoneclaw] L0 cache HIT step={step} "
                                f"hit#{l0_entry.hit_count + 1} "
                                f"action={'; '.join(cached_action_strs)}",
                                file=sys.stderr, flush=True,
                            )
                        synthetic_text = (
                            "[L0-cache] reusing cached action (hit #%d)\n"
                            "Thought: Screen matches cached fingerprint. "
                            "Replaying known-good action.\n"
                            "Action: %s"
                        ) % (l0_entry.hit_count + 1, "; ".join(cached_action_strs))
                        self.recorder.write_step(step, raw_model_text=synthetic_text)

                        l0_actions_payload: List[Dict[str, Any]] = []
                        for p in l0_entry.actions:
                            l0_actions_payload.append({
                                "action_type": p.action_type,
                                "raw_action": p.raw_action,
                                "thought": p.thought,
                                "inputs": p.action_inputs.__dict__,
                                "source": "l0_cache",
                            })
                        self.recorder.write_step(
                            step, action={"actions": l0_actions_payload, "source": "l0_cache"},
                        )

                        l0_exec_ok = True
                        l0_exec_results: List[Dict[str, Any]] = []
                        for pred in l0_entry.actions:
                            if pred.action_type in ("finished", "call_user", "error_env"):
                                l0_exec_ok = False
                                break
                            self.wf.activate_app()
                            res = execute_action(self.cfg, pred, shot)
                            self.recorder.log_event("exec", res)
                            l0_exec_results.append(res)

                            sig = f"{pred.action_type}|{(pred.raw_action or '').strip()}"
                            recent_sigs.append(sig)
                            if sig == last_sig:
                                repeat_streak += 1
                            else:
                                repeat_streak = 1
                                last_sig = sig

                            if not res.get("ok"):
                                l0_exec_ok = False
                                break

                        if l0_exec_ok:
                            time.sleep(float(self.cfg.loop_interval_ms) / 1000.0)
                            try:
                                verify_shot = self.cap.capture()
                                post_fp = self._l0.fingerprint(verify_shot.base64)
                            except Exception:
                                post_fp = None
                            verified = self._l0.verify_and_commit(
                                l0_entry, post_fp, step, success=True,
                            )
                        else:
                            self._l0.verify_and_commit(
                                l0_entry, None, step, success=False,
                            )
                            verified = False

                        if l0_exec_results:
                            if len(l0_exec_results) == 1:
                                self.recorder.write_step(
                                    step, exec_result=l0_exec_results[0],
                                )
                            else:
                                self.recorder.write_step(
                                    step,
                                    exec_result={"exec_results": l0_exec_results},
                                )

                        if verified:
                            self.recorder.log_event(
                                "automation_verify_ok", {"step": step},
                            )
                            if self.cfg.automation_verbose:
                                print(
                                    f"[iphoneclaw] L0 verify OK step={step} "
                                    f"(VLM call skipped)",
                                    file=sys.stderr, flush=True,
                                )
                            self.control.set_status(StatusEnum.RUNNING)
                            self.hub.set_status(
                                self.control.snapshot()["status"], step=step,
                            )
                            continue

                        self.recorder.log_event("automation_verify_fail", {
                            "step": step, "exec_ok": l0_exec_ok,
                        })
                        if self.cfg.automation_verbose:
                            print(
                                f"[iphoneclaw] L0 verify FAIL step={step} "
                                f"exec_ok={l0_exec_ok}, falling back to VLM",
                                file=sys.stderr, flush=True,
                            )
                        try:
                            shot = self.cap.capture()
                            self.recorder.write_step(step, screenshot=shot)
                        except Exception:
                            pass
                    else:
                        self.recorder.log_event("automation_miss", {
                            "step": step, "fingerprint": pre_fp,
                        })
                # ---- End L0 automation ----

                # Resize image to match pixel budget before sending to model.
                send_b64 = shot.base64
                tw, th = smart_resize(int(shot.image_width), int(shot.image_height))
                if tw and th and (tw != shot.image_width or th != shot.image_height):
                    send_b64 = resize_jpeg_base64(shot.base64, tw, th)

                # Build messages
                messages: List[Dict[str, Any]] = [{"role": "system", "content": self.system}]
                tail = self.conversation.to_openai_messages(include_system=False, tail_rounds=8)
                messages.extend(tail)
                messages.append(self._vision_msg("Current screen. Decide next action.", send_b64))

                extra_body = None
                if "volces.com" in self.cfg.model_base_url.lower():
                    extra_body = {"thinking": {"type": self.cfg.volc_thinking_type}}

                inv = invoke_model(
                    self.client,
                    messages,
                    max_tokens=self.cfg.max_tokens,
                    temperature=self.cfg.temperature,
                    top_p=self.cfg.top_p,
                    parse_fn=parse_predictions,
                    extra_body=extra_body,
                )

                self.recorder.write_step(step, raw_model_text=inv.prediction)
                self.recorder.log_event(
                    "model",
                    {"tokens": inv.cost_tokens, "dt": inv.cost_time},
                )

                # Log assistant text
                self.conversation.add("assistant", inv.prediction)
                self.recorder.log_conversation("assistant", inv.prediction)
                self._publish_conv("assistant", inv.prediction)

                preds = inv.parsed_predictions
                # Record all actions for this step in action.json so supervisors can debug
                # multi-action sequences (double-click, click+sleep+click, etc).
                actions_payload: List[Dict[str, Any]] = []
                for p in preds:
                    actions_payload.append(
                        {
                            "action_type": p.action_type,
                            "raw_action": p.raw_action,
                            "thought": p.thought,
                            "inputs": p.action_inputs.__dict__,
                        }
                    )
                self.recorder.write_step(step, action={"actions": actions_payload})

                # If all parsed actions are parse errors, treat as a parse error step.
                non_err = [p for p in preds if p.action_type != "error_env"]
                if not non_err:
                    parse_err_streak += 1
                    raw0 = preds[0].raw_action if preds else ""
                    self.hub.publish("error", {"where": "parse", "streak": parse_err_streak, "raw": raw0})
                    if parse_err_streak >= 3:
                        self.control.set_status(StatusEnum.HANG)
                        self.control.pause()
                        self.hub.set_status(self.control.snapshot()["status"], reason="parse_error_streak")
                        self.hub.publish("hang", {"reason": "parse_error_streak"})
                    continue
                parse_err_streak = 0

                # Execute each action sequentially (same screenshot mapping) until a terminal/hang.
                exec_results: List[Dict[str, Any]] = []
                # Expand run_script(...) into concrete actions before execution and caching.
                try:
                    non_err = expand_special_predictions(
                        non_err, registry_path=str(getattr(self.cfg, "script_registry_path", "./action_scripts/registry.json"))
                    )
                except Exception as e:
                    payload = {"reason": "run_script_error", "error": str(e), "step": step}
                    self.recorder.log_event("needs_supervisor", payload)
                    self.hub.publish("error", {"where": "run_script", **payload})
                    self.control.set_status(StatusEnum.HANG)
                    self.control.pause()
                    self.hub.set_status(self.control.snapshot()["status"], **payload)
                    self.hub.publish("hang", payload)
                    continue

                for pred in non_err:
                    # Terminal actions with hang semantics
                    if pred.action_type == "finished":
                        if self.cfg.hang_on_finished:
                            self.control.set_status(StatusEnum.HANG)
                            self.control.pause()
                            self.hub.set_status(self.control.snapshot()["status"])
                            self.hub.publish("hang", {"reason": "finished"})
                            break
                        self.control.set_status(StatusEnum.END)
                        self.hub.set_status(self.control.snapshot()["status"])
                        if self._monitor:
                            self._monitor.stop()
                        return

                    if pred.action_type == "call_user":
                        if self.cfg.hang_on_call_user:
                            self.control.set_status(StatusEnum.HANG)
                            self.control.pause()
                            self.hub.set_status(self.control.snapshot()["status"])
                            self.hub.publish("hang", {"reason": "call_user"})
                            break
                        self.control.set_status(StatusEnum.CALL_USER)
                        self.hub.set_status(self.control.snapshot()["status"])
                        if self._monitor:
                            self._monitor.stop()
                        return

                    self.wf.activate_app()
                    res = execute_action(self.cfg, pred, shot)
                    self.recorder.log_event("exec", res)
                    exec_results.append(res)

                    # Repeated-action loop detection: trigger only after we actually executed the action.
                    # This avoids blocking actions that are legitimately needed multiple times (e.g. scroll
                    # a few times to load more comments) while still pausing true dead-loops.
                    sig = f"{pred.action_type}|{(pred.raw_action or '').strip()}"
                    recent_sigs.append(sig)
                    if sig == last_sig:
                        repeat_streak += 1
                    else:
                        repeat_streak = 1
                        last_sig = sig
                    if (
                        getattr(self.cfg, "auto_pause_on_repeat_action", True)
                        and repeat_streak >= int(getattr(self.cfg, "repeat_action_streak_threshold", 4))
                        and pred.action_type not in ("finished", "call_user")
                    ):
                        payload = {
                            "reason": "repeat_action_streak",
                            "streak": repeat_streak,
                            "signature": sig,
                            "recent": list(recent_sigs)[-8:],
                            "step": step,
                            "run_id": getattr(self.recorder, "run_id", ""),
                        }
                        try:
                            print(
                                "[iphoneclaw] auto-paused due to repeated identical actions "
                                f"(streak={repeat_streak}). Use `python -m iphoneclaw ctl context --tail 5` "
                                "and inject guidance, then `python -m iphoneclaw ctl resume`.",
                                file=sys.stderr,
                                flush=True,
                            )
                        except Exception:
                            pass
                        try:
                            self.recorder.log_event("needs_supervisor", payload)
                        except Exception:
                            pass
                        self.control.set_status(StatusEnum.HANG)
                        self.control.pause()
                        # NOTE: payload already contains "reason"; do not pass duplicate kwargs.
                        self.hub.set_status(self.control.snapshot()["status"], **payload)
                        self.hub.publish("needs_supervisor", payload)
                        break

                    if not res.get("ok"):
                        err = str(res.get("error") or "")
                        self.hub.publish("error", {"where": "exec", "error": err, "step": step})

                        # If we blocked non-ASCII typing, guide the model to use IME (pinyin) instead.
                        if pred.action_type == "type" and ("ASCII only" in err) and (not self._sent_type_ascii_guidance):
                            self._sent_type_ascii_guidance = True
                            txt = (
                                "[System Constraint]\n"
                                "Typing constraint: `type(content=...)` must be ASCII only.\n"
                                "If you need to input Chinese, type pinyin letters (ASCII) with the iPhone IME, "
                                "then select the Chinese candidate by clicking the candidate bar.\n"
                                "Do NOT output Chinese characters inside `type(content=...)`."
                            )
                            self.conversation.add("user", txt, injected=True)
                            self.recorder.log_conversation("user", txt, injected=True)
                            self._publish_conv("user", txt)

                # If we were paused/hanging mid-step, skip status reset and loop delay.
                if self.control.snapshot().get("paused") or self.control.snapshot().get("status") == StatusEnum.HANG.value:
                    self.hub.set_status(self.control.snapshot()["status"], step=step)
                    continue

                # Persist exec results for this step (single or multi-action).
                if exec_results:
                    if len(exec_results) == 1:
                        self.recorder.write_step(step, exec_result=exec_results[0])
                    else:
                        self.recorder.write_step(step, exec_result={"exec_results": exec_results})

                # Store in L0 cache after successful VLM execution.
                if self._l0 is not None and pre_fp is not None and exec_results:
                    all_ok = all(r.get("ok") for r in exec_results)
                    if all_ok and self._l0.should_cache_actions(non_err):
                        try:
                            post_shot = self.cap.capture()
                            post_fp = self._l0.fingerprint(post_shot.base64)
                            self._l0.record(pre_fp, list(non_err), post_fp, step)
                        except Exception:
                            pass

                # Update status each loop
                self.control.set_status(StatusEnum.RUNNING)
                self.hub.set_status(self.control.snapshot()["status"], step=step)

                time.sleep(float(self.cfg.loop_interval_ms) / 1000.0)
            except Exception as e:
                self.control.set_status(StatusEnum.ERROR)
                self.hub.set_status(self.control.snapshot()["status"], error=str(e), step=step)
                self.hub.publish("error", {"where": "loop", "error": str(e), "step": step})
                self.recorder.log_event("error", {"error": str(e), "step": step})
                if self._monitor:
                    self._monitor.stop()
                return
