#!/usr/bin/env bash

if [ -f .env ]; then
    source .env
fi

ATLASSERVERPATH="$(dirname "$(realpath "$0")")"

tempdir=$(mktemp -d)
if curl -J -L -u 504450:$MAXMIND_LICENSE_KEY "https://download.maxmind.com/geoip/databases/GeoLite2-ASN/download?suffix=tar.gz" --output "$tempdir/GeoLite2-ASN.tar.gz"; then
    tar -zxvf "$tempdir/GeoLite2-ASN.tar.gz" -C $tempdir
    cp "$tempdir/GeoLite2-*/GeoLite2-ASN.mmdb" "$ATLASSERVERPATH/atlasserver/GeoLite2-ASN.mmdb"
    rm -rf "$tempdir"
fi


tempdir=$(mktemp -d)
if curl -J -L -u 504450:$MAXMIND_LICENSE_KEY "https://download.maxmind.com/geoip/databases/GeoLite2-City/download?suffix=tar.gz" --output "$tempdir/GeoLite2-City.tar.gz"; then
    tar -zxvf "$tempdir/GeoLite2-City.tar.gz" -C "$tempdir"
    cp "$tempdir/GeoLite2-*/GeoLite2-City.mmdb" "$ATLASSERVERPATH/atlasserver/GeoLite2-City.mmdb"
    rm -rf "$tempdir"
fi