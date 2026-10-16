#!/usr/bin/env python3
"""SimpleAQ BLE provisioning service using modern asynchronous Bless (Bleak GATT Server).

Advertises a GATT service that the SimpleAQ app uses to hand the device its
WiFi credentials, device id and API token. Built on Bless/Bleak to handle
asynchronous D-Bus events, MTU negotiations, and non-blocking I/O reliably.
"""

import asyncio
import logging
import os
import re
import subprocess
import sys
import time
from typing import Any

import dotenv
from bless import (
    BlessServer,
    BlessGATTCharacteristic,
    GATTCharacteristicProperties,
    GATTAttributePermissions,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("simpleaq-ble")

ENV_FILE = "/etc/environment"
TOKEN_ENV_FILE = "/etc/simpleaq/device_token.env"
TOKEN_PLACEHOLDER = "YOUR_TOKEN_HERE"
BUCKET_PLACEHOLDER = "YOUR_BUCKET_HERE"

WIFI_CONNECTION = "Wifi"
WIFI_INTERFACE = "wlan0"
INTERNET_CHECK_HOST = "www.simpleaq.org"

LOCAL_NAME = "SimpleAQ_Setup"
SERVICE_UUID = "b4250400-fb4b-4746-b2b0-93f0e61122c6"
SSID_UUID = "b4250401-fb4b-4746-b2b0-93f0e61122c6"
PASS_UUID = "b4250402-fb4b-4746-b2b0-93f0e61122c6"
TOKEN_UUID = "b4250403-fb4b-4746-b2b0-93f0e61122c6"
DEVICE_ID_UUID = "b4250404-fb4b-4746-b2b0-93f0e61122c6"
STATUS_UUID = "b4250405-fb4b-4746-b2b0-93f0e61122c6"

STATUS_IDLE = "IDLE"
STATUS_CONNECTING = "CONNECTING"
STATUS_CONNECTED = "CONNECTED"
STATUS_WIFI_FAILED = "WIFI_FAILED"
STATUS_NO_INTERNET = "NO_INTERNET"

WIFI_CONNECT_TIMEOUT_SEC = 45
INTERNET_CHECK_WINDOW_SEC = 30
SHUTDOWN_GRACE_SEC = 60


def env_value(key: str) -> str:
    try:
        return (dotenv.dotenv_values(ENV_FILE).get(key) or "").strip()
    except Exception as e:
        log.error("Could not read %s: %s", ENV_FILE, e)
        return ""


def is_provisioned() -> bool:
    token = env_value("influx_token")
    return bool(token) and token != TOKEN_PLACEHOLDER


def check_internet(timeout_sec=3) -> bool:
    try:
        subprocess.run(["ping", "-c", "1", "-W", str(timeout_sec), INTERNET_CHECK_HOST],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=True, timeout=timeout_sec + 5)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def wait_for_internet(window_sec: int) -> bool:
    deadline = time.monotonic() + window_sec
    while True:
        if check_internet():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(3)


def get_device_id() -> str:
    bucket = env_value("influx_bucket")
    if bucket and bucket != BUCKET_PLACEHOLDER:
        m = re.search(r"(SimpleAQ_Pi_[a-zA-Z0-9]+)", bucket)
        if m:
            return m.group(1)
        return bucket
    dev_id = "SimpleAQ_Pi"
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("Serial"):
                    dev_id = f"SimpleAQ_Pi_{line.split(':')[1].strip()}"
                    break
    except Exception:
        pass
    return dev_id


def persist_token(token: str):
    try:
        dotenv.set_key(ENV_FILE, "influx_token", token)
    except Exception as e:
        log.error("Failed to update influx_token in %s: %s", ENV_FILE, e)
    try:
        os.makedirs(os.path.dirname(TOKEN_ENV_FILE), exist_ok=True)
        with open(TOKEN_ENV_FILE, "w") as f:
            f.write(f"SIMPLEAQ_TOKEN={token}\n")
    except Exception as e:
        log.error("Failed to write %s: %s", TOKEN_ENV_FILE, e)


def nmcli(*args, timeout=60):
    return subprocess.run(["nmcli", *args], check=True, timeout=timeout,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def wifi_profile_exists() -> bool:
    names = nmcli("-t", "-f", "NAME", "connection", "show").stdout.splitlines()
    return WIFI_CONNECTION in names


def configure_wifi_profile(ssid: str, password: str):
    if not wifi_profile_exists():
        log.warning("NetworkManager profile %r is missing; recreating it", WIFI_CONNECTION)
        nmcli("connection", "add", "type", "wifi", "ifname", WIFI_INTERFACE,
              "con-name", WIFI_CONNECTION, "ssid", ssid)
    common = ["802-11-wireless.ssid", ssid,
              "802-11-wireless.band", "bg",
              "802-11-wireless.powersave", "2",
              "connection.autoconnect", "yes",
              "connection.autoconnect-retries", "0",
              "ipv4.dns-priority", "-100",
              "ipv4.route-metric", "100"]
    if password:
        nmcli("connection", "modify", WIFI_CONNECTION, *common,
              "802-11-wireless-security.key-mgmt", "wpa-psk",
              "802-11-wireless-security.psk", password,
              "802-11-wireless-security.psk-flags", "0")
    else:
        nmcli("connection", "modify", WIFI_CONNECTION, *common)
        nmcli("connection", "modify", WIFI_CONNECTION, "remove", "802-11-wireless-security")


def activate_wifi():
    nmcli("--wait", str(WIFI_CONNECT_TIMEOUT_SEC), "connection", "up", WIFI_CONNECTION,
          timeout=WIFI_CONNECT_TIMEOUT_SEC + 15)


def _sync_join(ssid: str, password: str, token: str) -> str:
    try:
        configure_wifi_profile(ssid, password)
        activate_wifi()
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or e.stdout or "").strip()
        log.error("WiFi join failed for %r: %s", ssid, detail)
        return STATUS_WIFI_FAILED
    except subprocess.TimeoutExpired:
        log.error("WiFi join timed out for %r", ssid)
        return STATUS_WIFI_FAILED
    except Exception:
        log.exception("Unexpected error while joining %r", ssid)
        return STATUS_WIFI_FAILED

    log.info("Joined WiFi network %r", ssid)
    persist_token(token)
    subprocess.run(["systemctl", "restart", "simpleaq.service"], check=False)

    if wait_for_internet(INTERNET_CHECK_WINDOW_SEC):
        log.info("Backend reachable; provisioning complete")
        return STATUS_CONNECTED
    else:
        log.warning("Joined WiFi but %s is unreachable; token saved", INTERNET_CHECK_HOST)
        return STATUS_NO_INTERNET


class BlessProvisioner:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.server: BlessServer = None
        self.ssid: str = ""
        self.password: str = None
        self.token: str = ""
        self.status: str = STATUS_IDLE
        self.join_task: asyncio.Task = None
        self.stop_event = asyncio.Event()

    def set_status(self, new_status: str):
        self.status = new_status
        log.info("Status -> %s", new_status)
        if self.server:
            try:
                char = self.server.get_characteristic(STATUS_UUID)
                if char:
                    char.value = bytearray(new_status.encode("utf-8"))
                    self.server.update_value(SERVICE_UUID, STATUS_UUID)
            except Exception as e:
                log.warning("Failed to notify status: %s", e)

    def read_request(self, characteristic: BlessGATTCharacteristic, **kwargs) -> bytearray:
        uuid = str(characteristic.uuid).lower()
        if uuid == DEVICE_ID_UUID:
            dev_id = get_device_id()
            log.info("Read Device ID: %r", dev_id)
            return bytearray(dev_id.encode("utf-8"))
        elif uuid == STATUS_UUID:
            log.info("Read Status: %r", self.status)
            return bytearray(self.status.encode("utf-8"))
        return bytearray(characteristic.value or b"")

    def write_request(self, characteristic: BlessGATTCharacteristic, value: Any, **kwargs):
        uuid = str(characteristic.uuid).lower()
        raw = bytes(value)
        text = raw.decode("utf-8", errors="replace")

        if uuid == SSID_UUID:
            self.ssid = text
            log.info("Received SSID: %r", self.ssid)
            self._maybe_start_join()
        elif uuid == PASS_UUID:
            self.password = text
            log.info("Received Wi-Fi password (%d characters)", len(self.password))
            self._maybe_start_join()
        elif uuid == TOKEN_UUID:
            self.token = text
            log.info("Received token (%d characters)", len(self.token))
            self._maybe_start_join()
        elif uuid == DEVICE_ID_UUID:
            dev_id = text.strip()
            m = re.search(r"(SimpleAQ_Pi_[a-zA-Z0-9]+)", dev_id)
            if m:
                dev_id = m.group(1)
            log.info("Set Device ID: %r", dev_id)
            try:
                dotenv.set_key(ENV_FILE, "influx_bucket", dev_id)
            except Exception as e:
                log.error("Failed to update influx_bucket: %s", e)

    def _maybe_start_join(self):
        if not self.ssid or self.password is None or not self.token:
            return
        if self.join_task and not self.join_task.done():
            log.info("WiFi join already in progress; ignoring write")
            return
        log.info("All credentials received! Starting Wi-Fi join task...")
        self.join_task = self.loop.create_task(self._do_join(self.ssid, self.password, self.token))

    async def _do_join(self, ssid: str, password: str, token: str):
        self.set_status(STATUS_CONNECTING)
        final_status = await asyncio.to_thread(_sync_join, ssid, password, token)
        self.set_status(final_status)

        if final_status in (STATUS_CONNECTED, STATUS_NO_INTERNET):
            log.info("Provisioning succeeded! Server will shutdown in %d s", SHUTDOWN_GRACE_SEC)
            await asyncio.sleep(SHUTDOWN_GRACE_SEC)
            self.stop_event.set()


async def run_server(loop: asyncio.AbstractEventLoop):
    prov = BlessProvisioner(loop)
    server = BlessServer(name=LOCAL_NAME, loop=loop)
    prov.server = server

    server.read_request_func = prov.read_request
    server.write_request_func = prov.write_request

    await server.add_new_service(SERVICE_UUID)

    write_flags = (
        GATTCharacteristicProperties.write |
        GATTCharacteristicProperties.write_without_response
    )
    read_write_flags = (
        GATTCharacteristicProperties.read |
        GATTCharacteristicProperties.write |
        GATTCharacteristicProperties.write_without_response
    )
    read_notify_flags = (
        GATTCharacteristicProperties.read |
        GATTCharacteristicProperties.notify
    )

    permissions_rw = (
        GATTAttributePermissions.readable |
        GATTAttributePermissions.writeable
    )
    permissions_w = GATTAttributePermissions.writeable
    permissions_r = GATTAttributePermissions.readable

    await server.add_new_characteristic(
        SERVICE_UUID, SSID_UUID, write_flags, None, permissions_w
    )
    await server.add_new_characteristic(
        SERVICE_UUID, PASS_UUID, write_flags, None, permissions_w
    )
    await server.add_new_characteristic(
        SERVICE_UUID, TOKEN_UUID, write_flags, None, permissions_w
    )
    await server.add_new_characteristic(
        SERVICE_UUID, DEVICE_ID_UUID, read_write_flags,
        bytearray(get_device_id().encode("utf-8")), permissions_rw
    )
    await server.add_new_characteristic(
        SERVICE_UUID, STATUS_UUID, read_notify_flags,
        bytearray(STATUS_IDLE.encode("utf-8")), permissions_r
    )

    log.info("Starting Bless BLE GATT server (%r)...", LOCAL_NAME)
    await server.start()
    log.info("Bless BLE GATT server is actively advertising!")

    await prov.stop_event.wait()
    log.info("Stopping BLE server...")
    await server.stop()
    log.info("Bless BLE server stopped.")


def main():
    if is_provisioned():
        log.info("Device is already provisioned; not advertising.")
        return 0

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(run_server(loop))
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        loop.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
