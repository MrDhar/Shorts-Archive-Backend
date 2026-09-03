#!/bin/sh
set -eu

# Start the bgutil PO-token provider locally. yt-dlp will use it via 127.0.0.1:4416.
node /opt/bgutil-ytdlp-pot-provider/server/build/main.js --port 4416 >/tmp/bgutil.log 2>&1 &
POT_PID=$!

# Stream its log to stdout (tagged) so it shows up in Render logs instead of
# being hidden inside /tmp/bgutil.log where nobody can see it -- including
# crashes/errors that happen after the initial startup check below.
tail -n +1 -F /tmp/bgutil.log 2>/dev/null | sed -u 's/^/[bgutil] /' &

# Give the provider a moment to bind before serving requests.
i=0
bound=0
while [ "$i" -lt 30 ]; do
  if node -e "const net=require('net'); const s=net.connect(4416,'127.0.0.1'); s.on('connect',()=>{s.end();process.exit(0)}); s.on('error',()=>process.exit(1)); setTimeout(()=>process.exit(1),500)"; then
    bound=1
    break
  fi
  i=$((i+1))
  sleep 1
done

if ! kill -0 "$POT_PID" 2>/dev/null; then
  echo "bgutil provider process died during startup:" >&2
  cat /tmp/bgutil.log >&2 || true
  exit 1
fi

if [ "$bound" -ne 1 ]; then
  echo "bgutil provider process is alive but never bound port 4416 after 30s -- continuing anyway, but PO tokens will fail:" >&2
  cat /tmp/bgutil.log >&2 || true
fi

PORT="${PORT:-8000}"
exec python3 -m uvicorn app:app --host 0.0.0.0 --port "$PORT"
