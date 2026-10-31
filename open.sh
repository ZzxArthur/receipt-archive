#!/usr/bin/env bash
# Start the local application independently of this terminal, then open it.
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$project_dir"
port="${RECEIPT_PORT:-8765}"
if [[ ! "$port" =~ ^[0-9]{1,5}$ ]] || (( 10#$port < 1 || 10#$port > 65535 )); then
  printf 'RECEIPT_PORT 必须是 1–65535 之间的端口号。\n' >&2
  exit 1
fi
url="http://127.0.0.1:$port"
runtime_dir="$project_dir/.runtime"
mkdir -p -- "$runtime_dir"
if [[ -x /usr/bin/python3 ]]; then
  launcher_python=/usr/bin/python3
else
  launcher_python=python3
fi

is_ready() {
  "$launcher_python" - "$url" <<'PY' >/dev/null 2>&1
import json, sys, urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open(sys.argv[1] + "/api/health", timeout=1) as response:
    health = json.load(response)
    if health.get("status") != "ok" or health.get("local_only") is not True:
        raise SystemExit(1)
PY
}

# A short launch lock prevents double-clicks from starting duplicate servers.
if command -v flock >/dev/null 2>&1; then
  exec 9>"$runtime_dir/launch-$port.lock"
  flock -x 9
fi

if ! is_ready; then
  unit="receipt-archive"
  [[ "$port" == 8765 ]] || unit="receipt-archive-$port"
  managed=false
  if command -v systemd-run >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    if systemctl --user is-active --quiet "$unit.service"; then
      managed=true
    elif systemd-run --user --unit="$unit" --description='凭证归档本地服务' \
      --collect --property="WorkingDirectory=$project_dir" \
      --setenv="RECEIPT_PORT=$port" --property=Restart=on-failure \
      /usr/bin/bash "$project_dir/start.sh" >>"$runtime_dir/launcher.log" 2>&1; then
      managed=true
    fi
  fi
  if [[ "$managed" == false ]]; then
    # Fallback for Linux desktops without a user systemd manager. The new
    # session and closed file descriptors keep the app independent of the UI.
    "$launcher_python" - "$project_dir" "$runtime_dir" "$port" <<'PY'
import os, pathlib, subprocess, sys
project, runtime, port = sys.argv[1:]
environment = dict(os.environ, RECEIPT_PORT=port)
with open(pathlib.Path(runtime) / "server.log", "ab", buffering=0) as log:
    process = subprocess.Popen(["/usr/bin/bash", str(pathlib.Path(project) / "start.sh")],
        cwd=project, env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        start_new_session=True, close_fds=True)
(pathlib.Path(runtime) / ("server-" + port + ".pid")).write_text(str(process.pid))
PY
  fi
  printf '正在启动凭证归档，请稍候…\n'
  for ((attempt = 0; attempt < 120; attempt++)); do
    if is_ready; then break; fi
    sleep 0.5
  done
fi

if ! is_ready; then
  printf '服务启动未完成。请在项目目录运行 ./start.sh 查看原因。\n启动日志：%s\n' "$runtime_dir" >&2
  exit 1
fi
printf '凭证归档已就绪：%s\n' "$url"
if [[ "${RECEIPT_NO_BROWSER:-0}" != 1 ]] && command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$url" >/dev/null 2>&1 || printf '请在本机浏览器打开 %s\n' "$url"
fi
