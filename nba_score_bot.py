# nba_score_bot.py
import os
import requests
import discord
from discord.ext import tasks
from dotenv import load_dotenv
from datetime import datetime, date
from typing import Optional, Tuple

load_dotenv()

TOKEN = os.getenv("TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))

if not TOKEN:
    raise ValueError("Missing TOKEN in .env")
if not CHANNEL_ID:
    raise ValueError("Missing CHANNEL_ID in .env")

ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
ESPN_SUMMARY = "https://site.web.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event="

POLL_SECONDS = 60

# (event_id, quarter) for quarter in 1,2,3,4 (we only send once per quarter per game)
alerted_quarters: Set[Tuple[str, int]] = set()
last_day = date.today()

intents = discord.Intents.default()
bot = discord.Client(intents=intents)


def fetch_scoreboard_for_date(yyyymmdd: str) -> dict:
    r = requests.get(f"{ESPN_SCOREBOARD}?dates={yyyymmdd}", timeout=10)
    r.raise_for_status()
    return r.json()


def fetch_scoreboard_today() -> dict:
    today = datetime.now().strftime("%Y%m%d")
    return fetch_scoreboard_for_date(today)


def fetch_summary(event_id: str) -> dict:
    r = requests.get(f"{ESPN_SUMMARY}{event_id}", timeout=10)
    r.raise_for_status()
    return r.json()


def is_halftime(event: dict) -> bool:
    st = event["status"]["type"]
    return st.get("name") == "STATUS_HALFTIME" or st.get("detail") == "Halftime"


def is_final(event: dict) -> bool:
    st = event["status"]["type"]
    return st.get("name") == "STATUS_FINAL" or st.get("detail") == "Final"


def ended_quarter(event: dict) -> Optional[int]:
    """
    If the game is at a quarter break (end of Q1, halftime, end of Q3, or final),
    return that quarter number 1–4. Otherwise return None.
    """
    status = event.get("status", {})
    st = status.get("type", {})
    name = st.get("name") or ""
    detail = (st.get("detail") or "").lower()
    period = status.get("period", 0)

    if name == "STATUS_FINAL" or "final" in detail:
        return 4
    if name == "STATUS_HALFTIME" or "halftime" in detail:
        return 2
    if name == "STATUS_END_PERIOD":
        # period 1 = end of 1st, period 2 = end of 2nd (halftime may be separate), period 3 = end of 3rd
        if period in (1, 2, 3):
            return period
    return None


def get_home_away_from_event(event: dict):
    competitors = event["competitions"][0]["competitors"]
    home = next(t for t in competitors if t["homeAway"] == "home")
    away = next(t for t in competitors if t["homeAway"] == "away")
    return away, home


def format_score_line_from_event(event: dict) -> str:
    away, home = get_home_away_from_event(event)
    away_team = away["team"]["displayName"]
    home_team = home["team"]["displayName"]
    away_score = away.get("score", "0")
    home_score = home.get("score", "0")
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
    """
    Returns (away_name, away_score, away_logo, home_name, home_score, home_logo)
    """
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


def top_scorer_for_team(team_block: dict) -> Tuple[str, int]:
    team_name = team_block["team"]["displayName"]
    best_name = "Unavailable"
    best_pts = -1

    for group in team_block.get("statistics", []):
        labels = group.get("labels") or []
        athletes = group.get("athletes") or []

        pts_idx = None
        for i, lab in enumerate(labels):
            if str(lab).upper() == "PTS":
                pts_idx = i
                break
        if pts_idx is None:
            continue

        for athlete in athletes:
            stats = athlete.get("stats") or []
            if pts_idx >= len(stats):
                continue
            try:
                pts = int(stats[pts_idx])
            except Exception:
                continue

            if pts > best_pts:
                best_pts = pts
                best_name = athlete.get("athlete", {}).get("displayName", "Unknown")

    if best_pts < 0:
        return f"{team_name}: (unavailable)", 0

    return f"{team_name}: {best_name} ({best_pts} pts)", best_pts


def get_leading_scorers(event_id: str) -> Tuple[str, str]:
    try:
        summary = fetch_summary(event_id)
        info = get_team_info_from_summary(summary)
        if not info:
            return "Away: (unavailable)", "Home: (unavailable)"

        away_name, _, _, home_name, _, _ = info

        players_blocks = summary.get("boxscore", {}).get("players", [])
        if not players_blocks or len(players_blocks) < 2:
            return f"{away_name}: (unavailable)", f"{home_name}: (unavailable)"

        leaders_by_team = {}
        for team_block in players_blocks:
            leader_str, _ = top_scorer_for_team(team_block)
            leaders_by_team[team_block["team"]["displayName"]] = leader_str

        away_leader = leaders_by_team.get(away_name, f"{away_name}: (unavailable)")
        home_leader = leaders_by_team.get(home_name, f"{home_name}: (unavailable)")

        return away_leader, home_leader

    except Exception as e:
        print("Leading scorers error:", e)
        return "Away: (unavailable)", "Home: (unavailable)"


QUARTER_TITLES = {
    1: "🏀 End of 1st Quarter",
    2: "🏀 HALFTIME",
    3: "🏀 End of 3rd Quarter",
    4: "🏁 FINAL",
}


def embed_quarter_end(
    quarter: int,
    score_line: str,
    away_leader: str,
    home_leader: str,
    thumbnail_url: Optional[str] = None,
) -> discord.Embed:
    """Build embed for end of quarter 1–4 with score and leading scorers."""
    title = QUARTER_TITLES.get(quarter, f"End of Q{quarter}")
    e = discord.Embed(title=title, description=score_line)
    if thumbnail_url:
        e.set_thumbnail(url=thumbnail_url)
    e.add_field(name="🔥 Leading scorers", value=f"{away_leader}\n{home_leader}", inline=False)
    return e


def thumbnail_for_quarter(event: dict, summary: Optional[dict], quarter: int) -> Optional[str]:
    """Logo for embed: only show winner logo at end of game (Q4)."""
    if quarter == 4 and summary:
        return winner_logo_from_summary(summary)
    return None


@tasks.loop(seconds=POLL_SECONDS)
async def poll_games():
    global last_day

    if date.today() != last_day:
        alerted_quarters.clear()
        last_day = date.today()

    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        return

    try:
        data = fetch_scoreboard_today()
    except Exception as e:
        print("Scoreboard fetch error:", e)
        return

    for event in data.get("events", []):
        raw_id = event.get("id")
        if raw_id is None:
            continue
        event_id = str(raw_id)  # normalize so int/str from API don't create duplicate keys

        quarter = ended_quarter(event)
        if quarter is None or (event_id, quarter) in alerted_quarters:
            continue

        try:
            summary = fetch_summary(event_id)
        except Exception:
            continue

        # Final check right before send in case another run sent in between
        if (event_id, quarter) in alerted_quarters:
            continue
        alerted_quarters.add((event_id, quarter))

        away_leader, home_leader = get_leading_scorers(event_id)
        score_line = (
            get_score_line_from_summary(summary)
            if quarter == 4
            else format_score_line_from_event(event)
        )
        thumb = thumbnail_for_quarter(event, summary, quarter)
        await channel.send(
            embed=embed_quarter_end(quarter, score_line, away_leader, home_leader, thumb)
        )


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

    if not poll_games.is_running():
        poll_games.start()


bot.run(TOKEN)