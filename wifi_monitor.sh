#!/bin/bash
# Mulchy WiFi monitor
# Activates the mulchywifi AP whenever no client connection is up.
#
# Flag file: /tmp/mulchy-connecting
#   Written by the web app before a connection attempt, cleared in a finally block.
#   Lives on tmpfs — wiped on every reboot, so a crash can never leave the Pi stuck.

AP_CON="mulchy-ap"
FLAG="/tmp/mulchy-connecting"
NO_CLIENT_COUNT=0        # consecutive polls with no client — AP only starts after threshold

log() { logger -t mulchy-wifi "$*"; }

# Grace period: give NetworkManager time to connect to known networks on boot
log "Starting — 25s startup grace period"
sleep 25

while true; do
    # A connection attempt is in progress — wait without intervening
    if [ -f "$FLAG" ]; then
        sleep 3
        continue
    fi

    # Active non-AP wifi connection?
    CLIENT=$(nmcli -t -f NAME,TYPE,STATE con show --active 2>/dev/null \
             | grep ":802-11-wireless:activated" \
             | grep -v "^${AP_CON}:")

    # AP currently up?
    AP_UP=$(nmcli -t -f NAME,STATE con show --active 2>/dev/null \
            | grep "^${AP_CON}:activated")

    if [ -z "$CLIENT" ] && [ -z "$AP_UP" ]; then
        NO_CLIENT_COUNT=$((NO_CLIENT_COUNT + 1))
        # Wait 3 consecutive polls (~30s) before starting AP — gives the user time
        # to select a network in the UI after disconnecting without the AP racing in.
        if [ "$NO_CLIENT_COUNT" -ge 3 ]; then
            log "No active connection for ${NO_CLIENT_COUNT} polls — starting AP"
            nmcli con up "$AP_CON" >/dev/null 2>&1 \
                && log "AP started" \
                || log "AP start failed"
        else
            log "No active connection (poll ${NO_CLIENT_COUNT}/3) — waiting before starting AP"
        fi
    elif [ -n "$CLIENT" ] && [ -n "$AP_UP" ]; then
        NO_CLIENT_COUNT=0
        log "Client connected — stopping AP"
        nmcli con down "$AP_CON" >/dev/null 2>&1 \
            && log "AP stopped"
    elif [ -n "$CLIENT" ]; then
        NO_CLIENT_COUNT=0
    fi

    sleep 10
done
