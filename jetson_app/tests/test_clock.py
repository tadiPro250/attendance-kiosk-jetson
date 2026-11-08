"""Simulates a Jetson that boots with a wrong clock (no RTC / dead battery), takes taps, then gets network time.
   python3 -m unittest tests.test_clock -v      (run from jetson_app/)"""
import atexit, glob, os, sys, tempfile, unittest
from datetime import datetime

_DB = os.path.join(tempfile.mkdtemp(prefix="att_clock_test_"), "test.db")      # a private folder, so cleanup touches only our files
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._load import fresh_server
server = fresh_server(ATT_SIM="1", ATT_DB=_DB, ATT_API_URL="")

def _cleanup():
    for p in glob.glob(_DB + "*"): os.remove(p)
    os.rmdir(os.path.dirname(_DB))
atexit.register(_cleanup)

S = {"wall": datetime(2026, 1, 1, 0, 0, 0), "mono": 100.0, "boot": "BOOT-1", "ok": False}
server.utcnow = lambda: S["wall"]
server.uptime = lambda: S["mono"]
server.boot_id = lambda: S["boot"]
server.K.clock_ok = lambda: S["ok"]
K = server.K
K.db.execute("INSERT OR REPLACE INTO people(key,reg_no,card_uid,name,department,registered) VALUES('RA1','RA1','','Asha Rao','CSE',1)")
K.db.execute("INSERT OR REPLACE INTO people(key,reg_no,card_uid,name,department,registered) VALUES('RA2','RA2','','Vikram Singh','ECE',1)")
K.db.commit()

def advance(seconds):
    S["mono"] += seconds; S["wall"] = datetime.fromtimestamp(S["wall"].timestamp() + seconds)
    K.last_ts = 0                                    # skip the double-tap debounce

def row(key): return K.db.execute("SELECT in_time,in_q,out_time,out_q,rev,synced_rev FROM visits WHERE key=? ORDER BY id DESC LIMIT 1", (key,)).fetchone()

class ClockTests(unittest.TestCase):
    def test_1_wrong_clock_is_flagged_then_corrected(self):
        K.card("RA1"); K.choose_purpose("lab")                      # tap at mono 100, wall says 2026-01-01 00:00:00
        advance(30); K.card("RA1")                                  # leaves 30 s later
        r = row("RA1")
        self.assertEqual((r[0], r[1], r[3]), ("2026-01-01T00:00:00Z", "unsynced", "unsynced"))
        self.assertEqual(K.pending(), 1)
        # network time arrives: it is really 2026-11-06 09:00:00 and 600 s of uptime have passed since the first tap
        S.update(wall=datetime(2026, 11, 6, 9, 0, 0), mono=700.0, ok=True)
        self.assertEqual(K.fix_times(), 1)
        r = row("RA1")
        self.assertEqual(r[0], "2026-11-06T08:50:00Z")             # 600 s before 09:00:00
        self.assertEqual(r[2], "2026-11-06T08:50:30Z")             # still exactly 30 s later
        self.assertEqual((r[1], r[3]), ("corrected", "corrected"))
        self.assertGreater(r[4], r[5])                              # marked dirty so the new times upload
        self.assertEqual(K.fix_times(), 0)                          # nothing left to do

    def test_2_a_stamp_from_an_earlier_boot_cannot_be_corrected(self):
        S.update(wall=datetime(2026, 1, 1, 0, 0, 0), mono=50.0, boot="BOOT-OLD", ok=False)
        K.card("RA2"); K.choose_purpose("class")
        S.update(wall=datetime(2026, 11, 6, 9, 5, 0), mono=900.0, boot="BOOT-NEW", ok=True)   # rebooted, clock fine now
        self.assertEqual(K.fix_times(), 0)
        r = row("RA2")
        self.assertEqual((r[0], r[1]), ("2026-01-01T00:00:00Z", "unsynced"))   # stays flagged, uploaded as 'estimated'

    def test_3_good_clock_is_never_touched(self):
        S.update(wall=datetime(2026, 11, 6, 10, 0, 0), mono=1000.0, boot="BOOT-NEW", ok=True)
        K.last_ts = 0; K.card("RA1"); K.choose_purpose("lab")
        self.assertEqual(row("RA1")[1], "ok")
        self.assertEqual(K.fix_times(), 0)

    def test_4_duration_comes_from_uptime_not_the_wrong_wall_clock(self):
        S.update(wall=datetime(2026, 1, 1, 0, 0, 0), mono=2000.0, boot="BOOT-NEW", ok=False)
        K.last_ts = 0; K.card("RA1")                                # closes the visit opened in test 3, while the wall clock is wrong
        self.assertEqual(K.screen, "out")
        self.assertEqual(K.data["duration"], "16 min")              # in at mono 1000, out at mono 2000 = 1000 s

if __name__ == "__main__":
    unittest.main(verbosity=2)
