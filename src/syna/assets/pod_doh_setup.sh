#!/bin/bash
# Pod DNS-over-HTTPS bootstrap, run once on container creation.
#
# Installs dnscrypt-proxy, points the system resolver at a local DoH
# listener (fed by the resolver stamp built on the host), and gives curl
# transparent HTTP/3, which races QUIC against TCP and falls back
# automatically for sites that do not offer h3. Everything fails open:
# any failure leaves the pod's native DNS untouched, and the pre-DoH
# resolvers stay in resolv.conf as fallbacks behind the local listener.

set -u

DOH_STAMP="${DOH_STAMP:?DOH_STAMP is required}"
LISTEN="127.0.0.1:53"

say() { echo "pod-doh: $*"; }
fail() { say "FAILED: $*"; exit 1; }

[ "$(id -u)" = "0" ] || fail "must run as root"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null 2>&1 || fail "apt-get update"
apt-get install -y -qq dnscrypt-proxy >/dev/null 2>&1 || fail "apt-get install dnscrypt-proxy"
DCP_BIN="$(command -v dnscrypt-proxy || true)"
[ -n "$DCP_BIN" ] || fail "dnscrypt-proxy binary not found"

# Minimal config: one static DoH server, no resolver-list downloads. The
# stamp pins the bootstrap IP, so the provider hostname is never resolved
# through the system resolver and a poisoning middlebox cannot redirect
# the DoH connection itself.
cat > /etc/dnscrypt-proxy/dnscrypt-proxy.toml <<TOML
listen_addresses = ['${LISTEN}']
server_names = ['syna-doh']
max_clients = 250
ipv4_servers = true
ipv6_servers = false
timeout = 5000
keepalive = 30
cert_refresh_delay = 240
ignore_system_dns = true
cache = true
cache_size = 4096
cache_min_ttl = 2400
cache_max_ttl = 86400
log_level = 2

[static.'syna-doh']
stamp = '${DOH_STAMP}'
TOML

nohup "$DCP_BIN" -config /etc/dnscrypt-proxy/dnscrypt-proxy.toml >/var/log/dnscrypt-proxy.log 2>&1 &

ready=0
for _ in $(seq 1 30); do
  if python3 - "$LISTEN" <<'PY' >/dev/null 2>&1
import socket
import struct
import sys

host, port = sys.argv[1].rsplit(":", 1)
query = struct.pack(">HHHHHH", 0x5A1D, 0x0100, 1, 0, 0, 0)
query += b"\x07example\x03com\x00" + struct.pack(">HH", 1, 1)
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.settimeout(2)
sock.sendto(query, (host, int(port)))
data, _ = sock.recvfrom(4096)
raise SystemExit(0 if data[3] & 0x0F == 0 else 1)
PY
  then ready=1; break; fi
  sleep 1
done
[ "$ready" = "1" ] || {
  tail -n 5 /var/log/dnscrypt-proxy.log 2>/dev/null
  fail "dnscrypt-proxy did not answer on ${LISTEN}"
}

cp /etc/resolv.conf /etc/resolv.conf.pre-doh 2>/dev/null || true
{
  echo "# Managed by Syna's pod DoH bootstrap; the pre-DoH resolvers stay as fallback."
  echo "nameserver ${LISTEN%:*}"
  grep '^nameserver ' /etc/resolv.conf.pre-doh 2>/dev/null |
    grep -v '^nameserver 127.0.0.1$' || true
} > /etc/resolv.conf.new
cat /etc/resolv.conf.new > /etc/resolv.conf 2>/dev/null ||
  cp /etc/resolv.conf.new /etc/resolv.conf
rm -f /etc/resolv.conf.new

if curl -V 2>/dev/null | grep -q HTTP3; then
  printf 'http3\n' > /root/.curlrc
  say "curl HTTP/3 enabled via /root/.curlrc"
fi

say "DoH active on ${LISTEN} (bootstrap pinned in the resolver stamp)"
echo "pod-init: step OK"
