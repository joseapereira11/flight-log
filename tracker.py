#!/usr/bin/env python3
"""
Aircraft takeoff/landing tracker.

Polls free, unfiltered ADS-B feeds for one aircraft (by ICAO hex), detects
takeoffs and landings, works out the departure/arrival airport, sends a
Telegram alert and logs each completed flight to flights.csv.

Designed to run every few minutes (e.g. GitHub Actions cron). State between
runs lives in state.json.
"""
import csv
import json
import math
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------- settings
REGISTRATION = os.environ.get("TRACK_REG", "PS-SRN")
HEX = os.environ.get("TRACK_HEX", "e4a0c5").lower()
LOCAL_TZ = ZoneInfo(os.environ.get("TRACK_TZ", "America/Sao_Paulo"))

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state.json")
LOG_FILE = os.path.join(HERE, "flights.csv")
AIRPORTS_FILE = os.path.join(HERE, "airports.csv")

# Free ADS-B aggregators that do NOT hide private/blocked aircraft.
SOURCES = [
    ("adsb.fi", f"https://opendata.adsb.fi/api/v2/hex/{HEX}"),
    ("adsb.lol", f"https://api.adsb.lol/v2/hex/{HEX}"),
    ("airplanes.live", f"https://api.airplanes.live/v2/hex/{HEX}"),
]
MAP_LINK = f"https://globe.adsb.fi/?icao={HEX}"

AIRBORNE_MIN_GS = 50          # kt; below this with no altitude we treat as ground
SIGNAL_LOSS_MIN = 25          # minutes without data before judging a lost signal
LOW_ALT_FT = 6000             # last seen below this + lost => probably landed
GIVE_UP_HOURS = 18            # lost at cruise this long => mark track lost
HEARTBEAT_DAYS = 7            # touch state weekly so GitHub keeps the cron alive


# ---------------------------------------------------------------- helpers
def now_ts():
    return time.time()


def fmt_time(ts):
    utc = datetime.fromtimestamp(ts, timezone.utc)
    local = utc.astimezone(LOCAL_TZ)
    return f"{local:%d/%m %H:%M} ({LOCAL_TZ.key.split('/')[-1].replace('_', ' ')}) · {utc:%H:%M}Z"


def fmt_dur(seconds):
    m = int(round(seconds / 60))
    return f"{m // 60}h{m % 60:02d}"


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


_AIRPORTS = None


def load_airports():
    global _AIRPORTS
    if _AIRPORTS is None:
        with open(AIRPORTS_FILE, encoding="utf-8") as f:
            _AIRPORTS = [
                {**r, "lat": float(r["lat"]), "lon": float(r["lon"])}
                for r in csv.DictReader(f)
            ]
    return _AIRPORTS


def nearest_airport(lat, lon):
    """Best guess at the airport for a position. Returns (label, code, km)."""
    if lat is None or lon is None:
        return ("unknown location", "", None)
    # cheap bounding-box prefilter (~0.5 deg) before exact distances
    cands = [a for a in load_airports()
             if abs(a["lat"] - lat) < 0.5 and abs(a["lon"] - lon) < 0.5 / max(math.cos(math.radians(lat)), 0.2)]
    scored = sorted(((haversine_km(lat, lon, a["lat"], a["lon"]), a) for a in cands), key=lambda x: x[0])
    if not scored:
        return (f"near {lat:.2f}, {lon:.2f}", "", None)
    d0, a0 = scored[0]
    choice = None
    if d0 <= 5:                                   # basically on top of it
        choice = (d0, a0)
    else:                                         # prefer a real airport for a jet
        big = [(d, a) for d, a in scored if a["type"] in ("l", "m") and d <= 25]
        if big:
            choice = big[0]
        elif d0 <= 25:
            choice = (d0, a0)
    if not choice:
        return (f"near {a0['city'] or a0['name']} ({lat:.2f}, {lon:.2f})", "", d0)
    d, a = choice
    code = a["code"] + (f"/{a['iata']}" if a["iata"] else "")
    city = f", {a['city']}" if a["city"] and a["city"] not in a["name"] else ""
    label = f"{code} – {a['name']}{city} ({a['country']})"
    if d > 5:
        label += f" ~{d:.0f} km"
    return (label, a["code"], d)


def fetch_aircraft():
    """Return the freshest aircraft record across sources, or None if not seen.
    Raises RuntimeError only if every source failed (network problem)."""
    best, errors = None, []
    for name, url in SOURCES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "aircraft-tracker/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.load(r)
            for ac in data.get("ac") or []:
                if ac.get("hex", "").lower().lstrip("~") != HEX:
                    continue
                if ac.get("lat") is None and ac.get("lastPosition"):
                    lp = ac["lastPosition"]
                    ac["lat"], ac["lon"] = lp.get("lat"), lp.get("lon")
                    ac["seen_pos"] = lp.get("seen_pos", ac.get("seen", 0))
                age = ac.get("seen_pos", ac.get("seen", 0)) or 0
                ac["_source"], ac["_ts"] = name, data.get("now", now_ts() * 1000) / 1000 - age
                if best is None or ac["_ts"] > best["_ts"]:
                    best = ac
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")
    if best is None and len(errors) == len(SOURCES):
        raise RuntimeError("; ".join(errors))
    return best


def is_airborne(ac):
    alt = ac.get("alt_baro")
    if alt == "ground":
        return False
    gs = ac.get("gs") or 0
    if isinstance(alt, (int, float)):
        return gs > AIRBORNE_MIN_GS or alt > 1500
    return gs > AIRBORNE_MIN_GS


def snapshot(ac):
    alt = ac.get("alt_baro")
    return {
        "ts": ac["_ts"],
        "lat": ac.get("lat"),
        "lon": ac.get("lon"),
        "alt": 0 if alt == "ground" else alt,
        "gs": ac.get("gs"),
        "callsign": (ac.get("flight") or "").strip(),
        "source": ac["_source"],
    }


def send(text):
    print("ALERT:\n" + text + "\n")
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("(Telegram not configured; alert printed only)")
        return
    body = json.dumps({"chat_id": chat, "text": text, "parse_mode": "HTML",
                       "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                 data=body, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:  # noqa: BLE001
        print(f"Telegram send failed: {e}", file=sys.stderr)


def log_flight(dep, arr, note=""):
    new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["registration", "callsign", "from_code", "from", "departed_utc",
                        "to_code", "to", "arrived_utc", "duration", "note"])
        iso = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M") if ts else ""
        w.writerow([REGISTRATION, dep.get("callsign", ""), dep.get("code", ""), dep.get("label", ""),
                    iso(dep.get("ts")), arr.get("code", ""), arr.get("label", ""), iso(arr.get("ts")),
                    fmt_dur(arr["ts"] - dep["ts"]) if dep.get("ts") and arr.get("ts") else "", note])


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"status": "unknown"}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.write("\n")


# ---------------------------------------------------------------- core logic
def departure_msg(dep, cur, note=""):
    cs = f" as <b>{cur['callsign']}</b>" if cur.get("callsign") else ""
    alt = f"{cur['alt']:,} ft" if isinstance(cur.get("alt"), (int, float)) else "n/a"
    return (f"🛫 <b>{REGISTRATION} took off</b>{cs}\n"
            f"From: {dep['label']}\n"
            f"Time: {fmt_time(dep['ts'])}\n"
            f"Now: {alt}, {cur.get('gs') or '?'} kt\n"
            f"{note}"
            f'<a href="{MAP_LINK}">Live map</a>')


def arrival_msg(dep, arr, note=""):
    frm = dep.get("label", "unknown") if dep else "unknown"
    dur = f"\nFlight time: {fmt_dur(arr['ts'] - dep['ts'])}" if dep and dep.get("ts") else ""
    return (f"🛬 <b>{REGISTRATION} landed</b>\n"
            f"At: {arr['label']}\n"
            f"Time: {fmt_time(arr['ts'])}\n"
            f"From: {frm}{dur}\n"
            f"{note}"
            f'<a href="{MAP_LINK}">Map</a>')


def process(state, ac, now):
    """Advance the state machine one step. Returns the new state."""
    status = state.get("status", "unknown")
    last = state.get("last")

    if ac is not None and ac.get("lat") is not None:
        cur = snapshot(ac)
        airborne = is_airborne(ac)

        if airborne:
            if status in ("ground", "unknown", "lost"):
                # TAKEOFF. Departure airport: last ground position if recent,
                # else the current position if still low, else unknown.
                if (status == "ground" and last and
                        haversine_km(last["lat"], last["lon"], cur["lat"], cur["lon"]) < 150):
                    label, code, _ = nearest_airport(last["lat"], last["lon"])
                    dep_ts, note = cur["ts"], ""
                elif isinstance(cur["alt"], (int, float)) and cur["alt"] < 8000:
                    label, code, _ = nearest_airport(cur["lat"], cur["lon"])
                    dep_ts, note = cur["ts"], ""
                else:
                    near, _, _ = nearest_airport(cur["lat"], cur["lon"])
                    label, code, dep_ts = "unknown (first picked up in flight)", "", cur["ts"]
                    note = f"First seen at cruise, {near}\n"
                dep = {"label": label, "code": code, "ts": dep_ts, "callsign": cur["callsign"]}
                send(departure_msg(dep, cur, note))
                state.update(status="air", departure=dep, lost_notified=False)
            elif status == "air" and state.get("lost_notified"):
                gap = now - last["ts"] if last else 0
                state["lost_notified"] = False
                send(f"📡 <b>{REGISTRATION}</b> back in coverage after {fmt_dur(gap)}, "
                     f"{cur['alt']:,} ft – position: {nearest_airport(cur['lat'], cur['lon'])[0]}\n"
                     f'<a href="{MAP_LINK}">Live map</a>')
            if cur["callsign"] and state.get("departure") and not state["departure"].get("callsign"):
                state["departure"]["callsign"] = cur["callsign"]
        else:  # on the ground
            if status == "air":
                label, code, _ = nearest_airport(cur["lat"], cur["lon"])
                arr = {"label": label, "code": code, "ts": cur["ts"]}
                send(arrival_msg(state.get("departure"), arr))
                log_flight(state.get("departure") or {}, arr)
                state["departure"] = None
            elif status in ("unknown", "lost"):
                label = nearest_airport(cur["lat"], cur["lon"])[0]
                print(f"Tracking started: {REGISTRATION} on the ground at {label}")
            # while parked, only record a new position if it actually moved
            # (keeps state.json – and the commit history – quiet)
            if (status == "ground" and last and last.get("lat") is not None and
                    haversine_km(last["lat"], last["lon"], cur["lat"], cur["lon"]) < 3):
                state.update(status="ground", lost_notified=False)
                return state
            state.update(status="ground", lost_notified=False)
        state["last"] = cur
        return state

    # ---- not currently seen ----
    if status == "air" and last:
        silent_min = (now - last["ts"]) / 60
        last_alt = last["alt"] if isinstance(last.get("alt"), (int, float)) else 99999
        if silent_min >= SIGNAL_LOSS_MIN and last_alt < LOW_ALT_FT:
            # disappeared low => landed where coverage doesn't reach the ground
            label, code, _ = nearest_airport(last["lat"], last["lon"])
            arr = {"label": label, "code": code, "ts": last["ts"]}
            send(arrival_msg(state.get("departure"), arr,
                             f"(signal lost at {last_alt:,} ft on approach; time = last contact)\n"))
            log_flight(state.get("departure") or {}, arr, "estimated from signal loss")
            state.update(status="ground", departure=None, lost_notified=False)
        elif silent_min >= GIVE_UP_HOURS * 60:
            send(f"❔ <b>{REGISTRATION}</b>: no signal for {GIVE_UP_HOURS}h since it was at "
                 f"{last_alt:,} ft (position: {nearest_airport(last['lat'], last['lon'])[0]}). "
                 f"Landing not observed; will alert on next sighting.")
            log_flight(state.get("departure") or {}, {"label": "unknown", "ts": None}, "track lost")
            state.update(status="lost", departure=None, lost_notified=False)
        elif silent_min >= SIGNAL_LOSS_MIN and not state.get("lost_notified"):
            # out of coverage at altitude (e.g. ocean crossing) – stay "air"
            print(f"Out of coverage at {last_alt} ft, {silent_min:.0f} min")
            state["lost_notified"] = True
    return state


def main():
    state = load_state()
    now = now_ts()
    try:
        ac = fetch_aircraft()
    except RuntimeError as e:
        print(f"All sources failed, skipping this run: {e}", file=sys.stderr)
        return
    if "--test" in sys.argv:
        where = (nearest_airport(ac.get("lat"), ac.get("lon"))[0] if ac else "not currently visible")
        send(f"✅ Tracker for <b>{REGISTRATION}</b> is working.\n"
             f"Right now: {'airborne' if ac and is_airborne(ac) else 'on the ground' if ac else 'no signal'}"
             f" – {where}\n<a href=\"{MAP_LINK}\">Map</a>")
    before = json.dumps({k: v for k, v in state.items() if k != "heartbeat"}, sort_keys=True)
    state = process(state, ac, now)
    after = json.dumps({k: v for k, v in state.items() if k != "heartbeat"}, sort_keys=True)

    # Only write when something changed (or weekly heartbeat) to keep commits few.
    if before != after or now - state.get("heartbeat", 0) > HEARTBEAT_DAYS * 86400:
        state["heartbeat"] = now
        save_state(state)
    print(f"{REGISTRATION}: status={state.get('status')} "
          f"seen={'yes via ' + ac['_source'] if ac else 'no'}")


if __name__ == "__main__":
    main()
