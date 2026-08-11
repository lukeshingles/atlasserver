// Characterisation tests for TaskPage, the component that owns the queue page's fetching,
// filtering, pagination and history handling.
//
// Written against the class implementation *before* converting it to hooks, so that the
// conversion has something to be checked against. The hazards in that conversion are stale
// closures in the polling intervals and lost setState callbacks, and both show up here as a fetch
// that goes to the wrong URL or does not happen at all.
import assert from 'node:assert/strict';
import test, { after, beforeEach, describe, mock } from 'node:test';

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
function stubFetch(results, pagination = {}) {
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
            json: () => Promise.resolve({ results: shown, taskcount: shown.length, next: null, previous: null, pagefirsttaskposition: 0, ...pagination }),
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

    const renderPage = async (results, pagination = {}) => {
        stubFetch(results, pagination);
        const rendered = await render(ReactDOM, React, React.createElement(TaskPage));
        mounted.push(rendered.root);
        await flush(120);
        return rendered;
    };

    const rowIds = (container) =>
        [...container.querySelectorAll('li.task')].map((li) => li.id).filter((id) => id.startsWith('task-'));

    /**
     * The label/value pairs of a row's metadata grid, as an object.
     *
     * Read as pairs rather than matched against the row's textContent: <dt> and <dd> are separate
     * elements, so the text of one runs straight into the next with no separator, and a substring
     * like "MPC Object: Makemake" would never appear however correct the row was.
     */
    const rowMeta = (container, selector = 'li.task') => {
        const meta = {};
        const list = container.querySelector(`${selector} dl.taskmeta`);
        if (!list) {
            return meta;
        }
        const children = [...list.children];
        for (let i = 0; i < children.length; i += 2) {
            meta[children[i].textContent] = children[i + 1].textContent;
        }
        return meta;
    };

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

    test('each row is badged with its status and edged with a class to match', async () => {
        const { container } = await renderPage([
            task(1),
            task(2, { starttimestamp: '2026-01-01T00:01:00Z' }),
            task(3, { finishtimestamp: '2026-01-01T00:02:00Z' }),
            task(4, { finishtimestamp: '2026-01-01T00:02:00Z', error_msg: 'No data returned' }),
        ]);

        const badge = (id) => container.querySelector(`#task-${id} .taskbadge`);
        assert.equal(badge(1).textContent, 'Queued');
        assert.equal(badge(2).textContent, 'Running');
        assert.equal(badge(3).textContent, 'Finished');
        assert.equal(badge(4).textContent, 'Error');

        assert.ok(badge(1).classList.contains('taskbadge-queued'));
        assert.ok(badge(2).classList.contains('taskbadge-running'));
        assert.ok(badge(3).classList.contains('taskbadge-finished'));
        assert.ok(badge(4).classList.contains('taskbadge-error'));

        // the classes the left edge is coloured from; a failed task must not be edged as a success
        const row = (id) => [...container.querySelector(`#task-${id}`).classList];
        assert.deepEqual(row(1), ['task', 'queued', 'notstarted']);
        assert.deepEqual(row(2), ['task', 'queued', 'started']);
        assert.deepEqual(row(3), ['task', 'finished']);
        assert.deepEqual(row(4), ['task', 'finished', 'errored']);
    });

    test('a running task gets an indeterminate bar rather than a made-up percentage', async () => {
        const { container } = await renderPage([task(1, { starttimestamp: '2026-01-01T00:01:00Z' })]);

        const bar = container.querySelector('#task-1 .taskprogress');
        assert.ok(bar, 'no progress bar on a running task');
        assert.equal(bar.getAttribute('role'), 'progressbar');
        // the server reports that it started, not how far through it is, so claiming a value here
        // would be inventing one
        assert.equal(bar.getAttribute('aria-valuenow'), null);
        assert.match(bar.getAttribute('aria-label'), /running/);
    });

    test('a waiting task shows its queue position as a chip, and "next" when it is first', async () => {
        const { container } = await renderPage([task(1, { queuepos: 3 }), task(2, { queuepos: 0 })]);

        assert.equal(container.querySelector('#task-1 .taskposition').textContent, '3 ahead in the queue');
        assert.equal(container.querySelector('#task-2 .taskposition').textContent, 'next in the queue');
        // no bar: there is no denominator for "how far up the queue" to draw one against
        assert.equal(container.querySelector('#task-1 .taskprogress'), null);
    });

    test('a finished task has neither a bar nor a position', async () => {
        const { container } = await renderPage([task(1, { finishtimestamp: '2026-01-01T00:02:00Z' })]);

        assert.equal(container.querySelector('#task-1 .taskprogress'), null);
        assert.equal(container.querySelector('#task-1 .taskposition'), null);
    });

    test('the metadata is label and value pairs, in order', async () => {
        const { container } = await renderPage([task(1, { comment: 'a comment', mjd_min: 60000.5, mjd_max: null })]);

        const meta = rowMeta(container);
        assert.equal(meta['Comment:'], 'a comment');
        assert.equal(meta['RA Dec:'], '150 20');
        assert.equal(meta['Images:'], 'Difference');
        assert.equal(meta['MJD request:'], '[60000.5, \u221e]');
        assert.ok('Queued at:' in meta);

        // one <dt> for every <dd>, or the grid would be misaligned from that point down
        const list = container.querySelector('li.task dl.taskmeta');
        assert.equal(list.querySelectorAll('dt').length, list.querySelectorAll('dd').length);
    });

    test('a coordinate can be copied, and the control says so', async () => {
        const written = [];
        // jsdom implements no clipboard, which is also the state of any page served over plain http to
        // a host other than localhost -- see the test below for what the row does then
        Object.defineProperty(global.navigator, 'clipboard', {
            configurable: true,
            value: { writeText: (text) => { written.push(text); return Promise.resolve(); } },
        });

        try {
            const { container } = await renderPage([task(1, { ra: 150.25, dec: -20.5 })]);
            const button = container.querySelector('li.task .copybutton');
            assert.ok(button, 'no copy control on the coordinates');

            button.click();
            await flush(40);

            // the pair alone: what it is for is pasting the position into something else
            assert.deepEqual(written, ['150.25 -20.5']);
            assert.match(container.querySelector('li.task .copyfeedback').textContent, /Copied/);
        } finally {
            delete global.navigator.clipboard;
        }
    });

    test('without a clipboard the value is shown with no control that could not work', async () => {
        const { container } = await renderPage([task(1)]);

        assert.equal(container.querySelector('li.task .copybutton'), null);
        // and the coordinates are still there
        assert.equal(rowMeta(container)['RA Dec:'], '150 20');
    });

    /**
     * Answer the three endpoints, with the queuepositions body under the test's control.
     *
     * Returns a handle whose `positions` can be reassigned, so a test can let the queue empty.
     */
    const stubFetchWithPositions = (results, positions) => {
        const control = { positions, results };
        global.fetch = (url) => {
            const href = url.toString();
            requested.push(href);

            if (href.includes('taskrunnerstatus')) {
                return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ stale: false, queued_task_count: 0 }) });
            }
            if (href.includes('queuepositions')) {
                // control.hold, when set, is a promise the test resolves: it keeps one response in
                // flight so that something else can happen before it lands
                const gate = control.hold ?? Promise.resolve();
                control.hold = null;
                return gate.then(() => ({
                    ok: true,
                    status: 200,
                    json: () => Promise.resolve({ queuepositions: control.heldpositions ?? control.positions }),
                }));
            }
            return Promise.resolve({
                ok: true,
                status: 200,
                headers: { get: () => null },
                json: () => Promise.resolve({
                    results: control.results,
                    taskcount: control.results.length,
                    next: null,
                    previous: null,
                    pagefirsttaskposition: 0,
                }),
            });
        };
        return control;
    };

    test('the tab title counts tasks that left the queue while the tab was hidden', async () => {
        // the away poll runs on an interval an hour of test time would not reach
        mock.timers.enable({ apis: ['setInterval'] });
        const wasHidden = Object.getOwnPropertyDescriptor(window.document, 'hidden');
        let hidden = false;
        Object.defineProperty(window.document, 'hidden', { configurable: true, get: () => hidden });

        // both tasks are in the queue to begin with, and neither is once the tab has been left
        const control = stubFetchWithPositions([task(1, { queuepos: 1 }), task(2, { queuepos: 2 })], { 1: 1, 2: 2 });

        try {
            const rendered = await render(ReactDOM, React, React.createElement(TaskPage));
            mounted.push(rendered.root);
            await flush(120);

            // one queuepositions poll while the tab is still being watched, which is what records the
            // set the count is later measured against
            mock.timers.tick(2000);
            await flush(60);
            assert.doesNotMatch(window.document.title, /^\(/, 'counting before the tab was left');

            hidden = true;
            control.positions = {};
            mock.timers.tick(60000);
            await flush(60);

            assert.match(window.document.title, /^\(2\) Task Queue/);

            // asked again, the answer is the same rather than doubled: the count is recomputed from
            // the set as last seen, not accumulated
            mock.timers.tick(60000);
            await flush(60);
            assert.match(window.document.title, /^\(2\) Task Queue/);

            // and looking at the tab again clears it. Dispatched at the document with bubbles, which
            // is how a browser fires it -- dispatching straight at the window would pass whether or
            // not the listener is reached the way the real event reaches it.
            hidden = false;
            window.document.dispatchEvent(new window.Event('visibilitychange', { bubbles: true }));
            await flush(60);

            assert.doesNotMatch(window.document.title, /^\(/);
        } finally {
            mock.timers.reset();
            if (wasHidden) {
                Object.defineProperty(window.document, 'hidden', wasHidden);
            }
        }
    });

    test('the count is measured against the whole queue, not the page of rows on screen', async () => {
        // one row on screen, five tasks queued: the list is paginated, so the rendered page is a slice
        mock.timers.enable({ apis: ['setInterval'] });
        const wasHidden = Object.getOwnPropertyDescriptor(window.document, 'hidden');
        let hidden = false;
        Object.defineProperty(window.document, 'hidden', { configurable: true, get: () => hidden });

        const control = stubFetchWithPositions([task(1, { queuepos: 1 })], { 1: 1, 2: 2, 3: 3, 4: 4, 5: 5 });

        try {
            const rendered = await render(ReactDOM, React, React.createElement(TaskPage));
            mounted.push(rendered.root);
            await flush(120);
            mock.timers.tick(2000);
            await flush(60);

            hidden = true;
            control.positions = {};
            mock.timers.tick(60000);
            await flush(60);

            // five, not the one row that was visible
            assert.match(window.document.title, /^\(5\) Task Queue/);
        } finally {
            mock.timers.reset();
            if (wasHidden) {
                Object.defineProperty(window.document, 'hidden', wasHidden);
            }
        }
    });

    test('the navbar queue badge follows the queue rather than the page it was drawn with', async () => {
        // the badge as navbar.html renders it for a user with nothing queued
        const badge = window.document.createElement('span');
        badge.className = 'badge rounded-pill queuecount';
        badge.hidden = true;
        badge.innerHTML = '<span class="queuecount-number">0</span>';
        window.document.body.appendChild(badge);

        mock.timers.enable({ apis: ['setInterval'] });
        const control = stubFetchWithPositions([task(1, { queuepos: 1 })], { 1: 1, 2: 2, 3: 3 });

        try {
            const rendered = await render(ReactDOM, React, React.createElement(TaskPage));
            mounted.push(rendered.root);
            await flush(120);
            mock.timers.tick(2000);
            await flush(60);

            // three, the whole queue, though one row is on screen
            assert.equal(badge.querySelector('.queuecount-number').textContent, '3');
            assert.equal(badge.hidden, false, 'the badge stayed hidden with three tasks queued');

            // and it goes away again when the queue empties, which no navigation would have told it
            control.positions = {};
            mock.timers.tick(2000);
            await flush(80);

            assert.equal(badge.querySelector('.queuecount-number').textContent, '0');
            assert.equal(badge.hidden, true, 'the badge is still showing a count with nothing queued');
        } finally {
            mock.timers.reset();
            badge.remove();
        }
    });

    test('a refused clipboard write selects the value, so Ctrl-C has something to copy', async () => {
        // what a browser does when clipboard permission is denied, or the document is not focused
        Object.defineProperty(global.navigator, 'clipboard', {
            configurable: true,
            value: { writeText: () => Promise.reject(new Error('NotAllowedError')) },
        });

        try {
            const { container } = await renderPage([task(1, { ra: 150.25, dec: -20.5 })]);
            container.querySelector('li.task .copybutton').click();
            await flush(40);

            assert.match(container.querySelector('li.task .copyfeedback').textContent, /Ctrl-C/);

            // the instruction is only useful if the value is under the selection
            const selection = window.getSelection();
            assert.equal(selection.rangeCount, 1, 'nothing was selected to copy');
            assert.equal(selection.toString(), '150.25 -20.5');
            assert.ok(
                container.querySelector('li.task .copyvalue').contains(selection.getRangeAt(0).commonAncestorContainer),
                'the selection is not inside the value',
            );
        } finally {
            delete global.navigator.clipboard;
            window.getSelection().removeAllRanges();
        }
    });

    test('the pager puts Newer and Older at opposite ends', async () => {
        const { container } = await renderPage([task(1)], { next: 'http://testserver/queue/?cursor=older' });

        // main.css pushes .pagenext right with an auto margin, so which item carries the class is what
        // decides whether Older ends up at the correct edge
        const items = [...container.querySelectorAll('#paginator .page-item')];
        assert.ok(items.length > 0, 'no pager rendered');
        const older = items.find((li) => li.textContent.includes('Older'));
        assert.ok(older, 'no Older button');
        assert.ok(older.classList.contains('pagenext'), 'Older is not the item that gets pushed right');
    });

    test('the queue is still polled when every row on screen has finished', async () => {
        // the case a single task view, the Running/Finished filter and an older page all produce: the
        // user still has tasks running, but nothing visible could change from the answer
        mock.timers.enable({ apis: ['setInterval'] });
        const badge = window.document.createElement('span');
        badge.className = 'badge rounded-pill queuecount';
        badge.innerHTML = '<span class="queuecount-number">2</span>';
        window.document.body.appendChild(badge);

        const control = stubFetchWithPositions([task(1, { queuepos: 1 })], { 1: 1, 2: 2 });

        try {
            const rendered = await render(ReactDOM, React, React.createElement(TaskPage));
            mounted.push(rendered.root);
            await flush(120);
            mock.timers.tick(2000);
            await flush(60);
            assert.equal(badge.querySelector('.queuecount-number').textContent, '2', 'the first poll did not run');

            // the row on screen finishes, so no visible row tracks a position any more
            control.results = [task(1, { finishtimestamp: '2026-01-01T00:05:00Z', queuepos: null })];
            mock.timers.tick(6000);
            await flush(80);

            // meanwhile one of the user's other tasks finishes too
            control.positions = { 2: 2 };
            mock.timers.tick(2000);
            await flush(80);

            // the poll has to have run for this to have changed
            assert.equal(badge.querySelector('.queuecount-number').textContent, '1');
            assert.equal(badge.hidden, false);
        } finally {
            mock.timers.reset();
            badge.remove();
        }
    });

    test('the queue is asked about once even when nothing on screen is queued', async () => {
        // opening a finished task, a completed-only filter or an older page directly: no row tracks a
        // position and nothing has been asked yet, so there is no answer to fall back on
        mock.timers.enable({ apis: ['setInterval'] });
        const badge = window.document.createElement('span');
        badge.className = 'badge rounded-pill queuecount';
        badge.hidden = true;
        badge.innerHTML = '<span class="queuecount-number">0</span>';
        window.document.body.appendChild(badge);

        // the one visible task has finished; two others of the user's are still queued
        stubFetchWithPositions([task(1, { finishtimestamp: '2026-01-01T00:05:00Z', queuepos: null })], { 2: 2, 3: 3 });

        try {
            const rendered = await render(ReactDOM, React, React.createElement(TaskPage));
            mounted.push(rendered.root);
            await flush(120);
            mock.timers.tick(2000);
            await flush(80);

            // the badge can only know this if the request was made at all
            assert.equal(badge.querySelector('.queuecount-number').textContent, '2');
            assert.equal(badge.hidden, false);
        } finally {
            mock.timers.reset();
            badge.remove();
        }
    });

    test('a poll that lands after the tab is hidden does not become the away baseline', async () => {
        mock.timers.enable({ apis: ['setInterval'] });
        const wasHidden = Object.getOwnPropertyDescriptor(window.document, 'hidden');
        let hidden = false;
        Object.defineProperty(window.document, 'hidden', { configurable: true, get: () => hidden });

        const control = stubFetchWithPositions([task(1, { queuepos: 1 })], { 1: 1, 2: 2 });

        try {
            const rendered = await render(ReactDOM, React, React.createElement(TaskPage));
            mounted.push(rendered.root);
            await flush(120);

            // a first poll while visible records both tasks as the baseline
            mock.timers.tick(2000);
            await flush(80);

            // a second poll starts, and is still in flight when the tab is left. Task 2 finishes during
            // that round trip, so the answer it eventually gives already omits it.
            let land;
            control.hold = new Promise((resolve) => {
                land = resolve;
            });
            control.heldpositions = { 1: 1 };
            mock.timers.tick(2000);
            hidden = true;
            land();
            await flush(80);
            control.heldpositions = null;

            // the queue has not changed again since
            control.positions = { 1: 1 };
            mock.timers.tick(60000);
            await flush(80);

            // task 2 left while the user was away, so the title has to say so. Taking the late
            // response as the baseline would have lost it, because that response already omitted it.
            assert.match(window.document.title, /^\(1\) Task Queue/);
        } finally {
            mock.timers.reset();
            if (wasHidden) {
                Object.defineProperty(window.document, 'hidden', wasHidden);
            }
        }
    });

    test('leaving the tab before the first poll still leaves the away count a baseline', async () => {
        // submitting a task and immediately switching tabs, which is what waiting for one looks like:
        // the two-second poll never gets a chance to run, and once hidden it is skipped
        mock.timers.enable({ apis: ['setInterval'] });
        const wasHidden = Object.getOwnPropertyDescriptor(window.document, 'hidden');
        Object.defineProperty(window.document, 'hidden', { configurable: true, get: () => true });

        const control = stubFetchWithPositions([task(1, { queuepos: 1 })], { 1: 1, 2: 2 });

        try {
            const rendered = await render(ReactDOM, React, React.createElement(TaskPage));
            mounted.push(rendered.root);
            await flush(150);

            // no tick(2000) here on purpose: the baseline can only exist if the arrival of the rows
            // asked for it
            control.positions = {};
            mock.timers.tick(60000);
            await flush(80);

            assert.match(window.document.title, /^\(2\) Task Queue/);
        } finally {
            mock.timers.reset();
            if (wasHidden) {
                Object.defineProperty(window.document, 'hidden', wasHidden);
            }
        }
    });

    test('an empty queue slows the poll rather than stopping it', async () => {
        // work can appear from another tab or the API, and a view showing a finished task never gains
        // a row that would start the poll again
        mock.timers.enable({ apis: ['setInterval'] });
        const badge = window.document.createElement('span');
        badge.className = 'badge rounded-pill queuecount';
        badge.hidden = true;
        badge.innerHTML = '<span class="queuecount-number">0</span>';
        window.document.body.appendChild(badge);

        const control = stubFetchWithPositions([task(1, { finishtimestamp: '2026-01-01T00:05:00Z', queuepos: null })], {});

        try {
            const rendered = await render(ReactDOM, React, React.createElement(TaskPage));
            mounted.push(rendered.root);
            await flush(150);
            assert.equal(badge.hidden, true, 'nothing is queued to begin with');

            // something is submitted elsewhere; this page's rows cannot show it
            control.positions = { 7: 0 };
            // a few ticks pass at the slower rate
            for (let tick = 0; tick < 5; tick += 1) {
                mock.timers.tick(2000);
                await flush(40);
            }

            assert.equal(badge.querySelector('.queuecount-number').textContent, '1');
            assert.equal(badge.hidden, false, 'the poll never asked again');
        } finally {
            mock.timers.reset();
            badge.remove();
        }
    });

    test('returning to the tab does not let the next absence recount what already finished', async () => {
        mock.timers.enable({ apis: ['setInterval'] });
        const wasHidden = Object.getOwnPropertyDescriptor(window.document, 'hidden');
        let hidden = false;
        Object.defineProperty(window.document, 'hidden', { configurable: true, get: () => hidden });

        const control = stubFetchWithPositions([task(1, { queuepos: 1 })], { 1: 1, 2: 2 });

        try {
            const rendered = await render(ReactDOM, React, React.createElement(TaskPage));
            mounted.push(rendered.root);
            await flush(150);

            // away once, during which task 2 finishes
            hidden = true;
            control.positions = { 1: 1 };
            mock.timers.tick(60000);
            await flush(80);
            assert.match(window.document.title, /^\(1\) Task Queue/, 'the first absence was not reported');

            // back, then away again immediately -- before the two-second poll could refresh anything
            hidden = false;
            window.document.dispatchEvent(new window.Event('visibilitychange', { bubbles: true }));
            await flush(80);
            hidden = true;
            mock.timers.tick(60000);
            await flush(80);

            // nothing has finished during this second absence, so there is nothing to report
            assert.doesNotMatch(window.document.title, /^\(/, 'task 2 was counted a second time');
        } finally {
            mock.timers.reset();
            if (wasHidden) {
                Object.defineProperty(window.document, 'hidden', wasHidden);
            }
        }
    });

    test('each row wraps its content in the element its show and hide animates over', async () => {
        const { container } = await renderPage([task(1)]);
        const row = container.querySelector('li.task');

        // The row is a one-track grid and .taskinner is the item whose height the track animates.
        // Exactly one child, because a second grid item would get a track of its own and be left
        // outside the one that collapses.
        assert.equal(row.children.length, 1, 'the row should have exactly one child');
        assert.ok(row.firstElementChild.classList.contains('taskinner'), 'the child is not .taskinner');
        assert.ok(row.querySelector('.taskinner .taskheading'), 'the content is not inside .taskinner');
    });

    test('placeholder rows stand in for the list until it arrives', async () => {
        // the task list request is held open, because every other stub here resolves immediately and
        // there would otherwise be no loading state left to look at by the time render() returns
        let release;
        const arrived = new Promise((resolve) => {
            release = resolve;
        });
        global.fetch = (url) => {
            const href = url.toString();
            requested.push(href);
            if (href.includes('taskrunnerstatus')) {
                return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ stale: false, queued_task_count: 0 }) });
            }
            if (href.includes('queuepositions')) {
                return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ queuepositions: {} }) });
            }
            return arrived.then(() => ({
                ok: true,
                status: 200,
                headers: { get: () => null },
                json: () => Promise.resolve({ results: [task(1)], taskcount: 1, next: null, previous: null, pagefirsttaskposition: 0 }),
            }));
        };

        const rendered = await render(ReactDOM, React, React.createElement(TaskPage));
        mounted.push(rendered.root);

        const skeletons = rendered.container.querySelectorAll('li.taskskeleton');
        assert.equal(skeletons.length, 3, 'no placeholder rows while loading');
        assert.equal(skeletons[0].getAttribute('aria-hidden'), 'true', 'the shapes are read out');
        assert.match(rendered.container.textContent, /Loading tasks/);
        assert.equal(rendered.container.querySelector('ul.tasks').getAttribute('aria-busy'), 'true');
        // and no row is claiming a status it cannot know yet
        assert.equal(rendered.container.querySelector('.taskbadge'), null);

        release();
        await flush(150);

        assert.equal(rendered.container.querySelectorAll('li.taskskeleton').length, 0);
        assert.deepEqual(rowIds(rendered.container), ['task-1']);
        assert.equal(rendered.container.querySelector('ul.tasks').getAttribute('aria-busy'), null);
    });

    test('an empty mpc_name is shown as the coordinate target it is', async () => {
        // task_mpc_name_not_blank means the server can only send "" or a real name here, so this
        // is the whole blank case rather than one spelling of it
        const { container } = await renderPage([task(1, { mpc_name: '', ra: 150.0, dec: 20.0 })]);

        assert.equal(rowMeta(container)['RA Dec:'], '150 20');
        assert.doesNotMatch(container.textContent, /MPC Object/);
    });

    test('a real mpc_name is still shown as one', async () => {
        const { container } = await renderPage([task(1, { mpc_name: 'Makemake', ra: null, dec: null })]);

        assert.equal(rowMeta(container)['MPC Object:'], 'Makemake');
        assert.ok(!('RA Dec:' in rowMeta(container)), 'the coordinate row should be absent');
    });
});
