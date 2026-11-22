#!/bin/zsh
set -eu
runtime="$HOME/Library/Application Support/LAN Task Center"
if [[ -n "${CODEX_HOME:-}" && "${CODEX_HOME:A}" != "${HOME:A}/.codex" ]]; then
  runtime="$CODEX_HOME/lan-task-center"
fi
if [[ ! -f "$runtime/client.py" ]]; then
  print '请先运行「接收端安装」或「发布端安装」。'
  exit 1
fi
export TASK_CENTER_STATE="$runtime"
exec /usr/bin/python3 -B "$runtime/client.py" browser
