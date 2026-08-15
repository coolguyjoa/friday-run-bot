"""
Run by the "Admin Controls" GitHub Actions workflow (manual trigger only).

Env vars:
  ACTION    "pause" | "resume" | "addlocation"
  LOCATION  new location name (only used when ACTION=addlocation)
"""

import os
import json

STATE_FILE = "state.json"
ACTION = os.environ["ACTION"]
LOCATION = os.environ.get("LOCATION", "").strip()

with open(STATE_FILE) as f:
    state = json.load(f)

if ACTION == "pause":
    state["paused"] = True
    print("Bot paused.")
elif ACTION == "resume":
    state["paused"] = False
    print("Bot resumed.")
elif ACTION == "addlocation":
    if not LOCATION:
        raise ValueError("LOCATION is required for addlocation")
    if LOCATION not in state["locations"]:
        state["locations"].append(LOCATION)
        print(f"Added location: {LOCATION}")
    else:
        print(f"'{LOCATION}' is already in the list.")
else:
    raise ValueError(f"Unknown ACTION: {ACTION}")

with open(STATE_FILE, "w") as f:
    json.dump(state, f, indent=2)
