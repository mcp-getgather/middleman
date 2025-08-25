#!/bin/sh
set -e

export DISPLAY=:99
echo "Starting Xvfb on DISPLAY=$DISPLAY..."
Xvfb :99 -screen 0 1920x1080x24 >/dev/null 2>&1 &

while [ ! -e /tmp/.X11-unix/X99 ]; do
  sleep 0.1
done
echo "Xvfb running on DISPLAY=$DISPLAY"

echo "Starting JWM (Joe's Window Manager)"
jwm >/dev/null 2>&1 &

echo "Starting x11vnc server..."
x11vnc \
  -forever \
  -nopw \
  -rfbport 5900 \
  -display :99 \
  -listen 0.0.0.0 \
  -quiet \
  -no6 >/dev/null 2>&1 &
echo "VNC server started on port 5900"
websockify --web /usr/share/novnc/ 3001 localhost:5900 &
echo "noVNC viewable at http://localhost:3001"

# So that the desktop is not completely empty
xeyes &
xclock &

/app/.venv/bin/python /app/middleman.py
