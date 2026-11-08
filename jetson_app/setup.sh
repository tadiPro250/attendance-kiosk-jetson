#!/usr/bin/env bash
# One-time setup on the Jetson Nano.  From this folder, as your normal user:   sudo bash setup.sh
# Safe to run again: it never overwrites /etc/attendance.env once it exists.
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "Run it with sudo:  sudo bash setup.sh"; exit 1; }
USER_NAME=${SUDO_USER:-}
if [ -z "$USER_NAME" ] || [ "$USER_NAME" = root ]; then echo "Run it as your normal user with sudo, not as root."; exit 1; fi
APP_DIR=$(cd "$(dirname "$0")" && pwd)
HOME_DIR=$(getent passwd "$USER_NAME" | cut -d: -f6)
ENV_FILE=/etc/attendance.env
DATA_DIR=/var/lib/attendance
step() { echo; echo "==> $*"; }

step "Installing packages"
apt-get update
apt-get install -y python3-serial python3-smbus i2c-tools x11-xserver-utils curl
if ! command -v chromium-browser >/dev/null && ! command -v firefox >/dev/null; then
  apt-get install -y chromium-browser || apt-get install -y firefox || echo "WARNING: no browser installed; install chromium-browser or firefox by hand."
fi

step "Time zone and network time"
timedatectl set-timezone Asia/Kolkata
timedatectl set-ntp true

step "I2C for the clock module"
echo i2c-dev > /etc/modules-load.d/i2c-dev.conf
modprobe i2c-dev || true

step "Folders and settings file"
install -d -o "$USER_NAME" -g "$USER_NAME" -m 755 "$DATA_DIR"
if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<ENV
# Settings for the attendance kiosk. Edit, then:  sudo systemctl restart attendance
ATT_DB=$DATA_DIR/attendance.db
ATT_LAB=Computer Lab
# Cloudflare upload. Leave ATT_API_URL empty to keep everything local only.
ATT_API_URL=
ATT_READER_ID=lab-kiosk
ATT_READER_KEY=
# Clock module on the 40-pin header: ds1307 or ds3231
RTC_CHIP=ds1307
ENV
  chmod 600 "$ENV_FILE"; echo "created $ENV_FILE"
else
  echo "kept existing $ENV_FILE"
  grep -q '^RTC_CHIP=' "$ENV_FILE" || { printf '\n# Clock module on the 40-pin header: ds1307 or ds3231\nRTC_CHIP=ds1307\n' >> "$ENV_FILE"; echo "added RTC_CHIP=ds1307"; }
fi

step "Serial port permission for the ESP8266"
usermod -aG dialout "$USER_NAME"

step "Services"
cat > /etc/systemd/system/attendance-rtc-load.service <<UNIT
[Unit]
Description=Set the system clock from the I2C RTC (DS1307 or DS3231) at boot
DefaultDependencies=no
After=systemd-modules-load.service local-fs.target
Before=systemd-timesyncd.service attendance.service
Conflicts=shutdown.target
Before=shutdown.target

[Service]
Type=oneshot
RemainAfterExit=yes
EnvironmentFile=-$ENV_FILE
ExecStartPre=/bin/mkdir -p /run/attendance
ExecStart=-/usr/bin/python3 $APP_DIR/rtc_clock.py load

[Install]
WantedBy=sysinit.target
UNIT

cat > /etc/systemd/system/attendance-rtc-save.service <<UNIT
[Unit]
Description=Copy network time into the I2C RTC (only when synced)

[Service]
Type=oneshot
EnvironmentFile=-$ENV_FILE
ExecStartPre=/bin/mkdir -p $DATA_DIR
ExecStart=-/usr/bin/python3 $APP_DIR/rtc_clock.py save
UNIT

cat > /etc/systemd/system/attendance-rtc-save.timer <<UNIT
[Unit]
Description=Keep the I2C RTC in step with network time

[Timer]
OnBootSec=5min
OnUnitActiveSec=30min

[Install]
WantedBy=timers.target
UNIT

cat > /etc/systemd/system/attendance.service <<UNIT
[Unit]
Description=Lab attendance backend
After=local-fs.target attendance-rtc-load.service
Wants=attendance-rtc-load.service

[Service]
User=$USER_NAME
WorkingDirectory=$APP_DIR
EnvironmentFile=-$ENV_FILE
ExecStart=/usr/bin/python3 $APP_DIR/server.py
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable attendance-rtc-load.service attendance-rtc-save.timer attendance.service
systemctl restart attendance.service
systemctl start attendance-rtc-save.timer

step "Open the attendance page automatically after every power-up"
mkdir -p "$HOME_DIR/.config/autostart"
cat > "$HOME_DIR/.config/autostart/attendance-kiosk.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=Attendance Kiosk
Exec=$APP_DIR/kiosk.sh
X-GNOME-Autostart-enabled=true
DESK
chown -R "$USER_NAME": "$HOME_DIR/.config/autostart"
chmod +x "$APP_DIR/kiosk.sh"

# log in automatically, otherwise the machine waits at the login screen after a power cut
if [ -f /etc/gdm3/custom.conf ]; then
  cp -n /etc/gdm3/custom.conf /etc/gdm3/custom.conf.attendance-backup
  sed -i '/^#\?[[:space:]]*AutomaticLogin\(Enable\)\?[[:space:]]*=/d' /etc/gdm3/custom.conf
  sed -i "/^\[daemon\]/a AutomaticLoginEnable=true\nAutomaticLogin=$USER_NAME" /etc/gdm3/custom.conf
  echo "automatic login enabled (gdm3) for $USER_NAME"
elif [ -d /etc/lightdm ]; then
  mkdir -p /etc/lightdm/lightdm.conf.d
  printf '[Seat:*]\nautologin-user=%s\nautologin-user-timeout=0\n' "$USER_NAME" > /etc/lightdm/lightdm.conf.d/50-attendance-autologin.conf
  echo "automatic login enabled (lightdm) for $USER_NAME"
else
  echo "WARNING: could not find gdm3 or lightdm. Turn on automatic login in the desktop settings by hand."
fi

step "Checks"
sleep 2
systemctl is-active attendance.service || true
curl -s -o /dev/null -w "kiosk page: HTTP %{http_code}\n" http://127.0.0.1:8080/ || true
timedatectl | grep -iE "time zone|synchronized|NTP" || true
ls /dev/i2c-1 >/dev/null 2>&1 && echo "I2C bus 1 present" || echo "I2C bus 1 not found yet (a reboot may be needed)"
echo
echo "Done. Next: edit $ENV_FILE (Cloudflare URL and reader key), connect the RTC, then reboot. See JETSON_SETUP.md."
