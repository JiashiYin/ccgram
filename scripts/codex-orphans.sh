#!/usr/bin/env bash
set -euo pipefail

self_pid="$$"
self_uid="$(id -u)"
self_tty="$(ps -p "$self_pid" -o tty= | tr -d '[:space:]')"

ps_parent_pid() {
  local pid="$1"
  local ppid

  if ! ppid="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d '[:space:]')"; then
    return 1
  fi

  [[ -n "$ppid" ]] || return 1
  printf '%s\n' "$ppid"
}

is_descendant_of_self() {
  local pid="$1"
  local ppid

  while [[ -n "$pid" && "$pid" != "0" ]]; do
    if [[ "$pid" == "$self_pid" ]]; then
      return 0
    fi

    if ! ppid="$(ps_parent_pid "$pid")"; then
      return 2
    fi

    if [[ "$ppid" == "$pid" ]]; then
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
    if ! ppid="$(ps_parent_pid "$pid")"; then
      return 2
    fi

    if [[ "$ppid" == "$target" ]]; then
      return 0
    fi

    if [[ "$ppid" == "$pid" ]]; then
      break
    fi

    pid="$ppid"
  done

  return 1
}

is_in_self_lineage() {
  local pid="$1"

  if is_descendant_of_self "$pid"; then
    return 0
  fi

  case "$?" in
    2) return 2 ;;
  esac

  if is_ancestor_of_self "$pid"; then
    return 0
  fi

  return $?
}

is_codex_like_command() {
  local comm="$1"
  local args="$2"

  case "$args" in
    *"ccgram notify launch --provider codex"*)
      return 0
      ;;
  esac

  case "$comm" in
    codex|codex-*)
      return 0
      ;;
    node)
      case "$args" in
        *codex*)
          return 0
          ;;
      esac
      ;;
  esac

  case "$args" in
    *" codex "*|codex|*"/codex"*|*"/codex "*)
      return 0
      ;;
  esac

  return 1
}

is_allowed_group_member() {
  case "$1" in
    codex|codex-*|node|bash|sh|dash|zsh|env|sudo|setsid|bwrap|ccgram)
      return 0
      ;;
  esac

  return 1
}

declare -A group_rows
declare -A group_has_codex
declare -A group_has_unrelated
declare -A group_has_race
declare -A group_has_self_lineage
declare -A group_has_tty
declare -A seen_groups
declare -a group_order

while IFS= read -r row; do
  [[ -n "$row" ]] || continue

  read -r pid ppid uid pgid tty_name comm args <<<"$row"
  if [[ -z "${pid:-}" || -z "${uid:-}" || -z "${pgid:-}" || -z "${tty_name:-}" || -z "${comm:-}" ]]; then
    continue
  fi

  if [[ "$uid" != "$self_uid" ]]; then
    continue
  fi

  if [[ -z "${seen_groups[$pgid]+x}" ]]; then
    seen_groups["$pgid"]=1
    group_order+=("$pgid")
  fi

  group_rows["$pgid"]+="$row"$'\n'

  if is_codex_like_command "$comm" "$args"; then
    group_has_codex["$pgid"]=1
  fi

  if ! is_allowed_group_member "$comm"; then
    group_has_unrelated["$pgid"]=1
  fi

  if [[ "$tty_name" != "?" ]]; then
    group_has_tty["$pgid"]=1
  fi

  if is_in_self_lineage "$pid"; then
    group_has_self_lineage["$pgid"]=1
  else
    case "$?" in
      2)
        group_has_race["$pgid"]=1
        ;;
    esac
  fi
done < <(
  ps -eo pid=,ppid=,uid=,pgid=,tty=,comm=,args=
)

if ((${#group_order[@]} == 0)); then
  echo "No Codex processes found."
  exit 0
fi

candidate_count=0

for pgid in "${group_order[@]}"; do
  if [[ -z "${group_has_codex[$pgid]+x}" ]]; then
    continue
  fi

  if [[ -n "${group_has_race[$pgid]+x}" ]]; then
    echo "Skipping pgid=$pgid: process disappeared during scan."
    continue
  fi

  if [[ -n "${group_has_self_lineage[$pgid]+x}" ]]; then
    continue
  fi

  if [[ -n "${group_has_unrelated[$pgid]+x}" ]]; then
    continue
  fi

  if [[ -n "${group_has_tty[$pgid]+x}" ]]; then
    continue
  fi

  candidate_count=$((candidate_count + 1))
  echo "Detached Codex candidate group: pgid=$pgid"
  while IFS= read -r member; do
    [[ -n "$member" ]] || continue
    echo "  $member"
  done <<<"${group_rows[$pgid]}"

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

if [[ "$candidate_count" -eq 0 ]]; then
  echo "No detached Codex candidates found."
fi
