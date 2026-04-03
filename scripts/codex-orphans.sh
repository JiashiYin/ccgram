#!/usr/bin/env bash
set -euo pipefail

self_pid="$$"
self_uid="$(id -u)"

declare -A pid_ppid
declare -A pid_tty
declare -A configured_codex_basenames
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
  local basename

  basename="${token##*/}"
  basename="${basename#-}"

  case "$basename" in
    codex)
      return 0
      ;;
    codex.js|codex.mjs|codex.cjs)
      case "$token" in
        *"@openai/codex/"*)
          return 0
          ;;
      esac
      ;;
  esac

  case "$token" in
    "@openai/codex"|@openai/codex@*)
      return 0
      ;;
  esac

  return 1
}

is_codex_wrapper_token() {
  local token="${1,,}"
  local basename

  basename="${token##*/}"
  basename="${basename#-}"

  [[ "$basename" != *.* ]] || return 1

  case "$basename" in
    codex-*)
      return 0
      ;;
  esac

  if [[ -n "${configured_codex_basenames[$basename]:-}" ]]; then
    return 0
  fi

  return 1
}

is_configured_codex_token() {
  local token="${1,,}"
  local basename

  basename="${token##*/}"
  basename="${basename#-}"

  [[ -n "${configured_codex_basenames[$basename]:-}" ]]
}

load_configured_codex_basenames() {
  local config_dir="${CCGRAM_DIR:-$HOME/.ccgram}"
  local env_path raw_value="" line token basename lower wrapper="" subcommand=""
  local skip_value=0
  local i
  local -a command_tokens=()

  if [[ -n "${CCGRAM_CODEX_COMMAND:-}" ]]; then
    raw_value="${CCGRAM_CODEX_COMMAND}"
  else
    for env_path in ".env" "$config_dir/.env" "$HOME/.ccbot/.env"; do
      [[ -f "$env_path" ]] || continue
      while IFS= read -r line; do
        case "$line" in
          CCGRAM_CODEX_COMMAND=*)
            raw_value="${line#CCGRAM_CODEX_COMMAND=}"
            ;;
        esac
      done < "$env_path"
      [[ -n "$raw_value" ]] && break
    done
  fi

  [[ -n "$raw_value" ]] || return 0

  case "$raw_value" in
    \"*\")
      raw_value="${raw_value:1:${#raw_value}-2}"
      ;;
    \'*\')
      raw_value="${raw_value:1:${#raw_value}-2}"
      ;;
  esac
  raw_value="${raw_value//\\\"/\"}"

  if ! mapfile -t command_tokens < <(
    python3 - "$raw_value" <<'PY'
import shlex
import sys

for token in shlex.split(sys.argv[1]):
    print(token)
PY
  ); then
    return 0
  fi
  for ((i = 0; i < ${#command_tokens[@]}; i++)); do
    token="${command_tokens[i]}"
    lower="${token,,}"

    if (( skip_value == 1 )); then
      skip_value=0
      continue
    fi

    case "$wrapper" in
      env)
        if [[ "$lower" == -* ]]; then
          continue
        fi
        if is_env_assignment_token "$token"; then
          continue
        fi
        ;;
      sudo|node|bun|npx|bunx)
        if [[ "$lower" == -* ]]; then
          if wrapper_option_takes_value "$wrapper" "$lower"; then
            skip_value=1
          fi
          continue
        fi
        ;;
      uv)
        if [[ -z "$subcommand" ]]; then
          if [[ "$lower" == -* ]]; then
            if wrapper_option_takes_value "$wrapper" "$lower"; then
              skip_value=1
            fi
            continue
          fi
          if [[ "$lower" == "run" ]]; then
            subcommand="run"
            continue
          fi
        elif [[ "$lower" == -* ]]; then
          if wrapper_option_takes_value "$wrapper" "$lower"; then
            skip_value=1
          fi
          continue
        fi
        ;;
      python|python3)
        if [[ "$lower" == "-m" ]]; then
          if (( i + 1 < ${#command_tokens[@]} )); then
            token="${command_tokens[i + 1]}"
          fi
        elif [[ "$lower" == -* ]]; then
          if wrapper_option_takes_value "$wrapper" "$lower"; then
            skip_value=1
          fi
          continue
        fi
        ;;
    esac

    lower="${token,,}"
    basename="${lower##*/}"
    basename="${basename#-}"

    case "$basename" in
      sudo|env|node|bun|npx|bunx|uv|python|python3)
        wrapper="$basename"
        subcommand=""
        continue
        ;;
    esac

    if [[ "$token" == *=* ]]; then
      continue
    fi

    [[ -n "$basename" ]] || return 0
    configured_codex_basenames["$basename"]=1
    return 0
  done
}

is_env_assignment_token() {
  local token="$1"

  [[ "$token" == *=* && "$token" != *= ]]
}

wrapper_option_takes_value() {
  local wrapper="${1,,}"
  local option="${2,,}"

  case "$wrapper:$option" in
    sudo:-u|sudo:-g|sudo:-h|sudo:-p|sudo:-c|sudo:-t|sudo:-r|sudo:-d|sudo:--user|sudo:--group|sudo:--host|sudo:--prompt|sudo:--close-from|sudo:--chdir|sudo:--role|sudo:--type|sudo:--preserve-env)
      return 0
      ;;
    node:-r|node:--require|node:--loader|node:--import|node:-e|node:--eval)
      return 0
      ;;
    bun:-r|bun:--require|bun:--loader|bun:--import|bun:-e|bun:--eval|bun:--cwd|bun:--config)
      return 0
      ;;
    npx:-p|npx:--package|npx:-c|npx:--call)
      return 0
      ;;
    bunx:-p|bunx:--package|bunx:-c|bunx:--call)
      return 0
      ;;
    uv:--project|uv:--directory|uv:--python|uv:--with|uv:--env-file)
      return 0
      ;;
    python:-c|python:-w|python:-x|python3:-c|python3:-w|python3:-x)
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
  local token lower basename wrapper="" subcommand=""
  local skip_value=0
  local i

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

  for ((i = 0; i < ${#tokens[@]}; i++)); do
    token="${tokens[i]}"
    lower="${token,,}"
    basename="${lower##*/}"
    basename="${basename#-}"

    if (( skip_value == 1 )); then
      skip_value=0
      continue
    fi

    case "$wrapper" in
      env)
        if [[ "$lower" == -* ]]; then
          continue
        fi
        if is_env_assignment_token "$token"; then
          continue
        fi
        ;;
      sudo|node|bun|npx|bunx)
        if [[ "$lower" == -* ]]; then
          if wrapper_option_takes_value "$wrapper" "$lower"; then
            skip_value=1
          fi
          continue
        fi
        ;;
      uv)
        if [[ -z "$subcommand" ]]; then
          if [[ "$lower" == -* ]]; then
            if wrapper_option_takes_value "$wrapper" "$lower"; then
              skip_value=1
            fi
            continue
          fi
          if [[ "$lower" == "run" ]]; then
            subcommand="run"
            continue
          fi
        elif [[ "$lower" == -* ]]; then
          if wrapper_option_takes_value "$wrapper" "$lower"; then
            skip_value=1
          fi
          continue
        fi
        ;;
      python|python3)
        if [[ "$lower" == "-m" ]]; then
          if (( i + 1 < ${#tokens[@]} )); then
            if is_codex_path_token "${tokens[i + 1]}" || is_configured_codex_token "${tokens[i + 1]}"; then
              return 0
            fi
          fi
          return 1
        fi
        if [[ "$lower" == -* ]]; then
          if wrapper_option_takes_value "$wrapper" "$lower"; then
            skip_value=1
          fi
          continue
        fi
        if is_codex_path_token "$token" || is_configured_codex_token "$token"; then
          return 0
        fi
        return 1
        ;;
    esac

    case "$basename" in
      sudo|env|node|bun|npx|bunx|uv|python|python3)
        wrapper="$basename"
        subcommand=""
        continue
        ;;
    esac

    if is_codex_path_token "$token" || is_configured_codex_token "$token"; then
      return 0
    fi

    if [[ "$wrapper" != "node" && "$wrapper" != "bun" && "$wrapper" != "npx" && "$wrapper" != "bunx" && "$wrapper" != "python" && "$wrapper" != "python3" ]] && is_codex_wrapper_token "$token"; then
      return 0
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

load_configured_codex_basenames

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
