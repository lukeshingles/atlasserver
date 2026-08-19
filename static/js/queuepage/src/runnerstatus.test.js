'use strict';

// The shared runner status: the wording, the gate that decides what counts as news, the store that
// polls, and the box it draws into. Every page loads this module, so these run without React and,
// for everything but the last group, without a DOM.

import assert from 'node:assert/strict';
import test, { afterEach, beforeEach, describe, mock } from 'node:test';

import {
    createStore, describeAge, renderInto, runnerMessage, runnerStatusEqual,
} from './runnerstatus.js';
import { setupDom, teardownDom } from './testing.js';

/** Let the fetch chain settle. Works with the timers mocked, which a `setTimeout(0)` would not. */
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
        // the boundary that motivated flooring: rounding reported this as "60 minutes ago"
        assert.equal(describeAge(3599), '59 minutes ago');
    });

    await t.test('hours', () => {
        assert.equal(describeAge(3600), '1 hour ago');
        assert.equal(describeAge(7199), '1 hour ago');
        assert.equal(describeAge(7200), '2 hours ago');
        // just under a day is still counted in hours, not rounded up into "24 hours ago"
        assert.equal(describeAge(86399), '23 hours ago');
    });

    await t.test('days', () => {
        assert.equal(describeAge(86400), '1 day ago');
        // the line that prompted the helper: 6401 minutes reads as days
        assert.equal(describeAge(6401 * 60), '4 days ago');
    });
});

/*
 * The gate that decides whether a poll is news.
 *
 * Tested directly rather than through the box: what it decides is whether anything is told, and a
 * status that produces identical wording leaves the box identical too, so no assertion on the page
 * can tell the two apart.
 */
describe('runnerStatusEqual', () => {
    test('a poll that only moved the clock says nothing new', () => {
        // what every healthy poll looks like, and the whole reason the volatile list exists
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
        // the point of listing what to ignore rather than what to read: a new field can cost a
        // rewrite of the box, but it cannot go silently unnoticed by a reader that wants it
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
            // a fresh object per poll, as response.json() gives: a shared reference would make the
            // comparison in the store unobservable, since both branches then yield the same one
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
        // every other field is frozen while the runner is down -- the status file is not being
        // rewritten, and the medians are withheld -- so the age is the only thing that moves. A
        // store that skips it leaves the box reporting its first reading for the whole outage.
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

        // four failed polls: our own connection says nothing about the runner
        for (let i = 0; i < 4; i += 1) {
            mock.timers.tick(1000);
            await settle();
        }
        assert.equal(store.current().slots_busy, 2, 'a status is not withdrawn on our own failures');

        // by the sixth the reading is too old to keep asserting, in either direction
        mock.timers.tick(2000);
        await settle();
        assert.equal(store.current(), null);
        assert.equal(seen.at(-1), null);
        unsubscribe();
    });

    test('a tab that comes back asks again rather than showing what it last heard', async () => {
        // a hidden tab is not polled, so an outage that started or ended while the reader was away
        // is exactly what its line gets wrong. A DOM of its own, because this is the one store
        // test whose subject is an event: the rest need no document and are quicker without one.
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
        // the class is on the box and not on the line, so that the panel holds the note as well
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
