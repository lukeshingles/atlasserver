#!/usr/bin/env bash

npx babel --minified --presets @babel/preset-react src/newrequest.jsx > ../newrequest.min.js
npx babel --minified --presets @babel/preset-react src/tasklist.jsx > ../tasklist.min.js

npx babel --minified --presets @babel/preset-react src/lightcurveplotly.js > ../lightcurveplotly.min.js
