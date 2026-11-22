# PlayBillDisplay settings. Sourced by update_playbill.sh on the Kindle.

# Mac running `uv run app.py`. "Install to Kindle" fills this in for you.
# Leave empty ("") for offline mode: only show what was copied over USB.
SERVER=""

# Minutes each Playbill stays up before moving to the next one.
# 1440 = one per day. 0 = never advance (stay on the current one).
ROTATE_MINUTES=1440

# 1 = jump straight to a Playbill as soon as it arrives from the Mac.
SHOW_NEW_IMMEDIATELY=1
