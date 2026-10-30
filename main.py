# main.py — v30 (Public Release)
import os
import json

def _load_env():
    # Standard fallback to .env if present
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

VERSION = "v30"
CREATOR = "YourName"

# ============ LOAD CONFIG ============
def load_config():
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise SystemExit("ERROR: config.json not found. Rename config.example.json to config.json and fill it out.")

CONFIG = load_config()
TOKEN = CONFIG.get("token")
if not TOKEN or TOKEN == "YOUR_BOT_TOKEN_HERE":
    raise SystemExit("ERROR: Set your DISCORD_TOKEN in config.json")

STAFF_ROLE_IDS = set(CONFIG.get("staff_role_ids", []))
LEAD_ROLE_IDS = set(CONFIG.get("lead_role_ids", []))
HEARTBEAT_CHANNEL_ID = CONFIG.get("heartbeat_channel_id", 0)
ALERT_PING_ID = CONFIG.get("alert_ping_id", 0)
os.environ["SYNC_CHANNEL_ID"] = str(CONFIG.get("sync_channel_id", 0))
os.environ["INSTANCE_NAME"] = CONFIG.get("instance_name", "local")

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

CHANNEL_BY_TEAM = {name: cid for cid, conf in CHANNELS.items() for name in [conf["name"]]}

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
bot = commands.Bot(command_prefix="!", intents=intents)
bot.remove_command("help")

sync.register(bot, os.environ.get("INSTANCE_NAME", "local"), lambda: _is_active)
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
    if member.guild_permissions.administrator:
        return True
    return bool({r.id for r in member.roles} & STAFF_ROLE_IDS)

def is_lead_or_colead(member):
    if member.guild_permissions.administrator:
        return True
    return bool({r.id for r in member.roles} & LEAD_ROLE_IDS)

def is_team_lead(member, team_name):
    if member.guild_permissions.administrator:
        return True
    ids = set(TEAM_LEAD_ROLE_IDS.get(team_name, []))
    return bool({r.id for r in member.roles} & ids)

def team_conf_by_name(name):
    for conf in CHANNELS.values():
        if conf["name"] == name:
            return conf
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
    try:
        await chan.send(f"{ping}{text}", allowed_mentions=discord.AllowedMentions(users=True))
    except discord.HTTPException: pass

def build_ping_message(event_type, state):
    role_m = state["role"].mention if state["role"] else ""
    anchor = state["war_start"]

    if event_type == "defence_start":
        return f"🛡️ {role_m} **DEFENCE PHASE HAS STARTED!** Phase ends {ts(anchor + PHASE)}."
    if event_type == "defence_30m":
        return f"⚠️ {role_m} **Defence phase ends in 30 minutes!** Get defences up."
    if event_type == "attack_start":
        return f"⚔️ {role_m} **ATTACK PHASE HAS STARTED!** War ends {ts(anchor + WAR)}."
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
    try:
        await channel.send(_safe_message(text), allowed_mentions=discord.AllowedMentions(everyone=False, roles=True, users=True))
    except discord.HTTPException as e:
        print(f"[ping] send failed: {e}")

def schedule_pings(team_name):
    state = botstorage.wars[team_name]
    anchor = state["war_start"]
    now = now_utc()
    for event_type, offset in PING_EVENTS:
        delay = (anchor + offset - now).total_seconds()
        if delay > 0:
            state["ping_tasks"].append(_spawn(_sleep_then_send(delay, team_name, event_type)))

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
                if botstorage.wars.get(team_name) is ended:
                    cancel_war(team_name)
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
            try:
                await message.edit(embed=build_status_embed(state))
            except discord.NotFound: return
            except discord.HTTPException as e: print(f"[status] edit failed: {e}")
            if now_utc() >= state["attack_end"]: return
    except asyncio.CancelledError: pass

def cancel_war(team_name):
    state = botstorage.wars.pop(team_name, None)
    if not state: return
    for t in state.get("ping_tasks", []):
        t.cancel()
    if state.get("status_task"):
        state["status_task"].cancel()
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

# ... (Continue with handle_missing, handle_absent, warn commands, etc. Same logic, just dynamic team names) ...
