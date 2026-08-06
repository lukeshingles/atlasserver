#!/usr/bin/env bash

# fail on the first error. With --out-file, a compile error exits non-zero and leaves the
# published bundle untouched (redirecting stdout with > would truncate it before babel runs)
set -euo pipefail

cd "$(dirname "$0")"

npx babel --minified --presets @babel/preset-react src/newrequest.jsx -o ../newrequest.min.js
npx babel --minified --presets @babel/preset-react src/tasklist.jsx -o ../tasklist.min.js
npx babel --minified --presets @babel/preset-react src/pollcache.js -o ../pollcache.min.js
npx babel --minified --presets @babel/preset-react src/agetext.js -o ../agetext.min.js
npx babel --minified --presets @babel/preset-react src/lightcurveplotly.js -o ../lightcurveplotly.min.js
