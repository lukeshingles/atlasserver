#!/usr/bin/env bash

source .env

ATLASSERVERPATH=$(dirname $(realpath "$0"))

curl "https://download.maxmind.com/app/geoip_download?edition_id=GeoLite2-ASN&license_key=$MAXMIND_LICENSE_KEY&suffix=tar.gz" --output /tmp/GeoLite2-ASN.tar.gz
tar  -zxvf /tmp/GeoLite2-ASN.tar.gz --to-stdout GeoLite2-ASN_*/GeoLite2-ASN.mmdb > $ATLASSERVERPATH/atlasserver/GeoLite2-ASN.mmdb
rm /tmp/GeoLite2-ASN.tar.gz


curl "https://download.maxmind.com/app/geoip_download?edition_id=GeoLite2-City&license_key=$MAXMIND_LICENSE_KEY&suffix=tar.gz" --output /tmp/GeoLite2-City.tar.gz
tar  -zxvf /tmp/GeoLite2-City.tar.gz --to-stdout GeoLite2-City_*/GeoLite2-City.mmdb > $ATLASSERVERPATH/atlasserver/GeoLite2-City.mmdb
rm /tmp/GeoLite2-City.tar.gz
