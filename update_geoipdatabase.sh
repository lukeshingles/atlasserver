#!/usr/bin/env bash

if [ -f .env ]; then
    source .env
fi

ATLASSERVERPATH="$(dirname "$(realpath "$0")")"


if curl -J -L -u 504450:$MAXMIND_LICENSE_KEY "https://download.maxmind.com/geoip/databases/GeoLite2-ASN/download?suffix=tar.gz" --output "/tmp/GeoLite2-ASN.tar.gz"; then
    tar -zxvf /tmp/GeoLite2-ASN.tar.gz --to-stdout GeoLite2-ASN_*/GeoLite2-ASN.mmdb > "$ATLASSERVERPATH/atlasserver/GeoLite2-ASN.mmdb"
    rm /tmp/GeoLite2-ASN.tar.gz
fi


if curl -J -L -u 504450:$MAXMIND_LICENSE_KEY "https://download.maxmind.com/geoip/databases/GeoLite2-City/download?suffix=tar.gz" --output "/tmp/GeoLite2-City.tar.gz"; then
    tar -zxvf /tmp/GeoLite2-City.tar.gz --to-stdout "GeoLite2-City_*/GeoLite2-City.mmdb" > "$ATLASSERVERPATH/atlasserver/GeoLite2-City.mmdb"
    rm /tmp/GeoLite2-City.tar.gz
fi
