#!/usr/bin/env python3
"""
Garmin Connect data sync — pulls activities + daily wellness
(sleep, HRV, resting HR, body battery, stress, steps, training readiness)
and writes them as readable Markdown notes + a combined data.json.

Built on python-garminconnect by cyberjunky.
Read-only: never writes anything back to Garmin.
"""

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from garminconnect import Garmin


# ── token persistence ────────────────────────────────────────────────────────

TOKEN_DIR = Path.home() / ".garmin-ai" / "tokens"
_mfa_code = None


def _mfa_callback() -> str:
    if _mfa_code:
        return _mfa_code
    return input("Garmin 2FA code: ").strip()


def do_login(email: str, password: str, mfa_code: str | None = None) -> Garmin:
    global _mfa_code
    _mfa_code = mfa_code
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    garmin = Garmin(email, password, is_cn=False, prompt_mfa=_mfa_callback)
    garmin.login(tokenstore=str(TOKEN_DIR))
    print(f"Token saved to {TOKEN_DIR}")
    return garmin


def get_client() -> Garmin:
    if TOKEN_DIR.exists():
        try:
            garmin = Garmin()
            garmin.login(tokenstore=str(TOKEN_DIR))
            return garmin
        except Exception:
            pass
    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")
    if not email or not password:
        sys.exit(
            "No saved token and GARMIN_EMAIL / GARMIN_PASSWORD not set.\n"
            "Run with --login first, or set the environment variables."
        )
    return do_login(email, password)


# ── data fetching ─────────────────────────────────────────────────────────────

def fetch_km_splits(garmin: Garmin, activity_id) -> list[dict]:
    """Fetch per-km split data for an activity. Returns simplified list."""
    try:
        splits = garmin.get_activity_splits(str(activity_id))
        laps = splits.get("lapDTOs", [])
        result = []
        for lap in laps:
            result.append({
                "km": lap.get("lapIndex"),
                "distance": lap.get("distance"),
                "duration": lap.get("duration"),
                "averageHR": lap.get("averageHR"),
                "maxHR": lap.get("maxHR"),
                "averageSpeed": lap.get("averageSpeed"),
                "averageCadence": lap.get("averageRunCadence"),
                "elevationGain": lap.get("elevationGain"),
                "elevationLoss": lap.get("elevationLoss"),
                "averagePower": lap.get("averagePower"),
                "avgGradeAdjustedSpeed": lap.get("avgGradeAdjustedSpeed"),
            })
        return result
    except Exception as e:
        print(f"  Warning: could not fetch splits for {activity_id}: {e}")
        return []


STRIP_FIELDS = {
    "ownerDisplayName", "ownerFullName", "ownerId",
    "ownerProfileImageUrlSmall", "ownerProfileImageUrlMedium", "ownerProfileImageUrlLarge",
    "startLatitude", "startLongitude", "endLatitude", "endLongitude",
    "userRoles",
}


def strip_sensitive(act: dict) -> dict:
    """Remove PII and location data from an activity."""
    return {k: v for k, v in act.items() if k not in STRIP_FIELDS}


def fetch_activities(garmin: Garmin, days: int) -> list[dict]:
    cutoff = datetime.now() - timedelta(days=days)
    activities = garmin.get_activities(0, 100)  # last 100
    result = []
    for act in activities:
        start = act.get("startTimeLocal") or act.get("startTimeGMT", "")
        try:
            dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if dt.replace(tzinfo=None) < cutoff:
            break
        # Fetch per-km splits for running activities
        type_key = act.get("activityType", {}).get("typeKey", "")
        if type_key in ("running", "trail_running", "treadmill_running"):
            act_id = act.get("activityId")
            if act_id:
                print(f"  Fetching splits for {act.get('activityName', type_key)} ...")
                act["kmSplits"] = fetch_km_splits(garmin, act_id)
        result.append(strip_sensitive(act))
    return result


def fetch_wellness(garmin: Garmin, day: date) -> dict:
    ds = day.isoformat()
    wellness = {}

    try:
        hr = garmin.get_heart_rates(ds)
        wellness["restingHR"] = hr.get("restingHeartRate")
    except Exception:
        wellness["restingHR"] = None

    try:
        hrv = garmin.get_hrv_data(ds)
        summary = hrv.get("hrvSummary", {}) or {}
        wellness["hrvOvernight"] = summary.get("lastNightAvg")
    except Exception:
        wellness["hrvOvernight"] = None

    try:
        sleep = garmin.get_sleep_data(ds)
        sd = sleep.get("dailySleepDTO", {}) or {}
        dur_sec = sd.get("sleepTimeSeconds") or 0
        wellness["sleepHours"] = round(dur_sec / 3600, 1) if dur_sec else None
        wellness["sleepScore"] = sd.get("sleepScores", {}).get("overall", {}).get("value") if sd.get("sleepScores") else None
    except Exception:
        wellness["sleepHours"] = None
        wellness["sleepScore"] = None

    try:
        bb = garmin.get_body_battery(ds)
        items = bb if isinstance(bb, list) else bb.get("bodyBatteryValuesArray", []) or []
        vals = [v[-1] if isinstance(v, list) else v.get("bodyBatteryValue", 0)
                for v in items if v]
        vals = [v for v in vals if v and v > 0]
        if vals:
            wellness["bodyBatteryLow"] = min(vals)
            wellness["bodyBatteryHigh"] = max(vals)
        else:
            wellness["bodyBatteryLow"] = None
            wellness["bodyBatteryHigh"] = None
    except Exception:
        wellness["bodyBatteryLow"] = None
        wellness["bodyBatteryHigh"] = None

    try:
        stress = garmin.get_stress_data(ds)
        wellness["stressAvg"] = stress.get("overallStressLevel")
    except Exception:
        wellness["stressAvg"] = None

    try:
        steps_data = garmin.get_steps_data(ds)
        if isinstance(steps_data, list):
            wellness["steps"] = sum(s.get("steps", 0) for s in steps_data)
        else:
            wellness["steps"] = steps_data.get("totalSteps")
    except Exception:
        wellness["steps"] = None

    try:
        tr = garmin.get_training_readiness(ds)
        wellness["trainingReadiness"] = tr.get("score") or tr.get("trainingReadinessScore")
    except Exception:
        wellness["trainingReadiness"] = None

    wellness["date"] = ds
    return wellness


# ── formatting ────────────────────────────────────────────────────────────────

def format_wellness_md(w: dict) -> str:
    lines = [f"# Garmin wellness {w['date']}"]
    if w.get("restingHR"):
        lines.append(f"- Resting HR: {w['restingHR']} bpm")
    if w.get("hrvOvernight"):
        lines.append(f"- HRV (overnight): {w['hrvOvernight']} ms")
    if w.get("sleepHours"):
        s = f"- Sleep: {w['sleepHours']} h"
        if w.get("sleepScore"):
            s += f" (score {w['sleepScore']})"
        lines.append(s)
    if w.get("bodyBatteryLow") and w.get("bodyBatteryHigh"):
        lines.append(f"- Body battery: {w['bodyBatteryLow']} -> {w['bodyBatteryHigh']}")
    if w.get("stressAvg"):
        lines.append(f"- Stress (avg): {w['stressAvg']}")
    if w.get("steps"):
        lines.append(f"- Steps: {w['steps']}")
    if w.get("trainingReadiness"):
        lines.append(f"- Training readiness: {w['trainingReadiness']}")
    return "\n".join(lines) + "\n"


def format_activity_md(act: dict) -> str:
    name = act.get("activityName", "Activity")
    atype = act.get("activityType", {}).get("typeKey", "unknown")
    start = act.get("startTimeLocal", "")[:16]
    dur_sec = act.get("duration", 0)
    dur_min = round(dur_sec / 60, 1) if dur_sec else 0
    dist_m = act.get("distance", 0) or 0
    dist_km = round(dist_m / 1000, 2)
    avg_hr = act.get("averageHR")
    max_hr = act.get("maxHR")
    calories = act.get("calories")
    avg_pace_sec = act.get("averageSpeed")

    lines = [f"# {name}"]
    lines.append(f"- Type: {atype}")
    lines.append(f"- Start: {start}")
    lines.append(f"- Duration: {dur_min} min")
    if dist_km > 0:
        lines.append(f"- Distance: {dist_km} km")
    if avg_pace_sec and dist_km > 0 and atype in ("running", "trail_running", "treadmill_running", "walking"):
        try:
            pace = (1000 / avg_pace_sec) / 60
            pace_min = int(pace)
            pace_s = int((pace - pace_min) * 60)
            lines.append(f"- Avg pace: {pace_min}:{pace_s:02d} /km")
        except (ZeroDivisionError, ValueError):
            pass
    if avg_hr:
        lines.append(f"- Avg HR: {avg_hr} bpm")
    if max_hr:
        lines.append(f"- Max HR: {max_hr} bpm")
    if calories:
        lines.append(f"- Calories: {calories}")
    return "\n".join(lines) + "\n"


def activity_filename(act: dict) -> str:
    start = act.get("startTimeLocal", "")[:10]
    atype = act.get("activityType", {}).get("typeKey", "activity")
    aid = act.get("activityId", "")
    return f"{start}-{atype}-{aid}.md"


# ── sinks ─────────────────────────────────────────────────────────────────────

def write_files(activities: list[dict], wellness_days: list[dict], out_dir: Path) -> None:
    daily_dir = out_dir / "daily"
    act_dir = out_dir / "activities"
    daily_dir.mkdir(parents=True, exist_ok=True)
    act_dir.mkdir(parents=True, exist_ok=True)

    for w in wellness_days:
        (daily_dir / f"{w['date']}.md").write_text(format_wellness_md(w))

    for act in activities:
        (act_dir / activity_filename(act)).write_text(format_activity_md(act))

    # combined JSON
    data_file = out_dir / "data.json"
    existing = {}
    if data_file.exists():
        try:
            existing = json.loads(data_file.read_text())
        except json.JSONDecodeError:
            existing = {}

    existing_wellness = {w["date"]: w for w in existing.get("wellness", [])}
    for w in wellness_days:
        existing_wellness[w["date"]] = w
    existing_acts = {a.get("activityId"): a for a in existing.get("activities", [])}
    for a in activities:
        existing_acts[a.get("activityId")] = a

    combined = {
        "last_sync": datetime.now(timezone.utc).isoformat(),
        "wellness": sorted(existing_wellness.values(), key=lambda x: x["date"], reverse=True),
        "activities": sorted(existing_acts.values(),
                             key=lambda x: x.get("startTimeLocal", ""), reverse=True),
    }
    data_file.write_text(json.dumps(combined, indent=2, default=str))
    print(f"Wrote {len(wellness_days)} daily notes, {len(activities)} activity notes to {out_dir}/")


def post_to_endpoint(activities: list[dict], wellness_days: list[dict]) -> None:
    import urllib.request
    url = os.environ["GARMIN_INGEST_URL"]
    secret = os.environ.get("GARMIN_INGEST_SECRET", "")
    payload = json.dumps({"activities": activities, "wellness": wellness_days}, default=str).encode()
    req = urllib.request.Request(url, data=payload, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {secret}",
    })
    with urllib.request.urlopen(req) as resp:
        print(f"POST {url} → {resp.status}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Sync Garmin data")
    parser.add_argument("--login", action="store_true", help="Login and save token, then exit")
    parser.add_argument("--mfa", type=str, default=None, help="2FA code for login")
    parser.add_argument("--days", type=int, default=3, help="Number of days to pull (default 3)")
    parser.add_argument("--sink", choices=["files", "supabase", "both"], default="files",
                        help="Where to send data (default: files)")
    parser.add_argument("--out", type=str, default="./garmin", help="Output directory for files sink")
    parser.add_argument("--dry-run", action="store_true", help="Print data without writing")
    args = parser.parse_args()

    if args.login:
        email = os.environ.get("GARMIN_EMAIL")
        password = os.environ.get("GARMIN_PASSWORD")
        if not email or not password:
            sys.exit("Set GARMIN_EMAIL and GARMIN_PASSWORD environment variables before --login")
        client = do_login(email, password, mfa_code=args.mfa)
        print(f"Logged in as {client.display_name}")
        # Print base64 token bundle for GitHub Actions use
        import base64
        token_data = client.client.dumps()
        if token_data:
            print(f"\nToken bundle (for GitHub Actions secret GARMIN_TOKEN_B64):\n{base64.b64encode(token_data.encode()).decode()}")
        return

    client = get_client()
    print(f"Connected as {client.display_name}")

    # Fetch wellness for each day
    today = date.today()
    wellness_days = []
    for i in range(args.days):
        day = today - timedelta(days=i)
        print(f"  Fetching wellness for {day} ...")
        wellness_days.append(fetch_wellness(client, day))

    # Fetch activities
    print(f"  Fetching activities (last {args.days} days) ...")
    activities = fetch_activities(client, args.days)
    print(f"  Found {len(activities)} activities")

    if args.dry_run:
        for w in wellness_days:
            print()
            print(format_wellness_md(w))
        for act in activities:
            print()
            print(format_activity_md(act))
        return

    out_dir = Path(args.out)

    if args.sink in ("files", "both"):
        write_files(activities, wellness_days, out_dir)

    if args.sink in ("supabase", "both"):
        post_to_endpoint(activities, wellness_days)

    print("Done.")


if __name__ == "__main__":
    main()
