#!/usr/bin/env python3
"""Commute traffic tracker for Lahore.

Records driving times with live traffic (Google Routes API) into SQLite around the
clock every day, and suggests the best time to leave inside each commute window.

Commands:
    poll [--force]                       record live traffic in both directions
    predict                              record predicted traffic for the next week
    report [--source live|predicted|all] build commute_heatmap.png and print a summary
    dashboard                            build commute_dashboard.html, an interactive page of trends
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sqlite3
import sys
import time
from contextlib import closing
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
HOME = "Shayyan Furniture, Chah Miran, Lahore, Pakistan"
OFFICE = "04 Old FCC Road, Lahore, Pakistan"

TZ = ZoneInfo("Asia/Karachi")
# Traffic is recorded all day; these windows only decide where departure times are suggested.
MORNING_WINDOW = (dtime(9, 0), dtime(11, 0))  # leaving home
EVENING_WINDOW = (dtime(19, 0), dtime(21, 0))  # leaving the office
SLOT_MINUTES = 15
DAYS = {0, 1, 2, 3, 4, 5, 6}  # days to record and forecast (Monday = 0): every day
PREDICT_DAYS = 7
# TRAFFIC_AWARE_OPTIMAL is Google's most accurate traffic mode; TRAFFIC_AWARE skips some of
# the traffic calculation to answer faster. Both are billed at the same (Pro) rate.
ROUTING_PREFERENCE = "TRAFFIC_AWARE_OPTIMAL"

MAX_RETRIES = 3  # extra attempts after the first on 429/5xx/network errors
BACKOFF_SECONDS = 2  # doubled after every retry: 2s, 4s, 8s
REQUEST_TIMEOUT = 30

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "commute.db"
HEATMAP_PATH = BASE_DIR / "commute_heatmap.png"
DASHBOARD_TEMPLATE = BASE_DIR / "dashboard_template.html"
DASHBOARD_PATH = BASE_DIR / "commute_dashboard.html"
DASHBOARD_DATA_MARKER = "/*COMMUTE_DATA*/null"

ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
FIELD_MASK = "routes.duration,routes.staticDuration,routes.distanceMeters,routes.description"

# direction -> (origin, destination, window)
DIRECTIONS = {
    "to_office": (HOME, OFFICE, MORNING_WINDOW),
    "to_home": (OFFICE, HOME, EVENING_WINDOW),
}
DIRECTION_LABELS = {"to_office": "Home → Office", "to_home": "Office → Home"}
WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

log = logging.getLogger("commute")


# --------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------
def now_local() -> datetime:
    return datetime.now(TZ)


def window_slots(day: date, window: tuple[dtime, dtime]) -> list[datetime]:
    """All departure slots in a window on a given day, both ends included."""
    start, end = window
    slot = datetime.combine(day, start, tzinfo=TZ)
    last = datetime.combine(day, end, tzinfo=TZ)
    slots = []
    while slot <= last:
        slots.append(slot)
        slot += timedelta(minutes=SLOT_MINUTES)
    return slots


def rfc3339_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Google Routes API
# --------------------------------------------------------------------------
def get_api_key() -> str:
    key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    if not key:
        log.error("GOOGLE_MAPS_API_KEY is not set")
        sys.exit(1)
    return key


def parse_seconds(value: str | None) -> int | None:
    # The API returns durations as strings like "1534s".
    if not value:
        return None
    return round(float(value.rstrip("s")))


def error_message(resp: requests.Response) -> str:
    try:
        error = resp.json()["error"]
        return f"{error.get('status', '')} {error.get('message', '')}".strip()
    except (ValueError, KeyError, TypeError, AttributeError):
        return " ".join(resp.text.split())[:300]


def compute_route(
    session: requests.Session,
    api_key: str,
    origin: str,
    destination: str,
    departure: datetime | None = None,
) -> dict | None:
    """Call computeRoutes and return the first route, or None on failure."""
    body = {
        "origin": {"address": origin},
        "destination": {"address": destination},
        "travelMode": "DRIVE",
        "routingPreference": ROUTING_PREFERENCE,
    }
    if departure is not None:
        body["departureTime"] = rfc3339_utc(departure)
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": FIELD_MASK,
    }

    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = session.post(ROUTES_URL, json=body, headers=headers, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            error = f"network error: {exc}"
        else:
            if resp.ok:
                routes = resp.json().get("routes") or []
                if not routes:
                    log.warning("No route returned for %s -> %s", origin, destination)
                    return None
                route = routes[0]
                return {
                    "duration_s": parse_seconds(route.get("duration")),
                    "static_s": parse_seconds(route.get("staticDuration")),
                    "distance_m": route.get("distanceMeters"),
                    "via": route.get("description"),
                }
            error = f"HTTP {resp.status_code}: {error_message(resp)}"
            if resp.status_code != 429 and resp.status_code < 500:
                log.error("Routes API request failed: %s", error)
                return None

        if attempt < MAX_RETRIES:
            delay = BACKOFF_SECONDS * 2**attempt
            log.warning("Routes API %s; retrying in %ss (%d/%d)", error, delay, attempt + 1, MAX_RETRIES)
            time.sleep(delay)
        else:
            log.error("Routes API %s; giving up after %d attempts", error, MAX_RETRIES + 1)
    return None


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------
def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trips (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_at TEXT NOT NULL,
            depart_at   TEXT NOT NULL,
            direction   TEXT NOT NULL,
            source      TEXT NOT NULL,
            duration_s  INTEGER,
            static_s    INTEGER,
            distance_m  INTEGER,
            via         TEXT
        )
        """
    )
    return conn


def save_trip(conn: sqlite3.Connection, depart_at: datetime, direction: str, source: str, route: dict) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO trips (recorded_at, depart_at, direction, source, duration_s, static_s, distance_m, via)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_local().isoformat(timespec="seconds"),
                depart_at.isoformat(timespec="seconds"),
                direction,
                source,
                route["duration_s"],
                route["static_s"],
                route["distance_m"],
                route["via"],
            ),
        )


def record(
    conn: sqlite3.Connection,
    session: requests.Session,
    api_key: str,
    direction: str,
    source: str,
    depart_at: datetime,
    departure: datetime | None,
) -> bool:
    """Fetch and store one trip. Never raises, so one bad call can't stop a run."""
    origin, destination, _ = DIRECTIONS[direction]
    try:
        route = compute_route(session, api_key, origin, destination, departure)
        if route is None or route["duration_s"] is None:
            return False
        save_trip(conn, depart_at, direction, source, route)
    except Exception:
        log.exception("Failed to record %s %s trip departing %s", source, direction, depart_at)
        return False
    log.info(
        "%s %s %s: %.1f min (no traffic %.1f min), %.1f km via %s",
        source,
        direction,
        depart_at.strftime("%a %Y-%m-%d %H:%M"),
        route["duration_s"] / 60,
        (route["static_s"] or 0) / 60,
        (route["distance_m"] or 0) / 1000,
        route["via"] or "?",
    )
    return True


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
def cmd_poll(force: bool) -> int:
    now = now_local()
    if now.weekday() not in DAYS and not force:
        log.info("%s is not a tracked day; nothing to record", now.strftime("%a %H:%M"))
        return 0
    directions = list(DIRECTIONS)

    api_key = get_api_key()
    saved = 0
    with closing(open_db()) as conn, requests.Session() as session:
        for direction in directions:
            saved += record(conn, session, api_key, direction, "live", now, None)
    log.info("Recorded %d of %d live trip(s)", saved, len(directions))
    return 0


def cmd_predict() -> int:
    now = now_local()
    earliest = now + timedelta(minutes=1)  # departureTime must be in the future
    horizon = now + timedelta(days=PREDICT_DAYS)

    jobs = []
    for offset in range(PREDICT_DAYS + 1):
        day = now.date() + timedelta(days=offset)
        if day.weekday() not in DAYS:
            continue
        for direction, (_, _, window) in DIRECTIONS.items():
            jobs += [(direction, s) for s in window_slots(day, window) if earliest <= s <= horizon]
    jobs.sort(key=lambda job: job[1])

    if not jobs:
        log.info("No tracked days in the next %d days", PREDICT_DAYS)
        return 0

    api_key = get_api_key()
    log.info("Requesting %d predicted trips up to %s", len(jobs), horizon.strftime("%a %Y-%m-%d %H:%M"))
    saved = 0
    with closing(open_db()) as conn, requests.Session() as session:
        for direction, slot in jobs:
            saved += record(conn, session, api_key, direction, "predicted", slot, slot)
    log.info("Recorded %d of %d predicted trip(s)", saved, len(jobs))
    return 0


def cmd_report(source: str) -> int:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import pandas as pd

    if not DB_PATH.exists():
        log.error("No database at %s yet; run poll or predict first", DB_PATH)
        return 1

    query = "SELECT depart_at, direction, source, duration_s, static_s, via FROM trips WHERE duration_s IS NOT NULL"
    params: tuple = ()
    if source != "all":
        query += " AND source = ?"
        params = (source,)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        df = pd.read_sql_query(query, conn, params=params)
    if df.empty:
        log.error("No trips for --source %s in %s", source, DB_PATH)
        return 1

    depart = pd.to_datetime(df["depart_at"], utc=True).dt.tz_convert(TZ)
    df["weekday"] = depart.dt.weekday
    df["slot"] = depart.dt.floor(f"{SLOT_MINUTES}min").dt.strftime("%H:%M")
    df["minutes"] = df["duration_s"] / 60
    df["congestion"] = df["duration_s"] / df["static_s"].where(df["static_s"] > 0)
    # Only departures inside each direction's window count (e.g. older readings from wider windows).
    window_bounds = {d: (w[0].strftime("%H:%M"), w[1].strftime("%H:%M")) for d, (_, _, w) in DIRECTIONS.items()}
    df = df[[window_bounds[d][0] <= slot <= window_bounds[d][1] for d, slot in zip(df["direction"], df["slot"])]]
    if df.empty:
        log.error("No %s trips inside the commute windows yet", source)
        return 1

    directions = [d for d in DIRECTIONS if d in set(df["direction"])]
    fig, axes = plt.subplots(
        len(directions), 1, figsize=(15, 3.2 * len(directions) + 1), squeeze=False, layout="constrained"
    )
    cmap = plt.get_cmap("RdYlGn_r")

    for ax, direction in zip(axes[:, 0], directions):
        sub = df[df["direction"] == direction]
        window = DIRECTIONS[direction][2]
        slots = [s.strftime("%H:%M") for s in window_slots(now_local().date(), window)]
        days = sorted(DAYS | set(sub["weekday"]))
        grid = sub.pivot_table(index="weekday", columns="slot", values="minutes", aggfunc="median")
        grid = grid.reindex(index=days, columns=slots)

        values = grid.to_numpy(dtype=float)
        vmin, vmax = sub["minutes"].min(), sub["minutes"].max()
        if vmin == vmax:
            vmin, vmax = vmin - 1, vmax + 1
        norm = matplotlib.colors.Normalize(vmin=vmin, vmax=vmax)
        image = ax.imshow(values, cmap=cmap, norm=norm, aspect="auto")

        ax.set_xticks(range(len(slots)), slots, rotation=45, ha="right", fontsize=9)
        ax.set_yticks(range(len(days)), [WEEKDAY_NAMES[d] for d in days])
        ax.tick_params(length=0)
        for spine in ax.spines.values():
            spine.set_visible(False)
        for y in range(values.shape[0]):
            for x in range(values.shape[1]):
                value = values[y, x]
                if pd.isna(value):
                    continue
                r, g, b, _ = cmap(norm(value))
                text_color = "black" if 0.299 * r + 0.587 * g + 0.114 * b > 0.5 else "white"
                ax.text(x, y, f"{value:.0f}", ha="center", va="center", fontsize=8, color=text_color)

        ax.set_title(
            f"{DIRECTION_LABELS[direction]}: median minutes by departure time ({source}, {len(sub)} trips)",
            loc="left",
            fontsize=11,
        )
        fig.colorbar(image, ax=ax, label="minutes", pad=0.01)

    fig.suptitle(f"Commute times: {HOME.split(',')[0]} ↔ {OFFICE.split(',')[0]}", fontsize=13, x=0.01, ha="left")
    fig.savefig(HEATMAP_PATH, dpi=120)
    plt.close(fig)
    log.info("Saved heatmap to %s", HEATMAP_PATH)

    for direction in directions:
        print_summary(df[df["direction"] == direction], direction, source)
    return 0


def print_summary(sub, direction: str, source: str) -> None:
    stats = (
        sub.groupby("slot")["minutes"]
        .agg(median="median", p90=lambda m: m.quantile(0.9), n="count")
        .sort_values(["median", "p90"])
    )
    best = stats.head(3)
    worst_slot, worst = stats["median"].idxmax(), stats.loc[stats["median"].idxmax()]
    congestion = sub["congestion"].mean()
    routes = sub["via"].fillna("(no description)").value_counts().head(3)

    print()
    print(f"=== {DIRECTION_LABELS[direction]} ({source}, {len(sub)} trips) ===")
    print("Best departure slots:")
    for slot, row in best.iterrows():
        print(f"  {slot}  median {row['median']:5.1f} min   p90 {row['p90']:5.1f} min   (n={row['n']:.0f})")
    print(f"Worst slot:   {worst_slot}  median {worst['median']:5.1f} min   p90 {worst['p90']:5.1f} min   (n={worst['n']:.0f})")
    if not math.isnan(congestion):
        print(f"Average congestion index: {congestion:.2f}x the no-traffic time")
    print("Most common routes:")
    for via, count in routes.items():
        print(f"  {count / len(sub):4.0%}  via {via}")


def cmd_dashboard() -> int:
    if not DB_PATH.exists():
        log.error("No database at %s yet; run poll or predict first", DB_PATH)
        return 1
    with closing(sqlite3.connect(DB_PATH)) as conn:
        rows = conn.execute(
            "SELECT depart_at, direction, source, duration_s, static_s, distance_m, via"
            " FROM trips WHERE duration_s IS NOT NULL ORDER BY depart_at"
        ).fetchall()

    trips = []
    for depart_at, direction, source, duration_s, static_s, distance_m, via in rows:
        local = datetime.fromisoformat(depart_at).astimezone(TZ)
        trips.append([
            local.strftime("%Y-%m-%d"),
            local.weekday(),
            local.hour * 60 + local.minute,
            direction,
            source,
            duration_s,
            static_s,
            distance_m,
            via,
        ])
    data = {
        "generated": now_local().isoformat(timespec="minutes"),
        "slotMinutes": SLOT_MINUTES,
        "windows": {d: [w[0].strftime("%H:%M"), w[1].strftime("%H:%M")] for d, (_, _, w) in DIRECTIONS.items()},
        "routing": ROUTING_PREFERENCE,
        "days": sorted(DAYS),
        "trips": trips,
    }

    template = DASHBOARD_TEMPLATE.read_text(encoding="utf-8")
    if DASHBOARD_DATA_MARKER not in template:
        log.error("%s is missing the %s marker", DASHBOARD_TEMPLATE.name, DASHBOARD_DATA_MARKER)
        return 1
    # "</" is escaped so a route description can never close the <script> tag early.
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    page = template.replace(DASHBOARD_DATA_MARKER, payload, 1)
    head = (
        '<!doctype html>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="robots" content="noindex, nofollow">\n'  # keep the public Pages copy out of search engines
    )
    DASHBOARD_PATH.write_text(head + page, encoding="utf-8")
    log.info("Saved dashboard with %d trips to %s", len(trips), DASHBOARD_PATH)
    return 0


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record and analyse Lahore commute times.")
    commands = parser.add_subparsers(dest="command", required=True)
    poll = commands.add_parser("poll", help="record live traffic in both directions")
    poll.add_argument("--force", action="store_true", help="record even if today is not in DAYS")
    commands.add_parser("predict", help=f"record predicted traffic for the next {PREDICT_DAYS} days")
    report = commands.add_parser("report", help="build the heatmap and print a summary")
    report.add_argument("--source", choices=["live", "predicted", "all"], default="live")
    commands.add_parser("dashboard", help="build an interactive dashboard page of trends")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("matplotlib").setLevel(logging.WARNING)

    if args.command == "poll":
        return cmd_poll(args.force)
    if args.command == "predict":
        return cmd_predict()
    if args.command == "dashboard":
        return cmd_dashboard()
    return cmd_report(args.source)


if __name__ == "__main__":
    sys.exit(main())
