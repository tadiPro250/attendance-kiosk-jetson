#!/usr/bin/env python3
"""I2C real-time-clock helper for the DS1307 (default) or DS3231. Talks to the chip from user space: no kernel driver needed.

  sudo python3 rtc_clock.py show    print the RTC time and the system time
  sudo python3 rtc_clock.py load    RTC -> system clock. Run at boot (systemd). Refuses a chip whose clock is halted or implausible.
  sudo python3 rtc_clock.py save    system clock -> RTC, but ONLY when network time says the clock is synchronised.
                                    Run once by hand after the first sync; a systemd timer then repeats it every 30 minutes.

Chip: set RTC_CHIP=ds1307 (default) or ds3231 (in /etc/attendance.env). The RTC always holds UTC. Bus 1 = header pins 3 (SDA) and 5 (SCL).
Python 3.6 compatible; needs `sudo apt install python3-smbus i2c-tools`.

DS1307 ACCURACY: it has no temperature compensation and can drift by a minute or two per month. That is why `save` runs every
30 minutes while the network is up: the drift only matters during a long time without any network.
"""
import os, subprocess, sys
from datetime import datetime, timedelta

CHIP = os.environ.get("RTC_CHIP", "ds1307").strip().lower()
BUS = int(os.environ.get("RTC_I2C_BUS", "1"))
ADDR = 0x68
RUN_DIR = os.environ.get("ATT_RUN_DIR", "/run/attendance")
STATE_DIR = os.environ.get("ATT_STATE_DIR", "/var/lib/attendance")


def bcd(n): return ((n // 10) << 4) | (n % 10)
def unbcd(b): return (b >> 4) * 10 + (b & 0x0F)

def bus():
    import smbus
    return smbus.SMBus(BUS)

def read_rtc():
    """Returns (UTC time, untrusted). untrusted = the oscillator was halted: battery dead or the chip was never set."""
    b = bus()
    d = b.read_i2c_block_data(ADDR, 0x00, 7)
    if CHIP == "ds3231": untrusted = bool(b.read_byte_data(ADDR, 0x0F) & 0x80)    # oscillator-stopped flag
    else: untrusted = bool(d[0] & 0x80)                                            # DS1307 clock-halt bit in the seconds register
    if d[2] & 0x40:                                                                # chip is in 12-hour mode
        hour = unbcd(d[2] & 0x1F) % 12 + (12 if d[2] & 0x20 else 0)
    else:
        hour = unbcd(d[2] & 0x3F)
    try:
        t = datetime(2000 + unbcd(d[6]), unbcd(d[5] & 0x1F), unbcd(d[4]), hour, unbcd(d[1]), unbcd(d[0] & 0x7F))
    except ValueError:                                                             # blank or garbage registers (battery was never fitted)
        t, untrusted = datetime(2000, 1, 1), True
    return t, untrusted

def write_rtc(t):
    b = bus()
    # bit 7 of the seconds byte is 0 here, which also starts a halted DS1307; hours are written in 24-hour mode
    b.write_i2c_block_data(ADDR, 0x00, [bcd(t.second), bcd(t.minute), bcd(t.hour), t.isoweekday(), bcd(t.day), bcd(t.month), bcd(t.year - 2000)])
    if CHIP == "ds3231": b.write_byte_data(ADDR, 0x0F, b.read_byte_data(ADDR, 0x0F) & 0x7F)

def ntp_synced():
    out = subprocess.Popen(["timedatectl", "status"], stdout=subprocess.PIPE, universal_newlines=True).communicate()[0]
    for line in out.splitlines():
        if "synchronized:" in line: return line.split(":", 1)[1].strip() == "yes"
    return False

def set_system_time(t):
    subprocess.check_call(["date", "-u", "-s", t.strftime("%Y-%m-%d %H:%M:%S")], stdout=subprocess.DEVNULL)

def utc_now(): return datetime.utcnow().replace(microsecond=0)


def main(argv=None):
    argv = sys.argv if argv is None else argv
    cmd = argv[1] if len(argv) > 1 else "show"
    now = utc_now()
    if cmd == "show":
        t, bad = read_rtc()
        print("Chip:         %s on I2C bus %d" % (CHIP, BUS))
        print("RTC (UTC):    %s%s" % (t, "   <-- clock halted: battery flat or never set. Fix with 'save'." if bad else ""))
        print("System (UTC): %s" % now)
    elif cmd == "load":
        t, bad = read_rtc()
        if bad: print("The RTC clock is halted (flat battery, or never set); not loading it. Run 'save' once network time is synced."); return 1
        if not (datetime(2025, 1, 1) < t < now + timedelta(days=366)): print("RTC time %s is implausible; not loading it." % t); return 1
        os.makedirs(RUN_DIR, exist_ok=True)
        with open(os.path.join(RUN_DIR, "rtc_loaded"), "w") as f: f.write(t.isoformat())
        if ntp_synced(): print("Network time is already synced; leaving the system clock alone."); return 0
        set_system_time(t)
        print("System clock set from RTC: %s UTC" % t)
    elif cmd == "save":
        if not ntp_synced(): print("Network time is not synced yet; leaving the RTC alone."); return 0
        write_rtc(now)
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, "rtc_ok"), "w") as f: f.write(now.isoformat())
        print("RTC set from system clock: %s UTC" % now)
    else:
        print(__doc__); return 2
    return 0

if __name__ == "__main__":
    sys.exit(main())
