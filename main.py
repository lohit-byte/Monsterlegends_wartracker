# main.py — v1 (Public Release)
import os
import json

def _load_env():
    try:
        with open(".env", "r", encoding="utf-8") as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    os.environ[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass

_load_env()

import asyncio
import re
from datetime import datetime, timedelta, timezone
import discord
from discord.ext import commands

from botutils import (
    now_utc, fmt_td, progress_bar, ts, parse_time, find_member,
    _normalize, _safe_field, _safe_message, message_says_done,
    extract_completion_name, parse_missing_entries, _dedup_resolved,
    parse_absence_range, fmt_date, load_aliases, save_aliases, 
    match_missing_entry, split_names_and_reason, NAME_ALIASES
)
import botstorage
import sync

VERSION = "v1"
CREATOR = "Lohit"

# ============ LOAD CONFIG ============
def load_config():
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise SystemExit("ERROR: config.json not found. Rename config.example.json to config.json and fill it out.")

CONFIG = load_config()
TOKEN = os.environ.get("DISCORD_TOKEN") or CONFIG.get("token")
if not TOKEN or TOKEN == "YOUR_DISCORD_BOT_TOKEN_HERE":
    raise SystemExit("ERROR: Set your bot token in config.json or as an environment variable.")

# Add these lines to support failover environments
os.environ["CODE_SOURCE"] = CONFIG.get("code_source", "primary")
PREFIX = CONFIG.get("prefix", "!")

STAFF_ROLE_IDS = set(CONFIG.get("staff_role_ids", []))
LEAD_ROLE_IDS = set(CONFIG.get("lead_role_ids", []))
HEARTBEAT_CHANNEL_ID = CONFIG.get("heartbeat_channel_id", 0)
ALERT_PING_ID = CONFIG.get("alert_ping_id", 0)
INSTANCE_NAME = CONFIG.get("instance_name", "local")
SYNC_CHANNEL_ID = CONFIG.get("sync_channel_id", 0)

# Dynamically build team dictionaries
TEAMS = CONFIG.get("teams", {})
CHANNELS = {}
ABSENCE_CHANNELS = {}
TEAM_LEAD_ROLE_IDS = {}

for team_name, t_data in TEAMS.items():
    CHANNELS[t_data["channel_id"]] = {
        "name": team_name,
        "role_id": t_data["member_role_id"]
    }
    ABSENCE_CHANNELS[team_name] = t_data["absence_channel_id"]
    TEAM_LEAD_ROLE_IDS[team_name] = t_data.get("team_lead_role_ids", [])

CHANNEL_BY_TEAM = {conf["name"]: cid for cid, conf in CHANNELS.items()}

# ============ FAILOVER ============
HEARTBEAT_INTERVAL = 10
HEARTBEAT_TIMEOUT = 30
_is_active = False
_instance_started = datetime.now(timezone.utc)
_instance_stats = {"commands": 0, "last_cmd": None, "last_cmd_at": None}

PHASE = timedelta(hours=24)
WAR = timedelta(hours=48)
AUTO_REMOVE_ON_COMPLETION = True

PING_EVENTS = [
    ("defence_start", timedelta(0)),
    ("defence_30m",   timedelta(hours=23, minutes=30)),
    ("attack_start",  timedelta(hours=24)),
    ("attack_12h",    timedelta(hours=36)),
    ("attack_6h",     timedelta(hours=42)),
    ("attack_3h",     timedelta(hours=45)),
    ("attack_1h",     timedelta(hours=47)),
    ("war_end",       timedelta(hours=48)),
]
PING_LABELS = {
    "defence_start": "Defence phase start",
    "defence_30m":   "30m warning (defence)",
    "attack_start":  "Attack phase start",
    "attack_12h":    "12h warning (attack)",
    "attack_6h":     "6h warning (attack)",
    "attack_3h":     "3h warning (attack)",
    "attack_1h":     "1h warning (attack)",
    "war_end":       "War end (leads ping)",
}

NO_PINGS = discord.AllowedMentions.none()

# ============ BOT ============
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.reactions = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents)
bot.remove_command("help")

sync.register(bot, INSTANCE_NAME, lambda: _is_active, SYNC_CHANNEL_ID)
botstorage.set_push_callback(sync.request_state_push)

_bg_tasks = set()

def _spawn(coro):
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    return t

def guard_active():
    return _is_active

def track_command(name):
    _instance_stats["commands"] += 1
    _instance_stats["last_cmd"] = name
    _instance_stats["last_cmd_at"] = datetime.now(timezone.utc).isoformat()

# ============ HELPERS ============
def is_staff(member):
    if member.guild_permissions.administrator: return True
    return bool({r.id for r in member.roles} & STAFF_ROLE_IDS)

def is_lead_or_colead(member):
    if member.guild_permissions.administrator: return True
    return bool({r.id for r in member.roles} & LEAD_ROLE_IDS)

def is_team_lead(member, team_name):
    if member.guild_permissions.administrator: return True
    ids = set(TEAM_LEAD_ROLE_IDS.get(team_name, []))
    return bool({r.id for r in member.roles} & ids)

def team_conf_by_name(name):
    for conf in CHANNELS.values():
        if conf["name"] == name: return conf
    return None

def war_channel_for(team_name):
    cid = CHANNEL_BY_TEAM.get(team_name)
    return bot.get_channel(cid) if cid else None

def get_staff_roles(guild):
    return [r for r in (guild.get_role(rid) for rid in STAFF_ROLE_IDS) if r]

def get_team_lead_mentions(guild, team_name):
    ids = TEAM_LEAD_ROLE_IDS.get(team_name, [])
    roles = [guild.get_role(rid) for rid in ids]
    return " ".join(r.mention for r in roles if r)

def _counts_as_absence_post(msg):
    if msg.author.bot: return False
    content = (msg.content or "").strip()
    if not content or content.startswith("!"): return False
    stripped = re.sub(r"<@!?\d+>", "", content).strip()
    return bool(stripped)

async def _send_failover_alert(text):
    if HEARTBEAT_CHANNEL_ID == 0: return
    chan = bot.get_channel(HEARTBEAT_CHANNEL_ID)
    if not chan: return
    ping = f"<@{ALERT_PING_ID}> " if ALERT_PING_ID else ""
    try: await chan.send(f"{ping}{text}", allowed_mentions=discord.AllowedMentions(users=True))
    except discord.HTTPException: pass

def build_ping_message(event_type, state):
    role_m = state["role"].mention if state["role"] else ""
    anchor = state["war_start"]

    if event_type == "defence_start": return f"🛡️ {role_m} **DEFENCE PHASE HAS STARTED!** Phase ends {ts(anchor + PHASE)}."
    if event_type == "defence_30m": return f"⚠️ {role_m} **Defence phase ends in 30 minutes!** Get defences up."
    if event_type == "attack_start": return f"⚔️ {role_m} **ATTACK PHASE HAS STARTED!** War ends {ts(anchor + WAR)}."
    if event_type in ("attack_12h", "attack_6h", "attack_3h", "attack_1h"):
        labels = {"attack_12h": "12 hours", "attack_6h": "6 hours", "attack_3h": "3 hours", "attack_1h": "1 hour"}
        missing = state.get("missing") or []
        if missing:
            mentions = botstorage.missing_as_mentions(state)
            names = ", ".join(e["name"] for e in missing)
            body = f"⚔️ **Attack phase: {labels[event_type]} left!**\n\n⚠️ {mentions}\n**Still haven't completed their attacks:**\n{names}"
        else:
            body = f"⚔️ {role_m} **Attack phase: {labels[event_type]} left!**"
        return _safe_message(body)
    if event_type == "war_end":
        leads_m = state.get("team_leads_mention", "")
        ping = leads_m or (state["role"].mention if state["role"] else "@staff")
        missing = state.get("missing") or []
        body = f"🏁 {ping} **The 48-hour war has ended!** Time to start a new war.\n"
        if missing:
            names = ", ".join(e["name"] for e in missing)
            body += f"\n⚠️ **Missing attacks — verify in-game:**\n{names}\n\n_Leads: log a warning with `!warning add <name> [reason]` if the miss counts._"
        else:
            body += "\n_No missing attacks this war — everyone cleared._"
        return _safe_message(body)
    return "(unknown event)"

async def _safe_send(channel, text):
    try: await channel.send(_safe_message(text), allowed_mentions=discord.AllowedMentions(everyone=False, roles=True, users=True))
    except discord.HTTPException as e: print(f"[ping] send failed: {e}")

def schedule_pings(team_name):
    state = botstorage.wars[team_name]
    anchor = state["war_start"]
    now = now_utc()
    for event_type, offset in PING_EVENTS:
        delay = (anchor + offset - now).total_seconds()
        if delay > 0: state["ping_tasks"].append(_spawn(_sleep_then_send(delay, team_name, event_type)))

async def _sleep_then_send(delay, team_name, event_type):
    try:
        await asyncio.sleep(delay)
        if not guard_active(): return
        state = botstorage.wars.get(team_name)
        if not state: return
        await _safe_send(state["channel"], build_ping_message(event_type, state))
        if event_type == "war_end":
            ended = state
            async def _delayed_cleanup():
                await asyncio.sleep(65)
                if botstorage.wars.get(team_name) is ended: cancel_war(team_name)
            _spawn(_delayed_cleanup())
    except asyncio.CancelledError: pass
    except Exception as e: print(f"[ping] {team_name}/{event_type} crashed: {e}")

async def scan_absence_channel(team_name, since):
    state = botstorage.wars.get(team_name)
    if not state: return
    chan_id = ABSENCE_CHANNELS.get(team_name)
    chan = bot.get_channel(chan_id) if chan_id else None
    if not chan: return
    try:
        count = 0
        async for msg in chan.history(after=since, limit=1000):
            if _counts_as_absence_post(msg):
                botstorage.excuse_member(state, msg.author.id)
                count += 1
    except discord.HTTPException as e: print(f"[absence] scan failed: {e}")

def apply_absence_windows(state):
    try:
        ws = state["war_start"]
        we = state["attack_end"]
    except KeyError: return 0
    added = 0
    for a in botstorage.get_all_absences():
        try:
            f = datetime.fromisoformat(a["from"])
            t = datetime.fromisoformat(a["to"])
        except (KeyError, ValueError): continue
        if f <= we and t >= ws:
            uid = a.get("user_id")
            if uid and uid not in state["absent"]:
                botstorage.excuse_member(state, uid)
                added += 1
    return added

async def scan_war_channel_for_completions(team_name):
    state = botstorage.wars.get(team_name)
    if not state or not state.get("missing"): return 0
    channel = state["channel"]
    removed = 0
    try:
        ids = botstorage.missing_member_ids(state)
        if not ids: return 0
        async for msg in channel.history(after=state["defence_end"], limit=1000):
            if msg.author.bot or msg.author.id not in ids: continue
            if botstorage._was_disputed(state, msg.author.id): continue
            if message_says_done(msg.content):
                if botstorage.remove_from_missing(state, msg.author, reason="history_scan"):
                    removed += 1
                    ids = botstorage.missing_member_ids(state)
        if removed: botstorage.save_wars()
    except discord.HTTPException as e: print(f"[missing] history scan failed: {e}")
    return removed

def build_status_embed(state):
    now = now_utc()
    team = state["team_name"]
    anchor = state["war_start"]
    d_end = state["defence_end"]
    a_end = state["attack_end"]
    guild = state["channel"].guild

    upcoming = [PING_LABELS[k] for k, off in PING_EVENTS if anchor + off > now]
    pings_line = f"**{len(upcoming)} ping(s) remaining**\n" + ("\n".join(f"• {p}" for p in upcoming) if upcoming else "_All pings sent._")

    if now < d_end:
        bar = progress_bar((now - anchor).total_seconds(), PHASE.total_seconds())
        embed = discord.Embed(
            title=f"🛡️ DEFENCE PHASE — {team}",
            color=discord.Color.blue(),
            description=f"{bar}\n**Time left in defence:** {fmt_td(d_end - now)}\n**Defence ends:** {ts(d_end)}\n**Attack starts:** {ts(d_end)}\n**War ends:** {ts(a_end)}",
            timestamp=now,
        )
    elif now < a_end:
        bar = progress_bar((now - d_end).total_seconds(), PHASE.total_seconds())
        embed = discord.Embed(
            title=f"⚔️ ATTACK PHASE — {team}",
            color=discord.Color.red(),
            description=f"{bar}\n**Defence phase ended:** {fmt_td(now - d_end)} ago\n**Time left in attack:** {fmt_td(a_end - now)}\n**Attack ends:** {ts(a_end)}",
            timestamp=now,
        )
        missing = state.get("missing") or []
        if missing:
            embed.add_field(name=f"⚠️ Yet to complete attacks ({len(missing)})", value=_safe_field(botstorage.missing_as_text(state)), inline=False)
    else:
        embed = discord.Embed(title=f"🏁 WAR ENDED — {team}", color=discord.Color.greyple(), description=f"War ended {ts(a_end)}.", timestamp=now)

    absent_ids = state.get("absent", set())
    if absent_ids:
        names = [guild.get_member(uid).display_name if guild.get_member(uid) else "unknown member" for uid in absent_ids]
        embed.add_field(name=f"🏖️ Excused (absence channel) — {len(names)}", value=_safe_field(", ".join(names)), inline=False)

    embed.add_field(name="🔔 Pings", value=_safe_field(pings_line), inline=False)
    embed.set_footer(text="Updates automatically every minute")
    return embed

async def status_updater(team_name, message):
    try:
        while True:
            await asyncio.sleep(60)
            state = botstorage.wars.get(team_name)
            if not state: return
            if not guard_active(): continue
            try: await message.edit(embed=build_status_embed(state))
            except discord.NotFound: return
            except discord.HTTPException as e: print(f"[status] edit failed: {e}")
            if now_utc() >= state["attack_end"]: return
    except asyncio.CancelledError: pass

def cancel_war(team_name):
    state = botstorage.wars.pop(team_name, None)
    if not state: return
    for t in state.get("ping_tasks", []): t.cancel()
    if state.get("status_task"): state["status_task"].cancel()
    botstorage.save_wars()

async def restore_wars():
    raw = botstorage.read_data_file()
    now = now_utc()
    restored = 0
    for team_name, entry in raw.items():
        conf = team_conf_by_name(team_name)
        if not conf: continue
        try:
            war_start = datetime.fromisoformat(entry["war_start"])
            channel_id = int(entry["channel_id"])
        except (KeyError, ValueError, TypeError): continue
        if now >= war_start + WAR: continue
        channel = bot.get_channel(channel_id)
        if not channel: continue
        guild = channel.guild
        state = {
            "team_name": team_name, "war_start": war_start, "defence_end": war_start + PHASE, "attack_end": war_start + WAR,
            "channel": channel, "role": guild.get_role(conf["role_id"]), "staff_roles": get_staff_roles(guild),
            "team_leads_mention": get_team_lead_mentions(guild, team_name), "ping_tasks": [], "status_message": None, "status_task": None,
            "missing": botstorage._normalize_missing_list(entry.get("missing", [])), "absent": set(entry.get("absent", [])), "removed_log": entry.get("removed_log", []),
        }
        botstorage.wars[team_name] = state
        apply_absence_windows(state)
        await scan_absence_channel(team_name, war_start)
        await scan_war_channel_for_completions(team_name)
        schedule_pings(team_name)
        restored += 1
        msg_id = entry.get("status_message_id")
        status_channel_id = entry.get("status_channel_id")
        if msg_id:
            try:
                fetch_channel = (bot.get_channel(int(status_channel_id)) if status_channel_id else channel) or channel
                msg = await fetch_channel.fetch_message(int(msg_id))
                state["status_message"] = msg
                state["status_task"] = _spawn(status_updater(team_name, msg))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError): pass
    print(f"[restore] {restored} war(s) restored.")

# ============ COMMANDS (WAR) ============
@bot.command(name="war")
@commands.guild_only()
async def war(ctx, *, args: str = None):
    if not guard_active(): return
    track_command("war")
    args = (args or "").strip()
    team_name, sub, rest = None, "", ""

    if args:
        parts = args.split(maxsplit=1)
        first = parts[0].lower()
        remainder = parts[1] if len(parts) > 1 else ""
        if first in [t.lower() for t in CHANNEL_BY_TEAM.keys()]:
            team_name = next(t for t in CHANNEL_BY_TEAM.keys() if t.lower() == first)
            if remainder:
                p2 = remainder.split(maxsplit=1)
                sub = p2[0].lower()
                rest = p2[1] if len(p2) > 1 else ""
        else:
            sub = first
            rest = remainder

    if team_name is None:
        if ctx.channel.id in CHANNELS: team_name = CHANNELS[ctx.channel.id]["name"]
        else: return await ctx.send(f"Use `!war <{'|'.join(CHANNEL_BY_TEAM.keys())}> <command>` in this channel, or run the command inside a team's war channel.", allowed_mentions=NO_PINGS)

    if sub == "status": return await show_status(ctx, team_name)
    if sub in ("absent", "absence"): return await handle_absent(ctx, team_name, rest, ctx.author)
    
    if sub == "missing":
        if not is_staff(ctx.author): return await ctx.send("Only staff can manage the missing list.", allowed_mentions=NO_PINGS)
        return await handle_missing(ctx, team_name, rest)

    if sub not in ("defence", "defense", "prep", "preparation", "attack", "cancel"): return await ctx.send("Invalid subcommand.", allowed_mentions=NO_PINGS)
    if not is_staff(ctx.author): return await ctx.send("Only staff can manage wars.", allowed_mentions=NO_PINGS)

    if sub == "cancel":
        cancel_war(team_name)
        return await ctx.send(f"War tracking for **{team_name}** cancelled.", allowed_mentions=NO_PINGS)

    if not rest.lower().startswith("ends"): return await ctx.send(f"Please use: `!war {team_name.lower()} {sub} ends <remaining time>`", allowed_mentions=NO_PINGS)
    
    remaining = parse_time(rest[4:].strip())
    if remaining is None: return await ctx.send("Couldn't parse that time. Try `21h50m`, `90m`, `1h`.", allowed_mentions=NO_PINGS)
    if remaining > PHASE: return await ctx.send("A single phase can't last longer than 24h.", allowed_mentions=NO_PINGS)

    now = now_utc()
    anchor = (now + remaining) - PHASE if sub in ("defence", "defense", "prep", "preparation") else (now + remaining) - PHASE - PHASE
    if now >= anchor + WAR: return await ctx.send("That would put the war past its 48-hour window.", allowed_mentions=NO_PINGS)

    war_channel = war_channel_for(team_name)
    if not war_channel: return await ctx.send(f"War channel for **{team_name}** not found.", allowed_mentions=NO_PINGS)

    cancel_war(team_name)
    team_conf = team_conf_by_name(team_name)
    state = {
        "team_name": team_name, "war_start": anchor, "defence_end": anchor + PHASE, "attack_end": anchor + WAR,
        "channel": war_channel, "role": ctx.guild.get_role(team_conf["role_id"]), "staff_roles": get_staff_roles(ctx.guild),
        "team_leads_mention": get_team_lead_mentions(ctx.guild, team_name), "ping_tasks": [], "status_message": None, "status_task": None,
        "missing": [], "absent": set(), "removed_log": [],
    }
    botstorage.wars[team_name] = state
    await scan_absence_channel(team_name, anchor)
    win_count = apply_absence_windows(state)
    schedule_pings(team_name)
    botstorage.save_wars()
    await ctx.send(f"✅ War tracking for **{team_name}** registered.\nDefence ends: {ts(anchor + PHASE)} • War ends: {ts(anchor + WAR)}\nExcused: **{len(state['absent'])}**\nPings will be sent in {war_channel.mention}.", allowed_mentions=NO_PINGS)

# ============ COMMANDS (MISSING) ============
async def handle_missing(ctx, team_name, rest):
    state = botstorage.wars.get(team_name)
    if not state: return await ctx.send(f"No active war tracked for **{team_name}**.", allowed_mentions=NO_PINGS)

    if not rest:
        lst = state.get("missing") or []
        if not lst: return await ctx.send("No missing players recorded yet.", allowed_mentions=NO_PINGS)
        return await ctx.send(_safe_message("**Missing:** " + botstorage.missing_as_text(state)), allowed_mentions=NO_PINGS)

    parts = rest.split(maxsplit=1)
    sub = parts[0].lower()
    tail = parts[1].strip() if len(parts) > 1 else ""

    if sub == "clear":
        state["missing"] = []
        botstorage.save_wars()
        return await ctx.send("✅ Missing list cleared.", allowed_mentions=NO_PINGS)

    if sub == "log": return await show_removal_log(ctx, state)

    if sub in ("add", "remove", "rm", "set"):
        if not tail: return await ctx.send(f"Format: `!war missing {sub} name1 name2`", allowed_mentions=NO_PINGS)
        return await _apply_missing(ctx, state, sub, tail)

    return await _apply_missing(ctx, state, "set", rest)

async def show_removal_log(ctx, state):
    log = state.get("removed_log") or []
    if not log: return await ctx.send("No changes recorded for this war yet.", allowed_mentions=NO_PINGS)
    lines = [f"**🗒️ Recent missing-list changes — {state['team_name']}**"]
    reason_label = {"self": "self-declared ✅", "staff": "staff-declared ✅", "history_scan": "history scan ✅", "manual": "manual remove", "re-add:undo": "↩️ undo (❌)"}
    for entry in reversed(log[-20:]):
        when = datetime.fromisoformat(entry["when"])
        r = entry.get("reason", "?")
        lines.append(f"• **{entry['name']}** — {ts(when)} — _{reason_label.get(r, r)}_")
    await ctx.send(_safe_message("\n".join(lines)), allowed_mentions=NO_PINGS)

async def _apply_missing(ctx, state, mode, names_str):
    team_name = state["team_name"]
    raw_names = parse_missing_entries(names_str)
    if not raw_names: return await ctx.send("Format: `!war missing name1 name2`", allowed_mentions=NO_PINGS)

    guild = ctx.guild
    absent_ids = state.get("absent", set())
    resolved, ignored = [], []
    for raw in raw_names:
        member = find_member(guild, raw)
        if member and member.id in absent_ids:
            ignored.append(member.display_name)
            continue
        resolved.append((raw, member))

    resolved = _dedup_resolved(resolved)
    unresolved = [raw for raw, m in resolved if m is None]

    def _entry_for(raw, member):
        if member:
            name = member.display_name if raw.startswith("<@") else raw
            return {"name": name, "id": member.id}
        return {"name": raw, "id": None}

    if mode == "set":
        state["missing"] = [_entry_for(r, m) for r, m in resolved]
        added = state["missing"]
        removed = []
    elif mode == "add":
        existing_ids = botstorage.missing_member_ids(state)
        existing_names = {e["name"].lower() for e in state.get("missing", [])}
        new_list = list(state.get("missing") or [])
        added = []
        for raw, member in resolved:
            if member is not None:
                if member.id in existing_ids: continue
                existing_ids.add(member.id)
            else:
                if raw.lower() in existing_names: continue
                existing_names.add(raw.lower())
            e = _entry_for(raw, member)
            new_list.append(e)
            added.append(e)
        state["missing"] = new_list
        removed = []
    else:
        removed = []
        for raw, member in resolved:
            entry = None
            if member: entry = next((e for e in state["missing"] if e.get("id") == member.id), None)
            if entry is None: entry = match_missing_entry(state["missing"], raw)
            if entry:
                state["missing"].remove(entry)
                removed.append(entry["name"])
                if raw in unresolved: unresolved.remove(raw)
                log = state.setdefault("removed_log", [])
                log.append({"id": entry.get("id"), "name": entry["name"], "when": now_utc().isoformat(), "reason": "manual", "actor_id": ctx.author.id})
                del log[:-50]
        added = []

    botstorage.save_wars()

    auto_removed = 0
    if AUTO_REMOVE_ON_COMPLETION and mode in ("set", "add") and state["missing"]: auto_removed = await scan_war_channel_for_completions(team_name)

    lines = []
    if mode == "set": lines.append(f"✅ Missing list set to {len(state['missing'])} player(s).")
    elif mode == "add": lines.append(f"✅ Added {len(added)} player(s).")
    else: lines.append(f"✅ Removed {len(removed)} player(s).")
    if ignored: lines.append("🏖️ Skipped (excused): " + ", ".join(ignored))
    if unresolved: lines.append("❓ Couldn't auto-resolve: " + ", ".join(unresolved))
    if auto_removed: lines.append(f"🔍 Auto-removed **{auto_removed}** who already posted 'attacks done'.")
    if state["missing"]: lines.append("**Current missing:** " + botstorage.missing_as_text(state))
    else: lines.append("Missing list is now empty.")
    await ctx.send(_safe_message("\n".join(lines)), allowed_mentions=NO_PINGS)

# ============ COMMANDS (ABSENCE) ============
async def handle_absent(ctx, team_name, rest, author):
    state = botstorage.wars.get(team_name)
    if not rest or rest.lower() == "list": return await show_absent(ctx, team_name)

    parts = rest.split(maxsplit=1)
    sub = parts[0].lower()
    tail = parts[1].strip() if len(parts) > 1 else ""

    if sub == "windows": return await show_windows(ctx, team_name)
    if sub == "request": return await handle_absent_request(ctx, team_name, tail, author)
    if not is_staff(author): return await ctx.send("Only staff can manage the excused list.", allowed_mentions=NO_PINGS)
    if not state: return await ctx.send(f"No active war tracked for **{team_name}**.", allowed_mentions=NO_PINGS)

    if sub == "clear":
        state["absent"] = set()
        botstorage.save_wars()
        return await ctx.send("✅ Excused list cleared for this war.", allowed_mentions=NO_PINGS)

    if sub == "add":
        if not tail: return await ctx.send("Format: `!war absent add <name> [until 16 oct]`", allowed_mentions=NO_PINGS)
        return await _apply_absent_add(ctx, state, tail)

    if sub in ("remove", "rm"):
        if not tail: return await ctx.send("Format: `!war absent remove <name>`", allowed_mentions=NO_PINGS)
        return await _apply_absent_remove(ctx, state, tail)

    return await _apply_absent_add(ctx, state, rest)

async def handle_absent_request(ctx, team_name, tail, author):
    if not tail: return await ctx.send("Usage: `!war absent request until 16 oct`", allowed_mentions=NO_PINGS)
    from_dt, to_dt = parse_absence_range(tail)
    if not from_dt or not to_dt: return await ctx.send("Couldn't parse that date range (or it's already in the past).", allowed_mentions=NO_PINGS)
    leads_m = get_team_lead_mentions(ctx.guild, team_name)
    msg = await ctx.send(f"📝 **Absence request — {author.mention}**\nWindow: **{fmt_date(from_dt)} → {fmt_date(to_dt)}**\n{leads_m}\n\n_Team leads: react ✅ to approve, ❌ to reject._", allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False, replied_user=False))
    try:
        await msg.add_reaction("✅")
        await msg.add_reaction("❌")
    except discord.HTTPException: pass
    botstorage.pending_abs_requests[msg.id] = {"user_id": author.id, "name": author.display_name, "team_name": team_name, "from": from_dt.isoformat(), "to": to_dt.isoformat(), "when": now_utc().isoformat()}
    if len(botstorage.pending_abs_requests) > 500:
        for k in list(botstorage.pending_abs_requests.keys())[:-400]: del botstorage.pending_abs_requests[k]
    botstorage.persist_pending()

async def _apply_absent_add(ctx, state, tail):
    name_part, date_part = tail, ""
    m = re.search(r"\s+(until\s+|from\s+|for\s+)", tail, re.IGNORECASE)
    if m:
        name_part = tail[:m.start()].strip()
        date_part = tail[m.start():].strip()
    raw_names = parse_missing_entries(name_part)
    if not raw_names: return await ctx.send("Couldn't parse name(s).", allowed_mentions=NO_PINGS)
    guild = ctx.guild
    added, scheduled, unresolved = [], [], []
    from_dt = to_dt = None
    if date_part:
        from_dt, to_dt = parse_absence_range(date_part)
        if not from_dt or not to_dt: return await ctx.send(f"Couldn't parse date range `{date_part}`.", allowed_mentions=NO_PINGS)
    for raw in raw_names:
        member = find_member(guild, raw)
        if member is None:
            unresolved.append(raw)
            continue
        if from_dt and to_dt:
            botstorage.add_absence_window(member.id, member.display_name, from_dt, to_dt, "", ctx.author.id)
            if from_dt <= state["attack_end"] and to_dt >= state["war_start"]:
                botstorage.excuse_member(state, member.id)
                added.append(member.display_name)
            else: scheduled.append(member.display_name)
        else:
            botstorage.excuse_member(state, member.id)
            added.append(member.display_name)
    botstorage.save_wars()
    lines = []
    if from_dt and to_dt: lines.append(f"📅 Absence window saved: **{fmt_date(from_dt)} → {fmt_date(to_dt)}**")
    if added: lines.append(f"✅ Excused this war: {', '.join(added)}")
    if scheduled: lines.append(f"🕒 Window saved (not this war): {', '.join(scheduled)}")
    if unresolved: lines.append("❓ Couldn't resolve: " + ", ".join(unresolved))
    if not lines: lines.append("Nothing to do.")
    await ctx.send(_safe_message("\n".join(lines)), allowed_mentions=NO_PINGS)

async def _apply_absent_remove(ctx, state, tail):
    raw_names = parse_missing_entries(tail)
    removed, unresolved = [], []
    for raw in raw_names:
        member = find_member(ctx.guild, raw)
        if member is None:
            unresolved.append(raw)
            continue
        state["absent"].discard(member.id)
        if botstorage.remove_absence_windows_for(member.id): removed.append(member.display_name + " (window removed)")
        else: removed.append(member.display_name)
    botstorage.save_wars()
    lines = [f"✅ Removed **{len(removed)}** player(s) from excused."]
    if removed: lines.append("  " + ", ".join(removed))
    if unresolved: lines.append("❓ Couldn't resolve: " + ", ".join(unresolved))
    await ctx.send(_safe_message("\n".join(lines)), allowed_mentions=NO_PINGS)

async def show_absent(ctx, team_name):
    state = botstorage.wars.get(team_name)
    lines = []
    if state and state.get("absent"):
        absent_ids = state["absent"]
        names = [ctx.guild.get_member(uid).display_name if ctx.guild.get_member(uid) else "unknown member" for uid in absent_ids]
        lines.append(f"🏖️ **Excused for {team_name} this war ({len(names)}):**")
        lines.append(", ".join(names))
    else: lines.append(f"🏖️ No excused players for **{team_name}** this war.")
    windows = botstorage.list_active_windows()
    if windows:
        lines.append("")
        lines.append(f"📅 **Active/upcoming absence windows ({len(windows)}):**")
        for a in windows:
            try:
                f = datetime.fromisoformat(a["from"])
                t = datetime.fromisoformat(a["to"])
            except (KeyError, ValueError): continue
            lines.append(f"• **{a['name']}** — {fmt_date(f)} → {fmt_date(t)}")
    await ctx.send(_safe_message("\n".join(lines)), allowed_mentions=NO_PINGS)

async def show_windows(ctx, team_name):
    windows = botstorage.list_active_windows()
    if not windows: return await ctx.send("No active or upcoming absence windows.", allowed_mentions=NO_PINGS)
    lines = [f"📅 **Absence windows ({len(windows)}):**"]
    for a in windows:
        try:
            f = datetime.fromisoformat(a["from"])
            t = datetime.fromisoformat(a["to"])
        except (KeyError, ValueError): continue
        lines.append(f"• **{a['name']}** — {fmt_date(f)} → {fmt_date(t)}")
    await ctx.send(_safe_message("\n".join(lines)), allowed_mentions=NO_PINGS)

async def show_status(ctx, team_name):
    state = botstorage.wars.get(team_name)
    if not state: return await ctx.send(f"No active war tracked for **{team_name}**.", allowed_mentions=NO_PINGS)
    if state.get("status_task"): state["status_task"].cancel()
    if state.get("status_message"):
        try: await state["status_message"].delete()
        except discord.HTTPException: pass
    msg = await ctx.send(embed=build_status_embed(state), allowed_mentions=NO_PINGS)
    state["status_message"] = msg
    state["status_task"] = _spawn(status_updater(team_name, msg))
    botstorage.save_wars()

# ============ COMMANDS (WARNINGS) ============
@bot.command(name="warning")
@commands.guild_only()
async def warning_cmd(ctx, *, args: str = None):
    if not guard_active(): return
    track_command("warning")
    args = (args or "").strip()
    team_name, sub, rest = None, "", ""

    if args:
        parts = args.split(maxsplit=1)
        first = parts[0].lower()
        remainder = parts[1] if len(parts) > 1 else ""
        if first in [t.lower() for t in CHANNEL_BY_TEAM.keys()]:
            team_name = next(t for t in CHANNEL_BY_TEAM.keys() if t.lower() == first)
            if remainder:
                p2 = remainder.split(maxsplit=1)
                sub = p2[0].lower()
                rest = p2[1] if len(p2) > 1 else ""
        else:
            sub = first
            rest = remainder

    if team_name is None:
        if ctx.channel.id in CHANNELS: team_name = CHANNELS[ctx.channel.id]["name"]
        else:
            try: await ctx.message.delete()
            except discord.HTTPException: pass
            return

    if not is_team_lead(ctx.author, team_name):
        try: await ctx.message.delete()
        except discord.HTTPException: pass
        return

    if sub == "add": return await warn_add(ctx, team_name, rest)
    if sub in ("remove", "rm"): return await warn_remove(ctx, rest)
    if sub == "list": return await warn_list(ctx, rest)
    if sub == "status": return await warn_status(ctx, team_name)
    if sub == "reset": return await warn_reset(ctx, rest)

    await ctx.send("**Warning commands** (lead + co-lead only)\n`!warning add <name1> <name2> ... [| reason] [public]`\n`!warning remove <name>` — undo the latest\n`!warning reset <name>` — clear all\n`!warning list <name>` — history for a player\n`!warning status` — team summary", allowed_mentions=NO_PINGS)

async def warn_add(ctx, team_name, rest):
    if not rest: return await ctx.send("Format: `!warning add <name1> <name2> [| reason] [public]`", allowed_mentions=NO_PINGS)
    is_public = False
    low = rest.lower()
    if low.endswith(" public"):
        is_public = True
        rest = rest[:-7].strip()
    elif low == "public":
        is_public = True
        rest = ""
    members, reason = split_names_and_reason(ctx.guild, rest)
    if not members: return await ctx.send(f"Couldn't find any members in `{rest}`.", allowed_mentions=NO_PINGS)
    confirmations = []
    for member in members:
        n = botstorage.add_warning(member.id, member.display_name, team_name, reason, ctx.author.id)
        label = {1: "first warning", 2: "final warning"}.get(n, "review for agreed rule")
        conf = f"📌 **{member.display_name}** — warning **#{n}** ({label})\nTeam: **{team_name}** • Reason: {reason or '_not specified_'}"
        confirmations.append(conf)
        try:
            await member.send(f"⚠️ You've received a warning from **{team_name}** leadership.\nWarning **#{n}** — {label}\nReason: {reason or '_not specified_'}\n\n_If you have questions, contact your team lead._")
        except discord.HTTPException: pass
    out_msg = "\n\n".join(confirmations)
    if len(members) > 1: out_msg = f"✅ Added warnings for **{len(members)}** players:\n\n" + out_msg
    out_msg += "\n_Bot reminders are NOT warnings — only this counts._"
    if is_public:
        mentions = " ".join(m.mention for m in members)
        await ctx.send(f"{mentions}\n{out_msg}", allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
    else: await ctx.send(out_msg, allowed_mentions=NO_PINGS)

async def warn_remove(ctx, rest):
    if not rest: return await ctx.send("Format: `!warning remove <name>`", allowed_mentions=NO_PINGS)
    member = find_member(ctx.guild, rest.strip())
    if not member: return await ctx.send(f"Couldn't find member `{rest}`.", allowed_mentions=NO_PINGS)
    if botstorage.remove_latest_warning(member.id):
        remaining = len(botstorage.get_warning_list(member.id))
        await ctx.send(f"↩️ Removed latest warning from **{member.display_name}** ({remaining} remaining).", allowed_mentions=NO_PINGS)
    else: await ctx.send(f"**{member.display_name}** has no warnings to remove.", allowed_mentions=NO_PINGS)

async def warn_reset(ctx, rest):
    if not rest: return await ctx.send("Format: `!warning reset <name>`", allowed_mentions=NO_PINGS)
    member = find_member(ctx.guild, rest.strip())
    if not member: return await ctx.send(f"Couldn't find member `{rest}`.", allowed_mentions=NO_PINGS)
    if botstorage.reset_warnings(member.id): await ctx.send(f"✅ All warnings cleared for **{member.display_name}**.", allowed_mentions=NO_PINGS)
    else: await ctx.send(f"**{member.display_name}** had no warnings.", allowed_mentions=NO_PINGS)

async def warn_list(ctx, rest):
    if not rest: return await ctx.send("Format: `!warning list <name>`", allowed_mentions=NO_PINGS)
    member = find_member(ctx.guild, rest.strip())
    if not member: return await ctx.send(f"Couldn't find member `{rest}`.", allowed_mentions=NO_PINGS)
    entries = botstorage.get_warning_list(member.id)
    if not entries: return await ctx.send(f"✅ **{member.display_name}** has no warnings recorded.", allowed_mentions=NO_PINGS)
    lines = [f"📋 **Warnings — {member.display_name}** ({len(entries)} total)"]
    for e in entries:
        try:
            d = datetime.fromisoformat(e["date"])
            when = ts(d, "d")
        except (KeyError, ValueError): when = "?"
        label = {1: "first warning", 2: "final warning"}.get(e["n"], "review")
        lines.append(f"• **#{e['n']}** ({label}) — {when} — {e.get('war','?')} — _{e.get('reason','') or 'no reason'}_")
    await ctx.send(_safe_message("\n".join(lines)), allowed_mentions=NO_PINGS)

async def warn_status(ctx, team_name):
    buckets = {1: [], 2: [], 3: [], "3+": []}
    guild = ctx.guild
    team_conf = team_conf_by_name(team_name)
    if not team_conf: return await ctx.send("Unknown team.", allowed_mentions=NO_PINGS)
    role = guild.get_role(team_conf["role_id"])
    for uid_str, data in botstorage.get_all_warnings().items():
        try: uid = int(uid_str)
        except ValueError: continue
        member = guild.get_member(uid)
        if not member or not role or role not in member.roles: continue
        n = len(data.get("entries", []))
        if n == 0: continue
        bucket = n if n <= 3 else "3+"
        buckets[bucket].append(member.display_name)
    lines = [f"📊 **Warning status — {team_name}**"]
    for k in (1, 2, 3, "3+"):
        arr = buckets[k]
        label = {1: "1 warning", 2: "2 warnings", 3: "3 warnings", "3+": "3+ warnings"}[k]
        lines.append(f"**{label}** ({len(arr)}): " + (", ".join(arr) if arr else "_none_"))
    await ctx.send(_safe_message("\n".join(lines)), allowed_mentions=NO_PINGS)

# ============ COMMANDS (MISC) ============
@bot.command(name="ping")
async def ping_cmd(ctx):
    if not guard_active(): return
    track_command("ping")
    latency_ms = round(bot.latency * 1000)
    await ctx.send(f"🏓 **Pong!** — `{latency_ms} ms` • warbot-{VERSION} by {CREATOR}😁", allowed_mentions=NO_PINGS)

@bot.command(name="roleid")
@commands.guild_only()
async def roleid_cmd(ctx, *, name: str = None):
    if not guard_active(): return
    track_command("roleid")
    if not name: return await ctx.send("Usage: `!roleid <role name or @role>`", allowed_mentions=NO_PINGS)
    if ctx.message.role_mentions: role = ctx.message.role_mentions[0]
    else:
        target = name.strip().lower()
        role = discord.utils.find(lambda r: r.name.lower() == target, ctx.guild.roles)
        if role is None: role = discord.utils.find(lambda r: target in r.name.lower(), ctx.guild.roles)
    if role is None: return await ctx.send(f"No role matching `{name}`.", allowed_mentions=NO_PINGS)
    await ctx.send(f"**{role.name}** → `{role.id}`", allowed_mentions=NO_PINGS)

@bot.command(name="id")
@commands.guild_only()
async def id_cmd(ctx, *, name: str = None):
    if not guard_active(): return
    track_command("id")
    if not name: return await ctx.send("Usage: `!id <name>` — resolves a player to their Discord ID", allowed_mentions=NO_PINGS)
    member = None
    if ctx.message.mentions: member = ctx.message.mentions[0]
    elif name.isdigit(): member = ctx.guild.get_member(int(name))
    else: member = find_member(ctx.guild, name)
    if not member:
        target = _normalize(name)
        if target in NAME_ALIASES:
            uid = NAME_ALIASES[target]
            m = ctx.guild.get_member(uid)
            if m: return await ctx.send(f"**{m.display_name}**\n• Mention: {m.mention}\n• ID: `{m.id}`\n• Username: `{m.name}`", allowed_mentions=NO_PINGS)
            return await ctx.send(f"Alias `{target}` → `{uid}` (not currently a member of this server)", allowed_mentions=NO_PINGS)
        return await ctx.send(f"Couldn't find member `{name}`.", allowed_mentions=NO_PINGS)
    await ctx.send(f"**{member.display_name}**\n• Mention: {member.mention}\n• ID: `{member.id}`\n• Username: `{member.name}`", allowed_mentions=NO_PINGS)

@bot.command(name="ids")
@commands.guild_only()
async def ids_cmd(ctx, *, team: str = None):
    if not guard_active(): return
    track_command("ids")
    if team and team.lower() in [t.lower() for t in CHANNEL_BY_TEAM.keys()]:
        teams = [next(t for t in CHANNEL_BY_TEAM.keys() if t.lower() == team.lower())]
    elif ctx.channel.id in CHANNELS: teams = [CHANNELS[ctx.channel.id]["name"]]
    else: teams = list(CHANNEL_BY_TEAM.keys())

    for team_name in teams:
        conf = team_conf_by_name(team_name)
        role = ctx.guild.get_role(conf["role_id"]) if conf else None
        if not role:
            await ctx.send(f"Member role for **{team_name}** not found.", allowed_mentions=NO_PINGS)
            continue
        members = sorted(role.members, key=lambda m: m.display_name.lower())
        if not members:
            await ctx.send(f"No members with the **{team_name}** role.", allowed_mentions=NO_PINGS)
            continue
        lines = [f"`{m.id}`  •  {m.display_name}" for m in members]
        chunks, cur = [], ""
        for line in lines:
            if len(cur) + len(line) + 1 > 1900:
                chunks.append(cur)
                cur = line
            else: cur += ("\n" + line) if cur else line
        if cur: chunks.append(cur)
        header = f"**{team_name} — {len(members)} member(s)**\n"
        for i, chunk in enumerate(chunks): await ctx.send((header if i == 0 else "") + chunk, allowed_mentions=NO_PINGS)

@bot.command(name="roles")
@commands.guild_only()
async def roles_cmd(ctx):
    if not guard_active(): return
    track_command("roles")
    roles = sorted(ctx.guild.roles, key=lambda r: r.position, reverse=True)
    roles = [r for r in roles if r.name != "@everyone"]
    lines = [f"`{r.id}`  •  {r.name}" for r in roles]
    chunks, cur = [], ""
    for line in lines:
        if len(cur) + len(line) + 1 > 1900:
            chunks.append(cur)
            cur = line
        else: cur += ("\n" + line) if cur else line
    if cur: chunks.append(cur)
    header = f"**{len(roles)} role(s) in this server**\n"
    for i, chunk in enumerate(chunks): await ctx.send((header if i == 0 else "") + chunk, allowed_mentions=NO_PINGS)

@bot.command(name="mywarnings")
async def mywarnings_cmd(ctx):
    if not guard_active(): return
    track_command("mywarnings")
    entries = botstorage.get_warning_list(ctx.author.id)
    if not entries: return await ctx.send("✅ You have no warnings recorded.", allowed_mentions=NO_PINGS)
    lines = [f"📋 **Your warnings ({len(entries)} total)**"]
    for e in entries:
        try:
            d = datetime.fromisoformat(e["date"])
            when = ts(d, "d")
        except (KeyError, ValueError): when = "?"
        label = {1: "first warning", 2: "final warning"}.get(e["n"], "review")
        lines.append(f"• **#{e['n']}** ({label}) — {when} — {e.get('war','?')} — _{e.get('reason','') or 'no reason'}_")
    lines.append("\n_If any of these look wrong, contact your team lead._")
    text = _safe_message("\n".join(lines))
    try:
        await ctx.author.send(text)
        try: await ctx.message.delete()
        except discord.HTTPException: pass
    except discord.Forbidden: await ctx.send("I couldn't DM you. Enable DMs from server members and try again.", allowed_mentions=NO_PINGS)

@bot.command(name="alias")
@commands.guild_only()
async def alias_cmd(ctx, action: str = None, aliases_str: str = None, *, user_input: str = None):
    if not guard_active() or not is_staff(ctx.author): return
    if action == "add":
        if aliases_str and aliases_str.startswith("<@") and not user_input:
            user_input = aliases_str
            aliases_str = None
        if not aliases_str and not user_input and not ctx.message.mentions: return await ctx.send("Usage: `!alias add \"<alias1>, <alias2>\" <@user>` or `!alias add <@user>`", allowed_mentions=NO_PINGS)
        member = None
        if ctx.message.mentions: member = ctx.message.mentions[0]
        elif user_input: member = find_member(ctx.guild, user_input)
        if not member: return await ctx.send("Couldn't resolve that user. Try pinging them.", allowed_mentions=NO_PINGS)
        if aliases_str: new_aliases = [a.strip().lower() for a in aliases_str.split(",") if a.strip()]
        else:
            new_aliases = [member.name.lower()]
            if member.nick: new_aliases.append(member.nick.lower())
            new_aliases = list(set(new_aliases))
        for a in new_aliases: NAME_ALIASES[a] = member.id
        save_aliases()
        _spawn(sync.push_bundle())
        alias_preview = ", ".join(f"`{a}`" for a in new_aliases)
        return await ctx.send(f"✅ Mapped {len(new_aliases)} aliases to **{member.display_name}**: {alias_preview}", allowed_mentions=NO_PINGS)
    elif action == "remove":
        if not aliases_str: return await ctx.send("Usage: `!alias remove <alias>`", allowed_mentions=NO_PINGS)
        if aliases_str.lower() in NAME_ALIASES:
            del NAME_ALIASES[aliases_str.lower()]
            save_aliases()
            _spawn(sync.push_bundle())
            return await ctx.send(f"✅ Removed `{aliases_str.lower()}`.", allowed_mentions=NO_PINGS)
        return await ctx.send("Alias not found.", allowed_mentions=NO_PINGS)
    elif action == "clean":
        dead_aliases = [k for k, v in NAME_ALIASES.items() if not ctx.guild.get_member(v)]
        for k in dead_aliases: del NAME_ALIASES[k]
        if dead_aliases:
            save_aliases()
            _spawn(sync.push_bundle())
            return await ctx.send(f"🧹 Removed **{len(dead_aliases)}** aliases for users who left the server.", allowed_mentions=NO_PINGS)
        return await ctx.send("✅ All aliases are clean. No dead users found.", allowed_mentions=NO_PINGS)
    elif action == "list":
        if not aliases_str and not ctx.message.mentions: return await ctx.send("Usage: `!alias list <@user>`", allowed_mentions=NO_PINGS)
        member = None
        if ctx.message.mentions: member = ctx.message.mentions[0]
        elif aliases_str: member = find_member(ctx.guild, aliases_str)
        if not member: return await ctx.send(f"Couldn't find user `{aliases_str}`. Try pinging them.", allowed_mentions=NO_PINGS)
        member_aliases = [k for k, v in NAME_ALIASES.items() if v == member.id]
        if not member_aliases: return await ctx.send(f"**{member.display_name}** has no aliases mapped.", allowed_mentions=NO_PINGS)
        alias_list = ", ".join(f"`{a}`" for a in sorted(member_aliases))
        return await ctx.send(f"**Aliases for {member.display_name} ({len(member_aliases)}):**\n{alias_list}", allowed_mentions=NO_PINGS)
    elif action == "check":
        if not aliases_str: return await ctx.send("Usage: `!alias check <name>`", allowed_mentions=NO_PINGS)
        if aliases_str.lower() not in NAME_ALIASES: return await ctx.send(f"❌ `{aliases_str.lower()}` is **not** in the aliases.", allowed_mentions=NO_PINGS)
        uid = NAME_ALIASES[aliases_str.lower()]
        member = ctx.guild.get_member(uid)
        if member: return await ctx.send(f"✅ `{aliases_str.lower()}` is mapped to **{member.display_name}** (`{uid}`).", allowed_mentions=NO_PINGS)
        else: return await ctx.send(f"⚠️ `{aliases_str.lower()}` is mapped to `{uid}`, but they are no longer in the server.", allowed_mentions=NO_PINGS)
    await ctx.send("**Alias Management**\n`!alias add \"<alias1>, <alias2>\" <@user>` — map multiple names at once\n`!alias add <@user>` — auto-map their Discord name\n`!alias remove <alias>` — remove a mapping\n`!alias list <@user>` — see all aliases for a player\n`!alias check <alias>` — see who a name points to\n`!alias clean` — remove mappings for users who left", allowed_mentions=NO_PINGS)

@bot.command(name="reload-aliases")
@commands.guild_only()
async def reload_aliases_cmd(ctx):
    if not guard_active(): return
    if not is_staff(ctx.author): return
    load_aliases()
    await ctx.send(f"✅ Loaded **{len(NAME_ALIASES)}** aliases.", allowed_mentions=NO_PINGS)

@bot.command(name="instance-status")
@commands.guild_only()
async def instance_status_cmd(ctx):
    if not guard_active(): return
    if not is_lead_or_colead(ctx.author): return
    if HEARTBEAT_CHANNEL_ID == 0: return await ctx.send("Heartbeat not configured.", allowed_mentions=NO_PINGS)
    chan = bot.get_channel(HEARTBEAT_CHANNEL_ID)
    if not chan: return await ctx.send("Heartbeat channel not found.", allowed_mentions=NO_PINGS)
    seen = {}
    async for m in chan.history(limit=40):
        if m.author.id != bot.user.id or not m.content.startswith("HB:"): continue
        try: payload = json.loads(m.content[3:].strip())
        except Exception: continue
        name = payload.get("name")
        if name and name not in seen: seen[name] = payload
    if not seen: return await ctx.send("No heartbeat data yet.", allowed_mentions=NO_PINGS)
    now = datetime.now(timezone.utc)
    active_name = INSTANCE_NAME if _is_active else None
    if not active_name:
        for n in seen:
            if n != INSTANCE_NAME:
                active_name = n
                break
    embed = discord.Embed(title="🛰️ Failover Status", description=f"Active instance: **{active_name or 'none'}**", color=discord.Color.green() if active_name else discord.Color.orange(), timestamp=now)
    for name, p in sorted(seen.items()):
        try:
            t = datetime.fromisoformat(p["ts"])
            age = int((now - t).total_seconds())
        except Exception: age = -1
        is_active = (name == active_name)
        is_stale = age > HEARTBEAT_TIMEOUT
        if is_stale: icon, state = "🔴", "**Offline** — heartbeat stale"
        elif is_active: icon, state = "🟢", "**Primary** — answering commands"
        else: icon, state = "⚪", "Standby — silent"
        value = f"{state}\n⏱️ Last heartbeat: **{age}s** ago\n⏳ Uptime: **{fmt_td(timedelta(seconds=p.get('uptime', 0)))}**\n⌨️ Commands: **{p.get('cmds', 0)}**"
        if p.get("last_cmd"): value += f"\n📝 Last: `{p['last_cmd']}`"
        embed.add_field(name=f"{icon} {name.upper()}", value=value, inline=False)
    embed.set_footer(text=f"This instance: {INSTANCE_NAME} • Timeout: {HEARTBEAT_TIMEOUT}s")
    try: await ctx.send(embed=embed, allowed_mentions=NO_PINGS)
    except discord.Forbidden:
        lines = [f"**🛰️ Failover Status — active: {active_name or 'none'}**", ""]
        for name, p in sorted(seen.items()):
            try:
                t = datetime.fromisoformat(p["ts"])
                age = int((now - t).total_seconds())
            except Exception: age = -1
            is_active = (name == active_name)
            is_stale = age > HEARTBEAT_TIMEOUT
            if is_stale: state = "🔴 Offline"
            elif is_active: state = "🟢 Primary"
            else: state = "⚪ Standby"
            lines.append(f"**{name.upper()}** — {state} • {age}s ago • up {fmt_td(timedelta(seconds=p.get('uptime', 0)))} • {p.get('cmds', 0)} cmds")
        await ctx.send("\n".join(lines), allowed_mentions=NO_PINGS)

# ============ HELP ============
@bot.command(name="help")
@commands.guild_only()
async def help_cmd(ctx):
    if not guard_active(): return
    track_command("help")
    is_lead = is_lead_or_colead(ctx.author)
    is_admin = ctx.author.guild_permissions.administrator
    is_staff_member = is_staff(ctx.author)

    embed = discord.Embed(
        title=f"⚔️ War Bot — {VERSION}",
        color=discord.Color.gold(),
        description="Run commands in the team's war channel, or prefix with `!war <team> ...` from anywhere."
    )
    embed.add_field(name="📊 General Info & Absences", value="`!war status` — Live war status\n`!war absent` — List excused + active windows\n`!war absent request until 16 oct` — Request absence (leads ✅❌)\n`!mywarnings` — View your warnings (DM)\n`!ping` — Latency\n`!id <name>` — Resolve player ID\n`!roleid <name>` — Resolve role ID\n`!roles` — All role IDs\n`!ids [team]` — List member IDs", inline=False)
    if not is_staff_member: return await ctx.send(embed=embed, allowed_mentions=NO_PINGS)
    embed.add_field(name="🛡️ War Tracking (Staff)", value="`!war <team> defence ends <time>` — e.g. `18h20m`\n`!war <team> attack ends <time>` — e.g. `21h50m`\n`!war <team> cancel` — Stop tracking", inline=False)
    embed.add_field(name="⚠️ Missing Attacks (Staff)", value="`!war missing <names>` — Replace list\n`!war missing add/remove/clear/log`\n`!war missing` — Show current list\n› Missing members get pinged at **12h / 6h / 3h / 1h**.", inline=False)
    embed.add_field(name="🏖️ Absences (Staff)", value="`!war absent add <name> [until X | from X to Y | for N days]`\n`!war absent remove <name>`\n`!war absent windows` — List all upcoming windows", inline=False)
    if is_lead or is_admin:
        embed.add_field(name="📌 Warning Tracker (Leads)", value="`!warning add <name1> <name2> ... [| reason] [public]`\n`!warning remove <name>` — Undo latest\n`!warning reset <name>` — Clear all\n`!warning list <name>` — History\n`!warning status` — Team summary", inline=False)
    embed.add_field(name="🔤 Player Aliases (Staff)", value="`!alias add \"<a1>, <a2>\" <@user>` — Map names\n`!alias add <@user>` — Auto-map Discord name\n`!alias remove <alias>` — Delete mapping\n`!alias list <@user>` — Show aliases for user\n`!alias check <alias>` — Check who a name points to\n`!alias clean` — Purge aliases of users who left", inline=False)
    if is_lead or is_admin: embed.add_field(name="🛰️ Diagnostics (Leads)", value="`!instance-status` — Check failover and server health", inline=False)
    embed.set_footer(text=f"Data persists in wars.json + warbot_data.json • {VERSION} by {CREATOR}")
    try:
        await ctx.author.send(embed=embed)
        await ctx.send(f"📩 {ctx.author.display_name}, I've sent the **Staff Command List** to your DMs.", allowed_mentions=NO_PINGS)
    except discord.Forbidden:
        await ctx.send(f"❌ {ctx.author.display_name}, I couldn't DM you the staff list (your DMs might be closed). Falling back to channel.", allowed_mentions=NO_PINGS)
        try: await ctx.send(embed=embed, allowed_mentions=NO_PINGS)
        except discord.Forbidden: pass

# ============ EVENTS ============
_restored = False

@bot.event
async def on_ready():
    global _restored
    print(f"Logged in as {bot.user} (ID: {bot.user.id}) — {VERSION} ({INSTANCE_NAME})")
    await asyncio.sleep(2)
    if not _restored:
        _restored = True
        if sync.SYNC_ENABLED:
            sync.init_event()
            _spawn(sync.sync_pusher())
            await sync.sync_pull_latest()
            await sync.sync_boot_code()
        botstorage.load_warbot_data()
        _spawn(heartbeat_loop())
        _spawn(sync.clear_bak())
    print("------")

async def heartbeat_loop():
    if HEARTBEAT_CHANNEL_ID == 0:
        print("[failover] HEARTBEAT_CHANNEL_ID not set — running ACTIVE")
        await _set_active(True)
        return
    await bot.wait_until_ready()
    chan = bot.get_channel(HEARTBEAT_CHANNEL_ID)
    if not chan:
        print("[failover] heartbeat channel not found — running ACTIVE")
        await _set_active(True)
        return
    _offline_since = None
    _alert_sent = False
    while True:
        try:
            now = datetime.now(timezone.utc)
            other_ts, other_active = None, False
            async for m in chan.history(limit=20):
                if m.author.id != bot.user.id or not m.content.startswith("HB:"): continue
                try: payload = json.loads(m.content[3:].strip())
                except Exception: continue
                if payload.get("name") == INSTANCE_NAME: continue
                try:
                    other_ts = datetime.fromisoformat(payload["ts"])
                    other_active = bool(payload.get("active"))
                except Exception: pass
                break
            other_fresh = (other_ts is not None and (now - other_ts).total_seconds() <= HEARTBEAT_TIMEOUT)
            other_name = "backup" if INSTANCE_NAME == "primary" else "primary"
            if other_fresh:
                if _alert_sent: await _send_failover_alert(f"✅ **RECOVERED:** The `{other_name}` instance is back online.")
                _offline_since = None
                _alert_sent = False
            else:
                if _offline_since is None: _offline_since = now
                down_dur = (now - _offline_since).total_seconds()
                if down_dur > 180 and not _alert_sent:
                    _alert_sent = True
                    await _send_failover_alert(f"🚨 **ALERT:** The `{other_name}` instance appears to be **OFFLINE**.\nNo heartbeat received for over {int(down_dur // 60)} minutes.")
            if INSTANCE_NAME == sync.CODE_SOURCE: want = _is_active or not (other_fresh and other_active)
            else: want = not other_fresh
            await _set_active(want)
            payload = {"name": INSTANCE_NAME, "ts": now.isoformat(), "active": _is_active, "uptime": int((now - _instance_started).total_seconds()), "cmds": _instance_stats["commands"], "last_cmd": _instance_stats["last_cmd"]}
            try: await chan.send(f"HB: {json.dumps(payload)}")
            except discord.HTTPException: pass
            await asyncio.sleep(HEARTBEAT_INTERVAL)
        except asyncio.CancelledError: return
        except Exception as e:
            print(f"[failover] heartbeat error: {e}")
            await asyncio.sleep(HEARTBEAT_INTERVAL)

async def activate_instance():
    print(f"[failover] {INSTANCE_NAME} becoming ACTIVE — syncing state")
    await sync.sync_pull_latest()
    botstorage.load_warbot_data()
    for st in list(botstorage.wars.values()):
        for t in st.get("ping_tasks", []): t.cancel()
        if st.get("status_task"): st["status_task"].cancel()
    botstorage.wars.clear()
    await restore_wars()
    sync.request_state_push()

async def _set_active(value):
    global _is_active
    was = _is_active
    if value and not was: await activate_instance()
    elif was and not value:
        print(f"[failover] {INSTANCE_NAME} -> standby")
        if sync.SYNC_ENABLED and sync._push_event is not None and sync._push_event.is_set():
            sync._push_event.clear()
            await sync.push_state()
        if sync.has_pending_restart():
            await bot.close()
            os._exit(0)
    _is_active = value

@bot.event
async def on_member_remove(member):
    if not guard_active(): return
    to_remove = [k for k, v in NAME_ALIASES.items() if v == member.id]
    if to_remove:
        for k in to_remove: del NAME_ALIASES[k]
        save_aliases()
        print(f"[aliases] auto-removed {len(to_remove)} aliases for departed user {member.name}")
        _spawn(sync.push_bundle())

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    if isinstance(error, discord.app_commands.errors.CommandNotFound): return
    print(f"[app_command] {error}")

async def _react_confirm_register_undo(message, team_name, member, text):
    try:
        await message.add_reaction("✅")
        await message.add_reaction("❌")
    except discord.HTTPException as e: print(f"[react] Failed to add reactions: {e}")
    try: await message.reply(text, mention_author=False, allowed_mentions=NO_PINGS)
    except discord.HTTPException: pass
    botstorage.pending_undos[message.id] = {"team_name": team_name, "member_id": member.id, "member_name": member.display_name, "when": now_utc().isoformat()}
    if len(botstorage.pending_undos) > 500:
        for k in list(botstorage.pending_undos.keys())[:-400]: del botstorage.pending_undos[k]
    botstorage.persist_pending()

@bot.event
async def on_raw_reaction_add(payload):
    if not guard_active() or payload.user_id == bot.user.id or payload.guild_id is None: return
    emoji = str(payload.emoji)
    if emoji not in ("✅", "❌"): return
    guild = bot.get_guild(payload.guild_id)
    if not guild: return
    reactor = guild.get_member(payload.user_id)
    if not reactor: return
    req = botstorage.pending_abs_requests.get(payload.message_id)
    if req:
        team_name = req["team_name"]
        if not is_team_lead(reactor, team_name): return
        del botstorage.pending_abs_requests[payload.message_id]
        botstorage.persist_pending()
        member = guild.get_member(req["user_id"])
        display_name = member.display_name if member else req["name"]
        channel = bot.get_channel(payload.channel_id)
        if emoji == "✅":
            try:
                f = datetime.fromisoformat(req["from"])
                t = datetime.fromisoformat(req["to"])
            except (KeyError, ValueError): return
            botstorage.add_absence_window(req["user_id"], display_name, f, t, "", reactor.id)
            for wstate in botstorage.wars.values():
                if wstate["team_name"] == team_name: apply_absence_windows(wstate)
            botstorage.save_wars()
            if channel:
                try: await channel.send(f"✅ Absence approved by {reactor.mention}.\n**{display_name}** — {fmt_date(f)} → {fmt_date(t)}", allowed_mentions=NO_PINGS)
                except discord.HTTPException: pass
        else:
            if channel:
                try: await channel.send(f"❌ Absence request from **{display_name}** rejected by {reactor.mention}.", allowed_mentions=NO_PINGS)
                except discord.HTTPException: pass
        return
    pending = botstorage.pending_undos.get(payload.message_id)
    if not pending or not is_staff(reactor): return
    if emoji == "❌":
        botstorage.pending_undos.pop(payload.message_id, None)
        botstorage.persist_pending()
        team_name = pending["team_name"]
        member_id = pending["member_id"]
        state = botstorage.wars.get(team_name)
        channel = bot.get_channel(payload.channel_id)
        member = guild.get_member(member_id)
        display_name = member.display_name if member else pending.get("member_name", "unknown")
        if member and state and not botstorage.is_member_missing(state, member):
            botstorage.add_to_missing(state, member, reason="undo", actor_id=reactor.id)
            botstorage.save_wars()
            if channel:
                try: await channel.send(f"↩️ **{display_name}** re-added to the missing list by {reactor.display_name} — earlier 'done' claim disputed.", allowed_mentions=NO_PINGS)
                except discord.HTTPException: pass

async def _message_hooks(message):
    if not guard_active() or message.author.bot: return
    raw = message.content
    if f"<@{bot.user.id}>" in raw or f"<@!{bot.user.id}>" in raw:
        try: await message.reply(f"warbot-{VERSION} created by {CREATOR}😁", mention_author=False, allowed_mentions=NO_PINGS)
        except discord.HTTPException: pass
    for team_name, chan_id in ABSENCE_CHANNELS.items():
        if (message.channel.id == chan_id and team_name in botstorage.wars and _counts_as_absence_post(message)):
            state = botstorage.wars[team_name]
            if state["war_start"] <= now_utc() < state["attack_end"]:
                botstorage.excuse_member(state, message.author.id)
                botstorage.save_wars()
    if AUTO_REMOVE_ON_COMPLETION and message.channel.id in CHANNELS:
        team_name = CHANNELS[message.channel.id]["name"]
        state = botstorage.wars.get(team_name)
        if state and state.get("missing"):
            if botstorage.is_member_missing(state, message.author) and message_says_done(message.content):
                if botstorage.remove_from_missing(state, message.author, reason="self"):
                    botstorage.save_wars()
                    await _react_confirm_register_undo(message, team_name, message.author, f"✅ Removed **{message.author.display_name}** from the missing list.\n_Staff: react ❌ to undo._")
            elif is_staff(message.author):
                target_name = extract_completion_name(message.content)
                if target_name:
                    member = find_member(message.guild, target_name)
                    if member and botstorage.is_member_missing(state, member):
                        if botstorage.remove_from_missing(state, member, reason="staff", actor_id=message.author.id):
                            botstorage.save_wars()
                            await _react_confirm_register_undo(message, team_name, member, f"✅ Removed **{member.display_name}** from the missing list.\n_Staff: react ❌ to undo._")

@bot.event
async def on_message(message):
    if await sync.handle_sync_message(message): return
    try: await _message_hooks(message)
    except Exception as e: print(f"[on_message] hook failed: {e}")
    await bot.process_commands(message)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, (commands.CommandNotFound, commands.NoPrivateMessage)): return
    if isinstance(error, commands.MissingRequiredArgument): return await ctx.send(f"Missing argument: `{error.param.name}`", allowed_mentions=NO_PINGS)
    if isinstance(error, (commands.UserInputError, commands.CheckFailure)): return await ctx.send("Couldn't read that command. Check for an unclosed quote (`\"`) or use `!help`.", allowed_mentions=NO_PINGS)
    print(f"[command_error] {ctx.command}: {error!r}")

bot.run(TOKEN)
