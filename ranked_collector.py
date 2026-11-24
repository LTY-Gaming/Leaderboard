#!/usr/bin/env python3
"""Ranked map-book collector. Run by the collect-ranked workflow every few hours.

Reads each club member's battle log (last 25 games), keeps ONLY ranked games and adds them to
combined per-map totals. Nothing personal is stored: no names, tags or timestamps. A game is
counted once, identified by a salted hash of (battle time + the six player tags).

Env: BS_API_TOKEN, BS_CLUB_TAG, BATTLE_SALT (any long random string; never change it later).
Files (next to this script): ranked_stats.csv (public totals), ranked_seen.txt (salted hashes),
ranked_meta.json (last update time).
"""
import csv, hashlib, json, os, sys, time
from datetime import datetime, timezone
from club_tracker import D, api_get, norm, q

STATS, SEEN, META = D / "ranked_stats.csv", D / "ranked_seen.txt", D / "ranked_meta.json"
HEADER = ["tier", "mode", "map", "brawler", "games", "appearances", "wins", "losses", "draws",
          "bans", "own_games", "own_wins", "opp_games", "opp_wins"]
COUNTS = HEADER[4:]
TIERS = {"diamond", "mythic", "legendary", "masters"}
SEEN_CAP = 20000
FLIP = {"victory": "defeat", "defeat": "victory", "draw": "draw"}
KEY = {"victory": "wins", "defeat": "losses", "draw": "draws"}


def member_tiers():
    """tag -> tier family from the daily snapshot (club_members.csv); unknown/lower tiers -> 'other'."""
    out, p = {}, D / "club_members.csv"
    if p.exists():
        with open(p, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                fam = (r.get("ranked_rank_name") or "").split(" ")[0].lower()
                out[r["tag"]] = fam if fam in TIERS else "other"
    return out


def load_stats():
    stats = {}
    if STATS.exists():
        with open(STATS, newline="", encoding="utf-8") as f:
            rd = csv.reader(f)
            head = next(rd, None)
            if head != HEADER:
                sys.exit(f"{STATS.name}: header mismatch.\nHave: {head}\nWant: {HEADER}\n"
                         "Back it up and delete it to regenerate (columns may only be appended).")
            for r in rd:
                stats[tuple(r[:4])] = dict(zip(COUNTS, (int(x) for x in r[4:])))
    return stats


def row(stats, k):
    return stats.setdefault(k, dict.fromkeys(COUNTS, 0))


def parse(it, salt):
    b, ev = it.get("battle") or {}, it.get("event") or {}
    teams, res = b.get("teams") or [], b.get("result")
    if ("ranked" not in str(b.get("type", "")).lower() or len(teams) != 2 or res not in FLIP
            or not ev.get("mode") or not ev.get("map")):
        return None
    tags = sorted(p.get("tag", "") for t in teams for p in t)
    key = hashlib.sha256(f"{salt}|{it.get('battleTime', '')}|{','.join(tags)}".encode()).hexdigest()[:20]
    return key, ev["mode"], ev["map"], teams, res


def tally(stats, tier, mode, mp, teams, res, own, club_tags):
    row(stats, (tier, mode, mp, "*"))["games"] += 1
    sides = [i for i, t in enumerate(teams) if any(p.get("tag") in club_tags for p in t)]
    ours = sides[0] if len(sides) == 1 else None   # club members on both sides -> no 'against us' stats
    for i, t in enumerate(teams):
        outcome = res if i == own else FLIP[res]   # result is from the log owner's side
        for p in t:
            br = ((p.get("brawler") or {}).get("name") or "").strip()
            if br:
                r = row(stats, (tier, mode, mp, br))
                r["appearances"] += 1
                r[KEY[outcome]] += 1
                if ours is not None and i != ours:
                    r["opp_games"] += 1
                    r["opp_wins"] += outcome == "victory"
                if p.get("tag") in club_tags:
                    r["own_games"] += 1
                    r["own_wins"] += outcome == "victory"
            ban = ((p.get("bannedBrawler") or {}).get("name") or "").strip()
            if ban:
                row(stats, (tier, mode, mp, ban))["bans"] += 1


def main():
    token, tag, salt = os.environ.get("BS_API_TOKEN"), os.environ.get("BS_CLUB_TAG"), os.environ.get("BATTLE_SALT")
    if not (token and tag and salt):
        sys.exit("Set BS_API_TOKEN, BS_CLUB_TAG and BATTLE_SALT (GitHub Secrets, no quotes).")
    tag = norm(tag)
    club = api_get(f"/clubs/{q(tag)}", token)
    members = club.get("members")
    if norm(club.get("tag", "")) != tag or not members:
        sys.exit("Club response missing tag/members; refusing to continue.")
    club_tags = {m["tag"] for m in members}
    tiers = member_tiers()
    stats = load_stats()
    seen_list = SEEN.read_text().split() if SEEN.exists() else []
    seen, new_seen, added, dup = set(seen_list), [], 0, 0
    for m in members:
        try:
            log = api_get(f"/players/{q(m['tag'])}/battlelog", token)
        except SystemExit as e:        # a member who just left/renamed shouldn't stop the run
            if "404" in str(e):
                print(f"skipping {m['tag']}: not found")
                continue
            raise
        for it in log.get("items") or []:
            parsed = parse(it, salt)
            if not parsed:
                continue
            key, mode, mp, teams, res = parsed
            if key in seen:
                dup += 1
                continue
            own = next((i for i, t in enumerate(teams) if any(p.get("tag") == m["tag"] for p in t)), None)
            if own is None:
                continue
            tally(stats, tiers.get(m["tag"], "other"), mode, mp, teams, res, own, club_tags)
            seen.add(key)
            new_seen.append(key)
            added += 1
        time.sleep(0.3)

    # all members fetched -> write everything
    tmp = STATS.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        for k in sorted(stats):
            w.writerow(list(k) + [stats[k][c] for c in COUNTS])
    tmp.replace(STATS)
    SEEN.write_text("\n".join((seen_list + new_seen)[-SEEN_CAP:]) + "\n")
    now = datetime.now(timezone.utc)
    meta = json.loads(META.read_text()) if META.exists() else {}
    meta["updated"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    meta.setdefault("since", now.strftime("%Y-%m-%d"))
    meta["games"] = sum(v["games"] for k, v in stats.items() if k[3] == "*")
    META.write_text(json.dumps(meta))
    print(f"{added} new ranked games ({dup} already counted) from {len(members)} members; total {meta['games']}")


if __name__ == "__main__":
    main()
