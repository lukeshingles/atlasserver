// Characterisation tests for TaskPage, the component that owns the queue page's fetching,
// filtering, pagination and history handling.
//
// Written against the class implementation *before* converting it to hooks, so that the
// conversion has something to be checked against. The hazards in that conversion are stale
// closures in the polling intervals and lost setState callbacks, and both show up here as a fetch
// that goes to the wrong URL or does not happen at all.
import assert from 'node:assert/strict';
import test, { after, beforeEach, describe } from 'node:test';

import { flush, importComponent, loadReact, render, setupDom, teardownDom } from './testing.js';

let window;
let TaskPage;
let React;
let ReactDOM;
let requested;
// every rendered page keeps polling intervals alive, which hold the node process open after the
// suite finishes; unmounting between tests is what lets `node --test` exit
let mounted = [];

function task(id, overrides = {}) {
    return {
        id,
        url: `http://testserver/queue/${id}/`,
        user_id: 1,
        ra: 150.0,
        dec: 20.0,
        mpc_name: null,
        comment: `task ${id}`,
        request_type: 'FP',
        use_reduced: false,
        starttimestamp: null,
        finishtimestamp: null,
        timestamp: '2026-01-01T00:00:00Z',
        error_msg: null,
        result_url: null,
        pdfplot_url: null,
        previewimage_url: null,
        imagerequest_url: null,
        imagerequest_task_id: null,
        parent_task_id: null,
        parent_task_url: null,
        queuepos: 0,
        ...overrides,
    };
}

/** Answer the three endpoints the page polls, recording every URL asked for. */
function stubFetch(results) {
    global.fetch = (url) => {
        const href = url.toString();
        requested.push(href);

        if (href.includes('taskrunnerstatus')) {
            return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ stale: false, queued_task_count: 0 }) });
        }
        if (href.includes('queuepositions')) {
            return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ queuepositions: {} }) });
        }

        const single = href.match(/\/queue\/(\d+)\//);
        if (single) {
            return Promise.resolve({ ok: true, status: 200, headers: { get: () => null }, json: () => Promise.resolve(task(parseInt(single[1]))) });
        }

        const started = href.includes('started=true');
        const shown = started ? results.filter((t) => t.starttimestamp != null) : results;
        return Promise.resolve({
            ok: true,
            status: 200,
            headers: { get: () => null },
            json: () => Promise.resolve({ results: shown, taskcount: shown.length, next: null, previous: null, pagefirsttaskposition: 0 }),
        });
    };
}

describe('TaskPage', () => {
    beforeEach(async () => {
        for (const root of mounted.splice(0)) {
            root.unmount();
        }
        if (window) {
            await teardownDom(window);
        }
        window = setupDom();
        requested = [];
        ({ React, ReactDOM } = await loadReact());
        ({ TaskPage } = await importComponent('tasklist.jsx'));
    });

    after(async () => {
        for (const root of mounted.splice(0)) {
            root.unmount();
        }
        if (window) {
            await teardownDom(window);
        }
    });

    const renderPage = async (results) => {
        stubFetch(results);
        const rendered = await render(ReactDOM, React, React.createElement(TaskPage));
        mounted.push(rendered.root);
        await flush(120);
        return rendered;
    };

    const rowIds = (container) =>
        [...container.querySelectorAll('li.task')].map((li) => li.id).filter((id) => id.startsWith('task-'));

    test('fetches the task list on mount and renders a row per task', async () => {
        const { container } = await renderPage([task(1), task(2)]);

        assert.deepEqual(rowIds(container), ['task-1', 'task-2']);
        assert.ok(requested.some((url) => url.includes('/queue/')), requested);
    });

    test('shows the task count from the response', async () => {
        const { container } = await renderPage([task(1), task(2)]);

        assert.match(container.textContent, /Showing tasks 1-2 of 2/);
    });

    test('the running filter changes the URL and re-fetches with it', async () => {
        const { container } = await renderPage([task(1), task(2, { starttimestamp: '2026-01-01T00:00:00Z' })]);
        requested.length = 0;

        [...container.querySelectorAll('button')].find((b) => b.textContent.includes('Running/Finished')).click();
        await flush(150);

        assert.match(window.location.href, /started=true/);
        assert.ok(requested.some((url) => url.includes('started=true')), requested);
        assert.deepEqual(rowIds(container), ['task-2']);
    });

    test('going back to all tasks restores the unfiltered list', async () => {
        const { container } = await renderPage([task(1), task(2, { starttimestamp: '2026-01-01T00:00:00Z' })]);
        [...container.querySelectorAll('button')].find((b) => b.textContent.includes('Running/Finished')).click();
        await flush(150);

        [...container.querySelectorAll('button')].find((b) => b.textContent.includes('All tasks')).click();
        await flush(150);

        assert.doesNotMatch(window.location.href, /started=true/);
        assert.deepEqual(rowIds(container), ['task-1', 'task-2']);
    });

    test('clicking a task shows just that task and retitles the page', async () => {
        const { container } = await renderPage([task(1), task(2)]);

        const link = container.querySelector('li.task a[href*="/queue/"]');
        link.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true }));
        await flush(150);

        assert.match(window.location.href, /\/queue\/1\//);
        assert.deepEqual(rowIds(container), ['task-1']);
        assert.equal(window.document.title, 'Task 1 – ATLAS Forced Photometry');
    });

    test('a modified click is left to the browser', async () => {
        // ctrl/cmd/shift click means "open in a new tab", and hijacking it opened the task in the
        // current one instead
        const { container } = await renderPage([task(1), task(2)]);
        const before = window.location.href;

        const link = container.querySelector('li.task a[href*="/queue/"]');
        link.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true, metaKey: true }));
        await flush(80);

        assert.equal(window.location.href, before);
    });

    test('a history navigation re-reads the URL and fetches it', async () => {
        // every navigation here is a pushState, so Back changes the URL without React hearing
        const { container } = await renderPage([task(1), task(2)]);
        const link = container.querySelector('li.task a[href*="/queue/"]');
        link.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true }));
        await flush(150);
        requested.length = 0;

        window.history.back();
        window.dispatchEvent(new window.Event('popstate'));
        await flush(200);

        assert.ok(requested.length > 0, 'a history navigation fetched nothing');
        assert.deepEqual(rowIds(container), ['task-1', 'task-2']);
        assert.equal(window.document.title, 'Task Queue – ATLAS Forced Photometry');
    });

    test('a fetch that fails after a successful one is reported on the page', async () => {
        // the error used to be a module variable, so the failure handler changed nothing on screen
        const { container } = await renderPage([task(1)]);
        assert.match(container.textContent, /Last updated:/);

        // the message hangs off the "Last updated" line, so it only appears once there has been a
        // successful fetch -- a connection failure on the very first load still shows nothing
        const working = global.fetch;
        global.fetch = (url) => (url.toString().includes('/queue/') && !url.toString().includes('queuepositions')
            ? Promise.reject(new Error('offline'))
            : working(url));

        [...container.querySelectorAll('button')].find((b) => b.textContent.includes('Running/Finished')).click();
        await flush(250);

        assert.match(container.textContent, /Connection error/);
    });
});
