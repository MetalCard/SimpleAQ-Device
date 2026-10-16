#!/bin/bash -e

cp -R /simpleaq "${ROOTFS_DIR}"

# Install SimpleAQ requirements.
# We force the upgrade to break system packages if needed.
on_chroot << EOF
    export PIP_BREAK_SYSTEM_PACKAGES=1

    python3 -m pip install --upgrade pip
    python3 -m pip install -r /simpleaq/requirements.txt
EOF

# Load i2c module at runtime.
echo i2c-dev >> "${ROOTFS_DIR}/etc/modules"

# Set up a system-scoped systemd service.
cp files/simpleaq.service "${ROOTFS_DIR}/etc/systemd/system"
cp files/hostap_config.service "${ROOTFS_DIR}/etc/systemd/system"
cp files/wifi.nmconnection "${ROOTFS_DIR}/etc/NetworkManager/system-connections/"
cp files/NetworkManager.conf "${ROOTFS_DIR}/etc/NetworkManager/NetworkManager.conf"
cp files/chrony.conf "${ROOTFS_DIR}/etc/chrony/chrony.conf"
cp files/simpleaq-ap.nmconnection "${ROOTFS_DIR}/etc/NetworkManager/system-connections/"
cp files/ap0-setup.service "${ROOTFS_DIR}/etc/systemd/system"
cp files/wifi-watchdog.sh "${ROOTFS_DIR}/usr/local/bin/wifi-watchdog.sh"
cp files/wifi-watchdog.service "${ROOTFS_DIR}/etc/systemd/system"
cp files/wifi-watchdog.timer "${ROOTFS_DIR}/etc/systemd/system"
mkdir -p "${ROOTFS_DIR}/etc/NetworkManager/dispatcher.d"
cp files/90-simpleaq-ap-channel "${ROOTFS_DIR}/etc/NetworkManager/dispatcher.d/90-simpleaq-ap-channel"

# --- Copy BLE and Reset scripts ---
cp files/simpleaq-ble.service "${ROOTFS_DIR}/etc/systemd/system/"
cp files/ble_setup.py "${ROOTFS_DIR}/usr/local/bin/"
cp files/simpleaq-reset.service "${ROOTFS_DIR}/etc/systemd/system/"
cp files/reset_server.py "${ROOTFS_DIR}/usr/local/bin/"

# SimpleAQ uses python-dotenv.
on_chroot << EOF
        cat /simpleaq/example.env | grep -v "image_name=" >> /etc/environment
        echo image_name=${IMG_NAME} >> /etc/environment
EOF

# Make sure our services have the right permissions and start on boot.
on_chroot << EOF
        chown root:root /etc/systemd/system/simpleaq.service
        chmod 644 /etc/systemd/system/simpleaq.service
        systemctl enable simpleaq

        chown root:root /etc/systemd/system/ap0-setup.service
        chmod 644 /etc/systemd/system/ap0-setup.service

        chown root:root /etc/systemd/system/hostap_config.service
        chmod 644 /etc/systemd/system/hostap_config.service

        chown root:root /usr/local/bin/wifi-watchdog.sh
        chmod 755 /usr/local/bin/wifi-watchdog.sh
        chown root:root /etc/systemd/system/wifi-watchdog.service
        chmod 644 /etc/systemd/system/wifi-watchdog.service
        chown root:root /etc/systemd/system/wifi-watchdog.timer
        chmod 644 /etc/systemd/system/wifi-watchdog.timer

        # Ensure wifi and bluetooth radios are not soft-blocked on first boot
        rfkill unblock all || true

        # BLE Provisioning Service
        chown root:root /etc/systemd/system/simpleaq-ble.service
        chmod 644 /etc/systemd/system/simpleaq-ble.service
        systemctl enable simpleaq-ble.service
        
        chown root:root /usr/local/bin/ble_setup.py
        chmod 755 /usr/local/bin/ble_setup.py

        # Local Reset Web Server
        chown root:root /etc/systemd/system/simpleaq-reset.service
        chmod 644 /etc/systemd/system/simpleaq-reset.service
        systemctl enable simpleaq-reset.service

        chown root:root /usr/local/bin/reset_server.py
        chmod 755 /usr/local/bin/reset_server.py

        chown root:root /etc/NetworkManager/dispatcher.d/90-simpleaq-ap-channel
        chmod 755 /etc/NetworkManager/dispatcher.d/90-simpleaq-ap-channel

        chown root:root /etc/NetworkManager/system-connections/wifi.nmconnection
        chmod 600 /etc/NetworkManager/system-connections/wifi.nmconnection

        chown root:root /etc/NetworkManager/system-connections/simpleaq-ap.nmconnection
        chmod 600 /etc/NetworkManager/system-connections/simpleaq-ap.nmconnection
EOF

# Delete now-unnecessary custom pigen stuff.
on_chroot << EOF
        rm -rf /simpleaq/custom_pigen
EOF

cp files/gpsd "${ROOTFS_DIR}/etc/default/gpsd"

# Also enable gpsd
on_chroot << EOF
        systemctl enable gpsd
        adduser gpsd dialout
        adduser gpsd tty
EOF

# Create NetworkManager dnsmasq shared directory
mkdir -p "${ROOTFS_DIR}/etc/NetworkManager/dnsmasq-shared.d"

# Add custom DNS entry for simpleaq.setup
cat > "${ROOTFS_DIR}/etc/NetworkManager/dnsmasq-shared.d/simpleaq.conf" << 'EOF'
# Resolve simpleaq.setup to the AP gateway
address=/simpleaq.setup/192.168.4.1
EOF

# Set permissions
on_chroot << EOF
    chown root:root /etc/NetworkManager/dnsmasq-shared.d/simpleaq.conf
    chmod 644 /etc/NetworkManager/dnsmasq-shared.d/simpleaq.conf
EOF

# Don't let logs get too big.
on_chroot << EOF
        sed -i '/SystemMaxUse/c\SystemMaxUse=10M' /etc/systemd/journald.conf
EOF

cp files/rc.local "${ROOTFS_DIR}/etc/rc.local"

# Disable firewall safeguards for debug builds.
if [ ${ENABLE_SSH} -eq 1 ]
then
        cp files/rc.local.debug "${ROOTFS_DIR}/etc/rc.local"
fi

on_chroot << EOF
        chown root:root /etc/rc.local
        chmod a+x /etc/rc.local
EOF

# Pure unbonded BLE peripheral configuration:
# - ControllerMode = le: Strictly BLE; disables Classic BR/EDR completely.
# - Bondable = false: Explicitly informs connecting centrals (Android) that pairing/bonding is disabled.
# - ReverseServiceDiscovery = false: Prevents BlueZ from probing connecting devices.
# - DiscoverableTimeout = 0: Adapter never stops being discoverable.
on_chroot << EOF
    sed -i '/^\[General\]/a ControllerMode = le\nBondable = false\nReverseServiceDiscovery = false\nDiscoverableTimeout = 0\nExperimental = true' /etc/bluetooth/main.conf
EOF
