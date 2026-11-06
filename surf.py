"""Cherry Grove surf checker.

Pulls the 7-day wave, wind and tide forecast, rates every daylight hour,
writes docs/data.json for the dashboard, and sends an ntfy push alert when
an upcoming day looks surfable.

Uses only the Python standard library, so there is nothing to install.

Usage:
    python surf.py                 # normal run (alerts only inside the evening/morning windows)
    python surf.py --dry-run       # print what would be sent, send nothing
    python surf.py --mode evening  # force the "tomorrow" check now
    python surf.py --mode morning  # force the "today" check now
    python surf.py --test-notify   # send a test push to check your phone setup

Environment variables:
    NTFY_TOPIC     your private ntfy topic name (required to send alerts)
    NTFY_SERVER    defaults to https://ntfy.sh
    DASHBOARD_URL  link opened when you tap the notification
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
DATA_PATH = ROOT / "docs" / "data.json"

RATINGS = ["Flat", "Poor", "Fair", "Good", "Epic"]
COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


# --------------------------------------------------------------------------- helpers

def get_json(url: str, params: dict) -> dict:
    full = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, headers={"User-Agent": "cherry-grove-surf-checker"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def compass(deg: float | None) -> str:
    if deg is None:
        return "?"
    return COMPASS[int((deg % 360) / 22.5 + 0.5) % 16]


def angle_diff(a: float, b: float) -> float:
    d = abs(a - b) % 360
    return 360 - d if d > 180 else d


def fmt_hour(h: int) -> str:
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12}{suffix}"


def rating_at_least(rating: str, minimum: str) -> bool:
    return RATINGS.index(rating) >= RATINGS.index(minimum)


# --------------------------------------------------------------------------- data

def local_tz(open_meteo_response: dict) -> timezone:
    # Open-Meteo reports the spot's current UTC offset (handles EST/EDT), so no tz database is needed
    return timezone(timedelta(seconds=open_meteo_response["utc_offset_seconds"]))


def fetch_forecast(cfg: dict) -> dict:
    loc = {"latitude": cfg["latitude"], "longitude": cfg["longitude"],
           "timezone": cfg["timezone"], "forecast_days": 7}

    marine = get_json("https://marine-api.open-meteo.com/v1/marine", {
        **loc,
        "hourly": ",".join([
            "wave_height", "wave_period", "wave_direction",
            "swell_wave_height", "swell_wave_period", "swell_wave_direction",
            "sea_surface_temperature",
        ]),
        "length_unit": "imperial",
    })
    weather = get_json("https://api.open-meteo.com/v1/forecast", {
        **loc,
        "hourly": "wind_speed_10m,wind_direction_10m,wind_gusts_10m,temperature_2m",
        "daily": "sunrise,sunset",
        "wind_speed_unit": "mph",
        "temperature_unit": "fahrenheit",
    })

    today = datetime.now(local_tz(weather)).date()
    try:
        tides = get_json("https://api.tidesandcurrents.noaa.gov/api/prod/datagetter", {
            "product": "predictions", "station": cfg["tide_station"],
            "begin_date": today.strftime("%Y%m%d"), "range": 24 * 8,
            "datum": "MLLW", "time_zone": "lst_ldt", "interval": "hilo",
            "units": "english", "format": "json", "application": "cherry_grove_surf",
        }).get("predictions", [])
    except Exception as exc:  # tides are nice-to-have; never block alerts on them
        print(f"warning: tide fetch failed: {exc}", file=sys.stderr)
        tides = []

    return {"marine": marine, "weather": weather, "tides": tides}


# --------------------------------------------------------------------------- rating

def wind_quality(wind_from: float, speed: float, offshore_from: float) -> str:
    if speed < 5:
        return "Glassy"
    d = angle_diff(wind_from, offshore_from)
    if d <= 45:
        return "Offshore"
    if d <= 80:
        return "Cross-offshore"
    if d <= 110:
        return "Cross-shore"
    if d <= 135:
        return "Cross-onshore"
    return "Onshore"


def rate_hour(h: dict, cfg: dict) -> tuple[str, int, str]:
    """Return (rating, score, reason) for one hour."""
    r = cfg["rideable"]
    wave, period = h["wave_ft"], h["period_s"]
    wind, wq = h["wind_mph"], h["wind_type"]

    if wave is None or period is None or wind is None:
        return "Poor", 0, "missing data"
    if wave < 1.0:
        return "Flat", 0, "flat"
    if wave < r["min_wave_ft"]:
        return "Poor", 0, "too small"
    if period < r["min_period_s"]:
        return "Poor", 0, "short-period wind chop"
    if wind > r["max_wind_mph"]:
        return "Poor", 0, "too windy"
    if wq in ("Onshore", "Cross-onshore") and wind > r["max_onshore_wind_mph"]:
        return "Poor", 0, "blown out by onshore wind"

    score = 0
    score += 1 if wave < 2 else 2 if wave < 3 else 3
    score += 0 if period < 7 else 1 if period < 9 else 2
    if wq in ("Glassy", "Offshore"):
        score += 2
    elif wq == "Cross-offshore" or wind < 10:
        score += 1
    elif wq in ("Onshore", "Cross-onshore"):
        score -= 1

    if score >= 6:
        rating = "Epic"
    elif score >= 4:
        rating = "Good"
    else:
        rating = "Fair"
    return rating, score, "rideable"


def build_days(raw: dict, cfg: dict) -> list[dict]:
    m, w = raw["marine"]["hourly"], raw["weather"]["hourly"]
    wind_by_time = {t: i for i, t in enumerate(w["time"])}
    offshore = cfg["offshore_wind_from_deg"]

    daily = raw["weather"]["daily"]
    sun = {d: (sr, ss) for d, sr, ss in zip(daily["time"], daily["sunrise"], daily["sunset"])}

    hours_by_day: dict[str, list[dict]] = {}
    for i, t in enumerate(m["time"]):
        wi = wind_by_time.get(t)
        if wi is None:
            continue
        day, hh = t[:10], int(t[11:13])
        sunrise, sunset = sun.get(day, (f"{day}T07:00", f"{day}T18:00"))
        # count an hour as daylight if it starts after sunrise and ends before sunset (+/- 30 min)
        if hh < int(sunrise[11:13]) or hh > int(sunset[11:13]) - 1:
            continue

        swell_h = m["swell_wave_height"][i]
        swell_p = m["swell_wave_period"][i]
        wave_p = m["wave_period"][i]
        # a real ground swell under the wind chop is a better guide than the mixed period
        period = swell_p if (swell_h or 0) >= 1.0 and swell_p and wave_p and swell_p > wave_p else wave_p

        speed = w["wind_speed_10m"][wi]
        wdir = w["wind_direction_10m"][wi]
        h = {
            "hour": hh,
            "wave_ft": m["wave_height"][i],
            "period_s": period,
            "wave_dir": compass(m["wave_direction"][i]),
            "swell_ft": swell_h,
            "swell_period_s": swell_p,
            "swell_dir": compass(m["swell_wave_direction"][i]),
            "wind_mph": speed,
            "gust_mph": w["wind_gusts_10m"][wi],
            "wind_dir": compass(wdir),
            "wind_type": wind_quality(wdir, speed, offshore) if speed is not None and wdir is not None else "?",
            "air_f": w["temperature_2m"][wi],
            "water_f": None if m["sea_surface_temperature"][i] is None
                       else round(m["sea_surface_temperature"][i] * 9 / 5 + 32),
        }
        h["rating"], h["score"], h["reason"] = rate_hour(h, cfg)
        hours_by_day.setdefault(day, []).append(h)

    tides_by_day: dict[str, list[dict]] = {}
    for p in raw["tides"]:
        tides_by_day.setdefault(p["t"][:10], []).append({
            "time": fmt_hour(int(p["t"][11:13])).replace("am", f":{p['t'][14:16]}am").replace("pm", f":{p['t'][14:16]}pm"),
            "type": "High" if p["type"] == "H" else "Low",
            "ft": round(float(p["v"]), 1),
        })

    min_rating = cfg["alerts"]["min_rating"]
    days = []
    for day, hours in sorted(hours_by_day.items()):
        best = max(hours, key=lambda h: (RATINGS.index(h["rating"]), h["score"], h["wave_ft"] or 0))
        good = [h for h in hours if rating_at_least(h["rating"], min_rating)]

        # longest run of consecutive qualifying hours
        window, run = [], []
        for h in hours:
            if rating_at_least(h["rating"], min_rating) and (not run or h["hour"] == run[-1]["hour"] + 1):
                run.append(h)
            elif rating_at_least(h["rating"], min_rating):
                run = [h]
            else:
                run = []
            if len(run) > len(window):
                window = list(run)

        d = datetime.strptime(day, "%Y-%m-%d")
        days.append({
            "date": day,
            "label": d.strftime("%a %b %-d") if os.name != "nt" else d.strftime("%a %b %#d"),
            "rating": best["rating"],
            "qualifying_hours": len(good),
            "qualifies": len(good) >= cfg["alerts"]["min_hours"],
            "window": f"{fmt_hour(window[0]['hour'])}–{fmt_hour(window[-1]['hour'] + 1)}" if window else None,
            "best": best,
            "summary": summarize(best),
            "sunrise": sun.get(day, ("", ""))[0][11:16],
            "sunset": sun.get(day, ("", ""))[1][11:16],
            "tides": tides_by_day.get(day, []),
            "hours": hours,
        })
    return days


def summarize(h: dict) -> str:
    lo = max(1, round(h["wave_ft"] - 0.5)) if h["wave_ft"] else 0
    hi = round(h["wave_ft"] + 0.5) if h["wave_ft"] else 0
    size = f"{lo}-{hi} ft" if hi > lo else f"{hi} ft"
    return (f"{size} @ {h['period_s']:.0f}s {h['wave_dir']}, "
            f"wind {h['wind_mph']:.0f} mph {h['wind_dir']} ({h['wind_type'].lower()})")


# --------------------------------------------------------------------------- alerts

def send_ntfy(title: str, body: str, tags: str, priority: str = "default", click: str | None = None) -> None:
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        raise SystemExit("NTFY_TOPIC is not set; cannot send a notification.")
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    headers = {"Title": title, "Tags": tags, "Priority": priority}
    if click:
        headers["Click"] = click
    req = urllib.request.Request(f"{server}/{topic}", data=body.encode("utf-8"),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def tide_line(day: dict) -> str:
    return ", ".join(f"{t['type']} {t['time']}" for t in day["tides"]) or "n/a"


def decide_alert(days: list[dict], mode: str, now: datetime, sent: dict, cfg: dict):
    """Return (key, title, body, tags, priority) or None."""
    today = now.date()
    target = today + timedelta(days=1) if mode == "evening" else today
    tkey = target.isoformat()
    day = next((d for d in days if d["date"] == tkey), None)
    if day is None:
        return None
    when = "Tomorrow" if mode == "evening" else "Today"
    prev = sent.get(tkey)

    if day["qualifies"]:
        if prev and prev.get("status") == "go":
            return None  # already told you about this day and it still looks good
        hours = day["qualifying_hours"]
        title = f"{when}: {day['rating']} surf at {cfg['spot_name']}"
        body = (f"{day['summary']}.\n"
                f"Best window: {day['window']} ({hours} rideable daylight hrs).\n"
                f"Tides: {tide_line(day)}. Water {day['best']['water_f']}F.\n"
                f"Pier cam: {cfg['cam_page_url']}")
        tags = "ocean,surfer" if day["rating"] in ("Good", "Epic") else "ocean"
        priority = "high" if day["rating"] == "Epic" else "default"
        return tkey, "go", title, body, tags, priority

    if prev and prev.get("status") == "go" and mode == "morning":
        title = f"Update: {cfg['spot_name']} fell apart today"
        body = f"Forecast dropped to {day['rating']}. Now: {day['summary']}."
        return tkey, "called_off", title, body, "warning", "default"
    return None


# --------------------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print the alert instead of sending it")
    ap.add_argument("--mode", choices=["auto", "evening", "morning", "none"], default="auto")
    ap.add_argument("--test-notify", action="store_true", help="send a test notification and exit")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    dashboard = os.environ.get("DASHBOARD_URL") or None

    if args.test_notify:
        send_ntfy("Surf alerts are working", f"You'll get {cfg['spot_name']} alerts here.",
                  "white_check_mark", click=dashboard)
        print("Test notification sent.")
        return

    raw = fetch_forecast(cfg)
    now = datetime.now(local_tz(raw["weather"]))
    days = build_days(raw, cfg)

    previous = {}
    if DATA_PATH.exists():
        try:
            previous = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    sent = {k: v for k, v in previous.get("alerts_sent", {}).items()
            if k >= (now.date() - timedelta(days=7)).isoformat()}

    mode = args.mode
    if mode == "auto":
        ev, mo = cfg["alerts"]["evening_hour_window"], cfg["alerts"]["morning_hour_window"]
        mode = "evening" if ev[0] <= now.hour < ev[1] else "morning" if mo[0] <= now.hour < mo[1] else "none"

    for d in days:
        flag = "ALERT" if d["qualifies"] else "     "
        print(f"{flag} {d['label']:<11} {d['rating']:<5} {d['window'] or '-':<11} {d['summary']}")
    print(f"mode: {mode}")

    if mode != "none":
        alert = decide_alert(days, mode, now, sent, cfg)
        if alert:
            key, status, title, body, tags, priority = alert
            print(f"\n--- notification ---\n{title}\n{body}\n--------------------")
            if args.dry_run:
                print("(dry run: not sent)")
            else:
                send_ntfy(title, body, tags, priority, click=dashboard)
                sent[key] = {"status": status, "at": now.isoformat(timespec="minutes")}
                print("sent.")
        else:
            print("no alert needed.")

    DATA_PATH.parent.mkdir(exist_ok=True)
    DATA_PATH.write_text(json.dumps({
        "spot": cfg["spot_name"],
        "generated_at": now.isoformat(timespec="minutes"),
        "cam_page_url": cfg["cam_page_url"],
        "backup_cam_url": cfg["backup_cam_url"],
        "min_rating": cfg["alerts"]["min_rating"],
        "days": days,
        "alerts_sent": sent,
    }, indent=1), encoding="utf-8")
    print(f"wrote {DATA_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
