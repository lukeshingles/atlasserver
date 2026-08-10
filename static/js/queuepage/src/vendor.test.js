// Checks the self-hosted React bundles that build.sh generates into ../../vendor.
//
// These replaced an import map pointing at esm.sh, so the thing most worth proving is that the
// replacement actually works as a pair: React 19 ships CommonJS only, the conversion is done by
// esbuild, and the first two attempts at it produced bundles that looked right and were broken --
// one left a require() shim that only fails in a browser, the other inlined a second copy of React
// that would have thrown on the first hook rendered.
import assert from 'node:assert/strict';
import test, { after, describe } from 'node:test';

import { flush, setupDom, teardownDom } from './testing.js';

// the DOM has to exist before the bundled react-dom is imported: it decides at import time whether
// it is running in a browser and caches the answer. See loadReact in testing.js.
const window = setupDom();
const React = (await import('../../vendor/react.min.js')).default;
const ReactDOM = (await import('../../vendor/react-dom-client.min.js')).default;

describe('self-hosted react bundles', () => {
    after(() => teardownDom(window));

    test('react exposes the API the sources use', () => {
        assert.equal(typeof React.createElement, 'function');
        assert.equal(typeof React.Component, 'function');
        assert.equal(typeof React.useState, 'function');
    });

    test('react-dom exposes createRoot', () => {
        // tasklist.jsx calls ReactDOM.createRoot(container).render(<TaskPage />)
        assert.equal(typeof ReactDOM.createRoot, 'function');
    });

    test('react-dom shares one React with the app, and hooks work', async () => {
        // The real check on the shared chunk. If react-dom carried its own copy of React, hooks
        // would dispatch against a different module-level dispatcher and this would throw
        // "Invalid hook call" rather than rendering.
        const container = window.document.createElement('div');
        window.document.body.appendChild(container);

        function Greeting() {
            const [who] = React.useState('world');
            return React.createElement('p', null, `hello ${who}`);
        }

        const root = ReactDOM.createRoot(container);
        root.render(React.createElement(Greeting));
        // React 19 renders concurrently, so let it flush before asserting
        await flush(50);

        assert.equal(container.textContent, 'hello world');

        root.unmount();
    });

    test('the bundles are self-contained, with no CDN or bare specifiers left', async () => {
        const { readFile } = await import('node:fs/promises');
        for (const name of ['react.min.js', 'react-dom-client.min.js', 'react-shared.js']) {
            const source = await readFile(new URL(`../../vendor/${name}`, import.meta.url), 'utf8');
            assert.ok(!source.includes('esm.sh'), `${name} still references esm.sh`);
            // a leftover require() shim is how the --external:react attempt failed: it builds and
            // it imports cleanly in node, and it breaks only once a browser reaches that line
            assert.ok(!source.includes('typeof require'), `${name} carries a require() shim`);
        }
    });
});
