"""
Sleeper weekly payout bot.

Each week it finds:
  1. the weekly high scorer            -> $HIGH_SCORE_PRIZE
  2. the winner of that week's challenge -> $CHALLENGE_PRIZE
and prints prefilled Venmo pay links for the commissioner to tap.
Paid weeks are recorded so re-running never double-pays.

Usage:
    python sleeper_payout_bot.py                # current NFL week
    python sleeper_payout_bot.py --week 3       # specific week
    python sleeper_payout_bot.py --dry-run      # don't record as paid
"""

import argparse
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------- config ----
LEAGUE_ID = os.environ.get("LEAGUE_ID") or "1403192355279904768"
# Full payout structure. Weekly prizes plus season prizes must equal the pot -
# pot_check() below enforces that, so editing one number without balancing the
# rest is caught rather than silently over-committing the money.
BUY_IN = 25.00
LEAGUE_SIZE = 12
SEASON_WEEKS = 14
HIGH_SCORE_PRIZE = 5.00
CHALLENGE_PRIZE = 2.50
SEASON_PRIZES = {1: 120.00, 2: 50.00, 3: 25.00}   # settled by hand after wk 14
PAID_FILE = Path("paid_weeks.json")
SITE_DATA = Path("docs/data")
SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK") or None

# Sleeper display name -> Venmo username (no @).
# Lives in the VENMO_HANDLES repo secret (JSON), never in source: this repo is
# public so the site can be served from GitHub Pages.
VENMO_HANDLES = json.loads(os.environ.get("VENMO_HANDLES") or "{}")
# ---------------------------------------------------------------------------

API = "https://api.sleeper.app/v1"


def get(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.load(r)


# ------------------------------------------------------------ data pull ----
class League:
    def __init__(self, week):
        self.week = week
        settings = get(f"{API}/league/{LEAGUE_ID}")
        # starters[] in matchups lines up with these slots (BN excluded)
        self.slots = [p for p in settings["roster_positions"] if p != "BN"]

        users = {u["user_id"]: u["display_name"]
                 for u in get(f"{API}/league/{LEAGUE_ID}/users")}
        self.names = {r["roster_id"]: users.get(r["owner_id"], "unknown")
                      for r in get(f"{API}/league/{LEAGUE_ID}/rosters")}

        self.matchups = get(f"{API}/league/{LEAGUE_ID}/matchups/{week}")
        self._players = None
        self._drafted = None

    @property
    def players(self):
        if self._players is None:                     # ~5 MB, once per run
            self._players = get(f"{API}/players/nfl")
        return self._players

    def pos(self, pid):
        return self.players.get(pid, {}).get("position", "?")

    @property
    def drafted(self):
        """roster_id -> set of player_ids that roster drafted."""
        if self._drafted is None:
            d = {}
            drafts = get(f"{API}/league/{LEAGUE_ID}/drafts")
            if drafts:
                for p in get(f"{API}/draft/{drafts[0]['draft_id']}/picks"):
                    d.setdefault(p["roster_id"], set()).add(p["player_id"])
            self._drafted = d
        return self._drafted

    def name(self, m):
        return self.names[m["roster_id"]]

    def starter_pts(self, m):
        return [(pid, m["players_points"].get(pid, 0.0)) for pid in m["starters"]]

    def bench_pts(self, m):
        s = set(m["starters"])
        return [(pid, pts) for pid, pts in m["players_points"].items() if pid not in s]

    def games(self):
        """list of (winner_matchup, loser_matchup, margin)."""
        by_id = {}
        for m in self.matchups:
            by_id.setdefault(m["matchup_id"], []).append(m)
        out = []
        for pair in by_id.values():
            if len(pair) == 2:
                a, b = sorted(pair, key=lambda m: m["points"], reverse=True)
                out.append((a, b, a["points"] - b["points"]))
        return out

    def optimal_score(self, m):
        pool = dict(m["players_points"])
        total = 0.0
        flex_slots = []
        for slot in self.slots:
            if slot in ("FLEX", "SUPER_FLEX", "REC_FLEX", "WRRB_FLEX", "IDP_FLEX"):
                flex_slots.append(slot)
                continue
            best = max((pid for pid in pool if self.pos(pid) == slot),
                       key=pool.get, default=None)
            if best:
                total += pool.pop(best)
        elig = {"FLEX": {"RB", "WR", "TE"}, "SUPER_FLEX": {"QB", "RB", "WR", "TE"},
                "REC_FLEX": {"WR", "TE"}, "WRRB_FLEX": {"RB", "WR"}}
        for slot in flex_slots:
            best = max((pid for pid in pool if self.pos(pid) in elig.get(slot, set())),
                       key=pool.get, default=None)
            if best:
                total += pool.pop(best)
        return total


# ----------------------------------------------------------- challenges ----
# Each returns (list of (value, name), lower_is_better, detail_fmt)

def high_score(L):
    return [(m["points"], L.name(m)) for m in L.matchups], False, "{:.2f} pts"

def best_bench(L):
    return [(sum(p for _, p in L.bench_pts(m)), L.name(m)) for m in L.matchups], False, "{:.2f} bench pts"

def closest_100(L):
    return [(abs(m["points"] - 100), L.name(m)) for m in L.matchups], True, "{:.2f} from 100"

def biggest_blowout(L):
    return [(mg, L.name(w)) for w, _, mg in L.games()], False, "won by {:.2f}"

def top_pos(position):
    def f(L):
        rows = []
        for m in L.matchups:
            pts = [p for pid, p in L.starter_pts(m) if L.pos(pid) == position]
            rows.append((max(pts, default=0.0), L.name(m)))
        return rows, False, position + " {:.2f} pts"
    return f

def k_def(L):
    rows = [(sum(p for pid, p in L.starter_pts(m) if L.pos(pid) in ("K", "DEF")), L.name(m))
            for m in L.matchups]
    return rows, False, "K+DEF {:.2f} pts"

def waiver_hero(L):
    rows = []
    for m in L.matchups:
        own = L.drafted.get(m["roster_id"], set())
        pts = [p for pid, p in L.starter_pts(m) if pid not in own]
        rows.append((max(pts, default=0.0), L.name(m)))
    return rows, False, "undrafted starter {:.2f} pts"

def perfect_lineup(L):
    return [(L.optimal_score(m) - m["points"], L.name(m)) for m in L.matchups], True, "left {:.2f} on bench"

def heartbreaker(L):
    return [(mg, L.name(l)) for _, l, mg in L.games() if mg > 0], True, "lost by {:.2f}"

def flex_master(L):
    idx = [i for i, s in enumerate(L.slots) if s == "FLEX"]
    rows = []
    for m in L.matchups:
        sp = L.starter_pts(m)
        rows.append((sum(sp[i][1] for i in idx if i < len(sp)), L.name(m)))
    return rows, False, "FLEX {:.2f} pts"

def season_high(L):
    best = {}
    for wk in range(1, L.week + 1):
        for m in get(f"{API}/league/{LEAGUE_ID}/matchups/{wk}"):
            n = L.names[m["roster_id"]]
            if m["points"] > best.get(n, (0, 0))[0]:
                best[n] = (m["points"], wk)
    return [(v, f"{n} (wk {wk})") for n, (v, wk) in best.items()], False, "season high {:.2f}"

def manual(L):
    return [], False, ""


CHALLENGES = {
    1:  ("Highest score", high_score),
    2:  ("Best bench", best_bench),
    3:  ("Closest to 100", closest_100),
    4:  ("Biggest blowout", biggest_blowout),
    5:  ("Top QB", top_pos("QB")),
    6:  ("Kicker & defense combo", k_def),
    7:  ("Waiver-wire hero", waiver_hero),
    8:  ("Perfect lineup", perfect_lineup),
    9:  ("Top RB", top_pos("RB")),
    10: ("Heartbreaker", heartbreaker),
    11: ("Top WR", top_pos("WR")),
    12: ("Flex master", flex_master),
    13: ("Rivalry week (settle manually: most pts from players in divisional games)", manual),
    14: ("Season-high", season_high),
}


# How each challenge is settled, in plain English, for the published schedule.
CHALLENGE_NOTES = {
    1:  "Most total points - same as the high-score prize, so that winner "
        "takes ${combined:.2f}",
    2:  "Highest combined bench points",
    3:  "Total nearest to 100.00, over or under",
    4:  "Largest margin of victory",
    5:  "Highest-scoring starting quarterback",
    6:  "Highest combined kicker + defense points",
    7:  "Highest-scoring starter that roster did not draft",
    8:  "Fewest points left on the bench vs. the optimal lineup",
    9:  "Highest-scoring starting running back",
    10: "Closest losing margin - the prize goes to the loser",
    11: "Highest-scoring starting wide receiver",
    12: "Highest combined points from the two FLEX slots",
    13: "Most points from players in divisional NFL games",
    14: "Highest single-week score of the entire season",
}


# -------------------------------------------------------------- helpers ----
def winners(rows, lower_is_better):
    if not rows:
        return None, []
    pick = min if lower_is_better else max
    best = pick(v for v, _ in rows)
    return best, [n for v, n in rows if v == best]


def venmo_link(handle, amount, note):
    q = urllib.parse.urlencode({"txn": "pay", "audience": "private",
                                "recipients": handle, "amount": f"{amount:.2f}", "note": note})
    return f"https://venmo.com/?{q}"


def pay_lines(names, prize, note):
    share = round(prize / len(names), 2)
    out = []
    for n in names:
        base = n.split(" (wk")[0]
        h = VENMO_HANDLES.get(base)
        out.append(f"  {n} -> " + (venmo_link(h, share, note) if h else "NO VENMO HANDLE CONFIGURED"))
    return out


def pot_check():
    """(pot, committed) - the buy-in pool vs. everything the structure pays out."""
    pot = BUY_IN * LEAGUE_SIZE
    weekly = (HIGH_SCORE_PRIZE + CHALLENGE_PRIZE) * SEASON_WEEKS
    return pot, weekly + sum(SEASON_PRIZES.values())


def write_challenges():
    """Publish the full 14-week challenge schedule, independent of any results."""
    SITE_DATA.mkdir(parents=True, exist_ok=True)
    (SITE_DATA / "challenges.json").write_text(json.dumps({
        "prize": CHALLENGE_PRIZE,
        "weeks": [
            {"week": wk,
             "title": title.split(" (")[0],
             "note": CHALLENGE_NOTES.get(wk, "").format(
                 combined=HIGH_SCORE_PRIZE + CHALLENGE_PRIZE),
             "auto": fn is not manual}
            for wk, (title, fn) in sorted(CHALLENGES.items())
        ],
    }, indent=2) + "\n")


def write_site_data(result):
    """Write the public GitHub Pages payload.

    Only non-identifying league data goes here - Sleeper display names, points
    and challenge results. Never Venmo handles, pay links or real names: the
    published site is world-readable.
    """
    write_challenges()
    weeks_dir = SITE_DATA / "weeks"
    weeks_dir.mkdir(parents=True, exist_ok=True)
    (weeks_dir / f"{result['week']}.json").write_text(json.dumps(result, indent=2) + "\n")

    weeks = [json.loads(p.read_text())
             for p in sorted(weeks_dir.glob("*.json"), key=lambda p: int(p.stem))]

    tally = {}

    def row(name):
        base = name.split(" (wk")[0]
        return tally.setdefault(base, {"name": base, "high_score_wins": 0,
                                       "challenge_wins": 0, "best_score": 0.0,
                                       "winnings": 0.0})

    for w in weeks:
        for sc in w["scores"]:
            r = row(sc["name"])
            r["best_score"] = max(r["best_score"], sc["points"])
        for key, field in (("high_score", "high_score_wins"),
                           ("challenge", "challenge_wins")):
            block = w.get(key)
            if not block or not block.get("winners"):
                continue
            share = round(block["prize"] / len(block["winners"]), 2)
            for n in block["winners"]:
                r = row(n)
                r[field] += 1
                r["winnings"] = round(r["winnings"] + share, 2)

    (SITE_DATA / "index.json").write_text(json.dumps({
        "updated": result["generated_at"],
        "latest_week": result["week"],
        "weeks": [w["week"] for w in weeks],
        "results": {str(w["week"]): (w.get("challenge") or {}).get("winners", [])
                    for w in weeks},
        "season": sorted(tally.values(),
                         key=lambda r: (-r["winnings"], -r["best_score"], r["name"])),
    }, indent=2) + "\n")


def load_paid():
    return set(json.loads(PAID_FILE.read_text())) if PAID_FILE.exists() else set()


def post_slack(text):
    if not SLACK_WEBHOOK:
        return
    req = urllib.request.Request(SLACK_WEBHOOK, data=json.dumps({"text": text}).encode(),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=15)


# ----------------------------------------------------------------- main ----
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", type=int)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    week = args.week or get(f"{API}/state/nfl")["week"]
    paid = load_paid()
    if week in paid and not args.dry_run:
        print(f"Week {week} already paid out. Use --dry-run to re-inspect.")
        return

    L = League(week)
    if not any(m["points"] for m in L.matchups):
        print(f"Week {week} has no scores yet.")
        return

    lines = [f"*Week {week} payouts*", ""]
    result = {"week": week,
              "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "high_score": None, "challenge": None, "scores": []}

    # 1) weekly high score
    rows, low, fmt = high_score(L)
    best, names = winners(rows, low)
    lines.append(f"High score (${HIGH_SCORE_PRIZE:.0f}): " + fmt.format(best))
    lines += pay_lines(names, HIGH_SCORE_PRIZE, f"Week {week} high score")
    result["high_score"] = {"prize": HIGH_SCORE_PRIZE, "winners": names,
                            "value": round(best, 2), "detail": fmt.format(best)}

    # 2) weekly challenge
    if week in CHALLENGES:
        title, fn = CHALLENGES[week]
        rows, low, fmt = fn(L)
        lines.append("")
        if not rows:
            lines.append(f"Challenge (${CHALLENGE_PRIZE:.0f}): {title}")
            result["challenge"] = {"prize": CHALLENGE_PRIZE, "title": title,
                                   "manual": True, "winners": [], "leaderboard": []}
        else:
            best, names = winners(rows, low)
            lines.append(f"Challenge (${CHALLENGE_PRIZE:.0f}): {title} - " + fmt.format(best))
            lines += pay_lines(names, CHALLENGE_PRIZE, f"Week {week} challenge: {title}")
            lines.append("  Leaderboard:")
            board = sorted(rows, key=lambda r: r[0], reverse=not low)
            for v, n in board:
                lines.append(f"    {n:<22} " + fmt.format(v))
            result["challenge"] = {
                "prize": CHALLENGE_PRIZE, "title": title, "manual": False,
                "winners": names, "value": round(best, 2), "detail": fmt.format(best),
                "leaderboard": [{"name": n, "value": round(v, 2), "detail": fmt.format(v)}
                                for v, n in board]}

    pot, committed = pot_check()
    if abs(pot - committed) > 0.005:
        lines += ["", f"WARNING: payouts total ${committed:.2f} against a "
                      f"${pot:.2f} pot (off by ${committed - pot:+.2f})."]

    lines += ["", "Scores this week:"]
    for m in sorted(L.matchups, key=lambda m: m["points"], reverse=True):
        lines.append(f"  {L.name(m):<22} {m['points']:7.2f}")
        result["scores"].append({"name": L.name(m), "points": round(m["points"], 2)})

    msg = "\n".join(lines)
    print(msg)
    post_slack(msg)

    if not args.dry_run:
        write_site_data(result)
        paid.add(week)
        PAID_FILE.write_text(json.dumps(sorted(paid)))


if __name__ == "__main__":
    main()
