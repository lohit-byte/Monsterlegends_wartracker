"""Discord-based bundle sync between primary and backup instances."""
import asyncio
import hashlib
import io
import json
import os
import shutil
import zipfile
from datetime import datetime
import discord
from botutils import now_utc
import botstorage

SYNC_CHANNEL_ID = 0
SYNC_ENABLED = False
CODE_SOURCE = "primary"
SELF_PATH = os.path.abspath(__file__)
_BASE = os.path.dirname(SELF_PATH)
SYNC_META_FILE = os.path.join(_BASE, "sync_meta.json")
BAD_SHA_FILE = os.path.join(_BASE, "bad_sha.txt")
CODE_EXTRA = ["aliases.json"]

_bot = None
_instance_name = "unknown"
_guard_active = lambda: True
_push_event = None
_code_pending_restart = False

def _meta_load():
    try:
        with open(SYNC_META_FILE, "r", encoding="utf-8") as f: return json.load(f)
    except (OSError, ValueError): return {}

_meta = _meta_load()

def register(bot, instance_name, guard_active, sync_channel_id=0):
    global _bot, _instance_name, _guard_active, SYNC_CHANNEL_ID, SYNC_ENABLED, CODE_SOURCE
    _bot = bot
    _instance_name = instance_name
    _guard_active = guard_active
    SYNC_CHANNEL_ID = sync_channel_id
    SYNC_ENABLED = SYNC_CHANNEL_ID != 0
    CODE_SOURCE = os.environ.get("CODE_SOURCE", "primary").lower()

def init_event():
    global _push_event
    _push_event = asyncio.Event()

def has_pending_restart(): return _code_pending_restart

def _atomic_write_json(path, obj):
    with open(path + ".tmp", "w", encoding="utf-8") as f: json.dump(obj, f, indent=2)
    os.replace(path + ".tmp", path)

def _parse_sync(m):
    if not _bot.user or m.author.id != _bot.user.id or not m.content.startswith("SYNC:"): return None
    try: return json.loads(m.content[5:])
    except ValueError: return None

def _code_names():
    n = [f for f in os.listdir(_BASE) if f.endswith(".py") and os.path.isfile(os.path.join(_BASE, f))]
    n += [f for f in CODE_EXTRA if os.path.isfile(os.path.join(_BASE, f))]
    return sorted(set(n))

def _read_local():
    return {n: open(os.path.join(_BASE, n), "rb").read() for n in _code_names()}

def _bundle_sha(files):
    h = hashlib.sha256()
    for n in sorted(files): h.update(n.encode() + b"\0" + hashlib.sha256(files[n]).digest())
    return h.hexdigest()

def _make_zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, d in files.items(): z.writestr(n, d)
    return buf.getvalue()

def _read_zip(raw):
    out = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        for i in z.infolist():
            if i.filename != os.path.basename(i.filename) or not (i.filename.endswith(".py") or i.filename in CODE_EXTRA): raise ValueError("bad")
            out[i.filename] = z.read(i.filename)
    return out

def request_state_push():
    if not SYNC_ENABLED or _push_event is None: return
    _meta["state_ts"] = now_utc().isoformat()
    try: _atomic_write_json(SYNC_META_FILE, _meta)
    except: pass
    _push_event.set()

async def _prune_sync(chan, kind, keep):
    n = 0
    async for m in chan.history(limit=100):
        meta = _parse_sync(m)
        if meta and meta.get("k") == kind:
            n += 1
            if n > keep: await m.delete()

async def push_state():
    chan = _bot.get_channel(SYNC_CHANNEL_ID)
    if not chan: return
    blob = {"wars": botstorage.read_data_file(), "warbot": botstorage._warbot_data}
    meta = {"k": "state", "from": _instance_name, "ts": _meta.get("state_ts") or now_utc().isoformat()}
    try:
        await chan.send("SYNC:" + json.dumps(meta), file=discord.File(io.BytesIO(json.dumps(blob).encode()), "state.json"))
        await _prune_sync(chan, "state", 3)
    except: pass

async def sync_pusher():
    while True:
        await _push_event.wait()
        await asyncio.sleep(3)
        _push_event.clear()
        if _guard_active(): await push_state()

async def apply_state_attachment(att, meta):
    if meta.get("ts") and _meta.get("state_ts") and datetime.fromisoformat(meta["ts"]) <= datetime.fromisoformat(_meta["state_ts"]): return
    try:
        blob = json.loads((await att.read()).decode())
        _atomic_write_json(botstorage.DATA_FILE, blob.get("wars") or {})
        _atomic_write_json(botstorage.DATA_FILE_2, blob.get("warbot") or {})
        _meta["state_ts"] = meta["ts"]
        _atomic_write_json(SYNC_META_FILE, _meta)
        print(f"[sync] state applied from {meta.get('from')}")
    except: pass

async def sync_pull_latest():
    chan = _bot.get_channel(SYNC_CHANNEL_ID)
    if not chan: return
    async for m in chan.history(limit=50):
        meta = _parse_sync(m)
        if meta and meta.get("k") == "state" and m.attachments:
            await apply_state_attachment(m.attachments[0], meta)
            break

async def push_bundle():
    chan = _bot.get_channel(SYNC_CHANNEL_ID)
    if not chan: return
    files = _read_local()
    sha = _bundle_sha(files)
    meta = {"k": "bundle", "from": _instance_name, "sha": sha, "files": sorted(files), "ts": now_utc().isoformat()}
    try:
        await chan.send("SYNC:" + json.dumps(meta), file=discord.File(io.BytesIO(_make_zip(files)), "bundle.zip"))
        _meta["code_sha_pushed"] = sha
        _atomic_write_json(SYNC_META_FILE, _meta)
        await _prune_sync(chan, "bundle", 2)
        print(f"[sync] bundle pushed: {', '.join(sorted(files))}")
    except: pass

async def apply_bundle_attachment(att, meta):
    global _code_pending_restart
    if _instance_name == CODE_SOURCE: return
    try:
        with open(BAD_SHA_FILE) as f: bad = f.read().strip()
    except: bad = ""
    if meta.get("sha") == bad: return
    try: files = _read_zip(await att.read())
    except: return
    if _bundle_sha(files) != meta.get("sha"): return
    local = _read_local()
    changed = [n for n in files if local.get(n) != files[n]]
    if not changed: return
    for n in changed:
        if n.endswith(".py"):
            try: compile(files[n], n, "exec")
            except: return
    for n in changed:
        p = os.path.join(_BASE, n)
        if os.path.exists(p): shutil.copy2(p, p + ".bak")
        with open(p + ".tmp", "wb") as f: f.write(files[n])
        os.replace(p + ".tmp", p)
    print(f"[sync] updated: {', '.join(changed)}")
    if _guard_active(): _code_pending_restart = True
    else:
        await _bot.close(); os._exit(0)

async def sync_boot_code():
    chan = _bot.get_channel(SYNC_CHANNEL_ID)
    if not chan: return
    if _instance_name == CODE_SOURCE:
        await push_bundle()
        return
    async for m in chan.history(limit=50):
        meta = _parse_sync(m)
        if meta and meta.get("k") == "bundle" and m.attachments:
            await apply_bundle_attachment(m.attachments[0], meta)
            break

async def handle_sync_message(message):
    if not SYNC_ENABLED or message.channel.id != SYNC_CHANNEL_ID: return False
    meta = _parse_sync(message)
    if not meta: return False
    if meta.get("from") != _instance_name and message.attachments:
        if meta.get("k") == "state": await apply_state_attachment(message.attachments[0], meta)
        elif meta.get("k") == "bundle": await apply_bundle_attachment(message.attachments[0], meta)
    return True

async def clear_bak():
    await asyncio.sleep(60)
    for n in os.listdir(_BASE):
        if n.endswith(".bak"):
            try: os.remove(os.path.join(_BASE, n))
            except: pass
