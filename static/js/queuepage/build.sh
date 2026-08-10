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

# React, self-hosted. The import map used to point "react" and "react-dom" at esm.sh, which put a
# third-party CDN on the critical path of the signed-in queue page: an outage took the page down,
# and a compromise would have run arbitrary code in every logged-in session. Import maps cannot
# carry an integrity hash in any broadly supported form, so serving the files ourselves is the only
# way to close that.
#
# esbuild converts React's CommonJS to ESM (v19 ships no ESM and no UMD). It is used for nothing
# else: this project's own code is still compiled by babel above, one file at a time, unbundled.
mkdir -p ../vendor

# NODE_ENV is what selects React's production branch and strips its development warnings; without
# the define, the bundle keeps both branches and the `process` reference fails in a browser.
#
# Production only, in DEBUG too. The import map used to switch to esm.sh's ?dev build when DEBUG
# was set, which is a real loss -- React's development build has far better error messages -- but
# react-dom's is 1 MB, and everything the page needs is committed to this repo so that a fresh
# clone runs without a build step. A megabyte of generated code in git is the worse trade. To get
# the warnings back while working on a component, rebuild that one file by hand with
# --define:process.env.NODE_ENV=\"development\" and do not commit the result.
#
# --splitting is what keeps the page on a single React. The obvious alternative, building the two
# entries separately with --external:react, does not work: react-dom is CommonJS, so esbuild cannot
# turn its require("react") into an ESM import and emits a require() shim instead, which resolves
# to nothing in a browser. Splitting instead lifts the shared React into react-shared.js, which
# both entries import relatively -- so the browser instantiates it once, and hooks (whose
# dispatcher is module-level state inside React) work across both. src/vendor.test.js renders a
# component with a hook against these exact files to keep that honest.
npx esbuild --bundle --format=esm --splitting --minify --log-level=warning \
    '--define:process.env.NODE_ENV="production"' \
    --outdir=../vendor --entry-names='[name].min' --chunk-names='react-shared' \
    vendor/entries/react.js vendor/entries/react-dom-client.js
