"""The 'last known time' fallback: a Jetson with a wrong or stale clock must never start earlier than it remembers.
   python3 -m unittest tests.test_floor -v      (run from jetson_app/)"""
import atexit, os, shutil, sys, tempfile, unittest
from datetime import datetime
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="att_floor_test_")
atexit.register(lambda: shutil.rmtree(_TMP, ignore_errors=True))
os.environ.update(ATT_SIM="1", ATT_DB=os.path.join(_TMP, "t.db"), ATT_API_URL="", ATT_STATE_DIR=os.path.join(_TMP, "state"), ATT_RUN_DIR=os.path.join(_TMP, "run"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server

K = server.K
FLOOR = datetime(2026, 11, 6, 9, 0, 0)

class FloorTests(unittest.TestCase):
    def setUp(self):
        server._OFFSET.update(v=0, src=None)
        server.STATE_DIR = Path(_TMP) / "state"; server.FLOOR_FILE = server.STATE_DIR / "last_seen"
        if server.FLOOR_FILE.exists(): server.FLOOR_FILE.unlink()
        self.os_time, self.os_ok = datetime(2026, 1, 1, 0, 0, 0), False          # the OS clock is badly behind and not synced
        server.os_now = lambda: self.os_time
        K._os_ok = lambda: self.os_ok
        K._os_via = None
        K.clock_ok = server.Kiosk.clock_ok.__get__(K)
        K.clock_source = server.Kiosk.clock_source.__get__(K)
    def put_floor(self, dt):
        server.STATE_DIR.mkdir(parents=True, exist_ok=True); server.FLOOR_FILE.write_text(server.iso(dt))

    def test_1_starts_from_the_remembered_time_when_the_clock_is_behind(self):
        self.put_floor(FLOOR); K.load_floor()
        self.assertEqual(server.utcnow(), FLOOR)
        self.assertEqual(K.clock_source(), "last-known")
        self.assertFalse(K.clock_ok())                                           # still flagged unsure: the real time is later
        self.os_time = datetime(2026, 1, 1, 0, 0, 30)                            # 30 s later the app clock moves on with it
        self.assertEqual(server.utcnow(), datetime(2026, 11, 6, 9, 0, 30))

    def test_2_a_good_os_clock_is_left_alone(self):
        self.put_floor(FLOOR); self.os_ok = True; self.os_time = datetime(2026, 11, 6, 20, 0, 0)
        K.load_floor()
        self.assertEqual(server.utcnow(), self.os_time); self.assertEqual(server._OFFSET["v"], 0)

    def test_3_an_os_clock_ahead_of_the_floor_is_left_alone(self):
        self.put_floor(FLOOR); self.os_time = datetime(2026, 11, 6, 10, 0, 0)    # unsynced but already later than the floor
        K.load_floor()
        self.assertEqual(server.utcnow(), self.os_time)

    def test_4_borrowed_time_is_dropped_once_the_os_clock_is_trusted(self):
        self.put_floor(FLOOR); K.load_floor()
        self.os_ok, self.os_time = True, datetime(2026, 11, 6, 15, 30, 0)        # network time arrived
        self.assertTrue(K.clock_ok())
        self.assertEqual((server._OFFSET["v"], server._OFFSET["src"]), (0, None))
        self.assertEqual(server.utcnow(), self.os_time)

    def test_5_saved_time_is_written_when_the_clock_is_trusted(self):
        self.os_ok, self.os_time = True, datetime(2026, 11, 6, 15, 30, 0)
        server.save_floor_once()
        self.assertEqual(server.FLOOR_FILE.read_text(), "2026-11-06T15:30:00Z")

    def test_6_saved_time_never_moves_backwards_while_untrusted(self):
        self.put_floor(FLOOR); self.os_time = datetime(2026, 1, 1, 0, 0, 0)
        server.save_floor_once()
        self.assertEqual(server.FLOOR_FILE.read_text(), "2026-11-06T09:00:00Z")
        self.os_time = datetime(2026, 11, 6, 9, 5, 0)                            # moves forward: allowed
        server.save_floor_once()
        self.assertEqual(server.FLOOR_FILE.read_text(), "2026-11-06T09:05:00Z")

    def test_7_a_trusted_clock_may_correct_a_wrong_saved_time(self):
        self.put_floor(datetime(2030, 1, 1, 0, 0, 0))                            # a bad value saved earlier
        self.os_ok, self.os_time = True, datetime(2026, 11, 6, 15, 30, 0)
        server.save_floor_once()
        self.assertEqual(server.FLOOR_FILE.read_text(), "2026-11-06T15:30:00Z")

    def test_8_missing_or_corrupt_file_is_harmless(self):
        K.load_floor()                                                           # no file
        self.put_floor(FLOOR); server.FLOOR_FILE.write_text("garbage"); K.load_floor()
        self.assertEqual(server._OFFSET["v"], 0)

if __name__ == "__main__":
    unittest.main(verbosity=2)
