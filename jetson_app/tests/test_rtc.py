"""Tests rtc_clock.py against a fake I2C chip (no hardware needed).
   python3 -m unittest tests.test_rtc -v      (run from jetson_app/)"""
import os, shutil, sys, tempfile, types, unittest
from datetime import datetime

REGS = bytearray(256)
class FakeSMBus(object):
    def __init__(self, bus): pass
    def read_i2c_block_data(self, addr, reg, n): return list(REGS[reg:reg + n])
    def write_i2c_block_data(self, addr, reg, data): REGS[reg:reg + len(data)] = bytes(data)
    def read_byte_data(self, addr, reg): return REGS[reg]
    def write_byte_data(self, addr, reg, v): REGS[reg] = v
sys.modules["smbus"] = types.SimpleNamespace(SMBus=FakeSMBus)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import rtc_clock as rtc

class RtcTests(unittest.TestCase):
    def setUp(self):
        REGS[:] = bytearray(256)
        self.tmp = tempfile.mkdtemp(prefix="att_rtc_test_")
        rtc.RUN_DIR, rtc.STATE_DIR, rtc.CHIP = os.path.join(self.tmp, "run"), os.path.join(self.tmp, "state"), "ds1307"
        self.set_calls, self.synced, self.now = [], False, datetime(2026, 11, 6, 9, 30, 15)
        rtc.set_system_time = lambda t: self.set_calls.append(t)
        rtc.ntp_synced = lambda: self.synced
        rtc.utc_now = lambda: self.now
    def tearDown(self): shutil.rmtree(self.tmp)

    def test_1_ds1307_write_then_read_round_trips(self):
        rtc.write_rtc(datetime(2026, 11, 6, 9, 30, 15))
        self.assertEqual(list(REGS[0:7]), [0x15, 0x30, 0x09, 5, 0x06, 0x11, 0x26])    # Friday = 5
        t, bad = rtc.read_rtc()
        self.assertEqual((t, bad), (datetime(2026, 11, 6, 9, 30, 15), False))

    def test_2_ds1307_halted_clock_is_flagged_but_still_parsed(self):
        rtc.write_rtc(datetime(2026, 11, 6, 9, 30, 15)); REGS[0] |= 0x80               # clock-halt bit
        t, bad = rtc.read_rtc()
        self.assertTrue(bad); self.assertEqual(t.second, 15)

    def test_3_writing_restarts_a_halted_ds1307(self):
        REGS[0] = 0x80
        rtc.write_rtc(datetime(2026, 11, 6, 9, 30, 15))
        self.assertEqual(REGS[0] & 0x80, 0)

    def test_4_twelve_hour_mode_is_read_correctly(self):
        rtc.write_rtc(datetime(2026, 11, 6, 9, 30, 15)); REGS[2] = 0x40 | 0x20 | 0x09  # 9 PM
        self.assertEqual(rtc.read_rtc()[0].hour, 21)
        REGS[2] = 0x40 | 0x12                                                          # 12 AM = midnight
        self.assertEqual(rtc.read_rtc()[0].hour, 0)

    def test_5_ds3231_uses_the_oscillator_stopped_flag(self):
        rtc.CHIP = "ds3231"; REGS[0x0F] = 0x80
        self.assertTrue(rtc.read_rtc()[1])
        rtc.write_rtc(datetime(2026, 11, 6, 9, 30, 15))
        self.assertFalse(rtc.read_rtc()[1])

    def test_6_load_sets_the_system_clock_from_a_good_rtc(self):
        rtc.write_rtc(datetime(2026, 11, 6, 8, 0, 0))
        self.assertEqual(rtc.main(["x", "load"]), 0)
        self.assertEqual(self.set_calls, [datetime(2026, 11, 6, 8, 0, 0)])
        self.assertTrue(os.path.exists(os.path.join(rtc.RUN_DIR, "rtc_loaded")))

    def test_7_load_refuses_a_halted_or_implausible_rtc(self):
        self.assertEqual(rtc.main(["x", "load"]), 1)                                   # all zeros = year 2000, halted not set
        rtc.write_rtc(datetime(2026, 11, 6, 8, 0, 0)); REGS[0] |= 0x80
        self.assertEqual(rtc.main(["x", "load"]), 1)
        rtc.write_rtc(datetime(2031, 1, 1, 0, 0, 0))                                   # far in the future
        self.assertEqual(rtc.main(["x", "load"]), 1)
        self.assertEqual(self.set_calls, []); self.assertFalse(os.path.exists(os.path.join(rtc.RUN_DIR, "rtc_loaded")))

    def test_8_load_never_overrides_good_network_time(self):
        rtc.write_rtc(datetime(2026, 11, 6, 8, 0, 0)); self.synced = True
        self.assertEqual(rtc.main(["x", "load"]), 0)
        self.assertEqual(self.set_calls, [])

    def test_9_save_waits_for_network_time(self):
        self.assertEqual(rtc.main(["x", "save"]), 0)
        self.assertEqual(list(REGS[0:7]), [0] * 7)
        self.synced = True
        self.assertEqual(rtc.main(["x", "save"]), 0)
        self.assertEqual(rtc.read_rtc()[0], self.now)
        self.assertTrue(os.path.exists(os.path.join(rtc.STATE_DIR, "rtc_ok")))

if __name__ == "__main__":
    unittest.main(verbosity=2)
