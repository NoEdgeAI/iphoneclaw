from __future__ import annotations

from typing import Optional


def maybe_rewrite_hotkey(action_type: str, key: Optional[str]) -> Optional[str]:
    """
    Optional stability layer:
    If the model emits hotkey(key='cmd 1/2'), rewrite it into explicit iPhone actions.
    """
    if action_type != "hotkey" or not key:
        return None
    k = " ".join(key.lower().strip().split())
    if k in ("cmd 1", "command 1"):
        return "iphone_home"
    if k in ("cmd 2", "command 2"):
        return "iphone_app_switcher"
    return None
