#!/usr/bin/env python3
"""Lab attendance kiosk backend. Runs on Python 3.6+ (the Jetson Nano's Ubuntu 18.04 ships 3.6); stdlib plus pyserial.

  * Serves the kiosk UI in ./ui at http://localhost:8080 and talks to the ESP8266 over USB serial.
  * Every visit is written to a local SQLite file FIRST (durable: WAL + synchronous=FULL), then uploaded to Cloudflare in the
    background whenever the network is up. Nothing is lost while offline; uploads are idempotent (one uuid per visit).
  * Time: stored in UTC. Each timestamp remembers whether the clock was trustworthy when it was taken. If it was not
    (no RTC, no network yet), the stamp is corrected as soon as the clock syncs, using the uptime counter.

  python3 server.py                      run for real
  ATT_SIM=1 python3 server.py            simulate card taps from the browser (no ESP)
  python3 server.py import students.csv  load reg_no,name,department[,card_uid]
  python3 server.py export visits.csv    dump all visits
  python3 server.py admin list           the admin cards (at most 2). They are the only people who can register a new card.
  python3 server.py admin add-last "Name"   make the most recently tapped UNKNOWN card an admin card (tap it first, it shows red)
  python3 server.py admin add UID "Name" / admin remove UID
Settings come from environment variables (setup.sh writes them to /etc/attendance.env). See JETSON_SETUP.md.
"""
import calendar, csv, glob, json, os, re, socketserver, sqlite3, subprocess, sys, threading, time, uuid
import urllib.error, urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB_PATH = os.environ.get("ATT_DB", str(Path.home() / "attendance.db"))
SERIAL_PORT = os.environ.get("ATT_PORT", "")          # blank = autodetect /dev/ttyUSB*, /dev/ttyACM*
HTTP_PORT = int(os.environ.get("ATT_HTTP", "8080"))
SIM = os.environ.get("ATT_SIM") == "1"
LAB_NAME = os.environ.get("ATT_LAB", "Computer Lab")
API_URL = os.environ.get("ATT_API_URL", "").rstrip("/")   # e.g. https://attendance.example.com ; blank = never upload
READER_ID = os.environ.get("ATT_READER_ID", "lab-kiosk")
READER_KEY = os.environ.get("ATT_READER_KEY", "")
SYNC_EVERY = int(os.environ.get("ATT_SYNC_EVERY", "20"))
STATE_DIR = Path(os.environ.get("ATT_STATE_DIR", "/var/lib/attendance"))   # rtc_ok stamp written by rtc_ds3231.py save
RUN_DIR = Path(os.environ.get("ATT_RUN_DIR", "/run/attendance"))           # rtc_loaded stamp written by rtc_ds3231.py load
CLOCK_FILE = os.environ.get("ATT_CLOCK_FILE", "")      # tests only: a file containing 1 or 0 overrides the clock check
FLOOR_EVERY = int(os.environ.get("ATT_FLOOR_EVERY", "5"))   # seconds between saves of the "last known time"
FLOOR_FILE = STATE_DIR / "last_seen"

PURPOSES = [("lab", "Lab work"), ("project", "Project work"), ("class", "Class / Practical"),
            ("research", "Research"), ("meeting", "Meeting"), ("other", "Other")]
TIMEOUT = {"purpose": 25, "unknown": 60, "admin_wait": 60, "admin_form": 180, "in": 6, "out": 7, "notice": 5}
MAX_ADMINS = int(os.environ.get("ATT_MAX_ADMINS", "2"))
LED_REFRESH = 3.0   # seconds: the ring mode is re-sent this often (the ESP falls back to idle if it hears nothing for 10 s)
DEBOUNCE = 4        # seconds: same card tapped again is ignored
UTC_FMT = "%Y-%m-%dT%H:%M:%SZ"


# ---------------- time helpers ----------------
# When the OS clock is wrong and cannot be fixed yet, the app borrows a better time and keeps it as an offset (seconds) on top of the
# OS clock. src: None | "last-known" (the saved floor). Never touches the OS clock.
_OFFSET = {"v": 0, "src": None}
def os_now(): return datetime.utcnow().replace(microsecond=0)
def utcnow():
    o = _OFFSET["v"]
    return os_now() + timedelta(seconds=o) if o else os_now()
def iso(dt): return dt.strftime(UTC_FMT)
def parse(s): return datetime.strptime(s.rstrip("Z")[:19], "%Y-%m-%dT%H:%M:%S")
def to_local(dt_utc): return datetime.fromtimestamp(calendar.timegm(dt_utc.timetuple()))
def fmt_t(dt_utc): return to_local(dt_utc).strftime("%I:%M %p").lstrip("0")
def fmt_dur(sec):
    m = int(sec // 60)
    if m < 1: return "less than a minute"
    h, m = divmod(m, 60)
    return "%d h %d min" % (h, m) if h else "%d min" % m

def uptime():
    """Seconds since boot (keeps counting through clock jumps, which is the point)."""
    try: return float(open("/proc/uptime").read().split()[0])
    except Exception: return time.monotonic()

_BOOT = None
def boot_id():
    global _BOOT
    if _BOOT is None:
        try: _BOOT = open("/proc/sys/kernel/random/boot_id").read().strip()
        except Exception: _BOOT = "dev-%d" % int(time.time())   # no /proc (macOS): one id per process
    return _BOOT

def run_text(cmd):
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    out, _ = p.communicate(timeout=5)
    return out


class Kiosk:
    def __init__(self):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL"); self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS people(
            key TEXT PRIMARY KEY, reg_no TEXT, card_uid TEXT, name TEXT, department TEXT, registered INTEGER DEFAULT 1)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS visits(
            id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT, name TEXT, department TEXT, purpose TEXT, in_time TEXT, out_time TEXT)""")
        self.db.execute("CREATE TABLE IF NOT EXISTS admins(card_uid TEXT PRIMARY KEY, name TEXT NOT NULL, added_at REAL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS unknown_taps(id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT, ident TEXT, ts REAL)")
        if "registered_by" not in set(r[1] for r in self.db.execute("PRAGMA table_info(people)")):
            self.db.execute("ALTER TABLE people ADD COLUMN registered_by TEXT")
        have = set(r[1] for r in self.db.execute("PRAGMA table_info(visits)"))
        for col, decl in [("uuid", "TEXT"), ("purpose_id", "TEXT"), ("in_mono", "REAL"), ("in_boot", "TEXT"), ("in_q", "TEXT"),
                          ("out_mono", "REAL"), ("out_boot", "TEXT"), ("out_q", "TEXT"),
                          ("rev", "INTEGER DEFAULT 1"), ("synced_rev", "INTEGER DEFAULT 0")]:
            if col not in have: self.db.execute("ALTER TABLE visits ADD COLUMN %s %s" % (col, decl))
        for (i,) in self.db.execute("SELECT id FROM visits WHERE uuid IS NULL").fetchall():
            self.db.execute("UPDATE visits SET uuid=? WHERE id=?", (uuid.uuid4().hex, i))
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_visits_uuid ON visits(uuid)")
        self.db.commit()
        self.screen, self.data, self.expires, self.ver = "idle", {}, 0, 0
        self.last_ident, self.last_ts = "", 0.0
        self.tap = None                       # time stamp of the card tap that opened the current screen
        self.hb = 0.0
        self.send = lambda cmd: None          # set by Reader
        self._clock, self._clock_ts, self._os_via = True, 0.0, None
        self.sync_ok_at, self.sync_error, self.sync_online = None, "", False
        self.wake = threading.Event()         # poke the uploader right after a change
        self.led_hold = (None, 0.0)           # (mode, until): a short-lived ring colour such as the green after a success
        self._led_last, self._led_sent = None, 0.0

    # ---- clock ----
    def _os_ok(self):
        """True when the operating system's own clock can be trusted: network time is synced, or the DS3231 was loaded at boot and has
        been written from a synced clock before. Cached for 5 s."""
        if time.time() - self._clock_ts < 5: return self._clock
        self._clock_ts = time.time()
        ok, via = None, None
        try:
            for line in run_text(["timedatectl", "status"]).splitlines():
                if "synchronized:" in line: ok = line.split(":", 1)[1].strip() == "yes"
        except Exception: pass
        if ok is None: ok = True              # no timedatectl (macOS dev machine): assume fine
        if ok: via = "network"
        elif (RUN_DIR / "rtc_loaded").exists() and (STATE_DIR / "rtc_ok").exists(): ok, via = True, "rtc"
        self._clock, self._os_via = ok, via
        return ok

    def clock_ok(self):
        """True when timestamps can be trusted right now."""
        if CLOCK_FILE and os.path.exists(CLOCK_FILE):          # tests only
            return open(CLOCK_FILE).read().strip() == "1"
        if self._os_ok():
            if _OFFSET["src"]:                                 # the OS clock is trustworthy again: drop the borrowed time
                _OFFSET.update(v=0, src=None); self.wake.set()
            return True
        return False

    def clock_source(self):
        if self._os_ok(): return self._os_via or "network"
        return "last-known" if _OFFSET["src"] == "last-known" else "unset"

    def load_floor(self):
        """At start-up, if the OS clock is behind the last time we remember, never go back in time: borrow the remembered time.
        It is a lower bound only (the real time is later by however long the power was off), so stamps stay flagged as unsure."""
        try: f = parse(FLOOR_FILE.read_text().strip())
        except Exception: return
        if self._os_ok(): return
        behind = int((f - os_now()).total_seconds())
        if behind > 0:
            _OFFSET.update(v=behind, src="last-known")
            print("clock was behind the last known time; starting from %s UTC" % iso(f), flush=True)

    def stamp(self):
        return {"wall": utcnow(), "mono": uptime(), "boot": boot_id(), "q": "ok" if self.clock_ok() else "unsynced"}

    def fix_times(self):
        """Once the clock is trustworthy, re-date timestamps taken earlier in THIS boot while it was not."""
        if not self.clock_ok(): return 0
        with self.lock:
            rows = self.db.execute("SELECT id,in_mono,in_boot,in_q,out_mono,out_boot,out_q FROM visits "
                                   "WHERE in_q='unsynced' OR out_q='unsynced'").fetchall()
            now_w, now_m, bid, n = utcnow(), uptime(), boot_id(), 0
            for (i, im, ib, iq, om, ob, oq) in rows:
                sets, vals = [], []
                if iq == "unsynced" and ib == bid:
                    sets += ["in_time=?", "in_q='corrected'"]; vals.append(iso(now_w - timedelta(seconds=now_m - im)))
                if oq == "unsynced" and ob == bid:
                    sets += ["out_time=?", "out_q='corrected'"]; vals.append(iso(now_w - timedelta(seconds=now_m - om)))
                if sets:
                    self.db.execute("UPDATE visits SET %s, rev=rev+1 WHERE id=?" % ", ".join(sets), vals + [i]); n += 1
            if n: self.db.commit(); self.wake.set()
            return n

    # ---- state helpers ----
    def _set(self, screen, **data):
        self.screen, self.data, self.ver = screen, data, self.ver + 1
        self.expires = time.time() + TIMEOUT[screen] if screen in TIMEOUT else 0

    def _expire(self):
        if self.expires and time.time() > self.expires: self._set("idle")

    def _hold(self, mode, seconds): self.led_hold = (mode, time.time() + seconds)

    def desired_led(self):
        """The ring mode the ESP should show. IDLE chase | NEW yellow (an admin is registering) | OK green | DENY red (card not registered).
        Blue 'scanning' is added by the ESP itself the moment it reads a card."""
        with self.lock:
            self._expire()
            if self.led_hold[0] and time.time() < self.led_hold[1]: return self.led_hold[0]
            if self.screen == "unknown": return "DENY"
            if self.screen in ("admin_wait", "admin_form"): return "NEW"
            return "IDLE"

    def led_tick(self):
        m = self.desired_led(); now = time.time()
        if m != self._led_last or now - self._led_sent >= LED_REFRESH:
            self.send("LED " + m); self._led_last, self._led_sent = m, now

    def admin_name(self, *ids):
        for i in ids:
            if not i: continue
            r = self.db.execute("SELECT name FROM admins WHERE card_uid=?", (i,)).fetchone()
            if r: return r[0]
        return None

    def admin_list(self): return self.db.execute("SELECT card_uid,name FROM admins ORDER BY added_at").fetchall()

    def admin_add(self, uid, name):
        uid, name = uid.strip().upper(), " ".join(name.split())
        if not uid or not name: return False, "A card id and a name are needed."
        if self.db.execute("SELECT 1 FROM people WHERE card_uid=? OR key=?", (uid, uid)).fetchone(): return False, "That card belongs to a registered person."
        have = [r[0] for r in self.admin_list()]
        if uid not in have and len(have) >= MAX_ADMINS: return False, "There are already %d admins. Remove one first." % MAX_ADMINS
        self.db.execute("INSERT OR REPLACE INTO admins(card_uid,name,added_at) VALUES(?,?,?)", (uid, name, time.time())); self.db.commit()
        return True, ""

    def _log_unknown(self, ident, uid):
        self.db.execute("INSERT INTO unknown_taps(uid,ident,ts) VALUES(?,?,?)", (uid, ident, time.time()))
        self.db.execute("DELETE FROM unknown_taps WHERE id NOT IN (SELECT id FROM unknown_taps ORDER BY id DESC LIMIT 30)")
        self.db.commit()

    def pending(self):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM visits WHERE rev>synced_rev").fetchone()[0]

    def snapshot(self):
        with self.lock:
            self._expire()
            inside = self.db.execute("SELECT COUNT(*) FROM visits WHERE out_time IS NULL").fetchone()[0]
            since = iso(utcnow() - timedelta(hours=24))
            rows = self.db.execute("SELECT id,name,purpose,in_time,out_time FROM visits WHERE in_time>=? ORDER BY id DESC LIMIT 9",
                                   (since,)).fetchall()
            board = [{"id": r[0], "name": r[1], "purpose": r[2], "in": fmt_t(parse(r[3])),
                      "out": fmt_t(parse(r[4])) if r[4] else None} for r in rows]
            pend = self.pending()
            return {"ver": self.ver, "screen": self.screen, "data": self.data,
                    "remaining": max(0, round(self.expires - time.time(), 2)) if self.expires else 0,
                    "total": TIMEOUT.get(self.screen, 0), "inside": inside, "board": board,
                    "reader": SIM or (time.time() - self.hb < 12), "clock_ok": self.clock_ok(), "clock_source": self.clock_source(),
                    "sync": {"configured": bool(API_URL and READER_KEY), "pending": pend, "online": self.sync_online,
                             "last_ok": fmt_t(self.sync_ok_at) if self.sync_ok_at else None},
                    "led": self.desired_led(), "admins": len(self.admin_list()),
                    "sim": SIM, "lab": LAB_NAME, "purposes": [{"id": i, "label": l} for i, l in PURPOSES]}

    # ---- events ----
    def card(self, uid, reg=""):
        ident = (reg or uid or "").strip().upper()
        if not ident: return
        with self.lock:
            if ident == self.last_ident and time.time() - self.last_ts < DEBOUNCE: return
            self.last_ident, self.last_ts = ident, time.time()
            self.led_hold = (None, 0.0); self._led_sent = 0.0      # re-send the ring mode at once so the ESP can leave its blue 'scanning' state
            tap = self.stamp()
            # 1. an ADMIN card never marks attendance: it unlocks registration
            admin = self.admin_name(uid, ident)
            if admin:
                if self.screen == "unknown":                         # a card was just refused: this admin registers it
                    d = self.data; self.tap = tap
                    self._set("admin_form", ident=d["ident"], uid=d["uid"], admin=admin)
                elif self.screen != "admin_form":                    # nothing pending: wait for the new card
                    self._set("admin_wait", admin=admin)
                return
            p = self.db.execute("SELECT key,name,department FROM people WHERE reg_no=? OR card_uid=? OR key IN (?,?)",
                                (ident, uid, ident, uid)).fetchone()
            # 2. a card that is not in the database
            if not p:
                self._log_unknown(ident, uid)
                if self.screen == "admin_form": return              # an admin is busy registering somebody else
                self.tap = tap
                if self.screen == "admin_wait": self._set("admin_form", ident=ident, uid=uid, admin=self.data.get("admin", ""))
                else: self._set("unknown", ident=ident, uid=uid, admins=len(self.admin_list()))
                return
            if self.screen == "admin_form": return                  # keep the admin's form; this person can tap again afterwards
            # 3. a registered person: leaving, or arriving
            key = p[0]
            v = self.db.execute("SELECT id,in_time,name,in_mono,in_boot FROM visits WHERE key=? AND out_time IS NULL ORDER BY id DESC LIMIT 1",
                                (key,)).fetchone()
            if v:                                                   # leaving
                self.db.execute("UPDATE visits SET out_time=?, out_mono=?, out_boot=?, out_q=?, rev=rev+1 WHERE id=?",
                                (iso(tap["wall"]), tap["mono"], tap["boot"], tap["q"], v[0]))
                self.db.commit()
                same_boot = v[4] == tap["boot"] and v[3] is not None
                secs = (tap["mono"] - v[3]) if same_boot else (tap["wall"] - parse(v[1])).total_seconds()
                self._set("out", name=v[2], time_in=fmt_t(parse(v[1])), time_out=fmt_t(tap["wall"]), duration=fmt_dur(secs))
                self._hold("OK", 3); self.wake.set()
            else:
                self.tap = tap
                self._set("purpose", key=p[0], name=p[1], dept=p[2], tapped=fmt_t(tap["wall"])); self._hold("OK", 3)

    def choose_purpose(self, purpose_id):
        with self.lock:
            if self.screen != "purpose": return False, "No visit in progress"
            label = dict(PURPOSES).get(purpose_id)
            if not label: return False, "Unknown purpose"
            d, tap = self.data, self.tap or self.stamp()
            self.db.execute("INSERT INTO visits(uuid,key,name,department,purpose,purpose_id,in_time,in_mono,in_boot,in_q,rev,synced_rev) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,1,0)",
                            (uuid.uuid4().hex, d["key"], d["name"], d["dept"], label, purpose_id,
                             iso(tap["wall"]), tap["mono"], tap["boot"], tap["q"]))
            self.db.commit()
            self._set("in", name=d["name"], purpose=label, time_in=fmt_t(tap["wall"]))
            self.wake.set()
            return True, ""

    def register(self, reg_no, name, dept):
        """An admin saves the new person: registration number + name + department, tied to the card that was just refused."""
        reg = "".join(reg_no.split()).upper()
        name, dept = " ".join(name.split()), " ".join(dept.split())
        if not re.match(r"^[A-Z0-9]{6,20}$", reg): return False, "Enter the registration number: letters and digits only, 6 to 20 characters."
        if len(name) < 2: return False, "Enter the person's full name."
        if len(dept) < 2: return False, "Enter the person's department."
        name, dept = name[:60], dept[:40]
        with self.lock:
            if self.screen != "admin_form": return False, "Tap an admin card first."
            d = self.data
            clash = self.db.execute("SELECT name FROM people WHERE reg_no=? OR key=?", (reg, reg)).fetchone()
            if clash: return False, "%s is already registered to %s." % (reg, clash[0])
            self.db.execute("INSERT INTO people(key,reg_no,card_uid,name,department,registered,registered_by) VALUES(?,?,?,?,?,0,?)",
                            (reg, reg, d.get("uid") or d["ident"], name, dept, d.get("admin", "")))
            self.db.commit()
            self.tap = self.stamp()                                  # in-time = the moment the registration was saved
            self._set("purpose", key=reg, name=name, dept=dept, tapped=fmt_t(self.tap["wall"]), by=d.get("admin", ""))
            self._hold("OK", 3)                                      # registration over: ring turns green
            return True, ""

    def cancel(self):
        with self.lock:
            if self.screen != "idle": self._set("idle")

    # ---- upload to Cloudflare ----
    def push(self):
        """Send every visit that changed since the last successful upload. Returns number of visits accepted."""
        if not (API_URL and READER_KEY): return 0
        with self.lock:
            rows = self.db.execute(
                "SELECT v.uuid,v.rev,v.name,v.department,v.purpose_id,v.in_time,v.out_time,v.in_q,v.out_q,p.reg_no,p.card_uid "
                "FROM visits v LEFT JOIN people p ON p.key=v.key WHERE v.rev>v.synced_rev ORDER BY v.id LIMIT 100").fetchall()
        if not rows:
            self.sync_online = True; return 0
        visits = []
        for (u, rev, name, dept, pid, tin, tout, iq, oq, reg, uid) in rows:
            qs = [iq or "ok"] + ([oq or "ok"] if tout else [])
            quality = "estimated" if "unsynced" in qs else ("corrected" if "corrected" in qs else "synced")
            visits.append({"uuid": u, "rev": rev, "name": name, "department": dept, "purpose_id": pid,
                           "reg_no": reg or None, "card_uid": uid or None, "entered_at": tin, "exited_at": tout, "time_quality": quality})
        body = json.dumps({"reader_id": READER_ID, "sent_at": iso(utcnow()), "visits": visits}).encode()
        req = urllib.request.Request(API_URL + "/v1/sync", data=body, method="POST", headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + READER_KEY, "X-Reader-Id": READER_ID,
            "User-Agent": "attendance-kiosk/1"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                resp = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            self.sync_online, self.sync_error = True, "server said %d" % e.code      # reachable, but refused (check the key)
            raise
        except Exception as e:
            self.sync_online, self.sync_error = False, str(e)
            raise
        self.sync_online = True
        results = dict((x["uuid"], x) for x in resp.get("results", []))
        accepted = 0
        with self.lock:
            for v in visits:
                res = results.get(v["uuid"])
                if res and res.get("ok"):
                    self.db.execute("UPDATE visits SET synced_rev=? WHERE uuid=? AND synced_rev<?", (v["rev"], v["uuid"], v["rev"]))
                    accepted += 1
                elif res:
                    self.sync_error = "%s: %s" % (v["uuid"][:8], res.get("error"))
            self.db.commit()
        if accepted: self.sync_ok_at, self.sync_error = utcnow(), ""
        return accepted


K = Kiosk()


def uploader():
    delay = SYNC_EVERY
    while True:
        K.wake.wait(delay); K.wake.clear()
        try:
            K.fix_times()
            n = K.push()
            delay = SYNC_EVERY if K.pending() == 0 or n else min(delay * 2, 120)
            if n: print("uploaded %d visit(s)" % n, flush=True)
        except Exception as e:
            delay = min(max(delay, SYNC_EVERY) * 2, 120)
            print("upload failed (will retry in %ds): %s" % (delay, e), flush=True)


def led_loop():
    while True:
        time.sleep(0.2)
        try: K.led_tick()
        except Exception: pass


def save_floor_once():
    """Remember 'the time was at least this', so a power cut never leaves the clock stale. Written atomically. While the clock is
    untrusted the saved time is never moved backwards."""
    now = utcnow()
    if not K.clock_ok():
        try:
            if parse(FLOOR_FILE.read_text().strip()) > now: return
        except Exception: pass
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = FLOOR_FILE.with_suffix(".tmp"); tmp.write_text(iso(now)); os.replace(str(tmp), str(FLOOR_FILE))


def floor_saver():
    warned = False
    while True:
        time.sleep(FLOOR_EVERY)
        try: save_floor_once()
        except Exception as e:
            if not warned: print("cannot save last known time (%s); continuing without it" % e, flush=True); warned = True


class Reader(threading.Thread):
    def __init__(self): threading.Thread.__init__(self); self.daemon = True; self.ser = None
    def run(self):
        if SIM: return
        import serial
        while True:
            try:
                port = SERIAL_PORT or next(iter(sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))), None)
                if not port: time.sleep(2); continue
                self.ser = serial.Serial(port, 115200, timeout=1)
                K.send = self._send
                while True:
                    line = self.ser.readline().decode(errors="ignore").strip()
                    if not line: continue
                    try: m = json.loads(line)
                    except ValueError: continue
                    K.hb = time.time()
                    if m.get("evt") == "boot": K._led_sent = 0.0          # the ESP restarted: tell it the ring mode again at once
                    if m.get("evt") == "card": K.card(m.get("uid", ""), m.get("reg", ""))
            except Exception:
                self.ser = None; K.send = lambda c: None; time.sleep(2)
    def _send(self, cmd):
        try: self.ser.write((cmd + "\n").encode())
        except Exception: pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.send_header("Cache-Control", "no-store"); self.end_headers()
        self.wfile.write(b)
    def do_GET(self):
        if self.path.startswith("/api/state"): return self._json(K.snapshot())
        f = HERE / "ui" / ("index.html" if self.path in ("/", "/index.html") else self.path.lstrip("/"))
        if f.is_file() and HERE / "ui" in f.resolve().parents:
            b = f.read_bytes(); ct = {"html": "text/html", "css": "text/css", "js": "text/javascript", "woff2": "font/woff2"}.get(f.suffix[1:], "application/octet-stream")
            self.send_response(200); self.send_header("Content-Type", ct + "; charset=utf-8")
            self.send_header("Content-Length", str(len(b))); self.send_header("Cache-Control", "no-store"); self.end_headers()
            self.wfile.write(b)
        else: self.send_error(404)
    def do_POST(self):
        try: body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except ValueError: return self._json({"ok": False, "error": "bad request"}, 400)
        p = self.path
        if p == "/api/purpose": ok, err = K.choose_purpose(body.get("purpose", ""))
        elif p == "/api/register": ok, err = K.register(str(body.get("reg_no", "")), str(body.get("name", "")), str(body.get("department", "")))
        elif p == "/api/cancel": K.cancel(); ok, err = True, ""
        elif p == "/api/sim/tap" and SIM: K.card(str(body.get("uid", "")).strip().upper()); ok, err = True, ""
        else: return self._json({"ok": False, "error": "not found"}, 404)
        self._json({"ok": ok, "error": err})


class Server(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def import_csv(path):
    for r in csv.DictReader(open(path, newline="", encoding="utf-8-sig")):
        k = r["reg_no"].strip().upper()
        K.db.execute("INSERT OR REPLACE INTO people(key,reg_no,card_uid,name,department,registered) VALUES(?,?,?,?,?,1)",
                     (k, k, r.get("card_uid", "").strip().upper(), r["name"].strip(), r["department"].strip()))
    K.db.commit(); print("imported")

def export_csv(path):
    rows = K.db.execute("SELECT key,name,department,purpose,in_time,out_time,in_q,out_q,rev>synced_rev FROM visits ORDER BY id").fetchall()
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "name", "department", "purpose", "in_utc", "out_utc", "in_local", "out_local", "in_time_quality", "out_time_quality", "waiting_to_upload"])
        for (k, n, d, p, ti, to, iq, oq, pend) in rows:
            w.writerow([k, n, d, p, ti, to or "", to_local(parse(ti)).strftime("%Y-%m-%d %H:%M:%S"),
                        to_local(parse(to)).strftime("%Y-%m-%d %H:%M:%S") if to else "", iq or "", oq or "", "yes" if pend else "no"])
    print("%d rows -> %s" % (len(rows), path))


def admin_cli(args):
    cmd = args[0] if args else "list"
    if cmd == "list":
        rows = K.admin_list()
        print("%d of %d admin card(s)" % (len(rows), MAX_ADMINS))
        for uid, name in rows: print("  %-20s %s" % (uid, name))
    elif cmd == "add" and len(args) >= 3: print(*K.admin_add(args[1], " ".join(args[2:])))
    elif cmd == "add-last" and len(args) >= 2:
        r = K.db.execute("SELECT uid,ident,ts FROM unknown_taps ORDER BY id DESC LIMIT 1").fetchone()
        if not r or time.time() - r[2] > 900: print("No unknown card was tapped in the last 15 minutes. Tap the admin card on the reader first (the ring turns red), then run this again.")
        else:
            ok, err = K.admin_add(r[0] or r[1], " ".join(args[1:])); print("Added %s as admin %s" % (r[0] or r[1], " ".join(args[1:])) if ok else err)
    elif cmd == "remove" and len(args) == 2:
        K.db.execute("DELETE FROM admins WHERE card_uid=?", (args[1].strip().upper(),)); K.db.commit(); print("removed")
    else: print(__doc__)


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "admin": admin_cli(sys.argv[2:])
    elif len(sys.argv) == 3 and sys.argv[1] == "import": import_csv(sys.argv[2])
    elif len(sys.argv) == 3 and sys.argv[1] == "export": export_csv(sys.argv[2])
    else:
        K.load_floor()
        if SIM and not K.admin_list():                          # the simulator ships two demo admins
            K.admin_add("ADMINCARD01", "Admin One"); K.admin_add("ADMINCARD02", "Admin Two")
        Reader().start()
        threading.Thread(target=led_loop, daemon=True).start()
        threading.Thread(target=uploader, daemon=True).start()
        threading.Thread(target=floor_saver, daemon=True).start()
        print("Kiosk UI on http://localhost:%d  (sim=%s, db=%s, upload=%s)" % (HTTP_PORT, SIM, DB_PATH, API_URL or "off"), flush=True)
        Server(("127.0.0.1", HTTP_PORT), Handler).serve_forever()
