#!/usr/bin/env bash
# Certificate for the daemon: its own root CA and a certificate issued by it for the addresses
# and names by which the page is opened.
#
# Why: the browser considers only https (and loopback) secure. Without https the page
# cannot be installed as an app, and Android will not show it in the share dialog at all.
# A public certificate is not suitable here: a home address like 192.168.* and a name like
# *.local belong to no one, and Let's Encrypt cannot issue for them. Your own CA
# solves this with one action on the tablet — installing ca.crt.
#
# Run: packaging/make-tls.sh [address or name ...]
# Names go into the certificate as DNS, addresses — as IP; both are checked by the
# browser literally, so "yt-trans.local" cannot be signed as an IP.
# This machine's addresses are always added, even if names are passed.
#
# The CA is created once and does not change on repeated runs: the daemon's certificate
# can be reissued as many times as you like, and the ca.crt installed on the tablet stays
# the same. To remove the pair — delete the tls directory entirely and install the CA anew.

set -euo pipefail

CONFIG_DIR="${YT_TRANS_CONFIG:-$HOME/.config/yt-trans}"
TLS_DIR="$CONFIG_DIR/tls"
HOSTNAME_NOW="$(hostname)"
DAYS_CA=3650
DAYS_CERT=825
IPV4='^[0-9]{1,3}(\.[0-9]{1,3}){3}$'

own_addresses() {
  # scope global — no loopback and link-local; container bridges are discarded, their
  # addresses do not belong to the tablet and only get in the way in the certificate.
  ip -4 -o addr show scope global |
    awk '$2 !~ /^(docker|br-|veth|virbr|tun|tap|cni|flannel)/ { split($4, a, "/"); print a[1] }'
}

extra_ips=""
extra_names=""

for arg in "$@"; do
  if [[ "$arg" =~ $IPV4 ]]; then
    extra_ips="$extra_ips $arg"
  else
    extra_names="$extra_names $arg"
  fi
done

addresses="$(own_addresses) $extra_ips"

if [ -z "${addresses// /}" ]; then
  echo "found no address on the network: pass one as an argument" >&2
  exit 1
fi

SAN="DNS:$HOSTNAME_NOW,DNS:localhost,IP:127.0.0.1"
for addr in $addresses; do
  SAN="$SAN,IP:$addr"
done
for name in $extra_names; do
  SAN="$SAN,DNS:$name"
done

mkdir -p "$TLS_DIR"
chmod 700 "$TLS_DIR"

if [ ! -f "$TLS_DIR/ca.key" ] || [ ! -f "$TLS_DIR/ca.crt" ]; then
  echo "creating the root CA"
  openssl req -x509 -newkey rsa:4096 -sha256 -days "$DAYS_CA" -nodes \
    -keyout "$TLS_DIR/ca.key" -out "$TLS_DIR/ca.crt" \
    -subj "/CN=yt-trans $HOSTNAME_NOW" -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
  chmod 600 "$TLS_DIR/ca.key"
fi

echo "issuing a certificate for: $SAN"
openssl req -newkey rsa:2048 -sha256 -nodes \
  -keyout "$TLS_DIR/server.key" -out "$TLS_DIR/server.csr" \
  -subj "/CN=$HOSTNAME_NOW" 2>/dev/null

cat > "$TLS_DIR/server.ext" <<EOF
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=$SAN
EOF

openssl x509 -req -in "$TLS_DIR/server.csr" -CA "$TLS_DIR/ca.crt" -CAkey "$TLS_DIR/ca.key" \
  -CAcreateserial -days "$DAYS_CERT" -sha256 -extfile "$TLS_DIR/server.ext" \
  -out "$TLS_DIR/server.crt" 2>/dev/null

chmod 600 "$TLS_DIR/server.key"
rm -f "$TLS_DIR/server.csr" "$TLS_DIR/server.ext"

PORT="${YT_TRANS_PORT:-8756}"
first_name="$(echo $extra_names | awk '{print $1}')"
where="${first_name:-<address>}"

cat <<EOF

done. In $CONFIG_DIR/config.toml:

[server]
host = "0.0.0.0"
tls_cert = "$TLS_DIR/server.crt"
tls_key = "$TLS_DIR/server.key"

then: systemctl --user restart yt-trans

install $TLS_DIR/ca.crt on the tablet once
(Settings → Security → Encryption & credentials → Install a certificate
→ CA certificate), then open the page at https://$where:$PORT/
and install it as an app.
EOF
