#!/usr/bin/env bash

babel --minified --presets @babel/preset-react src/newrequest.jsx > ../newrequest.min.js
babel --minified --presets @babel/preset-react src/tasklist.jsx > ../tasklist.min.js

babel --minified --presets @babel/preset-react src/lightcurveplotly.js > ../lightcurveplotly.min.js
