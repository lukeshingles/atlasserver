#!/usr/bin/env bash

# fail on the first error. With --out-file, a compile error exits non-zero and leaves the
# published bundle untouched (redirecting stdout with > would truncate it before babel runs)
set -euo pipefail

cd "$(dirname "$0")"

# --minified only takes out the whitespace: babel writes comments through by default, so every
# bundle was shipping the source's explanatory comments to every visitor (--no-comments cuts
# tasklist.min.js from 42kB to 26kB). Shared, so that a flag added here cannot reach some of the
# bundles and not others.
#
# preset-react is configured in babel.config.js rather than with --presets here: its Babel 8
# defaults need overriding, and the CLI flag has nowhere to put preset options.
babelopts=(--minified --no-comments)

for module in newrequest.jsx tasklist.jsx pollcache.js agetext.js lightcurveplotly.js; do
    npx babel "${babelopts[@]}" "src/${module}" -o "../${module%.js*}.min.js"
done
