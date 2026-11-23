#!/bin/sh
# Installs a nipovpn-dashboard .deb on a clean Debian/Ubuntu system (run it in
# a container) and checks that the dashboard starts and answers.
#
#   docker run --rm -v "$PWD:/src:ro" ubuntu:24.04 sh /src/packaging/smoke-test.sh /src/build/nipovpn-dashboard_X_all.deb
set -eu

DEB="${1:?usage: smoke-test.sh PACKAGE.deb}"
export DEBIAN_FRONTEND=noninteractive

apt-get update -qq >/dev/null
apt-get install -y -qq curl >/dev/null
cp "$DEB" /tmp/package.deb
apt-get install -y -qq /tmp/package.deb

test -x /usr/bin/nipovpn-dashboard
test -f /usr/lib/systemd/system/nipovpn-dashboard.service
test "$(stat -c %a /etc/nipovpn-dashboard)" = 700
test "$(stat -c %a /etc/nipovpn-dashboard/config.yaml)" = 600

# A server config for the dashboard to manage (nipovpn's sample config).
SAMPLE="$(dirname "$0")/../tests/fixtures/config.yaml"
mkdir -p /etc/nipovpn
[ -f /etc/nipovpn/config.yaml ] || cp "$SAMPLE" /etc/nipovpn/config.yaml
CONFIG=/etc/nipovpn-dashboard/config.yaml

HASH="$(echo 'smoke-test-password' | nipovpn-dashboard hash-password)"
sed -i "s|^passwordHash: \"\"|passwordHash: \"$HASH\"|" "$CONFIG"
nipovpn-dashboard > /tmp/dashboard.out 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null || true' EXIT

for i in $(seq 1 50); do
    curl -sf -o /dev/null http://127.0.0.1:8090/ && break
    sleep 0.2
done

API="-H X-Nipovpn-Dashboard:1"
curl -sf -o /dev/null http://127.0.0.1:8090/logo.png
curl -sf -c /tmp/cookies $API -H 'Content-Type: application/json' \
    -d '{"password":"smoke-test-password"}' http://127.0.0.1:8090/api/login | grep -q '"ok": true'
curl -sf -b /tmp/cookies $API http://127.0.0.1:8090/api/overview | grep -q '"users"'
curl -sf -b /tmp/cookies $API http://127.0.0.1:8090/api/settings | grep -q '"sections"'
test -z "$(find /usr/lib/nipovpn-dashboard -name __pycache__)"

kill $PID
wait $PID 2>/dev/null || true

# HTTPS with a self-signed certificate.
nipovpn-dashboard make-cert --host 127.0.0.1 > /dev/null
test "$(stat -c %a /etc/nipovpn-dashboard/dashboard.key)" = 600
sed -i -e 's|^tlsEnable: false|tlsEnable: true|' \
       -e 's|^tlsCertFile: ""|tlsCertFile: "/etc/nipovpn-dashboard/dashboard.crt"|' \
       -e 's|^tlsKeyFile: ""|tlsKeyFile: "/etc/nipovpn-dashboard/dashboard.key"|' "$CONFIG"
nipovpn-dashboard > /tmp/dashboard-tls.out 2>&1 &
PID=$!
for i in $(seq 1 50); do
    curl -sf -o /dev/null --cacert /etc/nipovpn-dashboard/dashboard.crt https://127.0.0.1:8090/ && break
    sleep 0.2
done
curl -sf -D /tmp/headers -o /dev/null --cacert /etc/nipovpn-dashboard/dashboard.crt $API \
    -H 'Content-Type: application/json' -d '{"password":"smoke-test-password"}' https://127.0.0.1:8090/api/login
grep -qi '^set-cookie:.*; Secure' /tmp/headers
grep -qi '^strict-transport-security:' /tmp/headers
! curl -s -o /dev/null --max-time 5 http://127.0.0.1:8090/
grep -q 'https://127.0.0.1:8090/' /tmp/dashboard-tls.out

kill $PID
trap - EXIT
apt-get purge -y -qq nipovpn-dashboard >/dev/null
test ! -e /usr/lib/nipovpn-dashboard
test ! -e /etc/nipovpn-dashboard

echo "smoke test passed"
