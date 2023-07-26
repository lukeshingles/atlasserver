#!/usr/bin/env zsh

babel --minified --presets @babel/preset-react newrequest.jsx task.jsx tasklist.jsx > tasklist.min.js
