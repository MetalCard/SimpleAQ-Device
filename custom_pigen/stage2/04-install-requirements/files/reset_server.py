"""Local reset and provisioning server for SimpleAQ."""

import os
import subprocess
import threading
import time

import dotenv
from flask import Flask, jsonify, request

app = Flask(__name__)

ENV_FILE = "/etc/environment"
TOKEN_ENV_FILE = "/etc/simpleaq/device_token.env"
TOKEN_PLACEHOLDER = "YOUR_TOKEN_HERE"

# Must match files/wifi.nmconnection and files/simpleaq-ap.nmconnection.
WIFI_CONNECTION = "Wifi"
AP_CONNECTION = "SimpleAQ-AP"
WIFI_SSID_PLACEHOLDER = "your_wifi_name"
WIFI_PSK_PLACEHOLDER = "your_wifi_password"


def nmcli(*args):
    return subprocess.run(["nmcli", *args], check=True, timeout=60,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def forget_wifi_networks():
    """Clear WiFi credentials without destroying the SimpleAQ-AP profile."""
    try:
        lines = nmcli("-t", "-f", "NAME,TYPE", "connection", "show").stdout.splitlines()
    except Exception as e:
        app.logger.error("Could not list NetworkManager connections: %s", e)
        return

    for line in lines:
        name, _, ctype = line.rpartition(":")
        if ctype != "802-11-wireless" or name == AP_CONNECTION:
            continue
        try:
            mode = nmcli("-g", "802-11-wireless.mode", "connection", "show", name).stdout.strip()
            if mode == "ap":
                continue
            if name == WIFI_CONNECTION:
                nmcli("connection", "modify", name,
                      "802-11-wireless.ssid", WIFI_SSID_PLACEHOLDER,
                      "802-11-wireless-security.key-mgmt", "wpa-psk",
                      "802-11-wireless-security.psk", WIFI_PSK_PLACEHOLDER,
                      "802-11-wireless-security.psk-flags", "0")
                subprocess.run(["nmcli", "connection", "down", name], check=False, timeout=60)
            else:
                nmcli("connection", "delete", name)
        except Exception as e:
            app.logger.error("Could not reset connection %r: %s", name, e)


@app.route('/reset', methods=['POST', 'GET'])
def reset_device():
    # 1. Forget WiFi credentials
    forget_wifi_networks()

    # 2. Reset the stored SimpleAQ device token
    try:
        dotenv.set_key(ENV_FILE, "influx_token", TOKEN_PLACEHOLDER)
    except Exception as e:
        app.logger.error("Could not reset influx_token in %s: %s", ENV_FILE, e)

    if os.path.exists(TOKEN_ENV_FILE):
        try:
            os.remove(TOKEN_ENV_FILE)
        except Exception as e:
            app.logger.error("Could not remove %s: %s", TOKEN_ENV_FILE, e)

    # 3. Stop SimpleAQ data collection service
    subprocess.run(["systemctl", "stop", "simpleaq.service"], check=False)

    # 4. Ensure SimpleAQ-AP hotspot is active for re-provisioning
    subprocess.run(["nmcli", "connection", "up", AP_CONNECTION], check=False)

    return jsonify({"status": "Reset complete"}), 200


def configure_wifi_profile(ssid, password):
    args = [
        "nmcli", "connection", "modify", WIFI_CONNECTION,
        "802-11-wireless.ssid", ssid,
        "connection.autoconnect", "yes",
        "connection.autoconnect-retries", "0",
        "802-11-wireless.powersave", "2",
        "ipv4.dns-priority", "-100",
        "ipv4.route-metric", "100"
    ]
    if password:
        args.extend([
            "802-11-wireless-security.key-mgmt", "wpa-psk",
            "802-11-wireless-security.psk", password,
            "802-11-wireless-security.psk-flags", "0"
        ])
        subprocess.run(args, check=True)
    else:
        subprocess.run(args, check=True)
        subprocess.run(["nmcli", "connection", "modify", WIFI_CONNECTION,
                        "remove", "802-11-wireless-security"], check=False)


def transition_to_station_mode():
    """Temporarily pause AP to allow wlan0 to associate without channel collision,
    then bring SimpleAQ-AP back up on the exact same channel as home Wi-Fi."""
    time.sleep(2)
    # 1. Pause AP so wlan0 can freely associate across channels
    subprocess.run(["nmcli", "connection", "down", AP_CONNECTION], check=False)

    # 2. Bring up home Wi-Fi connection
    subprocess.run(["nmcli", "connection", "reload"], check=False)
    subprocess.run(["nmcli", "connection", "up", WIFI_CONNECTION], check=False)

    # 3. Detect connected channel on wlan0
    channel = None
    res = subprocess.run(["iw", "dev", "wlan0", "info"], stdout=subprocess.PIPE, text=True, check=False)
    for line in res.stdout.splitlines():
        if "channel" in line:
            parts = line.strip().split()
            if len(parts) >= 2 and parts[1].isdigit():
                channel = parts[1]
                break

    # 4. Sync SimpleAQ-AP channel to match wlan0 and bring it back up
    if channel:
        subprocess.run(["nmcli", "connection", "modify", AP_CONNECTION, "802-11-wireless.channel", channel], check=False)

    # Ensure ap0 interface exists and bring SimpleAQ-AP back up on matching channel
    subprocess.run(
        "ip link show ap0 >/dev/null 2>&1 || (iw dev wlan0 interface add ap0 type __ap 2>/dev/null && ip link set ap0 up 2>/dev/null && nmcli device set ap0 managed yes 2>/dev/null)",
        shell=True, check=False
    )
    subprocess.run(["nmcli", "connection", "up", AP_CONNECTION], check=False)

    # 5. Restart simpleaq data collection
    subprocess.run(["systemctl", "restart", "simpleaq.service"], check=False)


def persist_token(token):
    os.makedirs(os.path.dirname(TOKEN_ENV_FILE), exist_ok=True)
    with open(TOKEN_ENV_FILE, "w") as f:
        f.write(f"influx_token={token}\n")
    dotenv.set_key(ENV_FILE, "influx_token", token, quote_mode="always")


@app.route('/provision', methods=['POST', 'GET'])
def provision_device():
    data = request.get_json(force=True, silent=True) or request.form or {}
    ssid = (data.get('ssid') or request.values.get('ssid') or '').strip()
    password = data.get('password') or request.values.get('password') or ''
    token = (data.get('device_token') or data.get('token') or request.values.get('device_token') or request.values.get('token') or '').strip()
    device_id = (data.get('device_id') or request.values.get('device_id') or '').strip()

    if not ssid:
        return jsonify({"error": "Missing ssid"}), 400

    if device_id:
        try:
            dotenv.set_key(ENV_FILE, "influx_bucket", device_id)
            dotenv.set_key(ENV_FILE, "simpleaq_device_id", device_id)
        except Exception as e:
            app.logger.error("Could not set influx_bucket/device_id: %s", e)

    try:
        configure_wifi_profile(ssid, password)
        if token:
            persist_token(token)

        threading.Thread(target=transition_to_station_mode, daemon=True).start()

        return jsonify({"success": True, "status": "Provisioning successful"}), 200
    except Exception as e:
        app.logger.exception("Provisioning failed: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
