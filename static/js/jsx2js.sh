#!/usr/bin/env zsh
babel --presets /usr/local/lib/node_modules/babel-preset-react tasklist.jsx |
minify --js > tasklist.min.js
#minify plotly-latest.kws.js > plotly-latest.kws.min.js
