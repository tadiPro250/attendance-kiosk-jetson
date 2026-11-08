"""Card lookup, forgotten tap-outs, CSV re-import and upload ordering.
   python3 -m unittest tests.test_lookup_and_sync -v      (run from jetson_app/)"""
import atexit, io, json, os, shutil, sys, tempfile, unittest
from datetime import datetime

_TMP = tempfile.mkdtemp(prefix="att_lookup_test_")
atexit.register(lambda: shutil.rmtree(_TMP, ignore_errors=True))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._load import fresh_server
server = fresh_server(ATT_SIM="1", ATT_DB=os.path.join(_TMP, "t.db"), ATT_API_URL="", ATT_MAX_OPEN_HOURS="14")

K = server.K
S = {"wall": datetime(2026, 11, 6, 9, 0, 0), "mono": 1000.0, "boot": "BOOT-1", "ok": True}
server.utcnow = lambda: S["wall"]
server.uptime = lambda: S["mono"]
server.boot_id = lambda: S["boot"]
K.clock_ok = lambda: S["ok"]


def tap(uid, reg=""):
    K.last_ts = 0; K.last_ident = ""; K.card(uid, reg)

def add(key, uid, name):
    K.db.execute("INSERT INTO people(key,reg_no,card_uid,name,department,registered) VALUES(?,?,?,?,'CSE',1)", (key, key, uid, name))
    K.db.commit()

def write_csv(text):
    p = os.path.join(_TMP, "students.csv")
    with open(p, "w") as f: f.write(text)
    return p


class Base(unittest.TestCase):
    def setUp(self):
        for t in ("people", "visits", "admins", "unknown_taps"): K.db.execute("DELETE FROM %s" % t)
        K.db.commit(); K._set("idle")
        S.update(wall=datetime(2026, 11, 6, 9, 0, 0), mono=1000.0, boot="BOOT-1", ok=True)


class LookupTests(Base):
    def test_1_a_reg_only_tap_never_matches_someone_without_a_card(self):
        add("RA1", None, "Asha Rao"); add("RA2", "", "Old Import")       # '' is what older imports stored
        tap("", reg="RA000BOGUS")
        self.assertEqual(K.screen, "unknown")

    def test_2_card_uid_case_and_spaces_do_not_matter(self):
        add("RA1", "04AABB01", "Asha Rao")
        tap(" 04aabb01 ")
        self.assertEqual((K.screen, K.data["name"]), ("purpose", "Asha Rao"))

    def test_3_a_registered_card_is_stored_upper_case(self):
        K.admin_add("ADM1", "Admin One")
        tap("cafe0001"); tap("adm1")
        self.assertEqual(K.screen, "admin_form")
        ok, err = K.register("RA2111003010777", "Ravi Kumar", "ECE"); self.assertTrue(ok, err)
        self.assertEqual(K.db.execute("SELECT card_uid FROM people WHERE key='RA2111003010777'").fetchone()[0], "CAFE0001")


class ForgottenTapOutTests(Base):
    def test_1_the_next_day_tap_is_an_arrival_not_an_exit(self):
        add("RA1", "C1", "Ravi Kumar")
        tap("C1"); K.choose_purpose("lab")
        S.update(wall=datetime(2026, 11, 8, 9, 0, 0), mono=1000.0 + 48 * 3600)          # two days later, same boot
        tap("C1")
        self.assertEqual(K.screen, "purpose")                                           # asked for a purpose: a new visit
        self.assertEqual(K.db.execute("SELECT out_time,out_q FROM visits").fetchone(), (None, "missed"))
        K.choose_purpose("lab")
        self.assertEqual(K.db.execute("SELECT COUNT(*) FROM visits").fetchone()[0], 2)
        self.assertEqual(K.snapshot()["inside"], 1)                                     # only today's visit counts

    def test_2_a_normal_exit_is_unchanged(self):
        add("RA1", "C1", "Ravi Kumar")
        tap("C1"); K.choose_purpose("lab")
        S.update(wall=datetime(2026, 11, 6, 12, 0, 0), mono=1000.0 + 3 * 3600)
        tap("C1")
        self.assertEqual((K.screen, K.data["duration"]), ("out", "3 h 0 min"))

    def test_3_close_forgotten_clears_the_inside_count_and_the_board_says_so(self):
        add("RA1", "C1", "Ravi Kumar")
        tap("C1"); K.choose_purpose("lab")
        S.update(wall=datetime(2026, 11, 7, 0, 0, 0), mono=1000.0 + 15 * 3600)
        self.assertEqual(K.close_forgotten(), 1)
        snap = K.snapshot()
        self.assertEqual(snap["inside"], 0)
        self.assertEqual(snap["board"][0]["out"], "No tap")

    def test_4_nothing_is_closed_while_the_clock_cannot_tell(self):
        add("RA1", "C1", "Ravi Kumar")
        tap("C1"); K.choose_purpose("lab")
        S.update(boot="BOOT-2", mono=50.0, ok=False)                                    # rebooted, clock untrusted
        self.assertEqual(K.close_forgotten(), 0)
        tap("C1"); self.assertEqual(K.screen, "out")


class ImportTests(Base):
    def test_1_reimport_keeps_a_card_registered_at_the_kiosk(self):
        K.db.execute("INSERT INTO people(key,reg_no,card_uid,name,department,registered,registered_by) "
                     "VALUES('RA7','RA7','DEADBEEF','Ravi','ECE',0,'Admin One')"); K.db.commit()
        server.import_csv(write_csv("reg_no,name,department\nRA7,Ravi Kumar,Electronics\nRA8,Meera Iyer,CSE\n"))
        self.assertEqual(K.db.execute("SELECT card_uid,name,department,registered_by FROM people WHERE key='RA7'").fetchone(),
                         ("DEADBEEF", "Ravi Kumar", "Electronics", "Admin One"))
        self.assertIsNone(K.db.execute("SELECT card_uid FROM people WHERE key='RA8'").fetchone()[0])   # NULL, not ''

    def test_2_a_card_in_the_csv_still_replaces_the_old_one(self):
        add("RA7", "DEADBEEF", "Ravi")
        server.import_csv(write_csv("reg_no,name,department,card_uid\nRA7,Ravi,ECE,cafe0002\n"))
        self.assertEqual(K.db.execute("SELECT card_uid FROM people WHERE key='RA7'").fetchone()[0], "CAFE0002")


class FakeResponse(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): pass


class UploadOrderTests(Base):
    def setUp(self):
        Base.setUp(self)
        server.API_URL, server.READER_KEY = "https://example.invalid", "k"
        self.sent = []
        def urlopen(req, timeout=None):
            visits = json.loads(req.data.decode())["visits"]
            self.sent.append([v["uuid"] for v in visits])
            return FakeResponse(json.dumps({"results": [{"uuid": v["uuid"], "ok": not v["uuid"].startswith("bad")} for v in visits]}).encode())
        self._real = server.urllib.request.urlopen
        server.urllib.request.urlopen = urlopen
    def tearDown(self):
        server.urllib.request.urlopen = self._real
        server.API_URL, server.READER_KEY = "", ""

    def test_1_rejected_visits_never_hold_up_newer_ones(self):
        for i in range(100):
            K.db.execute("INSERT INTO visits(uuid,key,name,purpose_id,in_time,rev,synced_rev) VALUES(?,'RA1','A','lab','2026-11-06T09:00:00Z',1,0)",
                         ("bad%03d" % i,))
        K.db.execute("INSERT INTO visits(uuid,key,name,purpose_id,in_time,rev,synced_rev) VALUES('good1','RA1','A','lab','2026-11-06T10:00:00Z',1,0)")
        K.db.commit()
        self.assertEqual(K.push(), 0)                                    # first batch: the oldest 100, all refused
        self.assertEqual(K.push(), 1)                                    # the refused ones went to the back; the new visit gets through
        self.assertIn("good1", self.sent[1])
        self.assertEqual(K.pending(), 100)                               # the refused ones are still kept and retried


if __name__ == "__main__":
    unittest.main(verbosity=2)
