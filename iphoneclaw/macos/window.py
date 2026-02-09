"""Find and track the target application window on macOS."""

from __future__ import annotations

import logging
import subprocess
import time

import Quartz

from typing import Optional

from iphoneclaw.types import Rect

logger = logging.getLogger(__name__)


def _norm(s: str) -> str:
    return "".join((s or "").lower().split())


_MIRROR_STRONG_TOKENS = ("mirroring", "mirror", "镜像")


def _looks_like_iphone_mirroring(app_name: str) -> bool:
    n = _norm(app_name)
    return ("iphone" in n) and any(t in n for t in _MIRROR_STRONG_TOKENS)


def expand_app_aliases(app_name: str):
    """
    Some macOS installs/locales use different names for the same app.
    For iPhone Mirroring, we try a set of common English/Chinese aliases.
    """
    aliases = [app_name]
    if _looks_like_iphone_mirroring(app_name) or _norm(app_name) in (
        "iphonemirroring",
        "iphonemirror",
        "iphone镜像",
        "iphone鏡像",
    ):
        aliases.extend(
            [
                "iPhone Mirroring",
                "iPhone mirror",
                "iPhone Mirror",
                "iPhone镜像",
                "iPhone 鏡像",
                "iPhone 镜像",
            ]
        )
    # De-dup while preserving order
    out = []
    seen = set()
    for a in aliases:
        if a and a not in seen:
            out.append(a)
            seen.add(a)
    return out


def _as_str(x) -> str:
    try:
        return str(x or "")
    except Exception:
        return ""


def list_on_screen_windows():
    """Return raw window info entries from CGWindowListCopyWindowInfo."""
    window_list = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
    )
    return window_list or []


def _score_window(win: dict) -> float:
    bounds = win.get("kCGWindowBounds") or {}
    w = float(bounds.get("Width") or 0)
    h = float(bounds.get("Height") or 0)
    area = w * h
    layer = int(win.get("kCGWindowLayer") or 0)
    # Prefer normal windows (layer 0), then large area.
    return area - (layer * 1_000_000)


def _matches_app(win: dict, app_name: str) -> bool:
    """Best-effort match: strict owner name, then fuzzy tokens across owner/title."""
    owner = _as_str(win.get("kCGWindowOwnerName"))
    title = _as_str(win.get("kCGWindowName"))
    for alias in expand_app_aliases(app_name):
        if owner == alias:
            return True

    # Fuzzy: require at least one informative token to match.
    hay = (owner + " " + title).lower()
    # Special-case: avoid matching random "iphone*" titles (like iphoneclaw).
    # For iPhone mirroring, require iphone + one strong token (mirroring/mirror/镜像).
    if "iphone" in hay and any(t in hay for t in _MIRROR_STRONG_TOKENS):
        return True

    tokens = [t for t in app_name.lower().replace("-", " ").split(" ") if t]
    tokens = [t for t in tokens if t not in ("the", "app")]
    if not tokens:
        return False

    hit = sum(1 for t in tokens if t in hay)
    need = 1 if len(tokens) <= 1 else min(2, len(tokens))
    return hit >= need


class WindowFinder:
    """Finds and tracks a macOS window by application name."""

    def __init__(self, app_name: str = "iPhone Mirroring", window_contains: str = ""):
        self.app_name = app_name
        self.window_contains = window_contains
        self._window_id: Optional[int] = None
        self._bounds: Optional[Rect] = None
        self._last_candidate_pids = set()

    def _candidate_owner_pids(self):
        """Find running app PIDs matching app_name (fuzzy)."""
        pids = set()
        try:
            from AppKit import NSWorkspace

            # Prefer iPhone Mirroring-like matching (iphone + mirror token).
            want = self.app_name.lower()
            tokens = [t for t in want.replace("-", " ").split(" ") if t]
            apps = NSWorkspace.sharedWorkspace().runningApplications()
            for ra in apps:
                name = (ra.localizedName() or "").lower()
                if not name:
                    continue
                match = False
                for alias in expand_app_aliases(self.app_name):
                    a = alias.lower()
                    if name == a or a in name or name in a:
                        match = True
                        break
                if not match:
                    if "iphone" in name and any(t in name for t in _MIRROR_STRONG_TOKENS):
                        match = True
                if not match and tokens:
                    # Fallback: token match
                    hit = sum(1 for t in tokens if t in name)
                    need = 1 if len(tokens) <= 1 else min(2, len(tokens))
                    match = hit >= need
                if match:
                    try:
                        pids.add(int(ra.processIdentifier()))
                    except Exception:
                        pass
        except Exception:
            pass
        self._last_candidate_pids = pids
        return pids

    def launch_app(self) -> None:
        """Launch the target app if not already running."""
        logger.info("Launching %s...", self.app_name)
        last_err = None
        for alias in expand_app_aliases(self.app_name):
            try:
                subprocess.run(["open", "-a", alias], check=True, capture_output=True)
                last_err = None
                break
            except Exception as e:
                last_err = e
                continue
        if last_err is not None:
            raise RuntimeError("Failed to launch app via `open -a`: %s" % last_err)
        # Wait for the window to appear
        for _ in range(20):
            time.sleep(0.5)
            try:
                self.find_window()
                logger.info("Window found for %s", self.app_name)
                return
            except RuntimeError:
                continue
        # On some macOS versions, iPhone Mirroring window owner/title may not match
        # the app name exactly. Provide a diagnostic hint.
        sample = []
        for win in list_on_screen_windows():
            owner = _as_str(win.get("kCGWindowOwnerName"))
            title = _as_str(win.get("kCGWindowName"))
            bounds = win.get("kCGWindowBounds") or {}
            w = int(bounds.get("Width") or 0)
            h = int(bounds.get("Height") or 0)
            if w >= 200 and h >= 200:
                sample.append((owner, title, w, h))
            if len(sample) >= 8:
                break
        raise RuntimeError(
            f"Timed out waiting for '{self.app_name}' window to appear. "
            f"Try `python -m iphoneclaw windows --contains iphone` to inspect CGWindowList. "
            f"Sample visible windows: {sample}"
        )

    def activate_app(self) -> None:
        """Bring the target app to the foreground."""
        aliases = [str(a or "") for a in expand_app_aliases(self.app_name)]
        aliases_l = [a.strip().lower() for a in aliases if a.strip()]

        try:
            from AppKit import NSWorkspace

            workspace = NSWorkspace.sharedWorkspace()
            apps = workspace.runningApplications()
            activate_opts = 0
            try:
                from AppKit import NSApplicationActivateAllWindows
                from AppKit import NSApplicationActivateIgnoringOtherApps

                activate_opts = int(NSApplicationActivateAllWindows) | int(
                    NSApplicationActivateIgnoringOtherApps
                )
            except Exception:
                activate_opts = 0

            for ra in apps:
                name = str((ra.localizedName() or "")).strip().lower()
                if not name:
                    continue
                matched = any((name == a) or (name in a) or (a in name) for a in aliases_l)
                if not matched and _looks_like_iphone_mirroring(self.app_name):
                    matched = ("iphone" in name) and any(t in name for t in _MIRROR_STRONG_TOKENS)
                if not matched:
                    continue
                try:
                    ra.activateWithOptions_(activate_opts)
                    time.sleep(0.08)
                    return
                except Exception:
                    continue
        except Exception:
            pass

        # Fallback 1: AppleScript activate (sometimes stronger than NSWorkspace activation).
        for alias in aliases:
            if not alias:
                continue
            try:
                subprocess.run(
                    ["osascript", "-e", 'tell application "%s" to activate' % alias],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                time.sleep(0.08)
                return
            except Exception:
                continue

        # Fallback 2: open -a
        for alias in aliases:
            if not alias:
                continue
            try:
                subprocess.run(["open", "-a", alias], check=False, capture_output=True, text=True)
                time.sleep(0.08)
                return
            except Exception:
                continue

    def find_window(self) -> dict:
        """Find the target window. Returns the raw CGWindowList info dict."""
        window_list = list_on_screen_windows()
        if not window_list:
            raise RuntimeError(
                "CGWindowListCopyWindowInfo returned empty — "
                "Screen Recording permission may be missing"
            )

        candidate_pids = set()
        if not self.window_contains:
            candidate_pids = self._candidate_owner_pids()
        best = None
        best_score = float("-inf")

        for win in window_list:
            owner = _as_str(win.get("kCGWindowOwnerName"))
            title = _as_str(win.get("kCGWindowName"))
            hay = (owner + " " + title).lower()

            if self.window_contains:
                if self.window_contains.lower() not in hay:
                    continue
            elif candidate_pids:
                try:
                    owner_pid = int(win.get("kCGWindowOwnerPID") or 0)
                except Exception:
                    owner_pid = 0
                if owner_pid not in candidate_pids and not _matches_app(win, self.app_name):
                    continue
            else:
                if not _matches_app(win, self.app_name):
                    continue
            # Prefer layer 0, but do not exclude other layers.
            bounds = win.get("kCGWindowBounds", {})
            w = bounds.get("Width", 0)
            h = bounds.get("Height", 0)
            if w < 50 or h < 50:
                continue
            # Score: prioritize big windows, but down-rank non-layer-0.
            score = _score_window(win)
            if score > best_score:
                best_score = score
                best = win

        if best is None:
            raise RuntimeError(
                f"No window found for '{self.app_name}'. "
                "Is the app running and visible? "
                "Try `python -m iphoneclaw windows --contains iphone` to inspect CGWindowList."
            )

        self._window_id = best["kCGWindowNumber"]
        b = best["kCGWindowBounds"]
        self._bounds = Rect(
            x=float(b["X"]),
            y=float(b["Y"]),
            width=float(b["Width"]),
            height=float(b["Height"]),
        )
        return best

    def refresh(self) -> Rect:
        """Re-query window bounds (user may have moved/resized the window)."""
        self.find_window()
        return self.bounds

    @property
    def window_id(self) -> int:
        if self._window_id is None:
            self.find_window()
        return self._window_id  # type: ignore[return-value]

    @property
    def bounds(self) -> Rect:
        if self._bounds is None:
            self.find_window()
        return self._bounds  # type: ignore[return-value]
