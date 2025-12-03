#!/bin/sh
set -e

export NO_AT_BRIDGE=1
export SESSION_MANAGER=""
export DBUS_SESSION_BUS_ADDRESS=""
export USER=middleman

if [ -e /tmp/.X11-unix/X99 ]; then
    echo "X socket already exists, using existing X server"
    export DISPLAY=:99
else
    export DISPLAY=:99
    echo "Starting TigerVNC server on DISPLAY=$DISPLAY..."
    Xvnc -alwaysshared ${DISPLAY} -geometry 1920x1080 -depth 24 -rfbport 5900 -SecurityTypes None &
    sleep 2
    echo "TigerVNC server running on DISPLAY=$DISPLAY"
fi

echo "Starting DBus session"
eval $(dbus-launch --sh-syntax)
export SESSION_MANAGER=""

echo "Starting JWM (Joe's Window Manager)"
cp /app/.jwmrc $HOME
jwm >/dev/null 2>&1 &

# So that the desktop is not completely empty
xeyes &

/app/.venv/bin/python /app/middleman.py
