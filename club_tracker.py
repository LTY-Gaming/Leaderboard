#!/usr/bin/env python3
"""Brawl Stars club tracker. Run daily by GitHub Actions; writes CSVs next to this file.

Env: BS_API_TOKEN, BS_CLUB_TAG. API via the RoyaleAPI proxy (token whitelists 128.128.128.128).
One snapshot per day (SAST date): re-running the same day replaces that day's rows.
Schema changes: only ever append columns at the END of a header list.
"""
import csv, os, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote
import requests

D = Path(__file__).resolve().parent
BASE = "https://bsproxy.royaleapi.dev/v1"
RETRY = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 527}
DELAYS = [5, 15, 30, 60, 120]

H_MEMBERS = ["date", "tag", "name", "role", "trophies", "ranked_season_id", "ranked_rank",
             "ranked_rank_name", "ranked_elo", "best_rank", "best_rank_name", "best_elo"]
H_CLUB = ["date", "tag", "name", "trophies", "required_trophies", "type", "member_count", "fetched_at"]
H_ZA_CLUBS = ["date", "rank", "tag", "name", "trophies", "member_count"]
H_ZA_PLAYERS = ["date", "rank", "tag", "name", "trophies"]


def short(r):
    t = r.text.strip()
    return "(HTML error page from proxy/CDN)" if "<html" in t[:300].lower() else t[:200]


def api_get(path, token):
    problem = None
    for i in range(len(DELAYS) + 1):
        try:
            r = requests.get(BASE + path, headers={"Authorization": f"Bearer {token}"}, timeout=20)
        except requests.RequestException as e:
            problem = type(e).__name__
        else:
            if r.status_code == 200:
                return r.json()
            if r.status_code not in RETRY:
                sys.exit(f"API error {r.status_code} on {path}: {short(r)}")
            problem = f"HTTP {r.status_code}"
        if i < len(DELAYS):
            print(f"{path}: {problem}; retry {i + 1}/{len(DELAYS)} in {DELAYS[i]}s")
            time.sleep(DELAYS[i])
    sys.exit(f"{path} still failing after {len(DELAYS) + 1} attempts ({problem}). Re-run later.")


def norm(tag):
    tag = tag.strip().upper()
    return tag if tag.startswith("#") else "#" + tag


def q(tag):
    return quote(tag, safe="")


def upsert(name, header, new_rows, date):
    """Replace all rows for `date`, keep the rest. Pads old rows if columns were appended."""
    path = D / name
    old = []
    if path.exists():
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        if rows:
            h = rows[0]
            if h == header:
                old = rows[1:]
            elif len(h) < len(header) and header[:len(h)] == h:
                old = [r + [""] * (len(header) - len(h)) for r in rows[1:]]
                print(f"Migrated {name}: padded {len(header) - len(h)} new column(s)")
            else:
                sys.exit(f"{name}: header mismatch (not a simple column addition).\n"
                         f"Have: {h}\nWant: {header}\nBack it up and delete it to regenerate.")
    keep = [r for r in old if r and r[0] != date]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(keep + new_rows)


def main():
    token, tag = os.environ.get("BS_API_TOKEN"), os.environ.get("BS_CLUB_TAG")
    if not token or not tag:
        sys.exit("Set BS_API_TOKEN and BS_CLUB_TAG (GitHub Secrets, no quotes).")
    tag = norm(tag)
    now = datetime.now(timezone.utc)
    date = (now + timedelta(hours=2)).strftime("%Y-%m-%d")  # SAST

    club = api_get(f"/clubs/{q(tag)}", token)
    if norm(club.get("tag", "")) != tag or not club.get("members"):
        sys.exit("Club response missing tag/members; refusing to write partial data.")
    members = club["members"]
    za_clubs = api_get("/rankings/ZA/clubs?limit=200", token).get("items")
    za_players = api_get("/rankings/ZA/players?limit=200", token).get("items")
    if za_clubs is None or za_players is None:
        sys.exit("Ranking response missing 'items'; refusing to write partial data.")

    ours = {m["tag"] for m in members}
    member_rows = []
    for m in members:  # one call per member; keep ONLY the ranked fields
        p = api_get(f"/players/{q(m['tag'])}", token)
        g = lambda k: "" if p.get(k) is None else p[k]
        member_rows.append([date, m["tag"], m["name"], m.get("role", ""), m["trophies"],
                            g("rankedSeasonId"), g("rankedRank"), g("rankedRankName"), g("rankedElo"),
                            g("highestAllTimeRankedRank"), g("highestAllTimeRankedRankName"),
                            g("highestAllTimeRankedElo")])
        time.sleep(0.3)

    club_row = [[date, tag, club["name"], club["trophies"], club.get("requiredTrophies", ""),
                 club.get("type", ""), len(members), now.strftime("%Y-%m-%dT%H:%M:%SZ")]]
    za_c = [[date, e["rank"], e["tag"], e["name"], e["trophies"], e.get("memberCount", "")]
            for e in za_clubs]
    za_p = [[date, e["rank"], e["tag"], e["name"], e["trophies"]] for e in za_players if e["tag"] in ours]

    # Everything fetched OK -> write all files.
    upsert("club_members.csv", H_MEMBERS, member_rows, date)
    upsert("club_daily.csv", H_CLUB, club_row, date)
    upsert("za_clubs.csv", H_ZA_CLUBS, za_c, date)
    upsert("za_players.csv", H_ZA_PLAYERS, za_p, date)
    print(f"{date}: {len(members)} members, {len(za_c)} ZA clubs, {len(za_p)} members in ZA top {len(za_players)}")


if __name__ == "__main__":
    main()
