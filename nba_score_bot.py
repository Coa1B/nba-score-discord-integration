# nba_score_bot.py
import argparse
import asyncio
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

import requests
import discord
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))

if not TOKEN:
    raise ValueError("Missing TOKEN in .env")
if not CHANNEL_ID:
    raise ValueError("Missing CHANNEL_ID in .env")

ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
ESPN_SUMMARY = "https://site.web.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event="
ET = ZoneInfo("America/New_York")
POLL_SECONDS = 15 * 60
EXPIRED_CLOCKS = {"0.0", "0:00", "00:00", "0.00"}
STATE_PATH = Path(__file__).resolve().parent / "bot_state.json"
BACKFILL_DATE = "20261022"
LOCK_KEYS = ("1", "2", "3", "4", "final")
# Only post halftime and final alerts.
ALERT_KEYS = {"2", "final"}
CLOSED_STATUS_NAMES = {
    "STATUS_FINAL",
    "STATUS_POSTPONED",
    "STATUS_CANCELED",
    "STATUS_CANCELLED",
    "STATUS_FORFEIT",
}
AWARDS = (
    ("pts", "🔥 Most Points"),
    ("reb", "🧹 Most Rebounds"),
    ("ast", "🎯 Most Assists"),
    ("blk", "🚫 Most Blocks"),
    ("stl", "🥷 Most Steals"),
)

intents = discord.Intents.default()
bot = discord.Client(intents=intents)
startup_sync_done = False


@dataclass
class GameSnapshot:
    period: int
    state: str
    is_final: bool
    is_break: bool
    is_halftime: bool
    status_name: str = ""


@dataclass
class GameRecord:
    event_id: str
    slate_date: str
    posted: Set[str] = field(default_factory=set)
    snapshot: Optional[GameSnapshot] = None


@dataclass
class DateFlags:
    schedule_posted: bool = False
    summary_posted: bool = False
    backfilled: bool = False


games: Dict[str, GameRecord] = {}
dates: Dict[str, DateFlags] = {}


def date_flags(yyyymmdd: str) -> DateFlags:
    flags = dates.get(yyyymmdd)
    if flags is None:
        flags = DateFlags()
        dates[yyyymmdd] = flags
    return flags


def game_record(event_id: str, slate_date: str) -> GameRecord:
    rec = games.get(event_id)
    if rec is None:
        rec = GameRecord(event_id=event_id, slate_date=slate_date)
        games[event_id] = rec
    elif slate_date and rec.slate_date != slate_date:
        rec.slate_date = slate_date
    return rec


def slate_label(yyyymmdd: str) -> str:
    d = datetime.strptime(yyyymmdd, "%Y%m%d")
    return d.strftime("%A, %B ") + str(d.day)


def watch_dates() -> List[str]:
    now = datetime.now(ET)
    return [
        (now - timedelta(days=1)).strftime("%Y%m%d"),
        now.strftime("%Y%m%d"),
        (now + timedelta(days=1)).strftime("%Y%m%d"),
    ]


def load_state() -> None:
    games.clear()
    dates.clear()
    if not STATE_PATH.exists():
        return
    try:
        data = json.loads(STATE_PATH.read_text())
    except Exception as e:
        print("State load error:", e, flush=True)
        return

    if "games" in data:
        _load_v2(data)
    else:
        _load_v1(data)


def _snapshot_from_dict(raw: Optional[dict]) -> Optional[GameSnapshot]:
    if not raw:
        return None
    return GameSnapshot(
        period=int(raw.get("period") or 0),
        state=str(raw.get("state") or ""),
        is_final=bool(raw.get("is_final")),
        is_break=bool(raw.get("is_break")),
        is_halftime=bool(raw.get("is_halftime")),
        status_name=str(raw.get("status_name") or ""),
    )


def _load_v2(data: dict) -> None:
    for event_id, raw in (data.get("games") or {}).items():
        rec = GameRecord(
            event_id=str(event_id),
            slate_date=str(raw.get("slate_date") or ""),
            posted=set(str(k) for k in (raw.get("posted") or [])),
            snapshot=_snapshot_from_dict(raw.get("snapshot")),
        )
        games[rec.event_id] = rec
    for yyyymmdd, raw in (data.get("dates") or {}).items():
        dates[str(yyyymmdd)] = DateFlags(
            schedule_posted=bool(raw.get("schedule_posted")),
            summary_posted=bool(raw.get("summary_posted")),
            backfilled=bool(raw.get("backfilled")),
        )


def _load_v1(data: dict) -> None:
    posted_by_game: Dict[str, Set[str]] = {}
    for event_id, alert_key in data.get("posted") or []:
        posted_by_game.setdefault(str(event_id), set()).add(str(alert_key))

    last_seen = data.get("last_seen") or {}
    for event_id, snap in last_seen.items():
        eid = str(event_id)
        rec = GameRecord(
            event_id=eid,
            slate_date="",
            posted=posted_by_game.get(eid, set()),
            snapshot=_snapshot_from_dict(snap),
        )
        games[eid] = rec

    for event_id, keys in posted_by_game.items():
        rec = game_record(event_id, "")
        rec.posted.update(keys)

    for yyyymmdd in data.get("backfilled_dates") or []:
        date_flags(str(yyyymmdd)).backfilled = True
    for yyyymmdd in data.get("schedule_posted") or []:
        date_flags(str(yyyymmdd)).schedule_posted = True
    for yyyymmdd in data.get("summary_posted") or []:
        date_flags(str(yyyymmdd)).summary_posted = True


def save_state() -> None:
    payload = {
        "games": {
            eid: {
                "slate_date": rec.slate_date,
                "posted": sorted(rec.posted),
                "snapshot": asdict(rec.snapshot) if rec.snapshot else None,
            }
            for eid, rec in games.items()
        },
        "dates": {
            day: {
                "schedule_posted": flags.schedule_posted,
                "summary_posted": flags.summary_posted,
                "backfilled": flags.backfilled,
            }
            for day, flags in dates.items()
        },
    }
    STATE_PATH.write_text(json.dumps(payload, indent=2))


def lock_game(event_id: str) -> None:
    rec = games.get(event_id)
    if rec is None:
        rec = game_record(event_id, "")
    rec.posted.update(LOCK_KEYS)


def fetch_scoreboard_for_date(yyyymmdd: str) -> dict:
    r = requests.get(f"{ESPN_SCOREBOARD}?dates={yyyymmdd}", timeout=10)
    r.raise_for_status()
    return r.json()


def events_for_date(yyyymmdd: str) -> List[dict]:
    try:
        data = fetch_scoreboard_for_date(yyyymmdd)
    except Exception as e:
        print(f"Scoreboard fetch error ({yyyymmdd}):", e, flush=True)
        return []
    events = []
    for event in data.get("events", []):
        if event.get("id") is not None:
            events.append(event)
    return events


def fetch_summary(event_id: str) -> dict:
    r = requests.get(f"{ESPN_SUMMARY}{event_id}", timeout=10)
    r.raise_for_status()
    return r.json()


def snapshot_event(event: dict) -> GameSnapshot:
    status = event.get("status") or {}
    st = status.get("type") or {}
    name = (st.get("name") or "").upper()
    detail = (st.get("detail") or "").lower()
    description = (st.get("description") or "").lower()
    state = (st.get("state") or "").lower()
    period = int(status.get("period") or 0)
    clock = str(status.get("displayClock") or "").strip()

    is_final = (
        name == "STATUS_FINAL"
        or state == "post"
        or bool(st.get("completed"))
        or "final" in detail
    )
    is_halftime = (
        name == "STATUS_HALFTIME"
        or "halftime" in detail
        or "halftime" in description
    )
    is_end_period = name == "STATUS_END_PERIOD" or "end of" in detail or "end of" in description
    starting_period = "start of" in detail or "start of" in description
    clock_expired = (
        clock in EXPIRED_CLOCKS
        and state == "in"
        and period >= 1
        and not is_final
        and not starting_period
    )
    is_break = (not is_final) and (is_halftime or is_end_period or clock_expired)
    return GameSnapshot(
        period=period,
        state=state,
        is_final=is_final,
        is_break=is_break,
        is_halftime=is_halftime,
        status_name=name,
    )


def event_is_closed(event: dict) -> bool:
    snap = snapshot_event(event)
    return snap.is_final or snap.status_name in CLOSED_STATUS_NAMES


def _quarter_key(period: int, is_halftime: bool = False) -> Optional[str]:
    if period <= 0:
        return None
    if period == 2 or is_halftime:
        return "2"
    if period >= 5:
        return f"ot{period}"
    return str(period)


def alerts_for_transition(prev: Optional[GameSnapshot], curr: GameSnapshot) -> List[str]:
    """
    Return alert keys for quarters this game just finished since the last poll.
    First time we see a game, return nothing so already-finished games are ignored.
    """
    if prev is None or curr.state == "pre":
        return []

    alerts: List[str] = []

    if curr.is_final and not prev.is_final:
        alerts.append("final")

    if curr.period > prev.period and prev.period >= 1:
        for q in range(prev.period, curr.period):
            key = _quarter_key(q)
            if key and key not in alerts:
                alerts.append(key)

    if curr.is_break and not prev.is_break and not curr.is_final:
        key = _quarter_key(curr.period, is_halftime=curr.is_halftime)
        if key and key not in alerts:
            alerts.append(key)

    return alerts


def get_home_away_from_event(event: dict):
    competitors = event["competitions"][0]["competitors"]
    home = next(t for t in competitors if t["homeAway"] == "home")
    away = next(t for t in competitors if t["homeAway"] == "away")
    return away, home


def _score_through_period(competitor: dict, period: int) -> Optional[int]:
    """Score at the end of `period`, from ESPN linescores. None if not available yet."""
    lines = competitor.get("linescores") or []
    if len(lines) < period:
        return None
    try:
        return sum(int(float(line.get("value") or 0)) for line in lines[:period])
    except Exception:
        return None


def _alert_period(alert_key: str) -> Optional[int]:
    if alert_key.startswith("ot"):
        alert_key = alert_key[2:]
    return int(alert_key) if alert_key.isdigit() else None


def format_score_line_from_event(event: dict, alert_key: Optional[str] = None) -> str:
    away, home = get_home_away_from_event(event)
    away_team = away["team"]["displayName"]
    home_team = home["team"]["displayName"]
    away_score = away.get("score", "0")
    home_score = home.get("score", "0")
    # If we noticed the break late, show the score as of the end of that quarter.
    period = _alert_period(alert_key) if alert_key else None
    if period:
        away_q = _score_through_period(away, period)
        home_q = _score_through_period(home, period)
        if away_q is not None and home_q is not None:
            away_score, home_score = away_q, home_q
    return f"{away_team} {away_score} – {home_team} {home_score}"


def get_score_line_from_summary(summary: dict) -> str:
    header = summary.get("header", {})
    comps = header.get("competitions", [])
    if not comps:
        return "Final score unavailable"
    comp = comps[0]
    competitors = comp.get("competitors", [])

    home = next(c for c in competitors if c.get("homeAway") == "home")
    away = next(c for c in competitors if c.get("homeAway") == "away")

    home_team = home["team"]["displayName"]
    away_team = away["team"]["displayName"]
    home_score = home.get("score", "0")
    away_score = away.get("score", "0")

    return f"{away_team} {away_score} – {home_team} {home_score}"


def get_team_info_from_summary(summary: dict):
    header = summary.get("header", {})
    comps = header.get("competitions", [])
    if not comps:
        return None

    comp = comps[0]
    competitors = comp.get("competitors", [])

    home = next((c for c in competitors if c.get("homeAway") == "home"), None)
    away = next((c for c in competitors if c.get("homeAway") == "away"), None)
    if not home or not away:
        return None

    def logo(team_obj: dict) -> Optional[str]:
        logos = team_obj.get("team", {}).get("logos", [])
        if logos and isinstance(logos, list):
            return logos[0].get("href")
        return None

    away_name = away["team"]["displayName"]
    home_name = home["team"]["displayName"]
    away_score = int(away.get("score", 0))
    home_score = int(home.get("score", 0))
    away_logo = logo(away)
    home_logo = logo(home)

    return away_name, away_score, away_logo, home_name, home_score, home_logo


def winner_logo_from_summary(summary: dict) -> Optional[str]:
    info = get_team_info_from_summary(summary)
    if not info:
        return None

    away_name, away_score, away_logo, home_name, home_score, home_logo = info

    if away_score > home_score:
        return away_logo or home_logo
    if home_score > away_score:
        return home_logo or away_logo
    return home_logo or away_logo


def _stat_map(stats: List[dict]) -> Dict[str, str]:
    mapped = {}
    for item in stats:
        value = item.get("displayValue")
        if value is None:
            continue
        for key in (item.get("abbreviation"), item.get("label"), item.get("name")):
            if key:
                mapped[str(key)] = str(value)
    return mapped


def format_team_box(summary: dict) -> str:
    info = get_team_info_from_summary(summary)
    teams = (summary.get("boxscore") or {}).get("teams") or []
    if not info or len(teams) < 2:
        return "Team stats unavailable"

    away_name, away_score, _, home_name, home_score, _ = info
    by_name = {t.get("team", {}).get("displayName"): t for t in teams}

    def line(team_name: str, score: int) -> str:
        block = by_name.get(team_name) or {}
        stats = _stat_map(block.get("statistics") or [])
        fg = stats.get("fieldGoalsMade-fieldGoalsAttempted") or stats.get("FG") or "—"
        fg_pct = stats.get("FG%") or stats.get("fieldGoalPct") or "—"
        three = stats.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted") or stats.get("3PT") or "—"
        three_pct = stats.get("3P%") or stats.get("threePointFieldGoalPct") or "—"
        ft = stats.get("freeThrowsMade-freeThrowsAttempted") or stats.get("FT") or "—"
        ft_pct = stats.get("FT%") or stats.get("freeThrowPct") or "—"
        reb = stats.get("REB") or stats.get("totalRebounds") or "—"
        ast = stats.get("AST") or stats.get("assists") or "—"
        to = stats.get("TO") or stats.get("turnovers") or "—"
        stl = stats.get("STL") or stats.get("steals") or "—"
        blk = stats.get("BLK") or stats.get("blocks") or "—"
        return (
            f"**{team_name} {score}**\n"
            f"FG {fg} ({fg_pct}%) · 3PT {three} ({three_pct}%) · FT {ft} ({ft_pct}%)\n"
            f"REB {reb} · AST {ast} · TO {to} · STL {stl} · BLK {blk}"
        )

    return f"{line(away_name, away_score)}\n\n{line(home_name, home_score)}"


def _stat_int(stats: List[str], idx: Optional[int]) -> int:
    if idx is None or idx >= len(stats):
        return 0
    raw = str(stats[idx]).split("-")[0]
    try:
        return int(raw)
    except Exception:
        return 0


def player_rows_from_summary(summary: dict) -> List[dict]:
    rows = []
    for team_block in (summary.get("boxscore") or {}).get("players") or []:
        team = team_block.get("team") or {}
        team_abbr = team.get("abbreviation") or team.get("displayName") or "?"
        for group in team_block.get("statistics") or []:
            labels = [str(lab).upper() for lab in (group.get("labels") or [])]
            idx = {lab: i for i, lab in enumerate(labels)}
            for athlete_row in group.get("athletes") or []:
                ath = athlete_row.get("athlete") or {}
                stats = athlete_row.get("stats") or []
                name = ath.get("displayName") or "Unknown"
                rows.append(
                    {
                        "id": str(ath.get("id") or name),
                        "name": name,
                        "team": team_abbr,
                        "pts": _stat_int(stats, idx.get("PTS")),
                        "reb": _stat_int(stats, idx.get("REB")),
                        "ast": _stat_int(stats, idx.get("AST")),
                        "stl": _stat_int(stats, idx.get("STL")),
                        "blk": _stat_int(stats, idx.get("BLK")),
                    }
                )
    return rows


def top_stat_for_team(team_block: dict, stat: str) -> str:
    team_name = team_block["team"]["displayName"]
    best_name = "Unavailable"
    best_val = -1

    for group in team_block.get("statistics", []):
        labels = group.get("labels") or []
        athletes = group.get("athletes") or []
        try:
            idx = [str(lab).upper() for lab in labels].index(stat.upper())
        except ValueError:
            continue

        for athlete in athletes:
            stats = athlete.get("stats") or []
            val = _stat_int(stats, idx)
            if val > best_val:
                best_val = val
                best_name = athlete.get("athlete", {}).get("displayName", "Unknown")

    if best_val < 0:
        return f"{team_name}: (unavailable)"
    return f"{team_name}: {best_name} ({best_val} {stat})"


def get_boxscore_leaders(summary: dict) -> Dict[str, Tuple[str, str]]:
    info = get_team_info_from_summary(summary)
    empty = ("Away: (unavailable)", "Home: (unavailable)")
    if not info:
        return {"PTS": empty, "REB": empty, "AST": empty}

    away_name, _, _, home_name, _, _ = info
    players_blocks = (summary.get("boxscore") or {}).get("players") or []
    result = {}
    for stat in ("PTS", "REB", "AST"):
        by_team = {}
        for team_block in players_blocks:
            by_team[team_block["team"]["displayName"]] = top_stat_for_team(team_block, stat)
        result[stat] = (
            by_team.get(away_name, f"{away_name}: (unavailable)"),
            by_team.get(home_name, f"{home_name}: (unavailable)"),
        )
    return result


def get_leading_scorers(event_id: str, summary: Optional[dict] = None) -> Tuple[str, str]:
    try:
        if summary is None:
            summary = fetch_summary(event_id)
        return get_boxscore_leaders(summary)["PTS"]
    except Exception as e:
        print("Leading scorers error:", e, flush=True)
        return "Away: (unavailable)", "Home: (unavailable)"


def award_line(rows: List[dict], key: str, title: str) -> str:
    if not rows:
        return f"{title} — n/a"
    best = max(int(row[key]) for row in rows)
    if best <= 0:
        return f"{title} — n/a"
    winners = [row for row in rows if int(row[key]) == best]
    names = ", ".join(f"{w['name']} ({w['team']})" for w in winners)
    return f"{title} — {names} ({best})"


def embed_nightly_summary(yyyymmdd: str, events: List[dict], rows: List[dict]) -> discord.Embed:
    score_lines = []
    for event in events:
        snap = snapshot_event(event)
        if snap.status_name in CLOSED_STATUS_NAMES and not snap.is_final:
            score_lines.append(f"{event.get('shortName') or event.get('name')} — postponed")
            continue
        score_lines.append(format_score_line_from_event(event))

    awards = "\n".join(award_line(rows, key, title) for key, title in AWARDS)
    e = discord.Embed(
        title=f"🌙 NBA Nightly Recap — {slate_label(yyyymmdd)}",
        description="\n".join(score_lines)[:4096] or "No final scores.",
        color=0xF4C430,
    )
    e.add_field(name="🏆 Nightly awards", value=awards[:1024], inline=False)
    return e


QUARTER_TITLES = {
    "1": "🏀 End of 1st Quarter",
    "2": "🏀 HALFTIME",
    "3": "🏀 End of 3rd Quarter",
    "4": "🏀 End of 4th Quarter",
    "final": "🏁 FINAL",
}


def title_for_alert(alert_key: str) -> str:
    if alert_key in QUARTER_TITLES:
        return QUARTER_TITLES[alert_key]
    if alert_key.startswith("ot"):
        return "🏀 End of OT"
    return f"End of {alert_key}"


def embed_quarter_end(
    alert_key: str,
    score_line: str,
    away_leader: str,
    home_leader: str,
    thumbnail_url: Optional[str] = None,
    summary: Optional[dict] = None,
) -> discord.Embed:
    e = discord.Embed(title=title_for_alert(alert_key), description=score_line)
    if thumbnail_url:
        e.set_thumbnail(url=thumbnail_url)
    if alert_key == "final" and summary:
        e.add_field(name="📊 Team stats", value=format_team_box(summary)[:1024], inline=False)
        leaders = get_boxscore_leaders(summary)
        e.add_field(
            name="🔥 Points",
            value=f"{leaders['PTS'][0]}\n{leaders['PTS'][1]}",
            inline=False,
        )
        e.add_field(
            name="🧹 Rebounds",
            value=f"{leaders['REB'][0]}\n{leaders['REB'][1]}",
            inline=True,
        )
        e.add_field(
            name="🎯 Assists",
            value=f"{leaders['AST'][0]}\n{leaders['AST'][1]}",
            inline=True,
        )
    else:
        e.add_field(name="🔥 Leading scorers", value=f"{away_leader}\n{home_leader}", inline=False)
    return e


def thumbnail_for_alert(summary: Optional[dict], alert_key: str) -> Optional[str]:
    if alert_key == "final" and summary:
        return winner_logo_from_summary(summary)
    return None


def schedule_time(event: dict) -> str:
    st = ((event.get("status") or {}).get("type") or {})
    return st.get("shortDetail") or st.get("detail") or "TBD"


def embed_schedule(date_label: str, events: List[dict]) -> discord.Embed:
    lines = []
    for event in events:
        try:
            away, home = get_home_away_from_event(event)
            matchup = f"{away['team']['displayName']} @ {home['team']['displayName']}"
        except Exception:
            matchup = event.get("name") or event.get("shortName") or "NBA game"
        lines.append(f"• {matchup} — {schedule_time(event)}")
    description = "\n".join(lines) if lines else "No games listed."
    return discord.Embed(
        title=f"📅 NBA schedule — {date_label}",
        description=description[:4096],
    )


async def send_alert(channel, event: dict, event_id: str, alert_key: str, summary: Optional[dict] = None) -> None:
    if summary is None:
        summary = fetch_summary(event_id)
    away_leader, home_leader = get_leading_scorers(event_id, summary)
    score_line = (
        get_score_line_from_summary(summary)
        if alert_key == "final"
        else format_score_line_from_event(event, alert_key)
    )
    thumb = thumbnail_for_alert(summary, alert_key)
    await channel.send(
        embed=embed_quarter_end(
            alert_key, score_line, away_leader, home_leader, thumb, summary=summary
        )
    )


async def get_target_channel():
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        channel = await bot.fetch_channel(CHANNEL_ID)
    return channel


def seed_slate(yyyymmdd: str, events: List[dict], overwrite: bool = False) -> None:
    """Attach games to a slate date. overwrite=True syncs live ESPN state without replaying alerts."""
    for event in events:
        rec = game_record(str(event["id"]), yyyymmdd)
        if rec.snapshot is None or overwrite:
            rec.snapshot = snapshot_event(event)


def earliest_unfinished_slate(slates: Dict[str, List[dict]]) -> Optional[str]:
    """The next NBA night still in progress or not started. Never skip ahead a day."""
    for yyyymmdd in sorted(slates):
        events = slates.get(yyyymmdd) or []
        if events and not all(event_is_closed(event) for event in events):
            return yyyymmdd
    return None


async def maybe_post_next_schedule(channel, slates: Dict[str, List[dict]]) -> None:
    """Post the schedule only for the current NBA night, after the previous day's results."""
    yyyymmdd = earliest_unfinished_slate(slates)
    if not yyyymmdd:
        return
    events = slates[yyyymmdd]
    flags = date_flags(yyyymmdd)
    if flags.schedule_posted:
        return
    await channel.send(embed=embed_schedule(slate_label(yyyymmdd), events))
    flags.schedule_posted = True
    print(f"Posted schedule for {yyyymmdd} ({len(events)} games)", flush=True)


async def maybe_send_nightly_summary(channel, yyyymmdd: str, events: List[dict]) -> None:
    flags = date_flags(yyyymmdd)
    if flags.summary_posted or not events:
        return
    if not all(event_is_closed(event) for event in events):
        return

    finals = [event for event in events if snapshot_event(event).is_final]
    if not finals:
        return

    rows: List[dict] = []
    for event in finals:
        try:
            summary = fetch_summary(str(event["id"]))
        except Exception as e:
            print(f"Summary fetch error ({event.get('id')}):", e, flush=True)
            return
        rows.extend(player_rows_from_summary(summary))

    try:
        await channel.send(embed=embed_nightly_summary(yyyymmdd, events, rows))
    except Exception as e:
        print(f"Nightly summary send error ({yyyymmdd}):", e, flush=True)
        return
    flags.summary_posted = True
    print(f"Posted nightly recap for {yyyymmdd}", flush=True)


async def backfill_date(channel, yyyymmdd: str, events: List[dict]) -> None:
    flags = date_flags(yyyymmdd)
    if flags.backfilled:
        return
    for event in events:
        event_id = str(event["id"])
        rec = game_record(event_id, yyyymmdd)
        rec.snapshot = snapshot_event(event)
        if not rec.snapshot.is_final:
            lock_game(event_id)
            continue
        if "final" in rec.posted:
            lock_game(event_id)
            continue
        try:
            await send_alert(channel, event, event_id, "final")
            lock_game(event_id)
            print(f"Backfilled {yyyymmdd} final: {event.get('shortName')}", flush=True)
        except Exception as e:
            print(f"Backfill send error ({event_id}):", e, flush=True)
            return
    flags.backfilled = True


async def process_slate(channel, yyyymmdd: str, events: List[dict], send_quarters: bool) -> None:
    for event in events:
        event_id = str(event["id"])
        rec = game_record(event_id, yyyymmdd)
        curr = snapshot_event(event)

        if not send_quarters:
            if rec.snapshot is None:
                rec.snapshot = curr
            continue

        if "final" in rec.posted and curr.is_final:
            rec.snapshot = curr
            continue

        prev = rec.snapshot
        rec.snapshot = curr

        for alert_key in alerts_for_transition(prev, curr):
            if alert_key not in ALERT_KEYS or alert_key in rec.posted:
                continue
            try:
                summary = fetch_summary(event_id)
            except Exception:
                continue
            rec.posted.add(alert_key)
            try:
                await send_alert(channel, event, event_id, alert_key, summary)
            except Exception as e:
                print("Send error:", e, flush=True)
                rec.posted.discard(alert_key)
                continue
            if alert_key == "final":
                lock_game(event_id)


async def startup_sync(channel) -> None:
    today = datetime.now(ET).strftime("%Y%m%d")
    slates = {day: events_for_date(day) for day in watch_dates()}

    if BACKFILL_DATE in slates:
        await backfill_date(channel, BACKFILL_DATE, slates[BACKFILL_DATE])

    for yyyymmdd, events in slates.items():
        seed_slate(yyyymmdd, events, overwrite=True)
        if yyyymmdd == BACKFILL_DATE:
            for event in events:
                lock_game(str(event["id"]))
        # Only recap dates that are today or earlier so we don't wait on tomorrow's slate.
        if yyyymmdd <= today:
            await maybe_send_nightly_summary(channel, yyyymmdd, events)
    await maybe_post_next_schedule(channel, slates)

    save_state()


async def run_poll(channel) -> None:
    today = datetime.now(ET).strftime("%Y%m%d")
    slates = {day: events_for_date(day) for day in watch_dates()}
    for yyyymmdd, events in slates.items():
        send_quarters = yyyymmdd <= today
        await process_slate(channel, yyyymmdd, events, send_quarters=send_quarters)
        if send_quarters:
            await maybe_send_nightly_summary(channel, yyyymmdd, events)
    await maybe_post_next_schedule(channel, slates)
    save_state()


@tasks.loop(seconds=POLL_SECONDS)
async def poll_games():
    channel = await get_target_channel()
    if not channel:
        return
    await run_poll(channel)


@bot.event
async def on_ready():
    global startup_sync_done
    print(f"Logged in as {bot.user}", flush=True)
    load_state()

    if not startup_sync_done:
        channel = await get_target_channel()
        if channel:
            await startup_sync(channel)
        startup_sync_done = True

    if not poll_games.is_running():
        poll_games.start()


async def run_once() -> None:
    load_state()
    await bot.login(TOKEN)
    try:
        channel = await bot.fetch_channel(CHANNEL_ID)
        print(f"One-shot poll as {bot.user}", flush=True)
        await run_poll(channel)
    finally:
        await bot.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--once",
        action="store_true",
        help="Check ESPN once, post any new alerts, then exit",
    )
    args = parser.parse_args()
    if args.once:
        asyncio.run(run_once())
        return
    load_state()
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
