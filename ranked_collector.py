#!/usr/bin/env python3
"""Pulls the ranked games out of every member's battle log and adds them to the map totals.

Only combined numbers get saved (no names, tags or times). To stop a game being counted twice
we keep a salted hash of the battle time plus the six player tags.

Needs BS_API_TOKEN, BS_CLUB_TAG and BATTLE_SALT set in the environment.
Writes ranked_stats.csv, ranked_daily.csv, ranked_seen.txt and ranked_meta.json next to this file.
"""
import csv, hashlib, json, os, sys, time
from datetime import datetime, timedelta, timezone
from club_tracker import D, api_get, norm, q

STATS, SEEN, META = D / "ranked_stats.csv", D / "ranked_seen.txt", D / "ranked_meta.json"
DAILY = D / "ranked_daily.csv"   # per-day map results for the Elo section, day level only
HEADER = ["tier", "mode", "map", "brawler", "games", "appearances", "wins", "losses", "draws",
          "bans", "own_games", "own_wins", "opp_games", "opp_wins", "brawler_id"]
COUNTS = HEADER[4:14]
DAILY_HEADER = ["date", "mode", "map", "brawler", "games", "club_wins", "club_losses", "brawler_id"]
KEEP_DAYS = 150   # older days get dropped so the file stays small
TIERS = {"diamond", "mythic", "legendary", "masters"}
SEEN_CAP = 20000
FLIP = {"victory": "defeat", "defeat": "victory", "draw": "draw"}
OUTCOME_COL = {"victory": "wins", "defeat": "losses", "draw": "draws"}


def member_tiers():
    # tier family per member, taken from the daily snapshot. anything else counts as "other"
    out, path = {}, D / "club_members.csv"
    if path.exists():
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                family = (r.get("ranked_rank_name") or "").split(" ")[0].lower()
                out[r["tag"]] = family if family in TIERS else "other"
    return out


def load_stats():
    stats = {}
    if not STATS.exists():
        return stats
    with open(STATS, newline="", encoding="utf-8") as f:
        rd = csv.reader(f)
        head = next(rd, None)
        # files from before the icons update don't have the brawler_id column yet
        if head not in (HEADER, HEADER[:-1]):
            sys.exit(f"{STATS.name} has the wrong columns.\nHave: {head}\nWant: {HEADER}\n"
                     "Back it up and delete it so it can be rebuilt.")
        for r in rd:
            counts = dict(zip(COUNTS, (int(x) for x in r[4:14])))
            counts["id"] = r[14] if len(r) > 14 else ""
            stats[tuple(r[:4])] = counts
    return stats


def load_daily():
    daily = {}
    if not DAILY.exists():
        return daily
    with open(DAILY, newline="", encoding="utf-8") as f:
        rd = csv.reader(f)
        head = next(rd, None)
        if head != DAILY_HEADER:
            sys.exit(f"{DAILY.name} has the wrong columns.\nHave: {head}\nWant: {DAILY_HEADER}\n"
                     "Back it up and delete it so it can be rebuilt.")
        for r in rd:
            daily[tuple(r[:4])] = {"games": int(r[4]), "club_wins": int(r[5]), "club_losses": int(r[6]), "id": r[7]}
    return daily


def sast_day(battle_time):
    # battle times look like 20261123T225156.000Z (UTC). we want the South African date
    try:
        return (datetime.strptime(battle_time[:15], "%Y%m%dT%H%M%S") + timedelta(hours=2)).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def bucket(stats, key):
    return stats.setdefault(key, {**dict.fromkeys(COUNTS, 0), "id": ""})


def read_game(item, salt):
    battle, event = item.get("battle") or {}, item.get("event") or {}
    teams, result = battle.get("teams") or [], battle.get("result")
    if ("ranked" not in str(battle.get("type", "")).lower() or len(teams) != 2
            or result not in FLIP or not event.get("mode") or not event.get("map")):
        return None
    tags = sorted(p.get("tag", "") for t in teams for p in t)
    raw = f"{salt}|{item.get('battleTime', '')}|{','.join(tags)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20], event["mode"], event["map"], teams, result, item.get("battleTime", "")


def count_game(stats, daily, day, tier, mode, map_name, teams, result, owner_side, club_tags):
    bucket(stats, (tier, mode, map_name, "*"))["games"] += 1
    club_sides = [i for i, t in enumerate(teams) if any(p.get("tag") in club_tags for p in t)]
    # if club members are on both teams there's no "against us" side, so we skip those numbers
    ours = club_sides[0] if len(club_sides) == 1 else None
    if day and ours is not None:
        our_result = result if ours == owner_side else FLIP[result]
        if our_result != "draw":
            col = "club_wins" if our_result == "victory" else "club_losses"
            whole_map = daily.setdefault((day, mode, map_name, "*"), {"games": 0, "club_wins": 0, "club_losses": 0, "id": ""})
            whole_map["games"] += 1
            whole_map[col] += 1
            for p in teams[ours]:
                brawler = p.get("brawler") or {}
                name = (brawler.get("name") or "").strip()
                if name and p.get("tag") in club_tags:
                    d = daily.setdefault((day, mode, map_name, name), {"games": 0, "club_wins": 0, "club_losses": 0, "id": ""})
                    d["games"] += 1
                    d[col] += 1
                    d["id"] = str(brawler.get("id") or d["id"])
    for i, team in enumerate(teams):
        outcome = result if i == owner_side else FLIP[result]   # the log gives the owner's result
        for p in team:
            brawler = p.get("brawler") or {}
            name = (brawler.get("name") or "").strip()
            if name:
                r = bucket(stats, (tier, mode, map_name, name))
                r["id"] = str(brawler.get("id") or r["id"])
                r["appearances"] += 1
                r[OUTCOME_COL[outcome]] += 1
                if ours is not None and i != ours:
                    r["opp_games"] += 1
                    r["opp_wins"] += outcome == "victory"
                if p.get("tag") in club_tags:
                    r["own_games"] += 1
                    r["own_wins"] += outcome == "victory"
            banned = p.get("bannedBrawler") or {}
            ban_name = (banned.get("name") or "").strip()
            if ban_name:
                b = bucket(stats, (tier, mode, map_name, ban_name))
                b["bans"] += 1
                b["id"] = str(banned.get("id") or b["id"])


def save_all(stats, daily, seen_list, new_seen):
    # rows that never saw their own brawler id can borrow it from another map/tier
    ids = {k[3]: v["id"] for k, v in stats.items() if v["id"]}
    for k, v in stats.items():
        if not v["id"] and k[3] in ids:
            v["id"] = ids[k[3]]
    tmp = STATS.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        for k in sorted(stats):
            w.writerow(list(k) + [stats[k][c] for c in COUNTS] + [stats[k]["id"]])
    tmp.replace(STATS)
    now = datetime.now(timezone.utc)
    cutoff = (now + timedelta(hours=2) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    tmp = DAILY.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(DAILY_HEADER)
        for k in sorted(daily):
            if k[0] >= cutoff:
                v = daily[k]
                w.writerow(list(k) + [v["games"], v["club_wins"], v["club_losses"], v["id"]])
    tmp.replace(DAILY)
    SEEN.write_text("\n".join((seen_list + new_seen)[-SEEN_CAP:]) + "\n")
    meta = json.loads(META.read_text()) if META.exists() else {}
    meta["updated"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    meta.setdefault("since", now.strftime("%Y-%m-%d"))
    meta["games"] = sum(v["games"] for k, v in stats.items() if k[3] == "*")
    META.write_text(json.dumps(meta))
    return meta["games"]


def main():
    token, tag, salt = os.environ.get("BS_API_TOKEN"), os.environ.get("BS_CLUB_TAG"), os.environ.get("BATTLE_SALT")
    if not (token and tag and salt):
        sys.exit("BS_API_TOKEN, BS_CLUB_TAG and BATTLE_SALT all need to be set as GitHub secrets (no quotes).")
    tag = norm(tag)
    club = api_get(f"/clubs/{q(tag)}", token)
    members = club.get("members")
    if norm(club.get("tag", "")) != tag or not members:
        sys.exit("Club response looks wrong (no tag or no members). Stopping.")
    club_tags = {m["tag"] for m in members}
    tiers = member_tiers()
    stats = load_stats()
    daily = load_daily()
    seen_list = SEEN.read_text().split() if SEEN.exists() else []
    seen, new_seen, added, dupes = set(seen_list), [], 0, 0
    for m in members:
        try:
            log = api_get(f"/players/{q(m['tag'])}/battlelog", token)
        except SystemExit as e:
            # someone who just left or changed tag shouldn't kill the whole run
            if "404" in str(e):
                print(f"skipping {m['tag']}, not found")
                continue
            raise
        for item in log.get("items") or []:
            game = read_game(item, salt)
            if not game:
                continue
            key, mode, map_name, teams, result, stamp = game
            if key in seen:
                dupes += 1
                continue
            owner_side = next((i for i, t in enumerate(teams) if any(p.get("tag") == m["tag"] for p in t)), None)
            if owner_side is None:
                continue
            count_game(stats, daily, sast_day(stamp), tiers.get(m["tag"], "other"), mode, map_name, teams, result, owner_side, club_tags)
            seen.add(key)
            new_seen.append(key)
            added += 1
        time.sleep(0.3)

    # nothing is written until every member has been read
    total = save_all(stats, daily, seen_list, new_seen)
    print(f"{added} new ranked games ({dupes} already counted), {len(members)} members, {total} games in total")


if __name__ == "__main__":
    main()
