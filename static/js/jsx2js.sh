#!/usr/bin/env zsh

babel --minified --presets $(npm -g root)/@babel/preset-react newrequest.jsx task.jsx tasklist.jsx > tasklist.min.js
