from __future__ import annotations

import json
import os
from typing import Dict, Optional


class ScriptRegistryError(ValueError):
    pass


def _repo_root() -> str:
    # This repo layout is: <root>/iphoneclaw/automation/script_registry.py
    here = os.path.abspath(os.path.dirname(__file__))
    return os.path.abspath(os.path.join(here, "..", ".."))


def default_registry_path() -> str:
    return os.path.join(_repo_root(), "action_scripts", "registry.json")


def load_registry(path: Optional[str]) -> Dict[str, str]:
    """
    Load registry mapping short name -> script path (relative to registry dir).
    If file is missing, returns empty mapping (caller decides behavior).
    """
    if not path:
        path = default_registry_path()
    p = os.path.abspath(path)
    if not os.path.exists(p) and path == "./action_scripts/registry.json":
        # Common case: started from somewhere else. Try repo-local default.
        p2 = default_registry_path()
        if os.path.exists(p2):
            p = p2

    if not os.path.exists(p):
        return {}

    try:
        with open(p, "r", encoding="utf-8") as f:
            obj = json.load(f)
    except Exception as e:
        raise ScriptRegistryError("failed to load registry: %s" % str(e)) from e

    if not isinstance(obj, dict):
        raise ScriptRegistryError("registry must be a JSON object (name -> path)")

    out: Dict[str, str] = {}
    for k, v in obj.items():
        if not isinstance(k, str) or not k.strip():
            continue
        if not isinstance(v, str) or not v.strip():
            continue
        out[k.strip()] = v.strip()
    return out


def resolve_script_path(
    name_or_path: str,
    *,
    registry_path: Optional[str],
) -> str:
    """
    Resolve a script from a short name (via registry) or a file path.
    Returns an absolute path.
    """
    if not isinstance(name_or_path, str) or not name_or_path.strip():
        raise ScriptRegistryError("script name/path is empty")
    key = name_or_path.strip()

    reg_path = registry_path or default_registry_path()
    reg_path_abs = os.path.abspath(reg_path)
    reg_dir = os.path.dirname(reg_path_abs)
    reg = load_registry(reg_path)

    # Registry hit
    if key in reg:
        rel = reg[key]
        p = rel
        if not os.path.isabs(p):
            p = os.path.join(reg_dir, p)
        p = os.path.abspath(p)
        if not os.path.exists(p):
            raise ScriptRegistryError("registry entry points to missing file: %s -> %s" % (key, p))
        return p

    # Fallback: treat as a path relative to registry dir, then cwd.
    cand = key
    if not os.path.isabs(cand):
        p1 = os.path.abspath(os.path.join(reg_dir, cand))
        if os.path.exists(p1):
            return p1
        p2 = os.path.abspath(cand)
        if os.path.exists(p2):
            return p2
    else:
        cand_abs = os.path.abspath(cand)
        if os.path.exists(cand_abs):
            return cand_abs

    raise ScriptRegistryError("unknown script %r (not in registry, file not found)" % key)

