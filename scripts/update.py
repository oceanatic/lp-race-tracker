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

# Split tracking start: January 9, 2025 00:00 UTC
SPLIT_START_UNIX = 1736380800

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
        # Helpful diagnostics in CI logs:
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


def update_player(state, p):
    label = p["label"]
    platform = p["platform"]
    regional = p["regional"]

    players = state.setdefault("players", {})
    st = players.setdefault(label, {
        "riotId": f'{p["gameName"]}#{p["tagLine"]}',
        "puuid": None,

        # League (authoritative ladder state)
        "current": None,
        "leagueRecord": None,

        # Peak/History (rank/LP snapshots)
        "peak": None,
        "lpHistory": [],

        # Match-derived split stats (incremental)
        "matchesSeen": [],

        # Persistent queue of match IDs we discovered but haven't processed yet
        "pendingMatchIds": [],

        # Backfill cursor/state
        "backfill": {
            "nextStart": 0,
            "done": False
        },

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

    # Ensure older saved states have new fields
    st.setdefault("backfill", {"nextStart": 0, "done": False})
    st.setdefault("pendingMatchIds", [])

    # Resolve PUUID
    if not st["puuid"]:
        st["puuid"] = get_puuid(p["gameName"], p["tagLine"], regional)

    # Current rank snapshot (Solo/Duo)
    entries = get_league_entries_by_puuid(st["puuid"], platform)
    if not isinstance(entries, list):
        raise RuntimeError(f"League entries lookup failed: {entries}")

    solo = get_ranked_solo_entry(entries)

    now_ts = int(time.time())
    snap = {"ts": now_ts}

    # Keep these defined for logging even if unranked
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

    stats = st["stats"]
    seen = set(st["matchesSeen"])
    backfill = st["backfill"]

    # Pending queue helpers
    pending = st["pendingMatchIds"]
    pending_set = set(pending)

    # -----------------------------
    # Backfill + realtime pipeline
    # -----------------------------
    recent_ids = None  # for logging only

    # 1) Backfill: scan pages and enqueue unseen IDs (do NOT rely on immediate processing)
    pages_scanned = 0
    if not backfill.get("done", False):
        while pages_scanned < BACKFILL_PAGES_PER_RUN:
            start = int(backfill.get("nextStart", 0))
            ids = get_match_ids(st["puuid"], regional, start=start, count=MATCH_PAGE_SIZE, start_time=SPLIT_START_UNIX)
            if not isinstance(ids, list):
                raise RuntimeError(f"Match ID lookup failed: {ids}")

            pages_scanned += 1

            if not ids:
                backfill["done"] = True
                break

            # Optional optimization: if we hit anything we've already processed, we're overlapping
            intersects_seen = any(mid in seen for mid in ids)

            # Enqueue any not already seen/pending
            for mid in ids:
                if mid not in seen and mid not in pending_set:
                    pending.append(mid)
                    pending_set.add(mid)

            # Advance cursor
            backfill["nextStart"] = start + MATCH_PAGE_SIZE

            # If optimization enabled, stop scanning when overlap is detected to save requests.
            if STOP_BACKFILL_ON_FIRST_SEEN and intersects_seen:
                break

    # 2) Realtime: even if backfill is done, always check newest page and enqueue new matches
    recent_ids = get_match_ids(st["puuid"], regional, start=0, count=MATCH_PAGE_SIZE, start_time=SPLIT_START_UNIX)
    if not isinstance(recent_ids, list):
        raise RuntimeError(f"Match ID lookup failed: {recent_ids}")

    for mid in recent_ids:
        if mid in seen:
            break
        if mid not in pending_set:
            pending.append(mid)
            pending_set.add(mid)

    # 3) Process from pending, oldest->newest, capped
    to_process = []
    while pending and len(to_process) < MAX_MATCH_DETAILS_PER_RUN:
        to_process.append(pending.pop())  # oldest-ish
        pending_set.discard(to_process[-1])

    rate_limit_hits = 0
    new_match_results = []

    for mid in to_process:
        try:
            m = get_match(mid, regional)
        except requests.HTTPError as e:
            msg = str(e)
            if "429" in msg:
                rate_limit_hits += 1
                if rate_limit_hits >= RATE_LIMIT_HIT_LIMIT:
                    print("Too many 429s; stopping early this run to stay stable.")
                    # Put unprocessed IDs back into pending so we don't lose them
                    for back_mid in reversed(to_process[to_process.index(mid):]):
                        if back_mid not in seen and back_mid not in pending_set:
                            pending.append(back_mid)
                            pending_set.add(back_mid)
                    break
            print(f"Skipping match due to HTTPError: {mid} -> {e}")
            # Put it back so we can retry next run
            if mid not in seen and mid not in pending_set:
                pending.append(mid)
                pending_set.add(mid)
            continue

        info = m.get("info", {})
        participants = info.get("participants", [])
        me = next((x for x in participants if x.get("puuid") == st["puuid"]), None)
        if not me:
            # Put back; something is off, but don't drop it
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

    # We know what we've processed; this is "match-details processed", not "total split games".
    stats["splitMatchesProcessed"] = len(st["matchesSeen"])
    stats["totalPlaytimeHMS"] = seconds_to_hms(stats["totalPlaytimeSeconds"])

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
        best = max(eligible, key=lambda r: (r[3], r[1]))      # higher WR, then more games
        worst = min(eligible, key=lambda r: (r[3], -r[1]))    # lower WR, then more games
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

    # LP gain/loss best-effort attribution:
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

    # Debug logging
    newest = recent_ids[0] if recent_ids else None
    newest_is_seen = (recent_ids and (recent_ids[0] in set(st["matchesSeen"])))

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
