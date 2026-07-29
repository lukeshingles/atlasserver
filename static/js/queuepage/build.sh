#!/usr/bin/env bash

set -euo pipefail

cd "$(dirname "$0")"

# build to a temporary file first: redirecting straight onto the published bundle truncates it
# before babel runs, so a compile error would leave a zero-byte file in static/js/
build() {
    local source="$1" target="$2" tmpfile
    tmpfile=$(mktemp)
    if npx babel --minified --presets @babel/preset-react "$source" > "$tmpfile"; then
        mv "$tmpfile" "$target"
    else
        rm -f "$tmpfile"
        echo "ERROR: failed to build $source"
        exit 1
    fi
}

build src/newrequest.jsx ../newrequest.min.js
build src/tasklist.jsx ../tasklist.min.js
build src/lightcurveplotly.js ../lightcurveplotly.min.js
