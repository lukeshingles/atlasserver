#!/usr/bin/env bash

ATLASSERVERPATH="$(dirname "$(realpath "$0")")"

# resolve .env relative to the script, so that this works from any working directory (e.g. cron)
if [ -f "$ATLASSERVERPATH/.env" ]; then
    # shellcheck disable=SC1091
    source "$ATLASSERVERPATH/.env"
fi

MAXMIND_ACCOUNT_ID="${MAXMIND_ACCOUNT_ID:-504450}"

exitcode=0

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
        exitcode=1
    fi
    # clean up even when the download failed, so the temporary directory is not left behind
    rm -rf "$tempdir"
done

# exit non-zero if any edition failed, so that cron/automation notices a stale database
exit $exitcode
