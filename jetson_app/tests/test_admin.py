"""Admin-gated registration and the ring (LED) modes the Jetson asks the ESP to show.
   python3 -m unittest tests.test_admin -v      (run from jetson_app/)"""
import atexit, glob, os, sys, tempfile, time, unittest

_DB = os.path.join(tempfile.mkdtemp(prefix="att_admin_test_"), "test.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._load import fresh_server
server = fresh_server(ATT_SIM="1", ATT_DB=_DB, ATT_API_URL="")

def _cleanup():
    for p in glob.glob(_DB + "*"): os.remove(p)
    os.rmdir(os.path.dirname(_DB))
atexit.register(_cleanup)

K = server.K
K.db.execute("INSERT OR REPLACE INTO people(key,reg_no,card_uid,name,department,registered) VALUES('RA2111003010001','RA2111003010001','04AABB01','Asha Rao','CSE',1)")
K.db.commit()
sent = []
K.send = lambda c: sent.append(c)

def tap(uid):
    K.last_ts = 0; K.last_ident = ""; K.card(uid)

class AdminTests(unittest.TestCase):
    def setUp(self):
        K.db.execute("DELETE FROM admins"); K.db.execute("DELETE FROM visits"); K.db.execute("DELETE FROM people WHERE registered=0"); K.db.commit()
        K._set("idle"); K.led_hold = (None, 0.0); del sent[:]
        K.admin_add("ADM1", "Admin One"); K.admin_add("ADM2", "Admin Two")

    def test_1_unknown_card_is_refused_and_the_ring_is_red(self):
        tap("DEADBEEF")
        self.assertEqual(K.screen, "unknown"); self.assertEqual(K.desired_led(), "DENY")
        self.assertEqual(K.db.execute("SELECT COUNT(*) FROM people WHERE card_uid='DEADBEEF'").fetchone()[0], 0)   # nothing was registered

    def test_2_nobody_can_register_without_an_admin_card(self):
        tap("DEADBEEF")
        ok, err = K.register("RA2111003010777", "Ravi Kumar", "ECE")
        self.assertFalse(ok); self.assertEqual(K.screen, "unknown")

    def test_3_full_flow_admin_registers_the_card_then_in_time_is_marked(self):
        tap("DEADBEEF"); tap("ADM1")
        self.assertEqual(K.screen, "admin_form"); self.assertEqual(K.desired_led(), "NEW")   # yellow while the admin types
        self.assertEqual(K.data["uid"], "DEADBEEF"); self.assertEqual(K.data["admin"], "Admin One")
        ok, err = K.register("ra2111003010777", "  Ravi   Kumar ", "Electronics and Communication")
        self.assertTrue(ok, err)
        self.assertEqual(K.desired_led(), "OK")                                              # green: registration is over
        p = K.db.execute("SELECT key,reg_no,card_uid,name,department,registered_by FROM people WHERE reg_no='RA2111003010777'").fetchone()
        self.assertEqual(p, ("RA2111003010777", "RA2111003010777", "DEADBEEF", "Ravi Kumar", "Electronics and Communication", "Admin One"))
        self.assertEqual(K.screen, "purpose")
        ok, err = K.choose_purpose("lab"); self.assertTrue(ok, err)                          # the in-time is recorded here
        v = K.db.execute("SELECT name,in_time,out_time FROM visits").fetchall()
        self.assertEqual(len(v), 1); self.assertEqual(v[0][0], "Ravi Kumar"); self.assertIsNone(v[0][2])
        tap("DEADBEEF"); self.assertEqual(K.screen, "out")                                   # tapping the new card again marks out

    def test_4_admin_first_then_the_new_card(self):
        tap("ADM2"); self.assertEqual(K.screen, "admin_wait"); self.assertEqual(K.desired_led(), "NEW")
        tap("CAFE0001"); self.assertEqual(K.screen, "admin_form"); self.assertEqual(K.data["uid"], "CAFE0001")

    def test_5_admin_taps_never_mark_attendance(self):
        tap("ADM1"); tap("ADM1")
        self.assertEqual(K.db.execute("SELECT COUNT(*) FROM visits").fetchone()[0], 0)

    def test_6_validation_and_duplicates(self):
        tap("DEADBEEF"); tap("ADM1")
        self.assertFalse(K.register("12", "Ravi Kumar", "ECE")[0])
        self.assertFalse(K.register("RA2111003010777", "R", "ECE")[0])
        self.assertFalse(K.register("RA2111003010777", "Ravi Kumar", "")[0])
        ok, err = K.register("RA2111003010001", "Someone Else", "ECE")                       # already belongs to Asha
        self.assertFalse(ok); self.assertIn("Asha Rao", err); self.assertEqual(K.screen, "admin_form")

    def test_7_registered_student_gets_green_and_the_normal_flow(self):
        tap("04AABB01"); self.assertEqual(K.screen, "purpose"); self.assertEqual(K.desired_led(), "OK")
        K.led_hold = (None, 0.0); K._set("idle"); self.assertEqual(K.desired_led(), "IDLE")

    def test_8_only_two_admins_and_a_student_card_cannot_be_an_admin(self):
        self.assertFalse(K.admin_add("ADM3", "Third")[0])
        K.db.execute("DELETE FROM admins WHERE card_uid='ADM2'"); K.db.commit()
        self.assertFalse(K.admin_add("04AABB01", "Asha")[0])
        self.assertTrue(K.admin_add("ADM3", "Third")[0])

    def test_9_a_busy_admin_form_is_not_hijacked(self):
        tap("DEADBEEF"); tap("ADM1"); tap("04AABB01"); tap("OTHER001")
        self.assertEqual(K.screen, "admin_form"); self.assertEqual(K.data["uid"], "DEADBEEF")

    def test_10_the_ring_command_is_sent_on_change_and_refreshed(self):
        K._led_last, K._led_sent = None, 0.0
        K.led_tick(); self.assertEqual(sent[-1], "LED IDLE")
        n = len(sent); K.led_tick(); self.assertEqual(len(sent), n)                            # unchanged and recent: nothing sent
        tap("DEADBEEF"); K.led_tick(); self.assertEqual(sent[-1], "LED DENY")
        K._led_sent = time.time() - 4; n = len(sent); K.led_tick(); self.assertEqual(len(sent), n + 1)   # refreshed after 3 s

    def test_11_screens_expire_back_to_idle_and_the_ring_follows(self):
        tap("DEADBEEF"); K.expires = time.time() - 1
        self.assertEqual(K.desired_led(), "IDLE"); self.assertEqual(K.screen, "idle")

if __name__ == "__main__":
    unittest.main(verbosity=2)
