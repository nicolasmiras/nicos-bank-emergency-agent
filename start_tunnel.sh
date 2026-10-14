#!/bin/zsh
# Opens a public Cloudflare tunnel to the mock core (localhost:8787)
# and points the agent's webhook tools at it. Leave it running during the demo.
cd "$(dirname "$0")"
LOG=$(mktemp)
/opt/homebrew/bin/cloudflared tunnel --no-autoupdate --url http://localhost:8787 > "$LOG" 2>&1 &
TUNNEL_PID=$!
trap "kill $TUNNEL_PID 2>/dev/null; rm -f $LOG" EXIT

echo "Opening tunnel..."
for i in {1..30}; do
  URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" | head -1)
  [[ -n "$URL" ]] && break
  sleep 1
done
[[ -z "$URL" ]] && { echo "Could not get a tunnel URL:"; cat "$LOG"; exit 1; }

echo "Tunnel: $URL"
python3 setup_tools.py "$URL" || exit 1
echo
echo "Ready. Keep this window open while you test (Ctrl+C to stop)."
wait $TUNNEL_PID
