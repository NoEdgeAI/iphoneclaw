# Defaults used by Config (kept here to avoid magic numbers sprinkled across files).
MAX_LOOP_COUNT = 100

# Image resize constants (UI-TARS / UniTAR-style VLM inputs)
IMAGE_FACTOR = 28
MIN_PIXELS = 100 * IMAGE_FACTOR * IMAGE_FACTOR      # 78,400
MAX_PIXELS_V1_5 = 16384 * IMAGE_FACTOR * IMAGE_FACTOR  # 12,845,056

# Action space for V1.5 system prompt
ACTION_SPACES_V1_5 = [
    "click(start_box='<|box_start|>(x1,y1)<|box_end|>')",
    "left_double(start_box='<|box_start|>(x1,y1)<|box_end|>')",
    "right_single(start_box='<|box_start|>(x1,y1)<|box_end|>')",
    "drag(start_box='<|box_start|>(x1,y1)<|box_end|>', end_box='<|box_start|>(x3,y3)<|box_end|>')",
    "iphone_home() # iPhone Home Screen (Cmd+1)",
    "iphone_app_switcher() # iPhone App Switcher (Cmd+2)",
    "hotkey(key='ctrl c') # Split keys with a space and use lowercase. Also, do not use more than 3 keys in one hotkey action.",
    r"type(content='xxx') # Use escape characters \', \", and \n in content part to ensure we can parse the content in normal python string format. If you want to submit your input, use \n at the end of content.",
    "scroll(start_box='<|box_start|>(x1,y1)<|box_end|>', direction='down or up or right or left') # Show more information on the `direction` side.",
    "wait() # Sleep for 5s and take a screenshot to check for any changes.",
    "finished()",
    "call_user() # Submit the task and call the user when the task is unsolvable, or when you need the user's help.",
]

# Terminal action types
TERMINAL_ACTIONS = {"finished", "call_user", "error_env"}
