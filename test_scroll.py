"""Test home screen swipe at different Y positions."""
import base64
import sys
import time
from iphoneclaw.macos.window import WindowFinder
from iphoneclaw.macos.capture import ScreenCapture
from iphoneclaw.macos import input_mouse


def save_shot(shot, path):
    with open(path, "wb") as f:
        f.write(base64.b64decode(shot.base64))
    print(f"  saved: {path}")

def main():
    wf = WindowFinder(app_name="iPhone Mirroring")
    wf.find_window()
    wf.activate_app()
    b = wf.bounds
    print(f"bounds: x={b.x}, y={b.y}, w={b.width}, h={b.height}")

    cap = ScreenCapture(wf)

    # Y positions to test (normalized 0-1000)
    # We'll convert to screen coords
    positions = {
        "center_500": 500,
        "below_apps_700": 700,
        "empty_area_780": 780,
        "search_bar_820": 820,
        "above_dock_870": 870,
        "dock_edge_920": 920,
    }

    test_name = sys.argv[1] if len(sys.argv) > 1 else None
    direction = sys.argv[2] if len(sys.argv) > 2 else "left"
    mode = sys.argv[3] if len(sys.argv) > 3 else "drag"

    if test_name and test_name in positions:
        targets = {test_name: positions[test_name]}
    elif test_name and test_name.isdigit():
        targets = {f"custom_{test_name}": int(test_name)}
    else:
        print(f"Usage: python test_scroll.py <position> [direction] [mode]")
        print(f"Positions: {list(positions.keys())}")
        print(f"Or provide a custom normalized Y value (0-1000)")
        print(f"Direction: left/right/up/down (default: left)")
        print(f"Mode: drag/wheel (default: drag)")
        return

    for name, norm_y in targets.items():
        norm_x = 500
        screen_x = b.x + (norm_x / 1000.0) * b.width
        screen_y = b.y + (norm_y / 1000.0) * b.height
        print(f"\n=== Testing '{name}': normalized=({norm_x},{norm_y}), screen=({screen_x:.1f},{screen_y:.1f}) ===")
        print(f"Direction: {direction}, Mode: {mode}")

        # Screenshot before
        shot = cap.capture()
        before_path = f"/tmp/scroll_test_{name}_before.jpg"
        save_shot(shot, before_path)
        print(f"Before: {before_path}")

        time.sleep(0.5)
        wf.activate_app()
        time.sleep(0.3)

        if mode == "drag":
            # Swipe gesture
            dist_x = b.width * 0.55  # horizontal swipe distance
            dist_y = b.height * 0.35  # vertical swipe distance
            sx, sy = screen_x, screen_y

            if direction == "left":
                ex, ey = sx - dist_x, sy
            elif direction == "right":
                ex, ey = sx + dist_x, sy
            elif direction == "down":
                ex, ey = sx, sy - dist_y
            elif direction == "up":
                ex, ey = sx, sy + dist_y
            else:
                print(f"Unknown direction: {direction}")
                return

            # Clamp to window
            ex = max(b.x + 2, min(ex, b.x + b.width - 2))
            ey = max(b.y + 2, min(ey, b.y + b.height - 2))

            print(f"Drag: ({sx:.1f},{sy:.1f}) -> ({ex:.1f},{ey:.1f})")
            input_mouse.mouse_drag(sx, sy, ex, ey, duration=0.18, hold_before_move_s=0.005)
        else:
            # Wheel scroll
            print(f"Wheel scroll at ({screen_x:.1f},{screen_y:.1f}), direction={direction}")
            input_mouse.mouse_scroll(
                screen_x, screen_y,
                direction=direction,
                amount=800,
                unit="pixel",
                repeat=8,
                focus_click=False,
                invert_y=False,
            )

        time.sleep(1.0)

        # Screenshot after
        shot2 = cap.capture()
        after_path = f"/tmp/scroll_test_{name}_after.jpg"
        save_shot(shot2, after_path)
        print(f"After: {after_path}")
        print(f"=== Done ===")

if __name__ == "__main__":
    main()
