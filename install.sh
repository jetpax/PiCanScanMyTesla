#!/usr/bin/env bash
set -e

if [ "$(id -u)" -ne 0 ]; then
    echo "run as root: sudo $0" >&2
    exit 1
fi

HERE="$(cd "$(dirname "$0")" && pwd)"

apt-get update
apt-get install -y can-utils python3-can python3-aiohttp

install -d /opt/teslamon
install -m 0644 "$HERE/teslamon.py" "$HERE/index.html" /opt/teslamon/

install -m 0644 "$HERE/systemd/slcand.service" /etc/systemd/system/
install -m 0644 "$HERE/systemd/canlogserver.service" /etc/systemd/system/
install -m 0644 "$HERE/systemd/teslamon.service" /etc/systemd/system/

touch /var/log/teslamon.csv

systemctl daemon-reload
systemctl enable --now slcand.service canlogserver.service teslamon.service

sleep 2
systemctl is-active slcand canlogserver teslamon

echo
echo "installed. dashboard: http://$(hostname).local:8080"
