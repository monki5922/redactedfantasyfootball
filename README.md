# Sleeper weekly payout bot

Runs every Tuesday morning after Monday Night Football. Pulls the week's scores
from Sleeper's public API, works out the weekly high scorer and the winner of
that week's side challenge, and publishes the results to a small static site.

**Results: https://monki5922.github.io/redactedfantasyfootball/**

## How it works

| Piece | What it does |
| --- | --- |
| `sleeper_payout_bot.py` | Pulls scores, settles the week, prints prefilled Venmo links for the commissioner to tap |
| `.github/workflows/payout.yml` | Cron trigger (Tuesdays 13:00 UTC), commits results back |
| `docs/` | The published site — reads `docs/data/*.json` |

Payments are deliberately not automatic. Venmo retired its consumer developer
API, so the bot prints prefilled links and a human taps Send.

## Configuration

Both are repo secrets, never committed — this repo is public so the site can be
served from GitHub Pages for free.

- `VENMO_HANDLES` — JSON mapping Sleeper display name to Venmo username
- `SLACK_WEBHOOK` — optional, posts the weekly summary to Slack

The published site carries only Sleeper display names, points and challenge
results. No Venmo handles, no pay links, no real names.

## Running it by hand

```
python sleeper_payout_bot.py            # current NFL week
python sleeper_payout_bot.py --week 3   # a specific week
python sleeper_payout_bot.py --dry-run  # print everything, record nothing
```

A paid week is never paid twice; drop the week from `paid_weeks.json` to redo it.
