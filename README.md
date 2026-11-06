# Cherry Grove Surf

**Dashboard:** https://hud-hill.github.io/surf-report/

A free, personal replacement for a Surfline subscription:

- **Push alerts** on your phone (via the free [ntfy](https://ntfy.sh) app):
  - **Evening (~6pm):** "Tomorrow: Good surf at Cherry Grove Pier", with the best time window, size, wind and tides.
  - **Morning (~6–7am):** an alert for today if the forecast improved overnight, or **"fell apart today"** if last night's call no longer holds.
- **Dashboard** (`docs/index.html`, hosted free on GitHub Pages): a one-tap link to the live Cherry Grove Pier cam, current conditions, and a 7-day outlook with hourly ratings and tides.
- Runs on **GitHub Actions** for free, so your PC can be off.

Data sources (all free, no API keys): Open-Meteo marine and wind forecasts, and NOAA tide predictions (Springmaid Pier station).

## One-time setup (about 10 minutes)

1. **Phone:** install **ntfy** (App Store / Play Store). Tap **+**, subscribe to a topic with a long random name nobody would guess, e.g. `cherrygrove-hud-7f3k9q2x`. Anyone who knows the name can read or send to it, so keep it private.
2. **GitHub:** create a free account if needed, then a new **public** repository (e.g. `surf-report`). Public repos get free Pages and unlimited Actions minutes; your ntfy topic stays secret.
3. **Push this folder** to that repository:
   ```bash
   git init -b main
   git add .
   git commit -m "Cherry Grove surf alerts"
   git remote add origin https://github.com/<your-username>/surf-report.git
   git push -u origin main
   ```
4. In the repo on GitHub:
   - **Settings → Secrets and variables → Actions → New repository secret:** name `NTFY_TOPIC`, value = your topic name.
   - **Settings → Pages:** Source = *Deploy from a branch*, Branch = `main`, folder = `/docs`. Save.
   - (The workflow grants itself write access to save the forecast, so the repo-wide workflow permissions can stay read-only.)
5. **Test it:** **Actions → Surf check → Run workflow**, tick *Send a test notification only*. Your phone should buzz within seconds. Run it again without the box ticked to build the first forecast.
6. Open `https://<your-username>.github.io/surf-report/` on your phone and **Share → Add to Home Screen**.

## Tuning what counts as "worth going"

Everything is in `config.json`:

| Setting | Default | Meaning |
|---|---|---|
| `rideable.min_wave_ft` | 1.5 | Smallest wave height that counts |
| `rideable.min_period_s` | 5 | Shorter than this is wind chop |
| `rideable.max_wind_mph` | 20 | Any direction above this is too windy |
| `rideable.max_onshore_wind_mph` | 14 | Onshore wind above this blows it out |
| `alerts.min_rating` | `Fair` | Alert threshold: `Fair`, `Good` or `Epic` |
| `alerts.min_hours` | 2 | Daylight hours that must meet it |
| `offshore_wind_from_deg` | 315 | NW wind = offshore at Cherry Grove |

If you're getting too many alerts, change `min_rating` to `Good` first.

Ratings: each daylight hour that passes the "rideable" limits is at least **Fair**. Points for size (2 ft+, 3 ft+), period (7s+, 9s+) and wind (glassy/offshore best, onshore worst) push it to **Good** or **Epic**.

## Running it on your PC

```bash
python surf.py --dry-run --mode evening   # see what tomorrow's alert would say
python -m http.server 8765 --directory docs   # then open http://localhost:8765
```

## About the cam

The official pier cam (top of Cherry Grove Pier, pan/zoom) is run through Ozolio and only plays on explorenorthmyrtlebeach.com, so the dashboard and alerts link straight to it rather than embedding it. HDOnTap's Cherry Grove beach cam is the backup link.
