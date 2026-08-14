// Test harness for the React components.
//
// Not named *.test.js on purpose: `npm test` runs `node --test 'src/*.test.js'`, and this file is
// a helper rather than a suite.
//
// Node cannot import the .jsx sources directly (it does not parse JSX) and cannot resolve the bare
// specifiers the import map provides in the browser ("newrequest", "csrftoken", ...). esbuild --
// already a dependency for the self-hosted React bundles -- handles both: it compiles the JSX and
// rewrites those specifiers to the local files via --alias. react and react-dom stay external so
// that node resolves them from node_modules, giving the test the same single React instance the
// component tree uses.

import { mkdir } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { fireEvent } from '@testing-library/dom';
import { build } from 'esbuild';
import { JSDOM } from 'jsdom';

const SRC = dirname(fileURLToPath(import.meta.url));

// inside the package so that node resolves "react" from node_modules by walking up from here
const OUTDIR = resolve(SRC, '..', 'node_modules', '.cache', 'atlas-component-tests');

/** Compile one source file and import it. Returns its module namespace. */
export async function importComponent(entry) {
    const outfile = join(OUTDIR, `${entry.replace(/[^\w]/g, '_')}.mjs`);
    await mkdir(dirname(outfile), { recursive: true });

    await build({
        entryPoints: [join(SRC, entry)],
        outfile,
        bundle: true,
        format: 'esm',
        platform: 'neutral',
        external: ['react', 'react-dom/client'],
        alias: {
            // the browser's import map points "react-dom" at react-dom's *client* entry; node
            // would otherwise resolve the package root, whose default export has no createRoot
            'react-dom': 'react-dom/client',
            csrftoken: join(SRC, 'csrftoken.js'),
            agetext: join(SRC, 'agetext.js'),
            waitestimate: join(SRC, 'waitestimate.js'),
            pollcache: join(SRC, 'pollcache.js'),
            newrequest: join(SRC, 'newrequest.jsx'),
        },
        logLevel: 'silent',
    });

    // cache-busting query: node caches modules by URL, and a suite may rebuild between tests
    return import(`${pathToFileURL(outfile).href}?t=${Date.now()}`);
}

/**
 * Install a fresh DOM and the globals the page's inline script normally defines.
 *
 * The components read several of those directly (api_url_base, newtaskids, the MJD helpers), which
 * is why they have to be provided rather than imported.
 */
export function setupDom({ url = 'http://testserver/queue/' } = {}) {
    // jsdom rather than happy-dom: under happy-dom, React 19 never fired onChange for a text
    // input, however the event was dispatched (onInput and onClick both worked), so a test of
    // typing failed as though the component were broken. React and testing-library are developed
    // against jsdom, and this is not a place to be debugging the environment.
    const dom = new JSDOM('<!doctype html><html><body></body></html>', { url, pretendToBeVisual: true });
    const window = dom.window;

    global.window = window;
    global.document = window.document;
    global.localStorage = window.localStorage;
    global.HTMLElement = window.HTMLElement;
    global.Event = window.Event;
    global.requestAnimationFrame = window.requestAnimationFrame.bind(window);
    global.cancelAnimationFrame = window.cancelAnimationFrame.bind(window);
    // node exposes globalThis.navigator as a getter-only property, so a plain assignment throws
    Object.defineProperty(global, 'navigator', { value: window.navigator, configurable: true });

    // globals from the inline script in tasklist-react.html
    global.api_url_base = 'http://testserver/queue/';
    global.queuepositions_url = 'http://testserver/queuepositions.json';
    global.taskrunnerstatus_url = 'http://testserver/taskrunnerstatus.json';
    global.user_id = 1;
    global.user_is_active = true;
    global.hidden = 'hidden';
    global.newtaskids = [];
    global.jslcdataglobal = {};
    global.jslabelsglobal = {};
    global.jslimitsglobal = {};
    global.mjdFromDate = (dateObj) => dateObj / 86400000 + 2440587.5 - 2400000.5;
    global.dateFromMJD = (mjd) => new Date(Math.round((mjd + 2400000.5 - 2440587.5) * 86400000));

    return window;
}

/** Tear down a window created by setupDom. */
export async function teardownDom(window) {
    window.close();
}

/**
 * Import react and react-dom, which must happen only after setupDom().
 *
 * react-dom decides at import time whether it is running in a browser, and caches that. Imported
 * before the DOM globals exist it concludes it is not, and its change-event handling silently
 * falls back to a legacy path that never fires onChange for a text input under a dispatched
 * event -- onClick keeps working, so the failure looks like a broken component rather than a
 * broken harness. Tests therefore get React from here rather than importing it themselves.
 */
export async function loadReact() {
    const React = (await import('react')).default;
    const ReactDOM = await import('react-dom/client');

    return { React, ReactDOM };
}

/**
 * Render an element into a container and return it.
 *
 * React 19 renders concurrently, so every helper that changes the tree awaits a flush; without it
 * an assertion runs against the DOM as it was before the update.
 */
export async function render(ReactDOM, React, element) {
    const container = window.document.createElement('div');
    window.document.body.appendChild(container);

    const root = ReactDOM.createRoot(container);
    root.render(element);
    await flush();

    return { container, root, unmount: () => root.unmount() };
}

/** Let React finish its work and any queued microtasks settle. */
export async function flush(ms = 20) {
    await new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Set the value of a controlled input the way a user would.
 *
 * Via testing-library's fireEvent rather than a hand-rolled dispatch. A plain `element.value = x`
 * is invisible to React, which installs its own value tracker on the node; going through the
 * prototype's setter and dispatching an event gets onInput and onClick through but still did not
 * produce an onChange under happy-dom. fireEvent is maintained to get exactly this right, and
 * getting it wrong is silent -- the assertion fails as though the component were broken.
 */
export async function setValue(element, value) {
    if (element.type === 'checkbox') {
        // a click toggles, so it is only the right way to reach `value` when the box disagrees
        // with it. Clicking unconditionally made setValue(box, false) on an unchecked box tick it,
        // and a second setValue(box, true) untick it -- the caller asks for a state, not a toggle.
        if (element.checked !== Boolean(value)) {
            fireEvent.click(element);
        }
    } else {
        fireEvent.change(element, { target: { value } });
    }

    await flush();
}
