"""Local reset endpoint: forget WiFi credentials and the API token, then
bring BLE provisioning back.  Served on the SimpleAQ-AP network."""

import os
import subprocess

import dotenv
from flask import Flask, jsonify

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
    """Clear WiFi credentials without destroying the SimpleAQ-AP profile.

    The shared "Wifi" profile is reset to its placeholder values rather than
    deleted, because hostap_config.py, wifi-watchdog.sh and ble_setup.py all
    look it up by name.  Any other station profile (for example one created by
    an older image with `nmcli dev wifi connect`) is deleted.
    """
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
                      "802-11-wireless-security.psk", WIFI_PSK_PLACEHOLDER)
                # Deactivate so the device actually goes offline.  Provisioning
                # re-activates the profile with an explicit `connection up`.
                subprocess.run(["nmcli", "connection", "down", name], check=False, timeout=60)
            else:
                nmcli("connection", "delete", name)
        except Exception as e:
            app.logger.error("Could not reset connection %r: %s", name, e)


@app.route('/reset', methods=['POST', 'GET'])
def reset_device():
    # 1. Forget WiFi credentials (keeps the setup access point).
    forget_wifi_networks()

    # 2. Reset the stored SimpleAQ device token.  ble_setup.py treats the
    #    placeholder as "unprovisioned" and starts advertising again.
    try:
        dotenv.set_key(ENV_FILE, "influx_token", TOKEN_PLACEHOLDER)
    except Exception as e:
        app.logger.error("Could not reset influx_token in %s: %s", ENV_FILE, e)

    if os.path.exists(TOKEN_ENV_FILE):
        try:
            os.remove(TOKEN_ENV_FILE)
        except Exception as e:
            app.logger.error("Could not remove %s: %s", TOKEN_ENV_FILE, e)

    # 3. Stop SimpleAQ data collection service.
    subprocess.run(["systemctl", "stop", "simpleaq.service"], check=False)

    # 4. Restart the BLE service so it picks up the cleared token and advertises.
    subprocess.Popen(["systemctl", "restart", "simpleaq-ble.service"])

@app.route('/provision', methods=['POST'])
def provision_device():
    from flask import request
    data = request.get_json(force=True, silent=True) or request.form or {}
    ssid = (data.get('ssid') or '').strip()
    password = data.get('password', '')
    token = (data.get('token') or '').strip()
    device_id = (data.get('device_id') or '').strip()

    if not ssid or not token:
        return jsonify({"error": "Missing ssid or token"}), 400

    if device_id:
        try:
            dotenv.set_key(ENV_FILE, "influx_bucket", device_id)
        except Exception as e:
            app.logger.error("Could not set influx_bucket: %s", e)

    try:
        from ble_setup import configure_wifi_profile, activate_wifi, persist_token
        configure_wifi_profile(ssid, password)
        activate_wifi()
        persist_token(token)
        subprocess.run(["systemctl", "restart", "simpleaq.service"], check=False)
        return jsonify({"status": "Provisioning successful"}), 200
    except Exception as e:
        app.logger.exception("Provisioning failed: %s", e)
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=80)

