#!/usr/bin/env bash
# Opens the attendance page fullscreen and keeps it open. Started at every login by ~/.config/autostart/attendance-kiosk.desktop.
URL=http://127.0.0.1:8080/
until curl -s -o /dev/null "$URL"; do sleep 1; done          # wait for the backend service

# never blank the screen or lock the session (best effort: depends on the desktop)
xset s off -dpms 2>/dev/null; xset s noblank 2>/dev/null
gsettings set org.gnome.desktop.session idle-delay 0 2>/dev/null
gsettings set org.gnome.desktop.screensaver lock-enabled false 2>/dev/null
gsettings set org.gnome.desktop.screensaver idle-activation-enabled false 2>/dev/null

while true; do
  if command -v chromium-browser >/dev/null; then
    chromium-browser --kiosk --incognito --noerrdialogs --disable-infobars --disable-session-crashed-bubble \
      --disable-translate --check-for-update-interval=31536000 --overscroll-history-navigation=0 "$URL"
  elif command -v firefox >/dev/null; then
    firefox --kiosk "$URL"
  else
    echo "No browser found. Install chromium-browser or firefox." >&2; sleep 30
  fi
  sleep 2                                                      # the browser closed or crashed: open it again
done
