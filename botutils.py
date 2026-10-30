"""Pure utility helpers for warbot — no bot or storage state."""
import json
import os
import re
import shlex
import unicodedata
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

DISCORD_MSG_LIMIT = 1900
DISCORD_FIELD_LIMIT = 1000

COMPLETION_PATTERNS = [
    r"\battacks?\s+(are\s+|is\s+)?(done|complete[d]?|finish(ed)?|used|clear(ed)?|over)\b",
    r"\b(done|finished|completed|used)\s+(with\s+|my\s+|all\s+|the\s+|our\s+)?attacks?\b",
    r"\ball\s+attacks?\s+(done|used|complete[d]?|finish(ed)?|over)\b",
    r"\bno\s+attacks?\s+left\b", r"\b0\s+attacks?\s+left\b",
    r"\battacks?\s+complete\b", r"\bdone\s+(for|with)\s+(this|the)\s+war\b",
    r"\bused\s+all\s+(my\s+|our\s+)?attacks?\b",
]
NEGATION_RE = re.compile(
    r"(?:\bnot\b|n't\b|\byet\s+to\b|\bstill\s+(?:have|need|got|gotta)\b)",
    re.IGNORECASE,
)
_COMPLETION_WORDS = (
    r"(?:attacks?\s+done|attacks?\s+complete[d]?|attacks?\s+finish(ed)?|"
    r"done\s+with\s+attacks?|finished\s+attacks?|used\s+all\s+attacks?|"
    r"used\s+attacks?|no\s+attacks?\s+left)"
)
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
MENTION_RE = re.compile(r"<@!?\d+>")
_ENTRY_ID_RE = re.compile(r"<@!?(\d+)>")

# ============ ALIASES ============
_BASE = os.path.dirname(os.path.abspath(__file__))
ALIASES_FILE = os.path.join(_BASE, "aliases.json")
NAME_ALIASES = {}

def load_aliases():
    global NAME_ALIASES
    try:
        if not os.path.exists(ALIASES_FILE):
            NAME_ALIASES.clear()
            return
        with open(ALIASES_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        loaded_dict = {str(k).lower(): int(v) for k, v in raw.items()}
        NAME_ALIASES.clear()
        NAME_ALIASES.update(loaded_dict)
        print(f"[aliases] loaded {len(NAME_ALIASES)} entries")
    except (OSError, ValueError) as e:
        print(f"[aliases] load failed: {e}")
        NAME_ALIASES.clear()

def save_aliases():
    try:
        tmp = ALIASES_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(NAME_ALIASES, f, indent=2)
        os.replace(tmp, ALIASES_FILE)
    except OSError as e:
        print(f"[aliases] save failed: {e}")

load_aliases()

# ============ TIME ============
def now_utc():
    return datetime.now(timezone.utc)

def fmt_td(td):
    total = max(0, int(td.total_seconds()))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    if s and not h: parts.append(f"{s}s")
    return " ".join(parts) or "0m"

def progress_bar(elapsed_sec, total_sec, length=20):
    if total_sec <= 0: return "▰" * length
    filled = max(0, min(length, int(length * elapsed_sec / total_sec)))
    return "▰" * filled + "▱" * (length - filled)

def ts(dt, style="R"):
    return f"<t:{int(dt.timestamp())}:{style}>"

def parse_time(text):
    if not text: return None
    t = text.strip().lower().replace(" ", "")
    t = t.replace("hours", "h").replace("hour", "h").replace("minutes", "m").replace("minute", "m").replace("mins", "m").replace("min", "m").replace("hrs", "h").replace("hr", "h").replace("seconds", "s").replace("second", "s").replace("secs", "s").replace("sec", "s")
    if re.fullmatch(r"\d+", t): return timedelta(minutes=int(t))
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", t)
    if not m or not any(g is not None for g in m.groups()): return None
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return timedelta(hours=h, minutes=mi, seconds=s)

# ============ TEXT ============
def _normalize(s):
    if not s: return ""
    s = unicodedata.normalize("NFKC", s).lower()
    s = "".join(ch if (ch.isalnum() or ch.isspace()) else " " for ch in s)
    return re.sub(r"\s+", " ", s).strip()

def _member_name_set(member):
    names = {_normalize(member.name), _normalize(member.display_name), _normalize(member.nick or "")}
    names.discard("")
    return names

def find_member(guild, name):
    if not name: return None
    name = name.strip()
    m = re.match(r"<@!?(\d+)>", name)
    if m: return guild.get_member(int(m.group(1)))
    if name.startswith("@"): name = name[1:]
    target = _normalize(name)
    if not target: return None
    if target in NAME_ALIASES:
        mem = guild.get_member(NAME_ALIASES[target])
        if mem: return mem
    members = list(guild.members)
    target_words = target.split()
    target_len = len(target)
    for mem in members:
        if target in _member_name_set(mem): return mem
    scored = []
    for mem in members:
        best = 0.0
        for c in (_normalize(mem.name), _normalize(mem.display_name), _normalize(mem.nick or "")):
            if not c: continue
            cw = c.split()
            if target_words and all(any(t == w for w in cw) for t in target_words): best = max(best, 0.9)
            if target_len >= 3 and len(c) >= 3 and (target in c or c in target): best = max(best, 0.8)
            best = max(best, SequenceMatcher(None, target, c).ratio())
        if best > 0: scored.append((best, mem))
    if not scored: return None
    scored.sort(key=lambda x: x[0], reverse=True)
    top_score, top_mem = scored[0]
    if top_score < 0.75: return None
    if len(scored) > 1 and scored[1][1].id != top_mem.id and scored[1][0] >= top_score - 0.05: return None
    return top_mem

def match_missing_entry(missing, query):
    q = _normalize(query)
    if not q: return None
    uid = NAME_ALIASES.get(q)
    if uid:
        for e in missing:
            if e.get("id") == uid: return e
    for test in (lambda n: n == q, lambda n: q in n.split(), lambda n: n in q):
        hits = [e for e in missing if test(_normalize(e.get("name", "")))]
        if len(hits) == 1: return hits[0]
    return None

def split_names_and_reason(guild, text):
    text = text.strip()
    if "|" in text:
        left, _, reason = text.partition("|")
        toks = left.replace(",", " ").split()
        reason = reason.strip()
        members = []
        for t in toks:
            m = find_member(guild, t)
            if m and m not in members: members.append(m)
        return members, reason
    toks = text.replace(",", " ").split()
    members, i = [], 0
    while i < len(toks):
        m = find_member(guild, toks[i])
        if not m: break
        if m not in members: members.append(m)
        i += 1
    reason = " ".join(toks[i:]) if i < len(toks) else None
    return members, reason

def _safe_field(text, limit=DISCORD_FIELD_LIMIT):
    if text is None: return "\u200b"
    text = str(text)
    return text if len(text) <= limit else text[: limit - 20] + "… and more"

def _safe_message(text, limit=DISCORD_MSG_LIMIT):
    return text if len(text) <= limit else text[: limit - 30] + "\n… _(truncated)_"

def message_says_done(text):
    if not text: return False
    t = text.lower().replace("\u2019", "'").replace("\u2018", "'")
    if NEGATION_RE.search(t): return False
    return any(re.search(p, t) for p in COMPLETION_PATTERNS)

def extract_completion_name(text):
    if not text: return None
    t = text.strip()
    if NEGATION_RE.search(t.lower()): return None
    m = re.match(rf"(?i)^{_COMPLETION_WORDS}\s*[:\-–—]?\s*(.+)$", t)
    if m:
        name = m.group(1).strip().strip("()[]{}").strip()
        if name: return name
    m = re.match(rf"(?i)^(.+?)\s+{_COMPLETION_WORDS}\s*[.!?]?$", t)
    if m:
        name = m.group(1).strip().strip("()[]{}").strip()
        if name: return name
    return None

# ============ NAME PARSING ============
def _split_plain_names(s):
    s = s.strip().strip(",").strip()
    if not s: return []
    if "," in s: return [p.strip() for p in s.split(",") if p.strip()]
    try: return shlex.split(s)
    except ValueError: return s.split()

def parse_missing_entries(text):
    text = (text or "").strip()
    if not text: return []
    tokens = []
    last_end = 0
    for m in MENTION_RE.finditer(text):
        for name in _split_plain_names(text[last_end:m.start()]): tokens.append(name)
        tokens.append("<@" + m.group(0).strip("<@!>") + ">")
        last_end = m.end()
    for name in _split_plain_names(text[last_end:]): tokens.append(name)
    seen, unique = set(), []
    for t in tokens:
        key = t.lower()
        if key not in seen:
            seen.add(key)
            unique.append(t)
    return unique

def _dedup_resolved(resolved):
    seen_ids, seen_names, out = set(), set(), []
    for raw, member in resolved:
        if member is not None:
            if member.id in seen_ids: continue
            seen_ids.add(member.id)
            out.append((raw, member))
        else:
            key = _normalize(raw)
            if key in seen_names: continue
            seen_names.add(key)
            out.append((raw, member))
    return out

# ============ DATE PARSING ============
def _parse_single_date(s):
    s = (s or "").strip().lower()
    if not s: return None
    if s in ("today", "tonight"):
        d = datetime.now(timezone.utc)
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    if s == "tomorrow":
        d = datetime.now(timezone.utc) + timedelta(days=1)
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    if m:
        y, mo, d = map(int, m.groups())
        try: return datetime(y, mo, d, tzinfo=timezone.utc)
        except ValueError: return None
    m = re.match(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", s)
    if m:
        d, mo, y = map(int, m.groups())
        try: return datetime(y, mo, d, tzinfo=timezone.utc)
        except ValueError: return None
    m = re.match(r"(\d{1,2})[/-](\d{1,2})$", s)
    if m:
        d, mo = map(int, m.groups())
        y = datetime.now(timezone.utc).year
        try: return datetime(y, mo, d, tzinfo=timezone.utc)
        except ValueError: return None
    m = re.match(r"(\d{1,2})\s+([a-z]+)$", s)
    if m:
        d = int(m.group(1))
        mo = _MONTHS.get(m.group(2)[:3])
        if mo:
            y = datetime.now(timezone.utc).year
            try: return datetime(y, mo, d, tzinfo=timezone.utc)
            except ValueError: return None
    m = re.match(r"([a-z]+)\s+(\d{1,2})$", s)
    if m:
        mo = _MONTHS.get(m.group(1)[:3])
        d = int(m.group(2))
        if mo:
            y = datetime.now(timezone.utc).year
            try: return datetime(y, mo, d, tzinfo=timezone.utc)
            except ValueError: return None
    return None

def _parse_absence_range_raw(text):
    text = (text or "").strip().lower()
    now = datetime.now(timezone.utc)
    m = re.match(r"for\s+(\d+)\s+(day|days|week|weeks)$", text)
    if m:
        n = int(m.group(1))
        if m.group(2).startswith("week"): n *= 7
        return now, now + timedelta(days=n)
    m = re.match(r"from\s+(.+?)\s+to\s+(.+)$", text)
    if m:
        d1 = _parse_single_date(m.group(1))
        d2 = _parse_single_date(m.group(2))
        if d1 and d2:
            start = d1
            end = d2 + timedelta(days=1) - timedelta(seconds=1)
            if end < start: return None, None
            return start, end
    m = re.match(r"until\s+(.+)$", text)
    if m:
        d = _parse_single_date(m.group(1))
        if d:
            end = d + timedelta(days=1) - timedelta(seconds=1)
            return now, end
    d = _parse_single_date(text)
    if d:
        end = d + timedelta(days=1) - timedelta(seconds=1)
        return now, end
    return None, None

def parse_absence_range(text):
    start, end = _parse_absence_range_raw(text)
    if start and end and end < datetime.now(timezone.utc): return None, None
    return start, end

def fmt_date(dt):
    return dt.strftime("%d %b %Y")
