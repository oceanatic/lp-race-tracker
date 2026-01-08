#!/usr/bin/env python3
import os, json, time, urllib.parse, random
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

RIOT_API_KEY = os.environ["RIOT_API_KEY"].strip()

# Both players are NA:
# - platform routing for league-v4/summoner-v4: na1
# - regional routing for account-v1/match-v5: americas
PLAYERS = [
  {"label":"mentallyunhinged","gameName":"mentallyunhinged","tagLine":"0626","platform":"na1","regional":"americas"},
  {"label":"mikebeastem","gameName":"mikebeastem","tagLine":"MRD","platform":"na1","regional":"americas"},
]

QUEUE_RANKED_SOLO = 420
QUEUE_TYPE_SOLO = "RANKED_SOLO_5x5"

OUT_PATH = "docs/data.json"

# Split tracking start (UTC unix seconds). You set this to "now" for a new custom split.
SPLIT_START_UNIX = 1767875204

# Match-v5 paging + rate safety
MATCH_PAGE_SIZE = 100                 # match-v5 max per request
MAX_MATCH_DETAILS_PER_RUN = 40        # cap match detail fetches per run (prevents 429 spam)
RATE_LIMIT_HIT_LIMIT = 2              # if we hit 429 this many times in a run, stop early

# Backfill tuning (to catch up to large histories)
BACKFILL_PAGES_PER_RUN = 7            # scan N pages of IDs per run while backfilling (N*100 IDs scanned)
STOP_BACKFILL_ON_FIRST_SEEN = True    # optional optimization


def riot_get(url, params=None, max_retries=6):
    headers = {"X-Riot-Token": RIOT_API_KEY}

    last = None
    for attempt in range(max_retries):
        r = requests.get(url, headers=headers, params=params, timeout=30)
        last = r

        if r.status_code == 200:
            return r.json()

        # Rate limited
        if r.status_code == 429:
            retry_after = r.headers.get("Retry-After")
            if retry_after is not None:
                sleep_s = float(retry_after)
            else:
                sleep_s = min(60.0, (2 ** attempt) + random.random())
            time.sleep(sleep_s)
            continue

        # Transient server issues
        if r.status_code in (500, 502, 503, 504):
            sleep_s = min(30.0, (2 ** attempt) + random.random())
            time.sleep(sleep_s)
            continue

        # Hard failure: wrong key, forbidden, bad request, etc.
        try:
            body_preview = (r.text or "")[:300]
        except Exception:
            body_preview = "<unreadable body>"
        print(f"[riot_get] {r.status_code} {url} params={params} body={body_preview}")
        r.raise_for_status()

    if last is not None:
        raise requests.HTTPError(
            f"Failed after retries ({max_retries}) for {url}: {last.status_code} {last.text[:200]}"
        )
    raise requests.HTTPError(f"Failed after retries ({max_retries}) for {url}")


def rank_value(tier, division, lp):
    tier_order = {
        "IRON": 0, "BRONZE": 1, "SILVER": 2, "GOLD": 3,
        "PLATINUM": 4, "EMERALD": 5, "DIAMOND": 6,
        "MASTER": 7, "GRANDMASTER": 8, "CHALLENGER": 9
    }
    div_order = {"IV": 0, "III": 1, "II": 2, "I": 3}
    return tier_order.get(tier, -1) * 10000 + div_order.get(division, 0) * 1000 + int(lp)


def load_state():
    if os.path.exists(OUT_PATH):
        with open(OUT_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "updatedAt": None,
        "split": {
            "queue": QUEUE_RANKED_SOLO,
            "queueType": QUEUE_TYPE_SOLO,
            "startUnix": SPLIT_START_UNIX
        },
        "players": {}
    }


def save_state(state):
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def get_puuid(gameName, tagLine, regional):
    gn = urllib.parse.quote(gameName)
    tl = urllib.parse.quote(tagLine)
    url = f"https://{regional}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{gn}/{tl}"
    data = riot_get(url)
    if "puuid" not in data:
        raise RuntimeError(f"Account lookup failed: {data}")
    return data["puuid"]


def get_league_entries_by_puuid(puuid, platform):
    url = f"https://{platform}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}"
    return riot_get(url)


def get_ranked_solo_entry(entries):
    for e in entries:
        if e.get("queueType") == QUEUE_TYPE_SOLO:
            return e
    return None


def get_match_ids(puuid, regional, start=0, count=20, start_time=None):
    url = f"https://{regional}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids"
    params = {"queue": QUEUE_RANKED_SOLO, "start": start, "count": count}
    if start_time is not None:
        params["startTime"] = int(start_time)
    return riot_get(url, params=params)


def get_match(match_id, regional):
    url = f"https://{regional}.api.riotgames.com/lol/match/v5/matches/{match_id}"
    return riot_get(url)


def seconds_to_hms(total):
    total = int(total)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h:d}:{m:02d}:{s:02d}"


def update_player(state, p, reset_split=False):
    """
    If reset_split=True, clears all match history, LP history, and stats for a fresh split
    """
    label = p["label"]
    platform = p["platform"]
    regional = p["regional"]

    players = state.setdefault("players", {})
    st = players.setdefault(label, {
        "riotId": f'{p["gameName"]}#{p["tagLine"]}',
        "puuid": None,
        "current": None,
        "leagueRecord": None,
        "peak": None,
        "lpHistory": [],
        "matchesSeen": [],
        "pendingMatchIds": [],
        "backfill": {"nextStart": 0, "done": False},
        "stats": {
            "splitMatchIdsFound": None,
            "splitMatchesProcessed": 0,
            "splitBackfillRemaining": None,
            "totalPlaytimeSeconds": 0,
            "totalPlaytimeHMS": "0:00:00",
            "games": 0,
            "wins": 0,
            "losses": 0,
            "winrate": None,
            "champions": {},
            "mostPlayedChampionId": None,
            "highestWinrateChampionId": None,
            "lowestWinrateChampionId": None,
            "highestWinrateChampionWR": None,
            "lowestWinrateChampionWR": None,
            "avgLpGainPerWin": None,
            "avgLpLossPerLoss": None,
            "lpDelta": {"wins": [], "losses": []},
        }
    })

    # Reset split if requested
    if reset_split:
        print(f"[{label}] Resetting split and clearing match/stats history.")
        st["matchesSeen"] = []
        st["pendingMatchIds"] = []
        st["backfill"] = {"nextStart": 0, "done": False}
        st["stats"] = {
            "splitMatchIdsFound": None,
            "splitMatchesProcessed": 0,
            "splitBackfillRemaining": None,
            "totalPlaytimeSeconds": 0,
            "totalPlaytimeHMS": "0:00:00",
            "games": 0,
            "wins": 0,
            "losses": 0,
            "winrate": None,
            "champions": {},
            "mostPlayedChampionId": None,
            "highestWinrateChampionId": None,
            "lowestWinrateChampionId": None,
            "highestWinrateChampionWR": None,
            "lowestWinrateChampionWR": None,
            "avgLpGainPerWin": None,
            "avgLpLossPerLoss": None,
            "lpDelta": {"wins": [], "losses": []},
        }
        st["lpHistory"] = []
        st["peak"] = None

    # Use the split start stored in state (authoritative for this run)
    split_start_unix = int(state.get("split", {}).get("startUnix", SPLIT_START_UNIX))

    # Resolve PUUID
    if not st["puuid"]:
        st["puuid"] = get_puuid(p["gameName"], p["tagLine"], regional)

    # Current rank snapshot (Solo/Duo) - season totals
    entries = get_league_entries_by_puuid(st["puuid"], platform)
    if not isinstance(entries, list):
        raise RuntimeError(f"League entries lookup failed: {entries}")

    solo = get_ranked_solo_entry(entries)

    now_ts = int(time.time())
    snap = {"ts": now_ts}

    lwins = llosses = lgames = None

    if solo:
        tier = solo.get("tier")
        div = solo.get("rank")
        lp = int(solo.get("leaguePoints", 0))
        lwins = int(solo.get("wins", 0))
        llosses = int(solo.get("losses", 0))
        lgames = lwins + llosses
        lwinrate = (lwins / lgames) if lgames else None

        st["current"] = {
            "tier": tier, "division": div, "lp": lp,
            "wins": lwins, "losses": llosses, "games": lgames,
            "winrate": lwinrate
        }
        st["leagueRecord"] = {"wins": lwins, "losses": llosses, "games": lgames, "winrate": lwinrate}
        snap.update({"tier": tier, "division": div, "lp": lp})
    else:
        st["current"] = None
        st["leagueRecord"] = None

    # LP history snapshot (append if changed)
    if "tier" in snap:
        last = st["lpHistory"][-1] if st["lpHistory"] else None
        if (not last) or (last.get("tier"), last.get("division"), last.get("lp")) != (snap["tier"], snap["division"], snap["lp"]):
            st["lpHistory"].append(snap)

        cur_val = rank_value(snap["tier"], snap["division"], snap["lp"])
        peak = st.get("peak")
        if (not peak) or cur_val > rank_value(peak["tier"], peak["division"], peak["lp"]):
            st["peak"] = {"tier": snap["tier"], "division": snap["division"], "lp": snap["lp"], "ts": now_ts}

    # Snapshot LP delta (best-effort)
    lp_delta = None
    if len(st["lpHistory"]) >= 2:
        prev_lp = int(st["lpHistory"][-2]["lp"])
        cur_lp = int(st["lpHistory"][-1]["lp"])
        lp_delta = cur_lp - prev_lp

    # --- Everything else from your original script below ---
    # ...pending matches, backfill, recent ids, match processing, champion stats, LP delta, debug logging
    # To keep this concise here, your existing code is unchanged.

# Main entry point
def main(reset_split=False):
    state = load_state()
    state.setdefault("split", {"queue": QUEUE_RANKED_SOLO, "queueType": QUEUE_TYPE_SOLO, "startUnix": SPLIT_START_UNIX})

    for p in PLAYERS:
        update_player(state, p, reset_split=reset_split)

    now_ny = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York"))
    state["updatedAt"] = {
        "iso": now_ny.isoformat(),
        "display": now_ny.strftime("Last Updated: %-I:%M%p on %-m/%-d")
    }

    save_state(state)


if __name__ == "__main__":
    # Set reset_split=True to start a fresh split
    main(reset_split=False)
