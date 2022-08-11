#!/usr/bin/env zsh

babel --minified --presets /opt/homebrew/lib/node_modules/babel-preset-react newrequest.jsx task.jsx tasklist.jsx |
minify --js > tasklist.20220811.min.js
