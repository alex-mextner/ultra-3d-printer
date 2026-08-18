#!/usr/bin/env bash
# agent-browser wrapper — makes the CLI actually work on this machine.
#
# WHY THIS EXISTS. Plain `agent-browser <cmd>` hangs here, with no error, for two
# separate reasons that took a while to separate:
#
#   1. IT CANNOT FIND CHROME TO LAUNCH. Chrome is installed per-user, in
#      %LOCALAPPDATA%\Google\Chrome\Application\, not in Program Files. The CLI
#      looks in the standard locations, finds nothing, and waits instead of
#      failing. `--auto-connect` is the command that finally said so out loud:
#      "No running Chrome instance found. Launch Chrome with
#      --remote-debugging-port or use --cdp."
#
#   2. ON THE OVERCAM PAGE, `open` WAITS FOR A LOAD EVENT THAT NEVER FIRES.
#      http://192.168.11.160/overcam/mbot holds an MJPEG stream, so the document
#      never finishes loading. The navigation DOES happen - `get url` afterwards
#      returns the right address - but `open` itself sits there. Use `open` on
#      ordinary pages; for the camera page, navigate and then query, or expect
#      the timeout and ignore it.
#
# So: this script keeps a headless-capable Chrome alive on a CDP port with its
# OWN profile (the user's browser and its logged-in state are never touched) and
# passes --cdp through.
#
# Usage:
#   bash scripts/ab.sh get url
#   bash scripts/ab.sh open https://example.com
#   bash scripts/ab.sh screenshot --full
#   bash scripts/ab.sh --  (no args: just ensure Chrome is up, print its version)
#
# The port and profile can be overridden with AB_PORT / AB_PROFILE.
set -uo pipefail

AB_PORT="${AB_PORT:-9222}"
AB_PROFILE="${AB_PROFILE:-C:/cygwin64/tmp/claude/C--Users-HP-Probook-430-G8-Documents-ultra-3d-printer/66551db0-1c5c-4a36-840f-2c4f974bc8d1/scratchpad/cdp-profile}"

CHROME="${CHROME:-$LOCALAPPDATA/Google/Chrome/Application/chrome.exe}"
[ -x "$CHROME" ] || CHROME="/c/Users/$USERNAME/AppData/Local/Google/Chrome/Application/chrome.exe"

NODE_BIN="/c/Program Files/nodejs"
AB="$APPDATA/npm/agent-browser"
[ -e "$AB" ] || AB="/c/Users/$USERNAME/AppData/Roaming/npm/agent-browser"

# Is CDP already answering? Cheapest possible check, and it also tells us the
# browser build, which is worth having in any bug report.
version_json() { curl -s --max-time 3 "http://127.0.0.1:$AB_PORT/json/version" 2>/dev/null; }

if ! version_json | grep -q Browser; then
    if [ ! -x "$CHROME" ]; then
        echo "ab.sh: chrome.exe not found at $CHROME - set CHROME=" >&2
        exit 2
    fi
    # Detached, own profile, no first-run interstitials that would block startup.
    "$CHROME" --remote-debugging-port="$AB_PORT" \
              --user-data-dir="$AB_PROFILE" \
              --no-first-run --no-default-browser-check about:blank &
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        sleep 1
        version_json | grep -q Browser && break
    done
fi

v="$(version_json)"
if ! echo "$v" | grep -q Browser; then
    echo "ab.sh: CDP did not come up on port $AB_PORT" >&2
    exit 3
fi

if [ "$#" -eq 0 ] || [ "$1" = "--" ]; then
    echo "$v" | tr ',' '\n' | grep -i '"Browser"'
    exit 0
fi

PATH="$NODE_BIN:$PATH" exec "$AB" --cdp "$AB_PORT" "$@"
