#!/usr/bin/env bash
set -euo pipefail

workspace_root="$(cd "$(dirname "$0")/../.." && pwd)"
health_url="http://127.0.0.1:6080/audio/health"

if curl --fail --silent --max-time 2 "$health_url" >/dev/null; then
    exit 0
fi

if ss -H -ltn 'sport = :6080' | grep -q .; then
    printf '%s\n' 'Port 6080 is occupied by a service other than the noVNC audio server.' >&2
    exit 1
fi

log_dir="${XDG_CACHE_HOME:-$HOME/.cache}"
mkdir -p "$log_dir"
nohup /usr/bin/python3 -u "$workspace_root/tools/novnc-audio/server.py" \
    --host 0.0.0.0 --port 6080 >>"$log_dir/novnc-audio.log" 2>&1 </dev/null &
server_pid=$!

for _ in {1..20}; do
    if curl --fail --silent --max-time 2 "$health_url" >/dev/null; then
        exit 0
    fi
    if ! kill -0 "$server_pid" 2>/dev/null; then
        break
    fi
    sleep 0.25
done

printf 'noVNC audio server did not start; see %s/novnc-audio.log\n' "$log_dir" >&2
exit 1
