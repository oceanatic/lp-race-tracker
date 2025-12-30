#!/usr/bin/env python3
import os, json, time, urllib.parse
import requests
from datetime import datetime, timezone

RIOT_API_KEY = os.environ["RIOT_API_KEY"]

PLAYERS = [
    {"label": "mentallyunhinged", "gameName": "mentallyunhinged", "tagLine": "0626"},
    {"label": "mikebeastem", "gameName": "mikebeastem", "tagLine": "MRD"},
]

# Routing:
# - account-v1 and match-v5: regional routing (AMERICAS for NA)
# - summoner-v4 and league-v4: platform routing (NA1 for NA)
REGIONAL = "americas"
PLATFORM = "na1"

QUEUE_RANKED_SOLO = 420
QUEUE_TYPE_SOLO = "RANKED_SOLO_5x5"

OUT_PATH = "docs/data.json"

# Current ranked split start (NA): 2025-08-27 19:00:00 UTC (noon PT)
SPLIT_START_UNIX = 1756321200

# Match backfill controls (split-wide stats)
MATCH_PAGE_SIZE = 100           # match-v5 max per request
MAX_SPLIT_MATCHES = 4000        # safety cap
MAX_MATCH_DETAILS_PER_RUN = 75  # avoid rate limits while catching up

def riot_get(url, params=None):
    headers = {"X-Riot-Token": RIOT_API_KEY}
    r = requests.get(url, headers=headers, params=params, timeout=30)
    r.raise_for_status()
    return r.json()

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
    return {"updatedAt": None, "players": {}}

def save_state(state):
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

def get_puuid(gameName, tagLine):
    gn = urllib.parse.quote(gameName)
    tl = urllib.parse.quote(tagLine)
    url = f"https://{REGIONAL}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{gn}/{tl}"
    data = riot_get(url)
    if "puuid" not in data:
        raise RuntimeError(f"Account lookup failed: {data}")
    return data["puuid"]

def get_summoner_by_puuid(puuid):
    # Not strictly needed for rank anymore, but kept in case you want display name later.
    url = f"https://{PLATFORM}.api.riotgames.com/lol/summoner/v4/summoners/by-puuid/{puuid}"
    return riot_get(url)

def get_league_entries_by_puuid(puuid):
    url = f"https://{PLATFORM}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}"
    return riot_get(url)

def get_ranked_solo_entry(entries):
    for e in entries:
        if e.get("queueType") == QUEUE_TYPE_SOLO:
            return e
    return None

def get_match_ids(puuid, start=0, count=20, start_time=None):
    url = f"https://{REGIONAL}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids"
    params = {"queue": QUEUE_RANKED_SOLO, "start": start, "count": count}
    if start_time is not None:
        params["startTime"] = int(start_time)
    return riot_get(url, params=params)

def get_match(match_id):
    url = f"https://{REGIONAL}.api.riotgames.com/lol/match/v5/matches/{match_id}"
    return riot_get(url)

def seconds_to_hms(total):
    total = int(total)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h:d}:{m:02d}:{s:02d}"

def update_player(state, p):
    label = p["label"]
    players = state.setdefault("players", {})
    st = players.setdefault(label, {
        "riotId": f'{p["gameName"]}#{p["tagLine"]}',
        "puuid": None,
        "current": None,
        "peak": None,
        "lpHistory": [],
        "matchesSeen": [],
        "stats": {
            "totalPlaytimeSeconds": 0,
            "totalPlaytimeHMS": "0:00:00",
            "games": 0, "wins": 0, "losses": 0,
            "champions": {},
            "mostPlayedChampionId": None,
            "highestWinrateChampionId": None,
            "lowestWinrateChampionId": None,
            "avgLpGainPerWin": None,
            "avgLpLossPerLoss": None,
            "lpDelta": {"wins": [], "losses": []},
        }
    })

    # Resolve PUUID (stable identifier)
    if not st["puuid"]:
        st["puuid"] = get_puuid(p["gameName"], p["tagLine"])

    # Current rank snapshot (Solo/Duo) via League-V4 by PUUID (no summonerId needed)
    entries = get_league_entries_by_puuid(st["puuid"])
    if not isinstance(entries, list):
        raise RuntimeError(f"League entries lookup failed: {entries}")

    solo = get_ranked_solo_entry(entries)

    now_ts = int(time.time())
    snap = {"ts": now_ts}

    if solo:
        tier = solo.get("tier")
        div = solo.get("rank")
        lp = int(solo.get("leaguePoints", 0))
        wins = int(solo.get("wins", 0))
        losses = int(solo.get("losses", 0))
        games = wins + losses
        winrate = (wins / games) if games else None

        st["current"] = {
            "tier": tier, "division": div, "lp": lp,
            "wins": wins, "losses": losses, "games": games,
            "winrate": winrate
        }
        snap.update({"tier": tier, "division": div, "lp": lp})
    else:
        st["current"] = None

    # LP history snapshot (append if changed)
    if "tier" in snap:
        last = st["lpHistory"][-1] if st["lpHistory"] else None
        if (not last) or (last.get("tier"), last.get("division"), last.get("lp")) != (snap["tier"], snap["division"], snap["lp"]):
            st["lpHistory"].append(snap)

        # Peak rank/LP since tracking began
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

    # Pull ALL solo/duo match IDs since split start (paginated), then process new ones only
    all_ids = []
    start = 0
    while len(all_ids) < MAX_SPLIT_MATCHES:
        batch = get_match_ids(
            st["puuid"],
            start=start,
            count=MATCH_PAGE_SIZE,
            start_time=SPLIT_START_UNIX
        )
        if not isinstance(batch, list) or not batch:
            break
        all_ids.extend(batch)
        start += len(batch)
        if len(batch) < MATCH_PAGE_SIZE:
            break

    seen = set(st["matchesSeen"])
    new_ids = [mid for mid in all_ids if mid not in seen]
    new_ids.reverse()  # oldest -> newest

    # Avoid rate limits while catching up
    if len(new_ids) > MAX_MATCH_DETAILS_PER_RUN:
        new_ids = new_ids[:MAX_MATCH_DETAILS_PER_RUN]

    stats = st["stats"]

    # Process matches
    new_match_results = []  # store (matchId, win) for LP delta attribution
    for mid in new_ids:
        m = get_match(mid)
        info = m.get("info", {})
        participants = info.get("participants", [])
        me = next((x for x in participants if x.get("puuid") == st["puuid"]), None)
        if not me:
            continue

        win = bool(me.get("win"))
        champ_id = str(me.get("championId"))
        duration = int(info.get("gameDuration", 0))

        stats["games"] += 1
        if win:
            stats["wins"] += 1
        else:
            stats["losses"] += 1

        stats["totalPlaytimeSeconds"] += max(duration, 0)

        c = stats["champions"].setdefault(
            champ_id,
            {"games": 0, "wins": 0, "losses": 0, "playtimeSeconds": 0}
        )
        c["games"] += 1
        if win:
            c["wins"] += 1
        else:
            c["losses"] += 1
        c["playtimeSeconds"] += max(duration, 0)

        st["matchesSeen"].append(mid)
        new_match_results.append((mid, win))

    # Derived champ stats
    champs = stats["champions"]
    champ_rows = []
    for cid, d in champs.items():
        g = d["games"]
        w = d["wins"]
        wr = (w / g) if g else None
        champ_rows.append((cid, g, w, wr))
    champ_rows.sort(key=lambda x: x[1], reverse=True)

    stats["mostPlayedChampionId"] = champ_rows[0][0] if champ_rows else None

    MIN_GAMES_FOR_BEST_WORST = 5
    eligible = [r for r in champ_rows if r[1] >= MIN_GAMES_FOR_BEST_WORST and r[3] is not None]
    if len(eligible) >= 2:
        best = max(eligible, key=lambda x: x[3])
        worst = min(eligible, key=lambda x: x[3])
        stats["highestWinrateChampionId"] = best[0]
        stats["lowestWinrateChampionId"] = worst[0]
    else:
        # Not enough champions with meaningful sample size yet
        stats["highestWinrateChampionId"] = None
        stats["lowestWinrateChampionId"] = None

    # Playtime formatted
    stats["totalPlaytimeHMS"] = seconds_to_hms(stats["totalPlaytimeSeconds"])

    # LP gain/loss best-effort attribution:
    # Only attribute if exactly ONE new match was processed this run AND we have an LP delta snapshot.
    if lp_delta is not None and len(new_match_results) == 1:
        _, win = new_match_results[0]
        if win:
            stats["lpDelta"]["wins"].append(lp_delta)
        else:
            stats["lpDelta"]["losses"].append(abs(lp_delta))

    wins = stats["lpDelta"]["wins"]
    losses = stats["lpDelta"]["losses"]
    stats["avgLpGainPerWin"] = (sum(wins) / len(wins)) if wins else None
    stats["avgLpLossPerLoss"] = (sum(losses) / len(losses)) if losses else None

def main():
    state = load_state()
    for p in PLAYERS:
        update_player(state, p)
    state["updatedAt"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

if __name__ == "__main__":
    main()
