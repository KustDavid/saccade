#!/bin/bash
# Double-click me (Finder) to start the reading tool and open it in your browser.
# Close this Terminal window (or press Ctrl-C) to stop the server.

cd "$(dirname "$0")" || exit 1

PORT="${READING_TOOL_PORT:-8377}"
URL="http://127.0.0.1:${PORT}"

# Finder-launched scripts get a minimal PATH; look for uv in the usual places.
UV="$(command -v uv 2>/dev/null)"
if [ -z "$UV" ]; then
  for p in /opt/homebrew/bin/uv /usr/local/bin/uv "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
    if [ -x "$p" ]; then UV="$p"; break; fi
  done
fi
if [ -z "$UV" ]; then
  echo "Could not find 'uv'. Install it with:  brew install uv"
  read -r -p "Press Enter to close..."
  exit 1
fi

# Already running? Just open the browser.
if curl -s -o /dev/null --max-time 2 "${URL}/api/papers"; then
  echo "Reading tool is already running at ${URL}"
  open "${URL}"
  exit 0
fi

mkdir -p papers/inbox papers/library

# Open the browser as soon as the server answers.
(
  for _ in $(seq 1 240); do
    if curl -s -o /dev/null --max-time 1 "${URL}/api/papers"; then
      open "${URL}"
      exit 0
    fi
    sleep 0.5
  done
  echo "Server did not come up on ${URL} - see the messages above."
) &

echo "Starting reading tool on ${URL}"
echo "Drop PDFs into: $(pwd)/papers/inbox"
echo "(Close this window or press Ctrl-C to stop.)"
cd app || exit 1
exec "$UV" run --quiet python -m backend.server
