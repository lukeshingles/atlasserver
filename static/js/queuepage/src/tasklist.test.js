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
        attempt_count: 1,
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

    /*
    A finished forced photometry task with a result file, which is what makes TaskPage mount a
    plot. The plot itself is drawn by lightcurveplotly.js, which the browser fetches with the data
    and which is not loaded here; these tests cover the controls around it.
    */
    const plottedtask = (id = 1) => task(id, {
        finishtimestamp: '2026-01-01T01:00:00Z',
        starttimestamp: '2026-01-01T00:30:00Z',
        result_url: `http://testserver/queue/${id}/resultfile.txt`,
    });

    test('unmounting a plot releases its data, even with Plotly on the page', async () => {
        /*
        React takes the div out of the document before this cleanup runs, and Plotly.purge throws
        when it is given the id of a div it cannot find. A throw there would skip the deletes that
        follow it, and the page would hold the data of every plot it had ever shown.
        */
        const purged = [];
        window.Plotly = {
            purge(gd) {
                if (typeof gd === 'string' && !document.getElementById(gd)) {
                    throw new Error(`No DOM element with id '${gd}' exists on the page.`);
                }
                purged.push(typeof gd === 'string' ? gd : gd.id);
            },
        };

        const rendered = await renderPage([plottedtask(1)]);
        assert.ok(rendered.container.querySelector('div.plot'), 'the task should have a plot');

        // what the data script would have left behind
        const key = '#plotforcedflux-task-1';
        jslcdataglobal[key] = [[59000, 100, 10, 18.0, 0.05]];
        jslimitsglobal[key] = {};
        jslabelsglobal[key] = [];
        window.atlasLightcurves = { 'plotforcedflux-task-1': () => {} };

        mounted.splice(mounted.indexOf(rendered.root), 1);
        rendered.root.unmount();
        await flush(40);

        assert.deepEqual(purged, ['plotforcedflux-task-1'], 'the plot was purged without throwing');
        assert.equal(jslcdataglobal[key], undefined);
        assert.equal(jslimitsglobal[key], undefined);
        assert.equal(jslabelsglobal[key], undefined);
        assert.equal(window.atlasLightcurves['plotforcedflux-task-1'], undefined);
    });

    test('a plot opens in flux, and the button switches the div to magnitudes', async () => {
        const { container } = await renderPage([plottedtask(1)]);

        const plot = container.querySelector('div.plot');
        assert.ok(plot, 'the task should have a plot');
        assert.equal(plot.dataset.unit, 'flux');

        const buttons = [...container.querySelectorAll('.plotunits button')];
        assert.deepEqual(buttons.map((b) => b.textContent), ['Flux', 'AB Mag']);
        assert.equal(buttons[0].getAttribute('aria-pressed'), 'true');

        // btn-sm on each button, not only btn-group-sm on the group: the stylesheet paints every
        // .btn in a task row that is not btn-sm white, for the big action buttons, which left the
        // unpressed one white on white and so invisible
        for (const button of buttons) {
            assert.ok(button.classList.contains('btn-sm'), `${button.textContent} needs btn-sm`);
        }

        // the buttons sit over the plot, so they must be inside the positioned box with it
        const box = container.querySelector('.plotbox');
        assert.ok(box, 'the plot and its buttons share a positioned box');
        assert.ok(box.querySelector('.plotunits') && box.querySelector('div.plot'));

        buttons[1].click();
        await flush(20);

        assert.equal(plot.dataset.unit, 'mag');
        assert.equal(buttons[1].getAttribute('aria-pressed'), 'true');
        assert.equal(buttons[0].getAttribute('aria-pressed'), 'false');
    });

    test('choosing a unit redraws that plot, and only that plot', async () => {
        const { container } = await renderPage([plottedtask(1), plottedtask(2)]);

        const redrawn = [];
        window.atlasLightcurves = {
            'plotforcedflux-task-1': () => redrawn.push(1),
            'plotforcedflux-task-2': () => redrawn.push(2),
        };

        const firstrow = container.querySelector('li#task-1');
        firstrow.querySelectorAll('.plotunits button')[1].click();
        await flush(20);

        assert.deepEqual(redrawn, [1]);
        assert.equal(container.querySelector('li#task-1 div.plot').dataset.unit, 'mag');
        assert.equal(container.querySelector('li#task-2 div.plot').dataset.unit, 'flux');
    });

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
     * `runnerstatus` on the same handle overrides the status body, which the wait estimates read.
     */
    const stubFetchWithPositions = (results, positions, runnerstatus = {}) => {
        const control = { positions, results, runnerstatus: { stale: false, queued_task_count: 0, ...runnerstatus } };
        global.fetch = (url, init = {}) => {
            const href = url.toString();
            requested.push(href);

            // a submission, when the test has set control.created to the tasks the server makes
            if (init.method == 'POST') {
                return Promise.resolve({ ok: true, status: 201, json: () => Promise.resolve(control.created ?? []) });
            }
            if (href.includes('taskrunnerstatus')) {
                return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(control.runnerstatus) });
            }
            if (href.includes('queuepositions')) {
                // control.hold, when set, is a promise the test resolves: it keeps one response in
                // flight so that something else can happen before it lands
                const gate = control.hold ?? Promise.resolve();
                control.hold = null;
                // read now rather than when the body is unwrapped, so a held answer carries the queue
                // as it stood when the request was made. That is what makes an overtaken response
                // stale, and a test about one cannot set it up otherwise.
                const body = control.heldpositions ?? control.positions;
                return gate.then(() => ({
                    ok: true,
                    status: 200,
                    json: () => Promise.resolve({
                        queuepositions: body,
                        // the endpoint sends a type per queued task; FP unless a test says otherwise
                        queuedtypes: { ...Object.fromEntries(Object.keys(body).map((id) => [id, 'FP'])),
                            ...control.queuedtypes },
                    }),
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

    test('a task submitted after the set was taken is still counted when the tab is left at once', async () => {
        // The story the away count is for: submit, switch tabs, come back to the answer. The set the
        // count measures against already exists by then, so a refresh gated on "no set yet" left the
        // one task the user is actually waiting for out of it.
        mock.timers.enable({ apis: ['setInterval'] });
        const wasHidden = Object.getOwnPropertyDescriptor(window.document, 'hidden');
        let hidden = false;
        Object.defineProperty(window.document, 'hidden', { configurable: true, get: () => hidden });

        const control = stubFetchWithPositions([task(1, { queuepos: 1 })], { 1: 1 });

        try {
            const rendered = await render(ReactDOM, React, React.createElement(TaskPage));
            mounted.push(rendered.root);
            await flush(120);

            // the set is taken while the tab is being watched, and holds task 1 alone
            mock.timers.tick(2000);
            await flush(60);

            // the submission creates task 2, and the fetch it triggers brings both rows back
            control.created = [{ id: 2 }];
            control.results = [task(1, { queuepos: 1 }), task(2, { queuepos: 2 })];
            control.positions = { 1: 1, 2: 2 };
            rendered.container.querySelector('#newrequest').dispatchEvent(
                new window.Event('submit', { bubbles: true, cancelable: true }));
            await flush(120);

            // left immediately -- no two-second poll has come round since the submission, so the only
            // thing that can have added task 2 to the set is the arrival of the rows
            hidden = true;
            control.positions = {};
            mock.timers.tick(60000);
            await flush(60);

            assert.match(window.document.title, /^\(2\) Task Queue/,
                'the task submitted just before the tab was left went uncounted');
        } finally {
            mock.timers.reset();
            if (wasHidden) {
                Object.defineProperty(window.document, 'hidden', wasHidden);
            }
        }
    });

    test('an away count response overtaken by a later one is discarded', async () => {
        // The away poll asks the same endpoint on its own interval, so it has the same overlap hazard as
        // the position poll: a request outliving its minute is answered after the next one.
        mock.timers.enable({ apis: ['setInterval'] });
        const wasHidden = Object.getOwnPropertyDescriptor(window.document, 'hidden');
        let hidden = false;
        Object.defineProperty(window.document, 'hidden', { configurable: true, get: () => hidden });

        const control = stubFetchWithPositions([task(1, { queuepos: 1 })], { 1: 1, 2: 2 });

        try {
            const rendered = await render(ReactDOM, React, React.createElement(TaskPage));
            mounted.push(rendered.root);
            await flush(120);
            mock.timers.tick(2000);
            await flush(60);

            hidden = true;

            // the first away poll goes out while both tasks are still queued, and is held open
            let land;
            control.hold = new Promise((resolve) => {
                land = resolve;
            });
            mock.timers.tick(60000);
            await flush(20);

            // both finish, and the next away poll -- issued second, answered first -- reports them
            control.positions = {};
            mock.timers.tick(60000);
            await flush(60);
            assert.match(window.document.title, /^\(2\) Task Queue/);

            // only now does the first land, from before either had finished
            land();
            await flush(80);

            assert.match(window.document.title, /^\(2\) Task Queue/,
                'an overtaken away response took the count back down');
        } finally {
            mock.timers.reset();
            if (wasHidden) {
                Object.defineProperty(window.document, 'hidden', wasHidden);
            }
        }
    });

    test('a completion already reported is not reported again on the next absence', async () => {
        // Returning to the tab asks for a fresh set for the next absence to be measured against. If the
        // tab is left again before that answer lands, the set must not still be the one that already
        // had these completions counted out of it, or the same ones are announced a second time.
        mock.timers.enable({ apis: ['setInterval'] });
        const wasHidden = Object.getOwnPropertyDescriptor(window.document, 'hidden');
        let hidden = false;
        Object.defineProperty(window.document, 'hidden', { configurable: true, get: () => hidden });

        const control = stubFetchWithPositions([task(1, { queuepos: 1 })], { 1: 1, 2: 2 });

        try {
            const rendered = await render(ReactDOM, React, React.createElement(TaskPage));
            mounted.push(rendered.root);
            await flush(120);
            mock.timers.tick(2000);
            await flush(60);

            // the first absence: task 2 finishes and is reported
            hidden = true;
            control.positions = { 1: 1 };
            mock.timers.tick(60000);
            await flush(60);
            assert.match(window.document.title, /^\(1\) Task Queue/);

            // the user comes back, which clears the count and asks for a fresh set -- and leaves again
            // before that answer lands
            let land;
            control.hold = new Promise((resolve) => {
                land = resolve;
            });
            hidden = false;
            window.document.dispatchEvent(new window.Event('visibilitychange', { bubbles: true }));
            await flush(40);
            assert.doesNotMatch(window.document.title, /^\(/);
            hidden = true;
            land();
            await flush(80);

            // nothing has finished since, so there is nothing to announce
            mock.timers.tick(60000);
            await flush(60);

            assert.doesNotMatch(window.document.title, /^\(/,
                'a completion counted during the previous absence was announced again');
        } finally {
            mock.timers.reset();
            if (wasHidden) {
                Object.defineProperty(window.document, 'hidden', wasHidden);
            }
        }
    });

    test('a submitted task joins the away baseline even if the tab goes before the answer lands', async () => {
        // The narrowest form of "submit, then leave": the request the new row asks for is itself still in
        // flight when the tab goes. It cannot be taken as the baseline wholesale -- a late answer already
        // omits whatever finished during its round trip -- but the task it adds is safe to keep.
        mock.timers.enable({ apis: ['setInterval'] });
        const wasHidden = Object.getOwnPropertyDescriptor(window.document, 'hidden');
        let hidden = false;
        Object.defineProperty(window.document, 'hidden', { configurable: true, get: () => hidden });

        const control = stubFetchWithPositions([task(1, { queuepos: 1 })], { 1: 1 });

        try {
            const rendered = await render(ReactDOM, React, React.createElement(TaskPage));
            mounted.push(rendered.root);
            await flush(120);

            // the set is taken while visible, and holds task 1 alone
            mock.timers.tick(2000);
            await flush(60);

            // the submission creates task 2, and the request its arrival asks for is held open
            let land;
            control.hold = new Promise((resolve) => {
                land = resolve;
            });
            control.created = [{ id: 2 }];
            control.results = [task(1, { queuepos: 1 }), task(2, { queuepos: 2 })];
            control.positions = { 1: 1, 2: 2 };
            rendered.container.querySelector('#newrequest').dispatchEvent(
                new window.Event('submit', { bubbles: true, cancelable: true }));
            await flush(80);

            // the tab is left before it lands
            hidden = true;
            land();
            await flush(80);

            control.positions = {};
            mock.timers.tick(60000);
            await flush(60);

            assert.match(window.document.title, /^\(2\) Task Queue/,
                'the submitted task was dropped along with the response that named it');
        } finally {
            mock.timers.reset();
            if (wasHidden) {
                Object.defineProperty(window.document, 'hidden', wasHidden);
            }
        }
    });

    test('a queue positions response overtaken by a later one is discarded', async () => {
        // Nothing serialises the two-second poll, so a request slower than the interval overlaps the
        // next. Landing second, its obsolete snapshot used to be written over the newer one.
        mock.timers.enable({ apis: ['setInterval'] });
        const badge = window.document.createElement('span');
        badge.className = 'badge rounded-pill queuecount';
        badge.hidden = true;
        badge.innerHTML = '<span class="queuecount-number">0</span>';
        window.document.body.appendChild(badge);

        const control = stubFetchWithPositions([task(1, { queuepos: 1 })], { 1: 1, 2: 2, 3: 3 });

        try {
            const rendered = await render(ReactDOM, React, React.createElement(TaskPage));
            mounted.push(rendered.root);
            await flush(120);
            mock.timers.tick(2000);
            await flush(80);
            assert.equal(badge.querySelector('.queuecount-number').textContent, '3');

            // a slow request goes out carrying the queue as it stands, and is still in flight when
            // the next one is due
            let land;
            control.hold = new Promise((resolve) => {
                land = resolve;
            });
            mock.timers.tick(2000);
            await flush(20);

            // two of the three finish, and the next poll -- issued second, answered first -- says so
            control.positions = { 1: 1 };
            mock.timers.tick(2000);
            await flush(80);
            assert.equal(badge.querySelector('.queuecount-number').textContent, '1');

            // only now does the slow one land, still saying three
            land();
            await flush(80);

            assert.equal(badge.querySelector('.queuecount-number').textContent, '1',
                'an overtaken response was applied over a newer one');
        } finally {
            mock.timers.reset();
            badge.remove();
        }
    });

    test('a queued row the positions endpoint never mentions does not start a request loop', async () => {
        // The positions poll fetches the whole list whenever a queued row is missing from the response.
        // So if arriving rows asked for the positions again whenever one was missing from the set, the
        // two would feed each other for as long as such a row was on screen -- two requests per round,
        // as fast as the server answers. This stub answers with no positions at all, which is that row
        // for good; no timers are ticked, so nothing here is the ordinary polling.
        stubFetchWithPositions([task(1, { queuepos: 1 })], {});

        const rendered = await render(ReactDOM, React, React.createElement(TaskPage));
        mounted.push(rendered.root);
        await flush(300);

        const positions = requested.filter((href) => href.includes('queuepositions')).length;
        const lists = requested.filter((href) => href.includes('/queue/') && !href.includes('queuepositions')).length;
        assert.ok(positions <= 3, `${positions} queuepositions requests for one unlisted row`);
        assert.ok(lists <= 3, `${lists} task list requests for one unlisted row`);
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

    test('a task that needed several attempts says so', async () => {
        const { container } = await renderPage([task(1, {
            finishtimestamp: '2026-01-01T00:02:50Z',
            attempt_count: 4,
        })]);

        assert.equal(rowMeta(container)['Attempts:'], '4');
    });

    test('a retried task does not blame the queue for time it spent failing', async () => {
        // starttimestamp moves to each new attempt, so the span from submission to it covers the
        // earlier attempts as well as the queue -- which is not a wait
        const { container } = await renderPage([task(1, {
            finishtimestamp: '2026-01-01T00:02:50Z',
            waittime: 2400.0,
            runtime: 130.0,
            attempt_count: 3,
        })]);

        assert.equal(rowMeta(container)['Took:'], 'ran 2m 10s');
    });

    test('a task that ran first time does not mention attempts', async () => {
        const { container } = await renderPage([task(1, {
            finishtimestamp: '2026-01-01T00:02:50Z',
            attempt_count: 1,
        })]);

        assert.ok(!('Attempts:' in rowMeta(container)), 'one attempt is the ordinary case and needs no line');
    });

    test('a finished task reports how long it waited and how long it ran', async () => {
        const { container } = await renderPage([task(1, {
            starttimestamp: '2026-01-01T00:00:40Z',
            finishtimestamp: '2026-01-01T00:02:50Z',
            waittime: 40.0,
            runtime: 130.0,
        })]);

        assert.equal(rowMeta(container)['Took:'], 'waited 40s · ran 2m 10s');
    });

    test('a task cancelled before it ran reports only the half that happened', async () => {
        // waittime is null when the task never started, and rendering "waited null" or dropping the
        // whole line would both be worse than showing the half that is known
        const { container } = await renderPage([task(1, {
            finishtimestamp: '2026-01-01T00:00:40Z',
            waittime: 40.0,
            runtime: null,
        })]);

        assert.equal(rowMeta(container)['Took:'], 'waited 40s');
    });

    /*
     * The wait estimate, as it reaches the page.
     *
     * waitestimate.test.js covers the arithmetic on its own; what these three check is the wiring
     * that only exists once it is assembled -- the runner status poll, the queue positions poll,
     * and the estimate computed from both in TaskPage and handed down to a row.
     */
    const renderWithStatus = async (results, positions, runnerstatus) => {
        stubFetchWithPositions(results, positions, runnerstatus);
        const rendered = await render(ReactDOM, React, React.createElement(TaskPage));
        mounted.push(rendered.root);
        await flush(120);
        return rendered;
    };

    test('a waiting row shows its position and an estimated wait', async () => {
        const { container } = await renderWithStatus([task(1, { queuepos: 20 })], { 1: 20 }, {
            queued_task_count: 21,
            numslots: 16,
            slots_busy: 5,
            distinct_queued_users: 5,
            typical_runtime_seconds: { FP: 600 },
            queued_by_request_type: { FP: 21 },
        });

        const status = container.querySelector('.taskstatus.waiting');
        assert.match(status.textContent, /20 ahead/);
        // 20 ahead, none of them this user's, 5 users sharing the queue: 4 passes x 600s
        assert.match(status.textContent, /~40 min/);
    });

    test('a waiting row shows no estimate when the runner is not running', async () => {
        const { container } = await renderWithStatus([task(1, { queuepos: 4 })], { 1: 4 }, {
            stale: true,
            queued_task_count: 5,
            typical_runtime_seconds: { FP: 600 },
        });

        const status = container.querySelector('.taskstatus.waiting');
        assert.match(status.textContent, /4 ahead/);
        assert.equal(status.querySelector('.taskestimate'), null,
            'a stopped queue must not be counted down against');
    });

    test('another user\'s task gets no estimate from this user\'s queue', async () => {
        // queuepositions.json answers for the signed-in user alone, and a task detail page is
        // public. Applying those positions to somebody else's task counts the owner's own earlier
        // tasks as other users' and divides them by the concurrency.
        const { container } = await renderWithStatus(
            [task(1, { queuepos: 20, user_id: 999 })], { 1: 20 }, {
                queued_task_count: 21,
                numslots: 16,
                slots_busy: 5,
                distinct_queued_users: 5,
                typical_runtime_seconds: { FP: 600 },
            });

        const status = container.querySelector('.taskstatus.waiting');
        assert.match(status.textContent, /20 ahead/);
        assert.equal(status.querySelector('.taskestimate'), null);
    });

    /**
     * Render, then drive one more runner-status poll with a replacement body.
     *
     * The second body is a different object, as `response.json()` gives at each poll. A test
     * function that returns one object makes the comparison in the store invisible, because both
     * results are then the same object.
     *
     * runnerstatus.test.js covers the words of the runner sentence. That sentence goes into
     * the site notice box, on each page, and not into this component. This test covers the other
     * reader of the same response, which is the reason that TaskPage subscribes: the estimates.
     */
    const repollRunnerStatus = async (results, positions, first, second) => {
        mock.timers.enable({ apis: ['setInterval'] });
        try {
            const control = stubFetchWithPositions(results, positions, first);
            const rendered = await render(ReactDOM, React, React.createElement(TaskPage));
            mounted.push(rendered.root);
            await flush(120);

            const estimate = () => rendered.container.querySelector('.taskestimate')?.textContent;

            const before = estimate();
            control.runnerstatus = second;
            mock.timers.tick(60000);
            await flush(120);

            return { before, after: estimate() };
        } finally {
            mock.timers.reset();
        }
    };

    test('a wait estimate follows the runner status it was computed from', async () => {
        const body = {
            numslots: 16, slots_busy: 5, distinct_queued_users: 5,
            queued_task_count: 21, queued_by_request_type: { FP: 21 },
        };
        const { before, after } = await repollRunnerStatus(
            [task(1, { queuepos: 20 })], { 1: 20 },
            { ...body, typical_runtime_seconds: { FP: 600 } },
            { ...body, typical_runtime_seconds: { FP: 1200 } });

        // 20 tasks in front, and no task of this user. 5 users share the queue, and thus the
        // task waits for 4 passes of the median.
        assert.match(before, /~40 min/);
        assert.match(after, /~80 min/);
    });

    test('a bulk submission is not told it will be finished in a moment', async () => {
        // the case the naive formula gets wrong, end to end: 20 of this user's own tasks queued and
        // every slot free. They run one at a time, so the last one waits 20 run times.
        const positions = Object.fromEntries(Array.from({ length: 21 }, (unused, index) => [index + 1, index]));
        const { container } = await renderWithStatus([task(21, { queuepos: 20 })], positions, {
            queued_task_count: 21,
            numslots: 16,
            distinct_queued_users: 1,
            typical_runtime_seconds: { FP: 60 },
        });

        // 20 x 60s = 20 min, not the 20/16 x 60s = 75s that dividing by the slots would claim
        assert.match(container.querySelector('.taskstatus.waiting').textContent, /~20 min/);
    });
});
