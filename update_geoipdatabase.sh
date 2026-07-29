#!/usr/bin/env bash

if [ -f .env ]; then
    # shellcheck disable=SC1091
    source .env
fi

ATLASSERVERPATH="$(dirname "$(realpath "$0")")"

MAXMIND_ACCOUNT_ID="${MAXMIND_ACCOUNT_ID:-504450}"

for edition in GeoLite2-ASN GeoLite2-City; do
    tempdir=$(mktemp -d)
    # --fail so that an HTTP error (e.g. a bad licence key) is not saved as a bogus .tar.gz
    if curl --fail -J -L -u "$MAXMIND_ACCOUNT_ID:$MAXMIND_LICENSE_KEY" \
        "https://download.maxmind.com/geoip/databases/$edition/download?suffix=tar.gz" \
        --output "$tempdir/$edition.tar.gz"; then
        tar -zxvf "$tempdir/$edition.tar.gz" -C "$tempdir"
        cp "$tempdir"/GeoLite2-*/"$edition.mmdb" "$ATLASSERVERPATH/atlasserver/$edition.mmdb"
    else
        echo "ERROR: failed to download $edition (is MAXMIND_LICENSE_KEY set in .env?)"
    fi
    # clean up even when the download failed, so the temporary directory is not left behind
    rm -rf "$tempdir"
done
