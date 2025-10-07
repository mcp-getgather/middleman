#!/bin/sh
set -e

export DISPLAY=:99
export NO_AT_BRIDGE=1
export SESSION_MANAGER=""
export DBUS_SESSION_BUS_ADDRESS=""

echo "Starting TigerVNC server on DISPLAY=$DISPLAY..."
Xvnc -alwaysshared ${DISPLAY} -geometry 1920x1080 -depth 24 -rfbport 5900 -SecurityTypes None &
sleep 2
echo "TigerVNC server running on DISPLAY=$DISPLAY"

echo "Starting DBus session"
eval $(dbus-launch --sh-syntax)
export SESSION_MANAGER=""

echo "Starting JWM (Joe's Window Manager)"
cp /app/.jwmrc $HOME
jwm >/dev/null 2>&1 &

echo "VNC server started on port 5900"
websockify --web /usr/share/novnc/ 3001 localhost:5900 &
echo "noVNC viewable at http://localhost:3001"

# So that the desktop is not completely empty
xeyes &

/app/.venv/bin/python /app/middleman.py
