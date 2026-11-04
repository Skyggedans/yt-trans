#!/usr/bin/env bash
# Сертификат для демона: свой корневой CA и выписанный им сертификат на адреса
# и имена, по которым открывают страницу.
#
# Зачем: браузер считает безопасным только https (и петлю). Без https страницу
# нельзя поставить как приложение, а в share-диалоге Android её не будет вовсе.
# Публичный сертификат тут не годен: домашний адрес вида 192.168.* и имя вида
# *.local не принадлежат никому, и выписать на них Let's Encrypt нельзя. Свой CA
# решает это одним действием на планшете — установкой ca.crt.
#
# Запуск: packaging/make-tls.sh [адрес или имя ...]
# Имена попадают в сертификат как DNS, адреса — как IP; и то и другое сверяется
# браузером буквально, поэтому «yt-trans.local» нельзя подписать как IP.
# Адреса этой машины добавляются всегда, даже если переданы имена.
#
# CA создаётся один раз и при повторных запусках не меняется: сертификат демона
# можно перевыпускать сколько угодно, а установленный на планшет ca.crt остаётся
# тем же. Удалить пару — удалить каталог tls целиком и поставить CA заново.

set -euo pipefail

CONFIG_DIR="${YT_TRANS_CONFIG:-$HOME/.config/yt-trans}"
TLS_DIR="$CONFIG_DIR/tls"
HOSTNAME_NOW="$(hostname)"
DAYS_CA=3650
DAYS_CERT=825
IPV4='^[0-9]{1,3}(\.[0-9]{1,3}){3}$'

own_addresses() {
  # scope global — без петли и link-local; контейнерные мосты отбрасываем, их
  # адреса планшету не принадлежат и в сертификате только мешают.
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
  echo "не нашёл ни одного адреса в сети: укажи его аргументом" >&2
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
  echo "создаю корневой CA"
  openssl req -x509 -newkey rsa:4096 -sha256 -days "$DAYS_CA" -nodes \
    -keyout "$TLS_DIR/ca.key" -out "$TLS_DIR/ca.crt" \
    -subj "/CN=yt-trans $HOSTNAME_NOW" -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
  chmod 600 "$TLS_DIR/ca.key"
fi

echo "выпускаю сертификат на: $SAN"
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
where="${first_name:-<адрес>}"

cat <<EOF

готово. В $CONFIG_DIR/config.toml:

[server]
host = "0.0.0.0"
tls_cert = "$TLS_DIR/server.crt"
tls_key = "$TLS_DIR/server.key"

затем: systemctl --user restart yt-trans

на планшет один раз поставить $TLS_DIR/ca.crt
(Настройки → Безопасность → Шифрование и учётные данные → Установить сертификат
→ Сертификат CA), после чего открывать страницу по https://$where:$PORT/
и поставить её как приложение.
EOF
