from __future__ import annotations

from iphoneclaw.automation.action_script import script_to_action_calls, script_to_predictions
from iphoneclaw.automation.action_script import run_script_to_predictions


def test_script_parses_compound_example() -> None:
    src = "iphone_home() sleep swipe left x 10, swipe down"
    calls = script_to_action_calls(src)
    assert calls[0] == "iphone_home()"
    assert calls[1].startswith("sleep(")
    assert calls.count("swipe(direction=\"left\")") == 10
    assert calls[-1] == "swipe(direction=\"down\")"


def test_script_open_app_macro_and_template() -> None:
    src = "open_app ${APP}"
    calls = script_to_action_calls(src, vars={"APP": "bilibili"})
    assert "iphone_home()" in calls
    assert any("type(content=" in c and "bilibili" in c for c in calls)


def test_script_to_predictions_no_error_env() -> None:
    preds = script_to_predictions("swipe left x 2\nsleep 50ms\nwait")
    assert [p.action_type for p in preds] == ["swipe", "swipe", "sleep", "wait"]


def test_run_script_expands_from_registry() -> None:
    preds = run_script_to_predictions(
        "run_script(name='iphone_home_swipe_left_10_then_down')",
        registry_path="./action_scripts/registry.json",
    )
    assert preds and preds[0].action_type == "iphone_home"
