#!/bin/bash
# ap0-setup.sh: Manage SoftAP (ap0) interface based on device provisioning state.
#
# Raspberry Pi 3 (BCM43438) has a single physical radio. Concurrent AP+STA across
# different channels fails due to hardware time-slicing limitations.
# Therefore, ap0 is only created and activated when the device is UNPROVISIONED.
# When provisioned, wlan0 has exclusive control of the radio to connect to home Wi-Fi.

IFACE="wlan0"
AP_IFACE="ap0"
WIFI_CONN="Wifi"
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

SSID=$(nmcli -t -f 802-11-wireless.ssid connection show "$WIFI_CONN" 2>/dev/null | cut -d: -f2)

if [ -z "$SSID" ] || [ "$SSID" = "your_wifi_name" ]; then
    logger -t ap0-setup "Device is unprovisioned; creating $AP_IFACE and starting $AP_CONN"
    if ! ip link show "$AP_IFACE" &>/dev/null; then
        iw dev "$IFACE" interface add "$AP_IFACE" type __ap 2>/dev/null || true
    fi
    ip link set "$AP_IFACE" up 2>/dev/null || true
    nmcli device set "$AP_IFACE" managed yes 2>/dev/null || true
    nmcli connection up "$AP_CONN" 2>/dev/null || true
else
    logger -t ap0-setup "Device is provisioned with SSID '$SSID'; ensuring $AP_IFACE is disabled"
    nmcli connection down "$AP_CONN" 2>/dev/null || true
    iw dev "$AP_IFACE" del 2>/dev/null || true
fi

exit 0
