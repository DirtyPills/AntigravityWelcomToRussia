#!/bin/sh
set -eu
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
install -d -m 0755 /etc/agy-net
install -d -m 0700 /run/agy-net
install -m 0755 agy_net.py /usr/local/bin/agy-net
install -d -m 0755 /usr/local/lib/agy-net
install -m 0755 desktop_exec.py /usr/local/lib/agy-net/desktop-exec
install -d -m 0755 /etc/agy-net/profiles
install -d -m 0700 /var/log/agy-net
install -m 0644 systemd/agy-net.service /etc/systemd/system/agy-net.service
install -m 0644 systemd/agy-net@.service /etc/systemd/system/agy-net@.service
install -m 0644 systemd/agy-net-antigravity@.service /etc/systemd/system/agy-net-antigravity@.service
systemctl daemon-reload
echo "Installed /usr/local/bin/agy-net and systemd units. Configure either /etc/agy-net/awg0.conf (mode 0600) or AGY_NET_DNS for VLESS TUN, then run: sudo agy-net start"
