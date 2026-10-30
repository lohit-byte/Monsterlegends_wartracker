"""Persistent state (wars.json + warbot_data.json) and mutations."""
import json
import os
from datetime import datetime
from botutils import now_utc

_BASE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(_BASE, "wars.json")
DATA_FILE_2 = os.path.join(_BASE, "warbot_data.json")

wars = {}
_warbot_data = {"absences": [], "warnings": {}}
pending_abs_requests = {}
pending_undos = {}
_push_callback = lambda: None

def set_push_callback(fn):
    global _push_callback
    _push_callback = fn

def _push():
    try: _push_callback()
    except Exception: pass

# ============ PERSISTENCE ============
def save_wars():
    data = {}
    for name, state in wars.items():
        data[name] = {
            "war_start": state["war_start"].isoformat(),
            "channel_id": state["channel"].id,
            "status_message_id": (state["status_message"].id if state.get("status_message") else None),
            "status_channel_id": (state["status_message"].channel.id if state.get("status_message") else None),
            "missing": state.get("missing", []),
            "absent": list(state.get("absent", set())),
            "removed_log": state.get("removed_log", []),
        }
    try:
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f: json.dump(data, f, indent=2)
        os.replace(tmp, DATA_FILE)
    except OSError as e: print(f"[persistence] save failed: {e}")
    _push()

def read_data_file():
    if not os.path.exists(DATA_FILE): return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f: return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[persistence] load failed: {e}")
        return {}

def load_warbot_data():
    global _warbot_data
    if not os.path.exists(DATA_FILE_2): return
    try:
        with open(DATA_FILE_2, "r", encoding="utf-8") as f: loaded = json.load(f)
        if isinstance(loaded, dict):
            _warbot_data["absences"] = loaded.get("absences", []) or []
            _warbot_data["warnings"] = (loaded.get("warnings") or loaded.get("offences") or {}) or {}
            pending_abs_requests.clear()
            pending_undos.clear()
            for k, v in (loaded.get("pending_abs") or {}).items(): pending_abs_requests[int(k)] = v
            for k, v in (loaded.get("pending_undo") or {}).items(): pending_undos[int(k)] = v
    except (OSError, json.JSONDecodeError) as e: print(f"[warbot_data] load failed: {e}")

def save_warbot_data():
    try:
        tmp = DATA_FILE_2 + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f: json.dump(_warbot_data, f, indent=2)
        os.replace(tmp, DATA_FILE_2)
    except OSError as e: print(f"[warbot_data] save failed: {e}")
    _push()

def persist_pending():
    _warbot_data["pending_abs"] = {str(k): v for k, v in pending_abs_requests.items()}
    _warbot_data["pending_undo"] = {str(k): v for k, v in pending_undos.items()}
    save_warbot_data()

# ============ ABSENCES ============
def add_absence_window(user_id, name, from_dt, to_dt, reason, added_by):
    _warbot_data["absences"].append({
        "user_id": user_id, "name": name, "from": from_dt.isoformat(), "to": to_dt.isoformat(),
        "reason": reason or "", "added_by": added_by, "added_at": now_utc().isoformat(),
    })
    save_warbot_data()

def remove_absence_windows_for(user_id):
    before = len(_warbot_data["absences"])
    _warbot_data["absences"] = [a for a in _warbot_data["absences"] if a.get("user_id") != user_id]
    if len(_warbot_data["absences"]) != before:
        save_warbot_data()
        return True
    return False

def list_active_windows():
    now = now_utc()
    out = []
    for a in _warbot_data["absences"]:
        try: t = datetime.fromisoformat(a["to"])
        except (KeyError, ValueError): continue
        if t >= now: out.append(a)
    return out

def get_all_absences():
    return _warbot_data["absences"]

# ============ WARNINGS ============
def get_warning_list(user_id):
    return _warbot_data["warnings"].get(str(user_id), {}).get("entries", [])

def add_warning(user_id, name, team, reason, added_by):
    key = str(user_id)
    entry = _warbot_data["warnings"].setdefault(key, {"name": name, "entries": []})
    entry["name"] = name
    n = len(entry["entries"]) + 1
    entry["entries"].append({"n": n, "date": now_utc().isoformat(), "war": team, "reason": reason or "", "added_by": added_by})
    save_warbot_data()
    return n

def remove_latest_warning(user_id):
    key = str(user_id)
    entry = _warbot_data["warnings"].get(key)
    if not entry or not entry.get("entries"): return False
    entry["entries"].pop()
    if not entry["entries"]: del _warbot_data["warnings"][key]
    save_warbot_data()
    return True

def reset_warnings(user_id):
    key = str(user_id)
    if key in _warbot_data["warnings"]:
        del _warbot_data["warnings"][key]
        save_warbot_data()
        return True
    return False

def get_all_warnings():
    return _warbot_data["warnings"]

# ============ MISSING LIST ============
def _normalize_missing_entry(e):
    if isinstance(e, dict): return {"name": str(e.get("name", "")), "id": e.get("id")}
    s = str(e)
    from botutils import _ENTRY_ID_RE
    m = _ENTRY_ID_RE.match(s)
    if m: return {"name": s, "id": int(m.group(1))}
    return {"name": s, "id": None}

def _normalize_missing_list(lst):
    return [_normalize_missing_entry(e) for e in (lst or [])]

def missing_member_ids(state):
    return {e["id"] for e in (state.get("missing") or []) if e.get("id")}

def is_member_missing(state, member):
    return member.id in missing_member_ids(state)

def remove_from_missing(state, member, reason, actor_id=None):
    old = state.get("missing") or []
    new_list = [e for e in old if e.get("id") != member.id]
    if len(new_list) == len(old): return False
    state["missing"] = new_list
    log = state.setdefault("removed_log", [])
    log.append({"id": member.id, "name": member.display_name, "when": now_utc().isoformat(), "reason": reason, "actor_id": actor_id})
    if len(log) > 50: del log[:-50]
    return True

def add_to_missing(state, member, reason, actor_id=None):
    if is_member_missing(state, member): return False
    state.setdefault("missing", []).append({"name": member.display_name, "id": member.id})
    log = state.setdefault("removed_log", [])
    log.append({"id": member.id, "name": member.display_name, "when": now_utc().isoformat(), "reason": f"re-add:{reason}", "actor_id": actor_id})
    if len(log) > 50: del log[:-50]
    return True

def excuse_member(state, member_id):
    if "absent" not in state or not isinstance(state.get("absent"), set): state["absent"] = set(state.get("absent") or [])
    state["absent"].add(member_id)
    state["missing"] = [e for e in (state.get("missing") or []) if e.get("id") != member_id]

def _was_disputed(state, member_id):
    for e in reversed(state.get("removed_log") or []):
        if e.get("id") == member_id: return str(e.get("reason", "")).startswith("re-add:")
    return False

def missing_as_text(state):
    lst = state.get("missing") or []
    return ", ".join(e["name"] for e in lst) if lst else "_(empty)_"

def missing_as_mentions(state):
    lst = state.get("missing") or []
    return " ".join(f"<@{e['id']}>" if e.get("id") else e["name"] for e in lst)
