# Running the attendance kiosk on the Jetson Nano

What you end up with: power on the Jetson and, with nobody touching anything, the attendance page opens full screen. Visits are saved on the Jetson first. They upload to your Cloudflare database whenever Wi-Fi is available. Times stay correct across power cuts.

**You do not need to read the whole file to get started.** Do Part A once on your Mac, Part B once on the Jetson, then test with Part C.

## What you need

| Item | Notes |
|---|---|
| Jetson Nano with JetPack (Ubuntu 18.04 or 20.04) | The code runs on Python 3.6 or newer. Check with `python3 --version`. |
| 5 V 4 A barrel-jack power supply | Fit the J48 jumper so the board takes power from the barrel jack. |
| microSD card, 32 GB or more, a good brand | Power cuts are hard on cheap cards. |
| HDMI monitor, USB keyboard, USB mouse | |
| ESP8266 NodeMCU + RC522, wired as in `PLAN.md` section 8b | Flash `esp8266_firmware/esp8266_firmware.ino` with the Arduino IDE first. |
| **DS1307 clock module with a coin battery** | Keeps the time while the power is off. It needs a small modification before it touches the Jetson: read step B6 first. |
| USB Wi-Fi dongle, or an Ethernet cable | The Nano dev kit has no Wi-Fi of its own. The four USB ports are then full: keyboard, mouse, ESP8266, dongle. |
| A Cloudflare account with your domain | Free plan is enough. |

---

## Part A: Cloudflare (on your Mac, about 15 minutes)

1. Install the Cloudflare tool and log in (a browser window opens for you to approve):
   ```
   npm install -g wrangler
   wrangler login
   ```
2. Create the database:
   ```
   cd ~/D/projects/attendance-kiosk/cloudflare/worker
   wrangler d1 create attendance
   ```
   Copy the `database_id` it prints into `wrangler.toml`, replacing `REPLACE_WITH_THE_ID_PRINTED_BY_wrangler_d1_create`.
3. Create the tables:
   ```
   wrangler d1 migrations apply attendance --remote
   ```
4. Make a secret key for the Jetson and register it. The database stores only a fingerprint of the key.
   ```
   KEY=$(openssl rand -hex 32)
   echo "$KEY"                                  # copy this somewhere safe: the Jetson needs it in step B4
   HASH=$(printf %s "$KEY" | shasum -a 256 | cut -d' ' -f1)
   wrangler d1 execute attendance --remote --command \
     "INSERT INTO readers(id,name,location,api_key_hash) VALUES ('lab-kiosk','Lab monitor','inside','$HASH')"
   ```
5. Put the Worker on your domain. In `wrangler.toml` uncomment the `routes` line and use your own name, for example `attendance.yourdomain.com`. The domain must already be in your Cloudflare account. Then:
   ```
   wrangler deploy
   curl https://attendance.yourdomain.com/v1/health      # should print {"ok":true,...}
   ```
6. Optional self-check: `node --no-warnings test/worker.test.mjs` (21 tests, all should pass).

---

## Part B: The Jetson (about 30 minutes)

**B1. Connect to Wi-Fi** (skip if you use Ethernet):
```
nmcli device wifi list
nmcli device wifi connect "YOUR-WIFI-NAME" password "YOUR-PASSWORD"
```
It is saved and reconnects by itself after every boot. If your campus Wi-Fi asks you to sign in on a web page, or uses a staff login, the Jetson cannot do that on its own. Use Ethernet, a lab router with a normal password, or ask IT to register the Jetson's MAC address.

**B2. Copy the project to the Jetson** (run on your Mac, with the Jetson's address and username):
```
scp -r ~/D/projects/attendance-kiosk/jetson_app jetson@JETSON-ADDRESS:~/
```

**B3. Run the setup script** (on the Jetson):
```
cd ~/jetson_app
sudo bash setup.sh
```
It installs what is needed, sets the Asia/Kolkata time zone and network time, installs the services, turns on automatic login, and makes the page open at every start.

**B4. Tell the Jetson where Cloudflare is.** Edit the settings file:
```
sudo nano /etc/attendance.env
```
Fill in two lines, then save (Ctrl+O, Enter, Ctrl+X):
```
ATT_API_URL=https://attendance.yourdomain.com
ATT_READER_KEY=the-key-from-step-A4
```
Then `sudo systemctl restart attendance`.

**B5. Check the browser.** `chromium-browser --version` should say 90 or higher. If it is older or missing, run `sudo apt install firefox`. The kiosk uses it automatically.

**B6. Fit the DS1307 clock module. Read all of this before connecting anything: a standard DS1307 module can damage the Jetson.**

The DS1307 chip needs 5 V. Most modules (the common "Tiny RTC I2C" board) pull the two data lines, SDA and SCL, up to that 5 V with two small resistors. The Jetson's header pins are 3.3 V only, so a module wired in as it comes puts 5 V onto them. The fix is to remove those two pull-up resistors. The Jetson already has its own 3.3 V pull-ups on pins 3 and 5, so the bus still works.

1. **Find and remove the two pull-up resistors.** On your board (checked against your photos): the chip marked `DS1307N` is U2, the `24C32` memory chip is U3, and the orange glass part beside the `SQ` pin is the diode D1. The two matching resistors along the bottom edge, labelled **R2** and **R3**, are my best guess for the SDA and SCL pull-ups. The photo is too small to read their markings, so prove it before you desolder anything. Module unpowered, battery out, multimeter on continuity (beep):
   - Touch one probe to the **VCC** pin. With the other probe, touch each end of R2, then each end of R3. Exactly one end of each should beep (that end is on the VCC line).
   - Then touch one probe to the **SDA** pin and test the *other* end of R2 and R3. One of the two should beep to SDA, and the other to **SCL**.
   - A resistor that links VCC to SDA, or VCC to SCL, is a pull-up. Remove those two (soldering iron: heat one end, then the other, and slide it off). If R2 and R3 turn out not to be the ones, test the other small resistors the same way.
   - Check the result with the multimeter on resistance (Ω): VCC to SDA and VCC to SCL must both read **open** (`OL`, or several MΩ). A reading of a few kΩ means a pull-up is still there.
   - If you would rather not modify the module, put a small bidirectional level-shifter board between it and the Jetson instead.
   - The DS1307 still reads the Jetson's 3.3 V signals correctly, because it accepts anything above 2.2 V as a high.
2. **Battery.** Your photo shows the holder empty. Read the label on the coin cell you fit.
   - **LIR2032** (rechargeable): leave the board alone.
   - **CR2032** (not rechargeable): it must not be charged, because a charged CR2032 can leak or overheat. The charge path is VCC, a small resistor, then the diode D1, then the battery. **Remove D1** to break it:
     1. With the battery out, power the module from 5 V (VCC and GND pins) and measure the pin labelled `BAT` on the right header to GND. About 4.3 to 4.7 V means the charge path is live.
     2. Unpower the module. Remove D1: add a little fresh solder to both ends so they join, heat both ends together with a wide tip, and slide the part off. Do not wrench it: it is glass. (Alternative with no soldering: crush it with flush cutters, wearing eye protection, and pick out the pieces.)
     3. Power the module again and measure `BAT` to GND: it must now read close to 0 V.
     4. Fit the CR2032 with the `+` side up and measure `BAT` to GND with the module powered. It should read about 3 V and **stay** at 3 V. A reading that creeps upward means something is still charging it: take the battery out.
   - After fitting the battery, the B7 power-off test shows whether the module keeps time.
3. **Wire it with the Jetson powered off.** Pin 1 is marked on the board. The odd pins (1, 3, 5) run along one side and the even pins (2, 4, 6) along the other, so pin 2 is directly across from pin 1.

| DS1307 module | Jetson header pin |
|---|---|
| VCC | 2 (5 V) |
| SDA | 3 |
| SCL | 5 |
| GND | 6 |

Use the four pins labelled `SCL`, `SDA`, `VCC` and `GND` on either header of the module. Leave `BAT`, `DS`, `SQW` and `32K` unconnected.

4. Power on, and check that the chip is seen: `sudo i2cdetect -y -r 1` should show `68` (it may also show `50`, the small memory chip on the same board). If nothing shows, check the wiring.
5. The module is about 28 × 25 mm and fits on the floor at the back of the pod, between the two rear screw posts. Fix it with double-sided tape.

**B7. Set the clock module once.** Wait until the Jetson has network time (`timedatectl` says `synchronized: yes`), then:
```
sudo python3 ~/jetson_app/rtc_clock.py save
sudo python3 ~/jetson_app/rtc_clock.py show
```
Before the first `save`, `show` says the clock is halted. That is normal. From now on the time is copied into the module every 30 minutes while the network is up, and loaded from it at every boot. The DS1307 is not temperature-compensated and can drift by a minute or two per month, which is why it is refreshed so often. It only matters if the Jetson goes weeks with no network at all.

**B8. Load your students** (a CSV with the columns `reg_no,name,department` and optionally `card_uid`):
```
ATT_DB=/var/lib/attendance/attendance.db python3 ~/jetson_app/server.py import ~/students.csv
```
A card that is not in the list is refused (red ring) until an admin registers it at the kiosk. See B9b.

**B9. Plug in the ESP8266.** First load `esp8266_firmware/esp8266_firmware.ino` onto it with the Arduino IDE (board NodeMCU 1.0; libraries "MFRC522" and "Adafruit NeoPixel"). Wire the LED ring: ring DI to D0 through a 330 ohm resistor, ring GND to G, ring 5V to VIN. If the ring flickers, put a 1N4001 diode in the ring's 5V line or a 74AHCT125 level shifter on DI. The ring is kept dim on purpose so a USB port can power it. Then plug the ESP into the Jetson: `ls /dev/ttyUSB*` should show a device, and the red banner "Card reader not connected" disappears within 15 seconds. The ring starts chasing a light round the circle.

**B9b. Set up the two admin cards.** Only an admin can register a new card, so do this once for each of your two admins:
1. The admin taps their card on the reader. It is not known yet, so the ring turns red and the screen says "Card not registered".
2. Straight after, on the Jetson: `python3 ~/jetson_app/server.py admin add-last "Admin Name"` (add `ATT_DB=/var/lib/attendance/attendance.db` in front if it says it cannot find the database).
3. Check with `python3 ~/jetson_app/server.py admin list`. At most 2 admins; `admin remove CARDID` frees a place.

An admin card never marks attendance: it only unlocks registration. To try everything on your Mac without any hardware, run `ATT_SIM=1 python3 jetson_app/server.py` and open http://localhost:8080: two demo admin cards are built in and a panel shows the ring colour.

**B10. Restart and watch it come up on its own:**
```
sudo reboot
```
After about a minute or two the attendance page should open full screen with no one touching anything.

---

## Part C: Prove that it works

0. **Registration.** Tap a card that is not in the list: the ring flashes blue, then goes red, and the screen says "Please call a lab admin". An admin taps their card: the ring turns yellow and a form opens. The admin types the registration number, name and department and presses Save: the ring turns green, the person picks a reason, and the in-time is recorded. Tapping the same card later marks them out.

1. **Power cut.** Pull the power while the page is showing, then plug it back in. The page returns by itself, and the visits from before are still on the board.
2. **No network.** Unplug the network (or turn off the router), tap three cards and choose reasons. The board shows "3 visits waiting to upload". Plug the network back in. Within about a minute it says "All visits uploaded".
3. **Look in the cloud** (on your Mac, in `cloudflare/worker`):
   ```
   wrangler d1 execute attendance --remote --command "SELECT p.name, v.entered_at, v.exited_at, v.time_quality FROM visits v JOIN people p ON p.id = v.person_id ORDER BY v.id DESC LIMIT 20"
   ```
4. **Time after a power cut with no network.** Disconnect the network, `sudo reboot`, and when it is back run `date`. It should be right to the second. If it is not, the clock module is not set (redo B7) or not wired (redo B6).

---

## How it works

**Saving.** Every visit goes into `/var/lib/attendance/attendance.db` on the Jetson the moment it happens, with a unique ID. A background task sends changed visits to Cloudflare every 20 seconds, or at once after a tap. If the network is down it waits and retries, slowing to once every two minutes. Each visit has a unique ID, so a retry can never create a duplicate in the cloud.

**Time.** All times are stored in UTC and shown in IST.

| Situation | What happens |
|---|---|
| Clock module fitted and set | The correct time is loaded at boot, even with no network. Times are marked `synced`. |
| Clock module battery flat or never set, no network | The Jetson also saves the time every 5 seconds. After a power cut it starts from that saved time instead of a stale date. The real time is later by however long the power was off, so these visits are marked unsure until the network returns. |
| No clock module, or its battery is dead, and no network yet | The tap is recorded with the Jetson's best guess and marked as unsure. When network time arrives, the time is corrected from the Jetson's uptime counter, to within about a second. These are marked `corrected`. |
| Power was cut before the network ever came back | The time cannot be corrected. The visit is kept and marked `estimated` so you can tell. |

You can see how many visits fall in each group:
```
wrangler d1 execute attendance --remote --command "SELECT time_quality, COUNT(*) FROM visits GROUP BY 1"
```

**If someone forgets to tap out.** When they next tap in, the old visit is closed as `auto` at that moment, so nobody is ever "inside" twice.

## Everyday commands (on the Jetson)

| Task | Command |
|---|---|
| Watch what it is doing | `journalctl -u attendance -f` |
| Restart the backend | `sudo systemctl restart attendance` |
| Export all visits to a spreadsheet | `ATT_DB=/var/lib/attendance/attendance.db python3 ~/jetson_app/server.py export ~/visits.csv` |
| How many visits are waiting to upload | `sqlite3 /var/lib/attendance/attendance.db "SELECT COUNT(*) FROM visits WHERE rev>synced_rev"` |
| Change a setting | `sudo nano /etc/attendance.env`, then restart the backend |

## When something is wrong

| What you see | Cause and fix |
|---|---|
| Stuck at a login screen after power-up | Automatic login did not switch on. Turn it on in Settings, Users, or check `setup.sh` printed "automatic login enabled". |
| Desktop appears but no attendance page | `systemctl status attendance` must say active. Look for the browser line in `~/.config/autostart/attendance-kiosk.desktop`. Run `~/jetson_app/kiosk.sh` by hand to see errors. |
| Page looks broken or unstyled | The browser is too old. See B5. |
| Red banner: card reader not connected | The ESP8266 USB cable is loose, it is a charge-only cable, or `ls /dev/ttyUSB*` shows nothing. |
| Red banner: clock isn't set yet | The clock module is not set or its battery is flat, and there is no network yet. It clears itself once the network connects; the visits are corrected automatically. |
| `i2cdetect` does not show 68 | The clock module is not wired correctly, or I2C is not enabled. Check the four wires and pins, then reboot. |
| Footer says visits are waiting for a long time | Check the network. `journalctl -u attendance` shows the reason. "server said 401" means the key in `/etc/attendance.env` does not match the one registered in step A4. |
| `wrangler` says it cannot find the database | The `database_id` in `wrangler.toml` is still the placeholder. |

## Keeping the data safe
- Use the 5 V 4 A supply and a good SD card. Visits are written with the safest SQLite setting, so a power cut should not lose a visit that was already confirmed on screen. A worn or fake SD card can still corrupt the file, which is why the cloud copy matters.
- The cloud copy is your backup. To keep another one, run the export command above now and then.
- The database holds students' names and registration numbers. Keep the Jetson and the Cloudflare account secure, and delete data you no longer need.
