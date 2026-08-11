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
