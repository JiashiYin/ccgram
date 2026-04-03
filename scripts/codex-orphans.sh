#!/usr/bin/env bash
set -euo pipefail

self_pid="$$"
self_uid="$(id -u)"

declare -A pid_ppid
declare -A pid_tty
declare -A self_ancestry
declare -A self_lineage_cache
declare -A live_ancestor_cache
declare -A group_rows
declare -A group_has_codex
declare -A group_has_unrelated
declare -A group_has_race
declare -A group_has_self_lineage
declare -A group_has_tty
declare -A group_has_attached_ancestor
declare -A seen_groups
declare -a group_order

is_codex_path_token() {
  local token="${1,,}"
  local basename stem

  basename="${token##*/}"
  basename="${basename#-}"
  stem="$basename"
  if [[ "$stem" == *.* ]]; then
    stem="${stem%%.*}"
  fi

  case "$token" in
    *"@openai/codex"*|*"/codex/"*)
      return 0
      ;;
  esac

  case "$stem" in
    codex)
      return 0
      ;;
  esac

  case "$basename" in
    codex|codex.*)
      return 0
      ;;
  esac

  case "$stem" in
    *-codex-*|*-codex|*_codex_*|*_codex)
      return 0
      ;;
  esac

  return 1
}

is_ccgram_codex_launch() {
  local -a tokens=("$@")
  local i

  for ((i = 0; i + 2 < ${#tokens[@]}; i++)); do
    if [[ "${tokens[i],,}" == "ccgram" && "${tokens[i + 1],,}" == "notify" && "${tokens[i + 2],,}" == "launch" ]]; then
      local j
      for ((j = i + 3; j < ${#tokens[@]}; j++)); do
        case "${tokens[j],,}" in
          --provider=codex)
            return 0
            ;;
          --provider)
            if (( j + 1 < ${#tokens[@]} )) && [[ "${tokens[j + 1],,}" == "codex" ]]; then
              return 0
            fi
            ;;
        esac
      done
    fi
  done

  return 1
}

is_codex_like_command() {
  local comm="${1:-}"
  local args="${2:-}"
  local -a tokens=()
  local token basename state=0

  if [[ -z "$args" ]]; then
    args="$comm"
  fi

  read -r -a tokens <<<"$args"
  if ((${#tokens[@]} == 0)); then
    return 1
  fi

  if is_ccgram_codex_launch "${tokens[@]}"; then
    return 0
  fi

  for token in "${tokens[@]}"; do
    token="${token,,}"
    basename="${token##*/}"
    basename="${basename#-}"

    case "$state:$basename" in
      0:sudo|0:env|0:node|0:bun|0:npx|0:bunx|0:uv)
        continue
        ;;
      0:python|0:python3)
        state=1
        continue
        ;;
      1:m)
        state=2
        continue
        ;;
    esac

    if is_codex_path_token "$token"; then
      return 0
    fi

    if (( state == 1 || state == 2 )); then
      return 1
    fi

    return 1
  done

  return 1
}

is_allowed_group_member() {
  local comm="${1,,}"
  local args="${2:-}"

  if is_codex_like_command "$comm" "$args"; then
    return 0
  fi

  comm="${comm#-}"
  if is_codex_path_token "$comm"; then
    return 0
  fi

  case "$comm" in
    node|bun|npx|bunx|bash|sh|dash|zsh|fish|env|sudo|setsid|bwrap|ccgram|uv|python|python3)
      return 0
      ;;
  esac

  return 1
}

build_self_ancestry() {
  local current="$self_pid"
  local next

  while [[ -n "$current" && "$current" != "0" ]]; do
    self_ancestry["$current"]=1
    if [[ -z "${pid_ppid[$current]:-}" ]]; then
      break
    fi
    next="${pid_ppid[$current]}"
    if [[ "$next" == "$current" ]]; then
      break
    fi
    current="$next"
  done
}

pid_is_in_self_lineage() {
  local pid="$1"
  local current="$pid"
  local next

  if [[ -n "${self_lineage_cache[$pid]+x}" ]]; then
    return "${self_lineage_cache[$pid]}"
  fi

  while [[ -n "$current" && "$current" != "0" ]]; do
    if [[ -n "${self_ancestry[$current]:-}" ]]; then
      self_lineage_cache["$pid"]=0
      return 0
    fi
    if [[ -z "${pid_ppid[$current]:-}" ]]; then
      self_lineage_cache["$pid"]=2
      return 2
    fi
    next="${pid_ppid[$current]}"
    if [[ "$next" == "$current" ]]; then
      break
    fi
    current="$next"
  done

  self_lineage_cache["$pid"]=1
  return 1
}

pid_has_live_ancestor() {
  local pid="$1"
  local current="$pid"
  local next

  if [[ -n "${live_ancestor_cache[$pid]:-}" ]]; then
    return "${live_ancestor_cache[$pid]}"
  fi

  while [[ -n "$current" && "$current" != "0" ]]; do
    if [[ "$current" != "$pid" && "${pid_tty[$current]:-?}" != "?" ]]; then
      live_ancestor_cache["$pid"]=0
      return 0
    fi
    if [[ -z "${pid_ppid[$current]:-}" ]]; then
      live_ancestor_cache["$pid"]=2
      return 2
    fi
    next="${pid_ppid[$current]}"
    if [[ "$next" == "$current" ]]; then
      break
    fi
    current="$next"
  done

  live_ancestor_cache["$pid"]=1
  return 1
}

while IFS= read -r row; do
  [[ -n "$row" ]] || continue

  read -r pid ppid uid pgid tty_name comm args <<<"$row"
  if [[ -z "${pid:-}" || -z "${uid:-}" || -z "${pgid:-}" || -z "${tty_name:-}" || -z "${comm:-}" ]]; then
    continue
  fi

  pid_ppid["$pid"]="$ppid"
  pid_tty["$pid"]="$tty_name"

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

  if ! is_allowed_group_member "$comm" "$args"; then
    group_has_unrelated["$pgid"]=1
  fi

  if [[ "$tty_name" != "?" ]]; then
    group_has_tty["$pgid"]=1
  fi
done < <(
  ps -eo pid=,ppid=,uid=,pgid=,tty=,comm=,args=
)

build_self_ancestry

for pgid in "${group_order[@]}"; do
  if [[ -z "${group_has_codex[$pgid]:-}" ]]; then
    continue
  fi

  while IFS= read -r member; do
    [[ -n "$member" ]] || continue
    read -r member_pid member_ppid member_uid member_pgid member_tty member_comm member_args <<<"$member"
    if [[ -z "${member_pid:-}" ]]; then
      continue
    fi

    if pid_is_in_self_lineage "$member_pid"; then
      group_has_self_lineage["$pgid"]=1
      break
    else
      status=$?
      case "$status" in
        2)
          group_has_race["$pgid"]=1
          break
          ;;
      esac
    fi

    if pid_has_live_ancestor "$member_pid"; then
      group_has_attached_ancestor["$pgid"]=1
      break
    else
      status=$?
      case "$status" in
        2)
          group_has_race["$pgid"]=1
          break
          ;;
      esac
    fi
  done <<<"${group_rows[$pgid]}"
done

if ((${#group_order[@]} == 0)); then
  echo "No same-user processes found."
  exit 0
fi

candidate_count=0

for pgid in "${group_order[@]}"; do
  if [[ -z "${group_has_codex[$pgid]:-}" ]]; then
    continue
  fi

  if [[ -n "${group_has_race[$pgid]:-}" ]]; then
    echo "Skipping pgid=$pgid: process disappeared during scan."
    continue
  fi

  if [[ -n "${group_has_self_lineage[$pgid]:-}" ]]; then
    continue
  fi

  if [[ -n "${group_has_unrelated[$pgid]:-}" ]]; then
    continue
  fi

  if [[ -n "${group_has_tty[$pgid]:-}" ]]; then
    continue
  fi

  if [[ -n "${group_has_attached_ancestor[$pgid]:-}" ]]; then
    continue
  fi

  candidate_count=$((candidate_count + 1))
  echo "Detached Codex candidate group: pgid=$pgid"
  while IFS= read -r member; do
    [[ -n "$member" ]] || continue
    echo "  $member"
  done <<<"${group_rows[$pgid]}"

  if ! read -r -p "Kill process group $pgid? [y/N] " answer; then
    answer=""
  fi
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
