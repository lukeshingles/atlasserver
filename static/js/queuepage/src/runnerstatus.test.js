'use strict';

// The shared runner status. These tests cover the words, the test for a change, the store that
// polls, and the box that the store draws into. Each page loads this module. Thus these tests run
// without React, and, except for the last two groups, without a document.

import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import test, { afterEach, beforeEach, describe, mock } from 'node:test';
import { fileURLToPath } from 'node:url';

import {
    createStore, describeAge, renderInto, runnerMessage, runnerStatusEqual,
} from './runnerstatus.js';
import { setupDom, teardownDom } from './testing.js';

const SRC = dirname(fileURLToPath(import.meta.url));

/** Let the promises of a fetch complete. This also works with test timers in place. */
async function settle() {
    for (let i = 0; i < 10; i += 1) {
        await Promise.resolve();
    }
}

const healthy = (overrides = {}) => ({
    stale: false,
    maintenance: false,
    numslots: 16,
    slots_busy: 2,
    queued_task_count: 3,
    distinct_queued_users: 2,
    typical_runtime_seconds: { FP: 60 },
    written: '2026-01-01T00:00:00Z',
    status_age_seconds: 3.1,
    ...overrides,
});

test('describeAge', async (t) => {
    await t.test('under a minute', () => {
        assert.equal(describeAge(0), 'less than a minute ago');
        assert.equal(describeAge(59), 'less than a minute ago');
    });

    await t.test('minutes count completed units only', () => {
        assert.equal(describeAge(60), '1 minute ago');
        assert.equal(describeAge(119), '1 minute ago');
        assert.equal(describeAge(120), '2 minutes ago');
        // This value is the reason to round down. To round up gives "60 minutes ago".
        assert.equal(describeAge(3599), '59 minutes ago');
    });

    await t.test('hours', () => {
        assert.equal(describeAge(3600), '1 hour ago');
        assert.equal(describeAge(7199), '1 hour ago');
        assert.equal(describeAge(7200), '2 hours ago');
        // A value that is a little less than one day stays in hours. It does not become a day.
        assert.equal(describeAge(86399), '23 hours ago');
    });

    await t.test('days', () => {
        assert.equal(describeAge(86400), '1 day ago');
        // This value is the reason for the function. 6401 minutes reads better as days.
        assert.equal(describeAge(6401 * 60), '4 days ago');
    });
});

/*
 * The test that decides whether a poll gives new information.
 *
 * These tests call the function directly, and not through the box. The function decides whether
 * the store reports at all. A status with the same words leaves the box the same, and thus no test
 * of the page can see the difference.
 */
describe('runnerStatusEqual', () => {
    test('a poll that only moved the clock says nothing new', () => {
        // Each healthy poll gives this difference. It is the reason for the list of fields to
        // ignore.
        assert.equal(runnerStatusEqual(
            healthy(), healthy({ written: '2026-01-01T00:01:00Z', status_age_seconds: 8.4 })), true);
    });

    test('the running task ids are not read, so they do not count as a change', () => {
        assert.equal(runnerStatusEqual(
            healthy({ running_taskids: [1, 2] }), healthy({ running_taskids: [3, 4] })), true);
    });

    for (const field of ['stale', 'maintenance', 'slots_busy', 'numslots', 'queued_task_count', 'distinct_queued_users']) {
        test(`a change to ${field} is a change`, () => {
            const before = healthy();
            assert.equal(runnerStatusEqual(before, healthy({ [field]: 99 })), false);
        });
    }

    test('a change to the medians is a change', () => {
        assert.equal(runnerStatusEqual(
            healthy(), healthy({ typical_runtime_seconds: { FP: 61 } })), false);
        assert.equal(runnerStatusEqual(
            healthy(), healthy({ typical_runtime_seconds: { FP: 60, IMGZIP: 900 } })), false);
    });

    test('a field the endpoint gains later is compared by default', () => {
        // This is why the module lists the fields to ignore, and not the fields to read. A new
        // field can cost one rewrite of the box. But no reader that needs the field can miss it.
        assert.equal(runnerStatusEqual(healthy(), healthy({ future_field: 'a' })), false);
    });

    test('the age is compared while the runner is down, since nothing else moves then', () => {
        assert.equal(runnerStatusEqual(
            healthy({ stale: true, status_age_seconds: 61 }),
            healthy({ stale: true, status_age_seconds: 7200 })), false);
    });

    test('a first answer is always a change', () => {
        assert.equal(runnerStatusEqual(null, healthy()), false);
        assert.equal(runnerStatusEqual(null, null), true);
    });
});

describe('runnerMessage', () => {
    test('a working runner with a queue reports its slots and the queue', () => {
        const message = runnerMessage(healthy({ slots_busy: 7, numslots: 16, queued_task_count: 9 }));
        assert.match(message, /^Task runner: 7 of 16 slots busy, 9 unfinished tasks from all users/);
    });

    test('one queued task is a task, not tasks', () => {
        assert.match(runnerMessage(healthy({ queued_task_count: 1 })), /1 unfinished task from/);
    });

    test('the maintenance sweep is named instead of the frozen slot counts', () => {
        const message = runnerMessage(healthy({ maintenance: true }));
        assert.match(message, /maintenance sweep in progress;/);
        assert.doesNotMatch(message, /slots busy/);
    });

    test('a working runner with an empty queue says nothing', () => {
        // the normal case, and the one the box must stay quiet in
        assert.equal(runnerMessage(healthy({ queued_task_count: 0 })), null);
    });

    test('the queue counts are withheld from a reader who is not waiting on the queue', () => {
        // "12 unfinished tasks from all users" answers "when does my task start". A reader of the
        // FAQ with nothing queued has not asked that question.
        assert.equal(runnerMessage(healthy(), { showQueue: false }), null);
    });

    test('the outage is told to everybody, waiting or not', () => {
        // this one is not an answer about a queue position: it says what to expect of the site
        const message = runnerMessage({ stale: true, status_age_seconds: 600 }, { showQueue: false });
        assert.match(message, /not currently processing jobs/);
    });

    test('no answer yet says nothing', () => {
        assert.equal(runnerMessage(null), null);
    });

    test('a stopped runner says so, and how long it has been quiet', () => {
        const message = runnerMessage(healthy({ stale: true, status_age_seconds: 7200 }));
        assert.match(message, /^The task runner is not currently processing jobs\. It last reported 2 hours ago\./);
        assert.match(message, /no need to submit them again/);
    });

    test('a stopped runner of unknown age still says it is stopped', () => {
        const message = runnerMessage({ stale: true });
        assert.match(message, /^The task runner is not currently processing jobs\. Queued/);
    });
});

describe('the store', () => {
    let bodies;
    let fetched;

    beforeEach(() => {
        fetched = [];
        bodies = [];
        global.fetch = (url) => {
            fetched.push(url.toString());
            const next = bodies.shift();
            if (next == null) {
                return Promise.reject(new Error('unreachable'));
            }
            // A new object at each poll, as response.json() gives. One shared object would make
            // the comparison in the store invisible, because both results are then that object.
            return Promise.resolve({ ok: next.status !== 503, status: next.status || 200, json: () => Promise.resolve(next.body) });
        };
    });

    afterEach(() => {
        delete global.fetch;
        mock.timers.reset();
    });

    test('the first response reaches a subscriber', async () => {
        bodies.push({ body: healthy() });
        const store = createStore({ url: '/taskrunnerstatus.json', poll: 1000 });
        const seen = [];

        const unsubscribe = store.subscribe((status) => seen.push(status));
        // subscribing answers at once with what is known so far, which is nothing
        assert.deepEqual(seen, [null]);

        await settle();
        assert.equal(seen.length, 2);
        assert.equal(seen[1].slots_busy, 2);
        unsubscribe();
    });

    test('a subscriber that arrives later is told the current status at once', async () => {
        bodies.push({ body: healthy() });
        const store = createStore({ url: '/taskrunnerstatus.json', poll: 1000 });
        const first = store.subscribe(() => {});
        await settle();

        const seen = [];
        const second = store.subscribe((status) => seen.push(status));
        assert.equal(seen.length, 1);
        assert.equal(seen[0].slots_busy, 2);

        first();
        second();
    });

    test('a poll that says the same thing tells nobody', async () => {
        bodies.push({ body: healthy() }, { body: healthy({ written: '2026-01-01T00:01:00Z', status_age_seconds: 9 }) });
        const store = createStore({ url: '/taskrunnerstatus.json', poll: 1000 });
        const seen = [];
        const unsubscribe = store.subscribe((status) => seen.push(status));

        await settle();
        const published = store.current();
        await store.refresh();
        await settle();

        assert.equal(seen.length, 2, 'an unchanged poll must not be reported');
        // the object is kept as well as the notification, because the queue page renders from it
        assert.equal(store.current(), published);
        unsubscribe();
    });

    test('a stopped runner whose age moved is news', async () => {
        // Each other field holds its value while the task runner is down. The task runner does
        // not write the status file, and the endpoint gives no medians. Thus the age is the one
        // field that changes. A store that ignores it would keep the first status for the full
        // outage.
        const down = (age) => ({ body: { stale: true, queued_task_count: 1, status_age_seconds: age, typical_runtime_seconds: {} } });
        bodies.push(down(61), down(7200));
        const store = createStore({ url: '/taskrunnerstatus.json', poll: 1000 });
        const seen = [];
        const unsubscribe = store.subscribe((status) => seen.push(status));

        await settle();
        await store.refresh();
        await settle();

        assert.equal(seen.length, 3);
        assert.match(runnerMessage(seen[1]), /1 minute ago/);
        assert.match(runnerMessage(seen[2]), /2 hours ago/);
        unsubscribe();
    });

    test('a 503 is read for its body, because that is how an outage is reported', async () => {
        bodies.push({ status: 503, body: { stale: true, status_age_seconds: 300 } });
        const store = createStore({ url: '/taskrunnerstatus.json', poll: 1000 });
        const seen = [];
        const unsubscribe = store.subscribe((status) => seen.push(status));

        await settle();
        assert.equal(seen[1].stale, true);
        unsubscribe();
    });

    test('an unreachable endpoint is not an outage, until it has been unreachable for a while', async () => {
        mock.timers.enable({ apis: ['setInterval', 'Date'] });
        bodies.push({ body: healthy() });
        const store = createStore({ url: '/taskrunnerstatus.json', poll: 1000 });
        const seen = [];
        const unsubscribe = store.subscribe((status) => seen.push(status));
        await settle();

        // Four failed polls. Our own connection tells us nothing about the task runner.
        for (let i = 0; i < 4; i += 1) {
            mock.timers.tick(1000);
            await settle();
        }
        assert.equal(store.current().slots_busy, 2, 'a status is not withdrawn on our own failures');

        // At the sixth poll the status is too old to report, in either condition.
        mock.timers.tick(2000);
        await settle();
        assert.equal(store.current(), null);
        assert.equal(seen.at(-1), null);
        unsubscribe();
    });

    test('a tab that comes back asks again rather than showing what it last heard', async () => {
        // A hidden tab gets no poll. Thus its sentence is wrong for an outage that started or
        // stopped while the reader was away. This test makes its own document, because it is the
        // one store test of an event. The other tests need no document, and they are faster.
        const window = setupDom();
        try {
            bodies.push({ body: healthy() }, { body: healthy({ slots_busy: 9 }) });
            const store = createStore({ url: '/taskrunnerstatus.json', poll: 1000 });
            const unsubscribe = store.subscribe(() => {});
            await settle();
            assert.equal(fetched.length, 1);

            window.document.dispatchEvent(new window.Event('visibilitychange'));
            await settle();

            assert.equal(fetched.length, 2);
            assert.equal(store.current().slots_busy, 9);
            unsubscribe();
        } finally {
            await teardownDom(window);
        }
    });

    test('the answer to the newest question is the one that is kept', async () => {
        // The interval and a return to the tab can occur in the same second, and the two
        // answers can arrive in either order. An older value that holds for a full poll would also
        // move the wait estimate of each row of the queue.
        const answers = [];
        global.fetch = () => new Promise((resolve) => {
            const body = { ...healthy(), queued_task_count: answers.length + 1 };
            answers.push((slow) => resolve({ ok: true, status: 200, json: () => Promise.resolve(body) }));
        });

        const store = createStore({ url: '/taskrunnerstatus.json', poll: 100000 });
        const unsubscribe = store.subscribe(() => {});
        store.refresh();
        await settle();

        // The store asked the second question last. Thus its answer is the one to keep, in
        // either order of arrival.
        answers[1]();
        await settle();
        answers[0]();
        await settle();

        assert.equal(store.current().queued_task_count, 2);
        unsubscribe();
    });

    test('a request in flight when the poll stops does not put back what was forgotten', async () => {
        let answer;
        global.fetch = () => new Promise((resolve) => {
            answer = () => resolve({ ok: true, status: 200, json: () => Promise.resolve(healthy()) });
        });

        const store = createStore({ url: '/taskrunnerstatus.json', poll: 100000 });
        store.subscribe(() => {})();
        answer();
        await settle();

        assert.equal(store.current(), null, 'a stopped store keeps no reading');
    });

    test('one reader that fails does not silence the others', async () => {
        // The box subscribes first and the queue page subscribes second. An exception from the
        // first reader went to the catch of the fetch, which reads each exception as an endpoint
        // that it cannot reach. The queue page then had no status for the life of the page.
        bodies.push({ body: healthy() });
        const store = createStore({ url: '/taskrunnerstatus.json', poll: 100000 });
        const seen = [];
        const errors = [];
        const consoleError = console.error;
        console.error = (...args) => errors.push(args);
        let first;
        let second;

        try {
            first = store.subscribe(() => { throw new Error('a reader is broken'); });
            second = store.subscribe((status) => seen.push(status));
            await settle();

            assert.equal(seen.at(-1).slots_busy, 2, 'the second reader is told');
            // Two times. The reader fails on the status that it gets at the subscription, and
            // it fails again on the first response.
            assert.equal(errors.length, 2, 'and the reader\'s own fault is reported');
        } finally {
            console.error = consoleError;
            if (first) { first(); }
            if (second) { second(); }
        }
    });

    test('the last reader to leave stops the poll', async () => {
        mock.timers.enable({ apis: ['setInterval', 'Date'] });
        bodies.push({ body: healthy() }, { body: healthy({ slots_busy: 9 }) });
        const store = createStore({ url: '/taskrunnerstatus.json', poll: 1000 });

        const first = store.subscribe(() => {});
        const second = store.subscribe(() => {});
        await settle();
        assert.equal(fetched.length, 1, 'a second reader shares the first one\'s poll');

        first();
        mock.timers.tick(1000);
        await settle();
        assert.equal(fetched.length, 2, 'one reader left is still a reader');

        second();
        mock.timers.tick(5000);
        await settle();
        assert.equal(fetched.length, 2);
    });

    test('unsubscribing twice does not stop a poll somebody else started', async () => {
        mock.timers.enable({ apis: ['setInterval', 'Date'] });
        bodies.push({ body: healthy() }, { body: healthy() }, { body: healthy() });
        const store = createStore({ url: '/taskrunnerstatus.json', poll: 1000 });

        const first = store.subscribe(() => {});
        first();
        first();
        const second = store.subscribe(() => {});
        await settle();

        mock.timers.tick(1000);
        await settle();
        assert.equal(fetched.length, 3, 'the second reader is still being polled for');
        second();
    });
});

/*
 * The two properties of this module that hold only outside it.
 *
 * Each page loads the module as a <script type="module">, and only the queue page renders an
 * import map. Thus the module must resolve no name, and it must find the box itself. A test of an
 * exported function shows neither property. An import in this module keeps the queue page correct
 * and empties the sentence on each other page. A start that stops to find #sitenotice leaves each
 * page quiet, and each test continues to pass.
 */
describe('the module as a page loads it', () => {
    test('it imports nothing, because most pages have no import map to resolve a name with', async () => {
        const built = await readFile(join(SRC, '..', '..', 'runnerstatus.min.js'), 'utf8');

        assert.equal(/\bfrom\s*["']/.test(built), false, 'a bare specifier here fails on every page but the queue');
        assert.equal(/\bimport\s*\(/.test(built), false);
    });

    test('it finds the box and fills it, with nothing else on the page to help', async () => {
        const window = setupDom();
        try {
            window.document.body.innerHTML = `
                <div class="sitenotice" id="sitenotice" data-showqueue>
                  <p class="sitenotice-note">Standing note.</p>
                  <p class="sitenotice-runner" id="runnerstatus" role="status" aria-live="off"></p>
                </div>`;
            global.fetch = () => Promise.resolve({
                ok: true, status: 200, json: () => Promise.resolve(healthy({ slots_busy: 5 })),
            });

            // A new copy. The module reads the document and subscribes as node evaluates it,
            // and node holds one copy of a module for each URL.
            const module = await import(`./runnerstatus.js?bootstrap=${Date.now()}`);
            await settle();

            assert.match(window.document.getElementById('runnerstatus').textContent, /5 of 16 slots busy/);
            module.stopPageStore();
        } finally {
            delete global.fetch;
            await teardownDom(window);
        }
    });
});

describe('the box', () => {
    let window;

    beforeEach(() => {
        window = setupDom();
        window.document.body.innerHTML = `
            <div class="sitenotice" id="sitenotice">
              <p class="sitenotice-note">Standing note about the data.</p>
              <p class="sitenotice-runner" id="runnerstatus" role="status"></p>
            </div>`;
    });

    afterEach(async () => {
        await teardownDom(window);
    });

    const box = () => window.document.getElementById('sitenotice');
    const note = () => window.document.querySelector('.sitenotice-note').textContent;

    test('the runner line is filled and the note is left alone', () => {
        box().setAttribute('data-showqueue', '');
        renderInto(box(), healthy({ slots_busy: 4, queued_task_count: 5 }));

        assert.match(window.document.getElementById('runnerstatus').textContent, /4 of 16 slots busy/);
        assert.equal(note(), 'Standing note about the data.');
        assert.equal(box().classList.contains('stale'), false);
    });

    test('the queue line is drawn only when the box asks for it', () => {
        const line = () => window.document.getElementById('runnerstatus').textContent;

        renderInto(box(), healthy());
        assert.equal(line(), '', 'the box carries no data-showqueue in this fixture');

        box().setAttribute('data-showqueue', '');
        renderInto(box(), healthy({ slots_busy: 4 }));
        assert.match(line(), /4 of 16 slots busy/);
    });

    test('an outage carries a mark as well as a colour', () => {
        renderInto(box(), { stale: true, status_age_seconds: 3600 });

        const mark = box().querySelector('.sitenotice-warnmark');
        assert.notEqual(mark, null, 'the yellow panel must not be the only sign of an outage');
        assert.equal(mark.getAttribute('aria-hidden'), 'true', 'the sentence beside it already says this');
        // the sentence is still the sentence, mark or no mark
        assert.match(window.document.getElementById('runnerstatus').textContent, /not currently processing jobs/);
    });

    test('an unchanged sentence is not written again', () => {
        // The sentence is in a live region, and a second write makes a screen reader speak it
        // again. In an outage the age changes at each poll, and the words stay the same for an
        // hour.
        renderInto(box(), { stale: true, status_age_seconds: 7200 });
        const line = window.document.getElementById('runnerstatus');
        const mark = line.firstChild;

        renderInto(box(), { stale: true, status_age_seconds: 7260 });

        assert.equal(line.firstChild, mark, 'the same nodes are still there, untouched');
    });

    test('only the outage sentence is announced', () => {
        // The queue counts are on each page of the site. Spoken each minute over the work of
        // the reader, they interrupt a reader who did not ask for them.
        const line = () => window.document.getElementById('runnerstatus');

        box().setAttribute('data-showqueue', '');
        renderInto(box(), healthy());
        assert.equal(line().getAttribute('aria-live'), 'off');

        renderInto(box(), { stale: true, status_age_seconds: 60 });
        assert.equal(line().getAttribute('aria-live'), 'polite');
    });

    test('a box with nothing to say collapses instead of leaving a panel', () => {
        // Both parts must be empty. The reader removed the note, or the note has no words in
        // it, and the task runner is quiet. One class comes from each part. Thus a browser needs
        // no support for :has() to remove an empty box.
        renderInto(box(), healthy({ queued_task_count: 0 }));

        assert.equal(box().classList.contains('sitenotice-noline'), true);

        box().setAttribute('data-showqueue', '');
        renderInto(box(), healthy());
        assert.equal(box().classList.contains('sitenotice-noline'), false);
    });

    test('a runner that came back takes the mark with it', () => {
        box().setAttribute('data-showqueue', '');
        renderInto(box(), { stale: true, status_age_seconds: 3600 });
        renderInto(box(), healthy());

        assert.equal(box().querySelector('.sitenotice-warnmark'), null);
    });

    test('a quiet runner leaves the line empty, so the box is the note alone', () => {
        renderInto(box(), healthy({ queued_task_count: 0 }));

        assert.equal(window.document.getElementById('runnerstatus').textContent, '');
        assert.equal(note(), 'Standing note about the data.');
    });

    test('an outage makes the whole box the warning panel', () => {
        // The class is on the box, and not on the sentence, so that the colours hold the note.
        renderInto(box(), { stale: true, status_age_seconds: 3600 });

        assert.equal(box().classList.contains('stale'), true);
        assert.match(window.document.getElementById('runnerstatus').textContent, /not currently processing jobs/);
        assert.equal(note(), 'Standing note about the data.');
    });

    test('a runner that came back takes its panel and its line away again', () => {
        renderInto(box(), { stale: true, status_age_seconds: 3600 });
        renderInto(box(), healthy({ queued_task_count: 0 }));

        assert.equal(box().classList.contains('stale'), false);
        assert.equal(window.document.getElementById('runnerstatus').textContent, '');
        assert.equal(note(), 'Standing note about the data.');
    });
});
