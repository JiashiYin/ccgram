#!/usr/bin/env bash
set -euo pipefail

self_pid="$$"
self_tty="$(ps -p "$self_pid" -o tty= | tr -d '[:space:]')"

is_descendant_of_self() {
  local pid="$1"
  local ppid

  while [[ -n "$pid" && "$pid" != "0" ]]; do
    if [[ "$pid" == "$self_pid" ]]; then
      return 0
    fi

    ppid="$(ps -o ppid= -p "$pid" | tr -d '[:space:]')"
    if [[ -z "$ppid" || "$ppid" == "$pid" ]]; then
      break
    fi

    pid="$ppid"
  done

  return 1
}

mapfile -t rows < <(
  ps -eo pid=,ppid=,pgid=,tty=,comm=,args= |
    awk '$5 != "awk" && $5 != "bwrap" && (/ccgram notify launch --provider codex/ || /node \/usr\/bin\/codex/ || /\/codex\/codex( |$)/) { print }'
)

if ((${#rows[@]} == 0)); then
  echo "No Codex processes found."
  exit 0
fi

found_candidate=0

for row in "${rows[@]}"; do
  read -r pid ppid pgid tty_name _args <<<"$row"

  if [[ -z "${pid:-}" || -z "${pgid:-}" || -z "${tty_name:-}" ]]; then
    continue
  fi

  if [[ "$pid" == "$self_pid" ]]; then
    continue
  fi

  if [[ -n "$self_tty" && "$self_tty" != "?" && "$tty_name" == "$self_tty" ]]; then
    continue
  fi

  if is_descendant_of_self "$pid"; then
    continue
  fi

  if [[ "$tty_name" != "?" ]]; then
    continue
  fi

  found_candidate=1
  echo "Orphan candidate: pid=$pid ppid=$ppid pgid=$pgid tty=$tty_name"
  echo "  $row"

  read -r -p "Kill process group $pgid? [y/N] " answer
  if [[ "$answer" =~ ^[Yy]$ ]]; then
    kill -TERM -- "-$pgid" || true
    sleep 2
    kill -KILL -- "-$pgid" 2>/dev/null || true
    echo "Killed process group $pgid"
  else
    echo "Skipped process group $pgid"
  fi
done

if [[ "$found_candidate" -eq 0 ]]; then
  echo "No detached Codex candidates found."
fi
