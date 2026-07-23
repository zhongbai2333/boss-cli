#!/bin/sh
set -u

# Container hostnames change on recreate; Chromium's process singleton files
# may therefore point at a process in a container that no longer exists.
# This dedicated profile is mounted by exactly one Chromium service.
rm -f \
    /home/chromium/profile/SingletonCookie \
    /home/chromium/profile/SingletonLock \
    /home/chromium/profile/SingletonSocket

chromium \
    --headless=new \
    --remote-debugging-address=127.0.0.1 \
    --remote-debugging-port=9222 \
    --remote-allow-origins=http://chromium \
    --user-data-dir=/home/chromium/profile \
    --no-first-run \
    --no-default-browser-check \
    --disable-sync \
    --disable-background-networking \
    --disable-component-update \
    --disable-default-apps \
    --disable-extensions \
    --window-size=1280,900 \
    about:blank &
chromium_pid=$!

socat TCP-LISTEN:9223,reuseaddr,fork TCP:127.0.0.1:9222 &
proxy_pid=$!

cleanup() {
    kill "$chromium_pid" "$proxy_pid" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

wait "$chromium_pid"
status=$?
kill "$proxy_pid" 2>/dev/null || true
wait "$proxy_pid" 2>/dev/null || true
exit "$status"