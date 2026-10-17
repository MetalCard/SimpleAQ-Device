#!/bin/bash
# ap0-setup.sh: Ensure ap0 hotspot interface is created and SimpleAQ-AP is active.
#
# Pi Zero W / Zero 2 W (BCM43438) supports concurrent SoftAP + Station mode
# as long as both operate on the same Wi-Fi channel.

IFACE="wlan0"
AP_IFACE="ap0"
AP_CONN="SimpleAQ-AP"

if [ "$1" = "stop" ]; then
    logger -t ap0-setup "Stopping $AP_CONN and removing $AP_IFACE"
    nmcli connection down "$AP_CONN" 2>/dev/null || true
    iw dev "$AP_IFACE" del 2>/dev/null || true
    exit 0
fi

# Wait up to 30 seconds for wlan0 to become available in NetworkManager
for _ in $(seq 1 30); do
    if nmcli -t -f DEVICE device status 2>/dev/null | grep -qx "$IFACE"; then
        break
    fi
    sleep 1
done

logger -t ap0-setup "Ensuring $AP_IFACE exists and starting $AP_CONN"
if ! ip link show "$AP_IFACE" &>/dev/null; then
    iw dev "$IFACE" interface add "$AP_IFACE" type __ap 2>/dev/null || true
fi
ip link set "$AP_IFACE" up 2>/dev/null || true
nmcli device set "$AP_IFACE" managed yes 2>/dev/null || true
nmcli connection up "$AP_CONN" 2>/dev/null || true

exit 0
