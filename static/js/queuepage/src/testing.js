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

import { mkdir, readFile } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { fireEvent } from '@testing-library/dom';
import { build } from 'esbuild';
import { JSDOM } from 'jsdom';

const SRC = dirname(fileURLToPath(import.meta.url));

// inside the package so that node resolves "react" from node_modules by walking up from here
const OUTDIR = resolve(SRC, '..', 'node_modules', '.cache', 'atlas-component-tests');

/**
 * Load one classic script into a window of its own, and return that window.
 *
 * For the plain scripts that base.html loads: they read the document and run, and they export
 * nothing. The wait below is necessary because jsdom sends DOMContentLoaded one tick after it
 * makes the window. Without the wait, a script that waits for that event would wait for an event
 * that has already occurred. The caller closes the window.
 */
export async function loadClassicScript({ file, html, url = 'http://testserver/' }) {
    const source = await readFile(file, 'utf8');
    const dom = new JSDOM(html, { runScripts: 'outside-only', url });

    await new Promise((resolve) => {
        if (dom.window.document.readyState === 'loading') {
            dom.window.document.addEventListener('DOMContentLoaded', resolve);
        } else {
            resolve();
        }
    });

    dom.window.eval(source);
    return dom.window;
}

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
            runnerstatus: join(SRC, 'runnerstatus.js'),
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

    /*
     * Where runnerstatus.js reads its endpoint, in the form that base.html gives.
     *
     * The box (#sitenotice) is absent here, and that is the intent. The module starts its poll
     * when it gets a box, or when a reader subscribes. Without a box here, the first request
     * occurs as TaskPage mounts, which is after each test installs its test fetch function. A box
     * here would make a request at the time of importComponent(), with no test function in
     * place. The wait estimates would then lose the response that they need.
     */
    window.document.head.insertAdjacentHTML(
        'beforeend', '<meta name="atlas-runnerstatus-url" content="/taskrunnerstatus.json">');

    // globals from the inline script in tasklist-react.html
    global.api_url_base = 'http://testserver/queue/';
    global.queuepositions_url = 'http://testserver/queuepositions.json';
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

/**
 * Close a window that setupDom made, and remove the global values with it.
 *
 * A closed jsdom window continues to answer. Its document gives visibilityState "visible", and it
 * accepts listeners. Thus a `typeof document` test in the code under test would find the closed
 * document of the test before it. runnerstatus.js makes such a test.
 */
export async function teardownDom(window) {
    window.close();

    // `window` stays. A poll or a promise from the page under test can complete while this
    // function runs. Such code reads window.location and window.history directly. A name that
    // does not exist gives a ReferenceError, which fails the suite from outside every test.
    for (const name of ['document', 'localStorage', 'HTMLElement', 'Event',
        'requestAnimationFrame', 'cancelAnimationFrame', 'navigator']) {
        delete global[name];
    }
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
