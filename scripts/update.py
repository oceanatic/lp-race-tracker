#!/usr/bin/env python3
import os, json, time, urllib.parse, random
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

RIOT_API_KEY = os.environ["RIOT_API_KEY"].strip()  # NEW: strip accidental whitespace/newlines

PLAYERS = [
    {"label": "mentallyunhinged", "gameName": "mentallyunhinged", "tagLine": "0626"},
    {"label": "mikebeastem", "gameName": "mikebeastem", "tagLine": "MRD"},
]

# Routing:
# - account-v1 and match-v5: regional routing (AMERICAS for NA/BR/LAN/LAS)
# - summoner-v4 and league-v4: PLATFORM routing (na1/br1/la1/la2/oc1, etc.)
REGIONAL = "americas"

QUEUE_RANKED_SOLO = 420
QUEUE_TYPE_SOLO = "RANKED_SOLO_5x5"

OUT_PATH = "docs/data.json"

# Split tracking start: January 9, 2025 00:00 UTC
SPLIT_START_UNIX = 1736380800

# Match-v5 paging + rate safety
MATCH_PAGE_SIZE = 100
MAX_MATCH_DETAILS_PER_RUN = 40
RATE_LIMIT_HIT_LIMIT = 2

# Backfill tuning
BACKFILL_PAGES_PER_RUN = 7
STOP_BACKFILL_ON_FIRST_SEEN = True


def riot_get(url, params=None, max_retries=6):
    headers = {"X-Riot-Token": RIOT_API_KEY}

    last = None
    for attempt in range(max_retries):
        r = requests.get(url, headers=headers, params=params, timeout=30)
        last = r

        if r.status_code == 200:
            return r.json()

        if r.status_code == 429:
            retry_after = r.headers.get("Retry-After")
            if retry_after is not None:
                sleep_s = float(retry_after)
            else:
                sleep_s = min(60.0, (2 ** attempt) + random.random())
            time.sleep(sleep_s)
            continue

        if r.status_code in (500, 502, 503, 504):
            sleep_s = min(30.0, (2 ** attempt) + random.random())
            time.sleep(sleep_s)
            continue

        # NEW: surface Riot's JSON error body (super helpful for 400s)
        body_preview = r.text[:500] if r.text else ""
        raise requests.HTTPError(f"{r.status_code} for {url} params={params} body={body_preview}", response=r)

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


def get_puuid(gameName, tagLine):
    gn = urllib.parse.quote(gameName)
    tl = urllib.parse.quote(tagLine)
    url = f"https://{REGIONAL}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{gn}/{tl}"
    data = riot_get(url)
    if "puuid" not in data:
        raise RuntimeError(f"Account lookup failed: {data}")
    return str(data["puuid"]).strip()


def get_match_ids(puuid, start=0, count=20, start_time=None):
    puuid = str(puuid).strip()
    url = f"https://{REGIONAL}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids"
    params = {"queue": QUEUE_RANKED_SOLO, "start": start, "count": count}
    if start_time is not None:
        params["startTime"] = int(start_time)
    return riot_get(url, params=params)


def get_match(match_id):
    url = f"https://{REGIONAL}.api.riotgames.com/lol/match/v5/matches/{match_id}"
    return riot_get(url)


# NEW: infer PLATFORM (na1/la1/la2/br1/oc1...) from match ID prefix like "NA1_123..."
def infer_platform_from_match_id(match_id: str) -> str | None:
    if not match_id or "_" not in match_id:
        return None
    prefix = match_id.split("_", 1)[0].strip()
    if not prefix:
        return None
    return prefix.lower()


# NEW: summoner-v4 by-puuid (PLATFORM routing) to obtain encryptedSummonerId (field "id")
# Docs explicitly recommend RiotID -> PUUID -> Summoner-V4 by-puuid to get summonerID. :contentReference[oaicite:1]{index=1}
def get_summoner_by_puuid(platform, puuid):
    puuid = str(puuid).strip()
    platform = str(platform).strip().lower()
    url = f"https://{platform}.api.riotgames.com/lol/summoner/v4/summoners/by-puuid/{puuid}"
    return riot_get(url)


def get_league_entries_by_summoner(platform, encrypted_summoner_id):
    platform = str(platform).strip().lower()
    sid = str(encrypted_summoner_id).strip()
    url = f"https://{platform}.api.riotgames.com/lol/league/v4/entries/by-summoner/{sid}"
    return riot_get(url)


def get_ranked_solo_entry(entries):
    for e in entries:
        if e.get("queueType") == QUEUE_TYPE_SOLO:
            return e
    return None


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

        # NEW: platform + encryptedSummonerId cache (needed for platform-routed APIs)
        "platform": None,
        "summonerId": None,

        # League (authoritative ladder state)
        "current": None,
        "leagueRecord": None,

        # Peak/History (rank/LP snapshots)
        "peak": None,
        "lpHistory": [],

        # Match-derived split stats (incremental)
        "matchesSeen": [],

        # Persistent queue
        "pendingMatchIds": [],

        # Backfill cursor/state
        "backfill": {"nextStart": 0, "done": False},

        "stats": {
            "splitMatchIdsFound": None,
            "splitMatchesProcessed": 0,
            "splitBackfillRemaining": None,

            "totalPlaytimeSeconds": 0,
            "totalPlaytimeHMS": "0:00:00",
            "games": 0, "wins": 0, "losses": 0,

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

    st.setdefault("backfill", {"nextStart": 0, "done": False})
    st.setdefault("pendingMatchIds", [])
    st.setdefault("platform", None)
    st.setdefault("summonerId", None)

    # Resolve PUUID (regional)
    if not st["puuid"]:
        st["puuid"] = get_puuid(p["gameName"], p["tagLine"])

    # NEW: infer PLATFORM from match ID prefix (works even if they are not NA1)
    # Try with split start first; if that yields nothing, try without time filter just to infer platform.
    recent_for_platform = []
    try:
        recent_for_platform = get_match_ids(st["puuid"], start=0, count=1, start_time=SPLIT_START_UNIX)
    except Exception:
        recent_for_platform = []

    if not recent_for_platform:
        try:
            recent_for_platform = get_match_ids(st["puuid"], start=0, count=1, start_time=None)
        except Exception:
            recent_for_platform = []

    inferred = infer_platform_from_match_id(recent_for_platform[0]) if recent_for_platform else None
    if inferred:
        st["platform"] = inferred

    if not st["platform"]:
        raise RuntimeError(f"[{label}] Could not infer platform from match IDs (no matches returned).")

    # NEW: get encryptedSummonerId via summoner-v4 by-puuid on the correct platform
    if not st["summonerId"]:
        summ = get_summoner_by_puuid(st["platform"], st["puuid"])
        sid = summ.get("id")
        if not sid:
            raise RuntimeError(f"[{label}] summoner-v4 did not return 'id': {summ}")
        st["summonerId"] = sid

    # Current rank snapshot (Solo/Duo) using league-v4 platform routing
    entries = get_league_entries_by_summoner(st["platform"], st["summonerId"])
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

    stats = st["stats"]
    seen = set(st["matchesSeen"])
    backfill = st["backfill"]

    pending = st["pendingMatchIds"]
    pending_set = set(pending)

    recent_ids = None
    pages_scanned = 0

    # 1) Backfill scan -> enqueue
    if not backfill.get("done", False):
        while pages_scanned < BACKFILL_PAGES_PER_RUN:
            start = int(backfill.get("nextStart", 0))
            ids = get_match_ids(st["puuid"], start=start, count=MATCH_PAGE_SIZE, start_time=SPLIT_START_UNIX)
            if not isinstance(ids, list):
                raise RuntimeError(f"Match ID lookup failed: {ids}")

            pages_scanned += 1

            if not ids:
                backfill["done"] = True
                break

            intersects_seen = any(mid in seen for mid in ids)

            for mid in ids:
                if mid not in seen and mid not in pending_set:
                    pending.append(mid)
                    pending_set.add(mid)

            backfill["nextStart"] = start + MATCH_PAGE_SIZE

            if STOP_BACKFILL_ON_FIRST_SEEN and intersects_seen:
                break

    # 2) Realtime enqueue newest page
    recent_ids = get_match_ids(st["puuid"], start=0, count=MATCH_PAGE_SIZE, start_time=SPLIT_START_UNIX)
    if not isinstance(recent_ids, list):
        raise RuntimeError(f"Match ID lookup failed: {recent_ids}")

    for mid in recent_ids:
        if mid in seen:
            break
        if mid not in pending_set:
            pending.append(mid)
            pending_set.add(mid)

    # 3) Process pending oldest->newest, capped
    to_process = []
    while pending and len(to_process) < MAX_MATCH_DETAILS_PER_RUN:
        to_process.append(pending.pop())
        pending_set.discard(to_process[-1])

    rate_limit_hits = 0
    new_match_results = []

    for mid in to_process:
        try:
            m = get_match(mid)
        except requests.HTTPError as e:
            msg = str(e)
            if "429" in msg:
                rate_limit_hits += 1
                if rate_limit_hits >= RATE_LIMIT_HIT_LIMIT:
                    print("Too many 429s; stopping early this run to stay stable.")
                    for back_mid in reversed(to_process[to_process.index(mid):]):
                        if back_mid not in seen and back_mid not in pending_set:
                            pending.append(back_mid)
                            pending_set.add(back_mid)
                    break
            print(f"Skipping match due to HTTPError: {mid} -> {e}")
            if mid not in seen and mid not in pending_set:
                pending.append(mid)
                pending_set.add(mid)
            continue

        info = m.get("info", {})
        participants = info.get("participants", [])
        me = next((x for x in participants if x.get("puuid") == st["puuid"]), None)
        if not me:
            if mid not in seen and mid not in pending_set:
                pending.append(mid)
                pending_set.add(mid)
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
        seen.add(mid)
        new_match_results.append((mid, win))

    stats["splitMatchesProcessed"] = len(st["matchesSeen"])
    stats["totalPlaytimeHMS"] = seconds_to_hms(stats["totalPlaytimeSeconds"])

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
        best = max(eligible, key=lambda r: (r[3], r[1]))
        worst = min(eligible, key=lambda r: (r[3], -r[1]))
        stats["highestWinrateChampionId"] = best[0]
        stats["lowestWinrateChampionId"] = worst[0]
        stats["highestWinrateChampionWR"] = best[3]
        stats["lowestWinrateChampionWR"] = worst[3]
    elif len(eligible) == 1:
        only = eligible[0]
        stats["highestWinrateChampionId"] = only[0]
        stats["lowestWinrateChampionId"] = only[0]
        stats["highestWinrateChampionWR"] = only[3]
        stats["lowestWinrateChampionWR"] = only[3]
    else:
        stats["highestWinrateChampionId"] = None
        stats["lowestWinrateChampionId"] = None
        stats["highestWinrateChampionWR"] = None
        stats["lowestWinrateChampionWR"] = None

    if lp_delta is not None and len(new_match_results) == 1:
        _, w = new_match_results[0]
        if w:
            stats["lpDelta"]["wins"].append(lp_delta)
        else:
            stats["lpDelta"]["losses"].append(abs(lp_delta))

    wins = stats["lpDelta"]["wins"]
    losses = stats["lpDelta"]["losses"]
    stats["avgLpGainPerWin"] = (sum(wins) / len(wins)) if wins else None
    stats["avgLpLossPerLoss"] = (sum(losses) / len(losses)) if losses else None

    newest = recent_ids[0] if recent_ids else None
    newest_is_seen = (recent_ids and (recent_ids[0] in set(st["matchesSeen"])))

    print(f"[{label}] platform={st['platform']} summonerId_cached={'yes' if st.get('summonerId') else 'no'}")
    print(f"[{label}] SOLO ladder games now={lgames if solo else 'n/a'} W={lwins if solo else 'n/a'} L={llosses if solo else 'n/a'}")
    print(f"[{label}] seen_total={len(st['matchesSeen'])} new_details_processed_this_run={len(new_match_results)}")
    print(f"[{label}] pending={len(st['pendingMatchIds'])} backfill_done={backfill.get('done')} nextStart={backfill.get('nextStart')} pages_scanned={pages_scanned}")
    print(f"[{label}] fetched_ids={len(recent_ids) if recent_ids is not None else 'n/a'} newest={newest} newest_is_seen={newest_is_seen}")


def main():
    state = load_state()
    state.setdefault("split", {"queue": QUEUE_RANKED_SOLO, "queueType": QUEUE_TYPE_SOLO, "startUnix": SPLIT_START_UNIX})

    for p in PLAYERS:
        update_player(state, p)

    now_ny = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York"))
    state["updatedAt"] = {
        "iso": now_ny.isoformat(),
        "display": now_ny.strftime("Last Updated: %-I:%M%p on %-m/%-d")
    }

    save_state(state)


if __name__ == "__main__":
    main()
