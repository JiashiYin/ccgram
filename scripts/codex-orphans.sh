#!/usr/bin/env bash
set -euo pipefail

self_pid="$$"
self_uid="$(id -u)"
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

is_ancestor_of_self() {
  local target="$1"
  local pid="$self_pid"
  local ppid

  while [[ -n "$pid" && "$pid" != "0" ]]; do
    ppid="$(ps -o ppid= -p "$pid" | tr -d '[:space:]')"
    if [[ -z "$ppid" || "$ppid" == "$pid" ]]; then
      break
    fi

    if [[ "$ppid" == "$target" ]]; then
      return 0
    fi

    pid="$ppid"
  done

  return 1
}

is_in_self_lineage() {
  local pid="$1"

  is_descendant_of_self "$pid" || is_ancestor_of_self "$pid"
}

mapfile -t rows < <(
  ps -eo pid=,ppid=,uid=,pgid=,tty=,comm=,args= |
    awk -v self_uid="$self_uid" '$3 == self_uid && $6 != "awk" && $6 != "bwrap" && (/ccgram notify launch --provider codex/ || /node \/usr\/bin\/codex/ || /\/codex\/codex( |$)/) { print }'
)

if ((${#rows[@]} == 0)); then
  echo "No Codex processes found."
  exit 0
fi

found_candidate=0

for row in "${rows[@]}"; do
  read -r pid ppid uid pgid tty_name _comm _args <<<"$row"

  if [[ -z "${pid:-}" || -z "${uid:-}" || -z "${pgid:-}" || -z "${tty_name:-}" ]]; then
    continue
  fi

  if [[ "$uid" != "$self_uid" ]]; then
    continue
  fi

  if [[ "$pid" == "$self_pid" ]]; then
    continue
  fi

  if [[ -n "$self_tty" && "$self_tty" != "?" && "$tty_name" == "$self_tty" ]]; then
    continue
  fi

  if is_in_self_lineage "$pid"; then
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
