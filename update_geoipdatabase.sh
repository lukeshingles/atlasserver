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
        # copy alongside the target and rename, rather than cp'ing over it: the web server holds
        # the database memory-mapped, and cp truncates and rewrites the same inode, so a plain cp
        # rewrites the bytes underneath the running process. A rename leaves the old inode intact
        # for anyone still reading it and publishes the new file in one step. The temporary copy
        # goes in the destination directory because rename cannot cross filesystems.
        # &&, because there is no `set -e`: a cp that ran out of disk part-way leaves a truncated
        # .new file, and an unconditional mv would publish that over the working database as
        # atomically as it publishes a good one
        staged="$ATLASSERVERPATH/atlasserver/$edition.mmdb.new"
        if cp "$tempdir"/GeoLite2-*/"$edition.mmdb" "$staged" &&
            mv -f "$staged" "$ATLASSERVERPATH/atlasserver/$edition.mmdb"; then
            echo "Installed $edition.mmdb"
        else
            echo "ERROR: failed to install $edition.mmdb"
            rm -f "$staged"
            exitcode=1
        fi
    else
        echo "ERROR: failed to download $edition (is MAXMIND_LICENSE_KEY set in .env?)"
        exitcode=1
    fi
    # clean up even when the download failed, so the temporary directory is not left behind
    rm -rf "$tempdir"
done

# exit non-zero if any edition failed, so that cron/automation notices a stale database
exit $exitcode
