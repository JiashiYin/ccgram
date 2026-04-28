"""Topic emoji status updates via editForumTopic.

Updates topic names with status emoji prefixes to reflect session state:
  - Active (working): topic name prefixed with working emoji
  - Idle (waiting): topic name prefixed with idle emoji
  - Done (Claude exited): topic name prefixed with done emoji
  - Dead (window gone): topic name prefixed with dead emoji

Tracks per-topic state to avoid redundant API calls. Debounces transitions
to prevent rapid active/idle toggling from flooding the chat with rename
messages. Gracefully degrades when the bot lacks editForumTopic permission.

Key functions:
  - update_topic_emoji: Update emoji for a specific topic (debounced)
  - clear_topic_emoji_state: Clean up tracking for a topic
"""

import json
import time
from pathlib import Path

import structlog
from telegram import Bot
from telegram.error import BadRequest, TelegramError

from ..utils import atomic_write_json, ccgram_dir

logger = structlog.get_logger()

# Emoji prefixes for session states
EMOJI_ACTIVE = "\U0001f7e2"  # Green circle
EMOJI_IDLE = "\U0001f7e1"  # Yellow circle (your turn / attention needed)
EMOJI_DONE = "\u2705"  # Check mark (Claude exited normally)
EMOJI_DEAD = "\U0001f4a5"  # Collision / crash
EMOJI_YOLO = "\U0001f3b2"  # Dice (risk/gamble — auto-approve mode)
EMOJI_RC = "\U0001f4e1"  # Satellite dish (Remote Control active)
_EMOJI_DEAD_OLD = (
    "\u26ab",
    "\u274c",
)  # Legacy dead emoji (black circle pre-2026-02, cross mark pre-2026-03)

# Debounce: state must be stable for this many seconds before updating topic name.
# Prevents rapid active↔idle toggling from flooding chat with rename messages.
#
# Asymmetric by design:
#   → active (5s):  fast feedback — user sees "agent is working" quickly
#   → idle (30s):   slow transition — brief pauses during work don't cause flicker
#   → done/dead (5s): meaningful lifecycle events, fire fast
DEBOUNCE_TO_ACTIVE_SECONDS = 5.0
DEBOUNCE_TO_IDLE_SECONDS = 30.0
DEBOUNCE_TERMINAL_SECONDS = 5.0  # done, dead

_DEBOUNCE_BY_STATE: dict[str, float] = {
    "active": DEBOUNCE_TO_ACTIVE_SECONDS,
    "idle": DEBOUNCE_TO_IDLE_SECONDS,
    "done": DEBOUNCE_TERMINAL_SECONDS,
    "dead": DEBOUNCE_TERMINAL_SECONDS,
}

# Topic state tracking: (chat_id, thread_id) -> (state, approval_mode, rc_active)
_topic_states: dict[tuple[int, int], tuple[str, str, bool]] = {}

# Pending transitions: (chat_id, thread_id) -> (desired_state, first_seen_monotonic)
_pending_transitions: dict[tuple[int, int], tuple[str, float]] = {}

# Topic display names: (chat_id, thread_id) -> clean name (without emoji prefix).
# Updated when the incoming display name changes (write-through cache) so that
# tmux window renames and Telegram topic renames propagate correctly.
_topic_names: dict[tuple[int, int], str] = {}
_TOPIC_NAME_CACHE_FILE = "topic-names.json"
_STALE_TOPIC_CACHE_FILE = "stale-topics.json"


def _topic_name_cache_path() -> Path:
    return ccgram_dir() / _TOPIC_NAME_CACHE_FILE


def _stale_topic_cache_path() -> Path:
    return ccgram_dir() / _STALE_TOPIC_CACHE_FILE


def _load_topic_name_cache() -> dict[tuple[int, int], str]:
    path = _topic_name_cache_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}

    parsed: dict[tuple[int, int], str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        chat_raw, sep, thread_raw = key.partition(":")
        if not sep:
            continue
        try:
            parsed[(int(chat_raw), int(thread_raw))] = value
        except ValueError:
            continue
    return parsed


def _save_topic_name_cache() -> None:
    serialized = {
        f"{chat_id}:{thread_id}": name
        for (chat_id, thread_id), name in _topic_names.items()
    }
    atomic_write_json(_topic_name_cache_path(), serialized)


_topic_names.update(_load_topic_name_cache())


def _load_stale_topic_cache() -> set[tuple[int, int]]:
    path = _stale_topic_cache_path()
    if not path.exists():
        return set()
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return set()
    if not isinstance(raw, list):
        return set()

    parsed: set[tuple[int, int]] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        chat_raw, sep, thread_raw = item.partition(":")
        if not sep:
            continue
        try:
            parsed.add((int(chat_raw), int(thread_raw)))
        except ValueError:
            continue
    return parsed


def _save_stale_topic_cache() -> None:
    serialized = sorted(f"{chat_id}:{thread_id}" for chat_id, thread_id in _stale_topics)
    atomic_write_json(_stale_topic_cache_path(), serialized)


_stale_topics: set[tuple[int, int]] = _load_stale_topic_cache()

# Chats where editForumTopic is disabled due to permission errors
_disabled_chats: set[int] = set()


def _is_bound_topic(chat_id: int, thread_id: int) -> bool:
    """Return True when a topic is still bound to a live session."""
    from ..session import session_manager

    return session_manager.get_window_for_chat_thread(chat_id, thread_id) is not None


def _same_topic_identity(candidate_name: str, preferred_name: str) -> bool:
    """Return True when two cache names refer to the same logical topic."""
    from .topic_routing import extract_default_responder

    candidate_base, candidate_owner = extract_default_responder(
        strip_emoji_prefix(candidate_name)
    )
    preferred_base, preferred_owner = extract_default_responder(
        strip_emoji_prefix(preferred_name)
    )
    if candidate_base != preferred_base:
        return False
    if candidate_owner == preferred_owner:
        return True
    # If the preferred name now carries an owner marker, treat older plain
    # entries for the same base topic as stale duplicates.
    return preferred_owner is not None and candidate_owner is None


def _prune_duplicate_topic_names(
    chat_id: int, preferred_thread_id: int, preferred_name: str
) -> bool:
    """Drop stale duplicate cache entries for the same logical topic.

    Only prunes entries in the same chat that match the preferred topic name
    and are not currently bound to a live session.
    """
    changed = False
    for (cached_chat_id, cached_thread_id), cached_name in list(_topic_names.items()):
        if cached_chat_id != chat_id or cached_thread_id == preferred_thread_id:
            continue
        if not _same_topic_identity(cached_name, preferred_name):
            continue
        if _is_bound_topic(chat_id, cached_thread_id):
            continue
        logger.info(
            "Pruning stale topic-name cache entry: chat=%d thread=%d name=%s",
            chat_id,
            cached_thread_id,
            cached_name,
        )
        _stale_topics.add((cached_chat_id, cached_thread_id))
        _topic_names.pop((cached_chat_id, cached_thread_id), None)
        changed = True
    return changed


def _resolve_topic_name(key: tuple[int, int], display_name: str) -> str:
    """Return the clean topic name, updating the cache when the name changes.

    On first call, strips emoji and stores the clean name. On subsequent calls,
    if the incoming display_name (stripped) differs from the stored name,
    overwrites the cache so tmux renames propagate to Telegram.
    """
    clean = strip_emoji_prefix(display_name)
    cached = _topic_names.get(key)
    if cached is None:
        _topic_names[key] = clean
        return clean
    from .topic_routing import extract_default_responder

    cached_base, cached_owner = extract_default_responder(cached)
    clean_base, clean_owner = extract_default_responder(clean)
    if clean_owner is None and cached_owner is not None and clean_base == cached_base:
        return cached
    if cached != clean:
        _topic_names[key] = clean
        # Invalidate state so next update_topic_emoji re-applies emoji with new name
        _topic_states.pop(key, None)
    return _topic_names[key]


def _resolve_approval_mode(chat_id: int, thread_id: int) -> str:
    """Resolve approval mode for a topic via session bindings."""
    from ..session import DEFAULT_APPROVAL_MODE, session_manager

    window_id = session_manager.get_window_for_chat_thread(chat_id, thread_id)
    if not window_id:
        return DEFAULT_APPROVAL_MODE
    return session_manager.get_approval_mode(window_id)


def _resolve_rc_mode(chat_id: int, thread_id: int) -> bool:
    """Resolve Remote Control active state for a topic via session bindings."""
    from ..session import session_manager

    window_id = session_manager.get_window_for_chat_thread(chat_id, thread_id)
    if not window_id:
        return False
    from .status_polling import is_rc_active

    return is_rc_active(window_id)


def format_topic_name_for_mode(display_name: str, approval_mode: str) -> str:
    """Format a topic display name with a positive mode badge."""
    clean_name = strip_emoji_prefix(display_name)
    if approval_mode == "yolo":
        return f"{EMOJI_YOLO} {clean_name}"
    return clean_name


async def update_topic_emoji(
    bot: Bot,
    chat_id: int,
    thread_id: int,
    state: str,
    display_name: str,
) -> None:
    """Update topic name with emoji prefix reflecting session state.

    Debounces transitions: the new state must be requested consistently for
    the debounce period before the API call is made. This prevents rapid
    active/idle flickering from generating lots of "topic renamed" messages.

    Args:
        bot: Telegram Bot instance
        chat_id: Group chat ID
        thread_id: Forum topic thread ID
        state: One of "active", "idle", "done", "dead"
        display_name: Base topic name (without emoji prefix)
    """
    if chat_id in _disabled_chats:
        return

    key = (chat_id, thread_id)

    approval_mode = _resolve_approval_mode(chat_id, thread_id)
    rc_active = _resolve_rc_mode(chat_id, thread_id)
    state_token = (state, approval_mode, rc_active)

    # Already in this state/mode — no transition needed
    if _topic_states.get(key) == state_token:
        _pending_transitions.pop(key, None)
        return

    emoji = {
        "active": EMOJI_ACTIVE,
        "idle": EMOJI_IDLE,
        "done": EMOJI_DONE,
        "dead": EMOJI_DEAD,
    }.get(state, "")

    if not emoji:
        return

    # Debounce: require the new state to be stable before applying
    now = time.monotonic()
    pending = _pending_transitions.get(key)
    if pending is None or pending[0] != state:
        # New or changed desired state — start debounce timer
        _pending_transitions[key] = (state, now)
        return

    debounce = _DEBOUNCE_BY_STATE.get(state, DEBOUNCE_TO_IDLE_SECONDS)
    if now - pending[1] < debounce:
        # Not stable long enough yet
        return

    # Debounce passed — execute the transition
    _pending_transitions.pop(key, None)

    clean_name = _resolve_topic_name(key, display_name)
    rc_prefix = f"{EMOJI_RC} " if rc_active else ""
    mode_prefix = f"{EMOJI_YOLO} " if approval_mode == "yolo" else ""
    new_name = f"{emoji} {rc_prefix}{mode_prefix}{clean_name}"

    try:
        await bot.edit_forum_topic(
            chat_id=chat_id,
            message_thread_id=thread_id,
            name=new_name,
        )
        _topic_states[key] = state_token
        logger.debug(
            "Updated topic emoji: chat=%d thread=%d state=%s name='%s'",
            chat_id,
            thread_id,
            state,
            new_name,
        )
    except BadRequest as e:
        if "Not enough rights" in e.message:
            _disabled_chats.add(chat_id)
            logger.info(
                "Topic emoji disabled for chat %d: insufficient permissions",
                chat_id,
            )
        elif (
            "topic_not_modified" in e.message.lower() or "Topic_id_invalid" in e.message
        ):
            # Expected no-ops: already correct name or invalid topic
            _topic_states[key] = state_token
        else:
            logger.debug("Failed to update topic emoji: %s", e)
    except TelegramError:
        pass


def strip_emoji_prefix(name: str) -> str:
    """Remove known emoji prefix from a topic name."""
    for emoji in (EMOJI_ACTIVE, EMOJI_IDLE, EMOJI_DONE, EMOJI_DEAD, *_EMOJI_DEAD_OLD):
        prefix = f"{emoji} "
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    # Strip badge emojis (RC, YOLO) — order: RC before YOLO matches composition order
    for badge in (EMOJI_RC, EMOJI_YOLO):
        badge_prefix = f"{badge} "
        if name.startswith(badge_prefix):
            name = name[len(badge_prefix) :]
    return name


def update_stored_topic_name(chat_id: int, thread_id: int, new_clean_name: str) -> None:
    """Overwrite the stored clean name for a topic.

    Called from FORUM_TOPIC_EDITED handler. Does not invalidate _topic_states
    since the Telegram topic already has the correct name — the next emoji
    cycle will naturally use the updated base name.
    """
    clean_name = strip_emoji_prefix(new_clean_name)
    key = (chat_id, thread_id)
    changed = _topic_names.get(key) != clean_name
    _topic_names[key] = clean_name
    changed = _prune_duplicate_topic_names(chat_id, thread_id, clean_name) or changed
    if changed:
        _save_topic_name_cache()
        _save_stale_topic_cache()


def get_stored_topic_name(chat_id: int, thread_id: int) -> str | None:
    """Return the last persisted clean topic name for a chat/thread pair."""
    return _topic_names.get((chat_id, thread_id))


def iter_stored_topic_names() -> list[tuple[int, int, str]]:
    """Return a snapshot of persisted topic-name cache entries."""
    return [(chat_id, thread_id, name) for (chat_id, thread_id), name in _topic_names.items()]


def iter_stale_topic_threads() -> list[tuple[int, int]]:
    """Return chat/thread pairs of unbound duplicate topics queued for deletion."""
    return sorted(_stale_topics)


def clear_stale_topic(chat_id: int, thread_id: int) -> None:
    """Forget a stale duplicate-topic cleanup entry."""
    if (chat_id, thread_id) in _stale_topics:
        _stale_topics.remove((chat_id, thread_id))
        _save_stale_topic_cache()


def clear_topic_emoji_state(chat_id: int, thread_id: int) -> None:
    """Clear emoji tracking for a topic (called on topic cleanup)."""
    key = (chat_id, thread_id)
    _topic_states.pop(key, None)
    _pending_transitions.pop(key, None)
    topic_changed = _topic_names.pop(key, None) is not None
    stale_changed = False
    if key in _stale_topics:
        _stale_topics.remove(key)
        stale_changed = True
    if topic_changed:
        _save_topic_name_cache()
    if stale_changed:
        _save_stale_topic_cache()


def reset_all_state() -> None:
    """Reset all tracking state (for testing)."""
    _topic_states.clear()
    _pending_transitions.clear()
    _disabled_chats.clear()
    _topic_names.clear()
    _stale_topics.clear()
