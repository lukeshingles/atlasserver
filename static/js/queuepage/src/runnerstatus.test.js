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
    createCache, createStore, describeAge, renderInto, runnerMessage, runnerStatusEqual,
} from './runnerstatus.js';
import { setupDom, teardownDom } from './testing.js';

const SRC = dirname(fileURLToPath(import.meta.url));

/** Let the promises of a fetch complete. This also works with test timers in place. */
async function settle() {
    for (let i = 0; i < 10; i += 1) {
        await Promise.resolve();
    }
}

// Where the box reads the status, in the form that setupDom's meta tag gives, and the one name
// the module keeps its record under.
const STATUS_URL = '/taskrunnerstatus.json';
const CACHE_KEY = 'atlas-runnerstatus';

/**
 * A fetch that answers from `bodies` in turn, and records each URL it was called with.
 *
 * Each entry is `{ body, status }`, as the endpoint answers. Every answer of the endpoint is 200,
 * an outage included, so `status` is only for the tests that hand the store some other code. An
 * empty queue rejects, which is a request that did not reach the endpoint.
 */
function queuedFetch(bodies, fetched) {
    return (url) => {
        fetched.push(url.toString());
        const next = bodies.shift();
        if (next == null) {
            return Promise.reject(new Error('unreachable'));
        }
        // A new object at each poll, as response.json() gives. One shared object would make
        // the comparison in the store invisible, because both results are then that object.
        return Promise.resolve({ ok: next.status !== 503, status: next.status || 200, json: () => Promise.resolve(next.body) });
    };
}

/**
 * The box as sitenotice.html renders it.
 *
 * `note` gives the standing note about the data, and `showqueue` the attribute that marks the
 * queue page. A box with no sentence in it carries sitenotice-noline, as a fresh page load does.
 */
function noticeBoxHtml({ note = null, showqueue = false } = {}) {
    return `
        <div class="sitenotice sitenotice-noline" id="sitenotice"${showqueue ? ' data-showqueue' : ''}>
          ${note == null ? '' : `<p class="sitenotice-note">${note}</p>`}
          <p class="sitenotice-runner" id="runnerstatus" role="status" aria-live="off"><svg class="sitenotice-warnmark" aria-hidden="true" hidden></svg><span class="sitenotice-runnertext"></span></p>
        </div>`;
}

/**
 * Load a copy of the module, as a page load reaches it.
 *
 * A new copy at each call: the module reads the document and subscribes as node evaluates it, and
 * node holds one copy of a module for each URL. The caller calls stopPageStore() on the result.
 */
function loadPageModule(tag) {
    return import(`./runnerstatus.js?${tag}=${Date.now()}`);
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
        global.fetch = queuedFetch(bodies, fetched);
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

    test('an answer that is not 200 is still read for its body', async () => {
        // The endpoint answers 200 for an outage, and the outage is in the body. This store does
        // not take a status code for an answer either way, so a release that still answers 503 is
        // read while it is being replaced.
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

    test('a tab that comes back asks again, even after the reader went idle', async () => {
        // The queue page reports an idle reader as user_is_active false, and it sets that flag
        // from mouse and touch events only. A change of tab is neither. The reader is at the page,
        // and the status they see is the one from before the tab became hidden.
        const window = setupDom();
        let unsubscribe = () => {};
        try {
            window.user_is_active = false;
            bodies.push({ body: healthy() }, { body: healthy({ slots_busy: 9 }) });
            const store = createStore({ url: '/taskrunnerstatus.json', poll: 100000 });
            unsubscribe = store.subscribe(() => {});
            await settle();

            window.document.dispatchEvent(new window.Event('visibilitychange'));
            await settle();

            assert.equal(store.current().slots_busy, 9);
        } finally {
            unsubscribe();
            await teardownDom(window);
        }
    });

    test('an idle reader is not polled on the interval', async () => {
        // the other half: the pause the queue page had before this module owned the poll
        mock.timers.enable({ apis: ['setInterval', 'Date'] });
        const window = setupDom();
        let unsubscribe = () => {};
        try {
            window.user_is_active = false;
            bodies.push({ body: healthy() }, { body: healthy({ slots_busy: 9 }) });
            const store = createStore({ url: '/taskrunnerstatus.json', poll: 1000 });
            unsubscribe = store.subscribe(() => {});
            await settle();
            assert.equal(fetched.length, 1, 'the first request fills the box either way');

            mock.timers.tick(5000);
            await settle();

            assert.equal(fetched.length, 1, 'no interval request while the reader is away');
        } finally {
            unsubscribe();
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

    test('a later request that fails does not discard an earlier answer', async () => {
        // The first request of a page can overlap the request from a return to the tab, or from
        // the interval. A failure gives no status. Thus a failure must not discard a status that
        // arrives after it. On a page that just opened, the box would else stay empty for a
        // minute, with each wait estimate of the queue page.
        let answerFirst;
        const calls = [];
        global.fetch = () => {
            const call = calls.length;
            return new Promise((resolve, reject) => {
                if (call === 0) {
                    answerFirst = () => resolve({ ok: true, status: 200, json: () => Promise.resolve(healthy({ slots_busy: 4 })) });
                } else {
                    reject(new Error('unreachable'));
                }
                calls.push(call);
            });
        };

        const store = createStore({ url: '/taskrunnerstatus.json', poll: 100000 });
        const unsubscribe = store.subscribe(() => {});
        try {
            store.refresh();
            await settle();
            answerFirst();
            await settle();

            assert.notEqual(store.current(), null, 'the answer that arrived is the one to keep');
            assert.equal(store.current().slots_busy, 4);
        } finally {
            unsubscribe();
        }
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
 * The status that one page leaves for the next one.
 *
 * The box is on every page, and each page load used to start with nothing and show nothing until
 * its first response arrived. These tests cover the record itself, and then the property that the
 * record is for: a page that starts from a kept status draws the sentence at once, and the
 * response that follows a moment later changes nothing.
 *
 * As with the store above, these tests use a storage of their own rather than a document. The
 * page that reaches sessionStorage itself is tested with the rest of the page load, below.
 */
describe('the status kept for the next page', () => {
    /**
     * A storage of the shape that the cache uses, holding its items in an object.
     *
     * `refuse` gives the storage of a browser that is set to refuse site data, which throws from
     * each of these rather than giving null.
     */
    function fakeStorage(items = {}, { refuse = false } = {}) {
        const guard = () => { if (refuse) { throw new Error('refused'); } };
        return {
            items,
            getItem: (key) => { guard(); return key in items ? items[key] : null; },
            setItem: (key, value) => { guard(); items[key] = String(value); },
            removeItem: (key) => { guard(); delete items[key]; },
        };
    }

    /**
     * The items of a storage holding a record as a page would have left it `ago` ms ago.
     *
     * `reader` is the reader the page was rendered for, empty for one who is not signed in.
     */
    function kept(status, ago, { url = STATUS_URL, reader = '' } = {}) {
        return { [CACHE_KEY]: JSON.stringify({ url, reader, saved: Date.now() - ago, status }) };
    }

    /** The cache of a page of this endpoint, read by a reader who is not signed in. */
    function cacheFor(storage, { url = STATUS_URL, reader = '' } = {}) {
        return createCache({ storage, url, reader });
    }

    describe('the record', () => {
        test('what one page writes, the next one reads', () => {
            const storage = fakeStorage();
            cacheFor(storage).write(healthy({ slots_busy: 7 }), Date.now());

            assert.equal(cacheFor(storage).read(60000).status.slots_busy, 7);
        });

        test('the age of the status file grows while no page is open', () => {
            // The outage sentence gives this age. A page that reported it as it was kept would
            // move the start of the outage forward by the reading time of every page before it.
            const cache = cacheFor(fakeStorage(kept({ stale: true, status_age_seconds: 3540 }, 90000)));

            assert.match(runnerMessage(cache.read(300000).status), /1 hour ago/);
        });

        test('a status that no poll confirmed for a full interval is not worth showing', () => {
            // For as long as the page that kept it would have shown it, and no longer: an open
            // tab would have replaced this sentence by now.
            assert.equal(cacheFor(fakeStorage(kept(healthy(), 61000))).read(60000), null);
        });

        test('a clock that moved back leaves a record of unknown age, which is no use', () => {
            assert.equal(cacheFor(fakeStorage(kept(healthy(), -5000))).read(60000), null);
        });

        test('a record from another endpoint on this origin is not this page\'s status', () => {
            const storage = fakeStorage(kept(healthy(), 1000, { url: '/other/taskrunnerstatus.json' }));

            assert.equal(cacheFor(storage).read(60000), null);
        });

        test('a record kept for another reader of this browser is not this reader\'s status', () => {
            // A tab keeps its storage across a sign-out and a sign-in. The status holds a count
            // of the reader's own queued tasks, which decides whether they are shown the queue
            // counts, so the record of one reader must not reach the page of another.
            const signedin = fakeStorage(kept(healthy(), 1000, { reader: '7' }));
            assert.equal(cacheFor(signedin).read(60000), null, 'signed out, and the record is not theirs');
            assert.equal(cacheFor(signedin, { reader: '8' }).read(60000), null, 'nor another reader\'s');
            assert.equal(cacheFor(signedin, { reader: '7' }).read(60000).status.slots_busy, 2);

            // A page that names no reader is a page for the reader who is not signed in.
            const signedout = fakeStorage(kept(healthy(), 1000));
            assert.equal(cacheFor(signedout, { reader: '7' }).read(60000), null);
        });

        test('a value that this file did not write is ignored rather than thrown over', () => {
            for (const item of ['', 'not json', 'null', '"a string"', '{"saved":1}',
                '{"saved":"soon","status":{}}']) {
                assert.equal(cacheFor(fakeStorage({ [CACHE_KEY]: item })).read(60000), null, item);
            }
        });

        test('a storage that refuses to hold anything is not a failure of the page', () => {
            // The page then does what it did before this cache existed, which is to start empty.
            const debug = console.debug;
            console.debug = () => {};
            try {
                const cache = cacheFor(fakeStorage({}, { refuse: true }));
                cache.write(healthy(), Date.now());
                assert.equal(cache.read(60000), null);
                cache.clear();
            } finally {
                console.debug = debug;
            }
        });

        test('there is nothing to read where there is no storage at all', () => {
            // node, and a browser with no sessionStorage. write() and clear() must also pass.
            const cache = cacheFor(null);
            cache.write(healthy(), Date.now());
            cache.clear();
            assert.equal(cache.read(60000), null);
        });
    });

    describe('the store that starts from it', () => {
        let bodies;

        beforeEach(() => {
            bodies = [];
            global.fetch = queuedFetch(bodies, []);
        });

        afterEach(() => {
            delete global.fetch;
            mock.timers.reset();
        });

        const storeWith = (storage, poll = 60000) => createStore({ url: STATUS_URL, poll, storage });

        test('the first reader is told the kept status before any response', () => {
            // The point of the whole thing. The box is filled at the first paint of the page,
            // rather than one request later. No response is staged: there is nothing to wait for.
            const store = storeWith(fakeStorage(kept(healthy({ slots_busy: 7 }), 5000)));

            const seen = [];
            const unsubscribe = store.subscribe((status) => seen.push(status));

            assert.equal(seen.length, 1);
            assert.equal(seen[0].slots_busy, 7);
            unsubscribe();
        });

        test('the response that follows redraws nothing when it says the same thing', async () => {
            // Without this, the box would be drawn twice at each page load: once from the kept
            // status and once from the response. The queue page would render twice as well.
            bodies.push({ body: healthy({ written: '2026-01-01T00:01:00Z', status_age_seconds: 8 }) });
            const store = storeWith(fakeStorage(kept(healthy(), 5000)));

            const seen = [];
            const unsubscribe = store.subscribe((status) => seen.push(status));
            await settle();

            assert.equal(seen.length, 1, 'the kept status only');
            unsubscribe();
        });

        test('a response that says something else replaces the kept status', async () => {
            bodies.push({ body: healthy({ slots_busy: 11 }) });
            const store = storeWith(fakeStorage(kept(healthy({ slots_busy: 7 }), 5000)));

            const seen = [];
            const unsubscribe = store.subscribe((status) => seen.push(status));
            await settle();

            assert.deepEqual(seen.map((status) => status.slots_busy), [7, 11]);
            unsubscribe();
        });

        test('each confirming poll keeps the record, and not each change of it', async () => {
            // A reader who spends ten minutes on one page leaves a status that is ten minutes
            // old, and the page after it would start empty. What is kept is the time of the
            // answer, so an unchanged answer is worth keeping.
            mock.timers.enable({ apis: ['setInterval', 'Date'] });
            const storage = fakeStorage();
            bodies.push({ body: healthy() }, { body: healthy({ written: 'later' }) });
            const store = storeWith(storage, 1000);
            const unsubscribe = store.subscribe(() => {});
            await settle();

            mock.timers.tick(1000);
            await settle();

            const record = JSON.parse(storage.items[CACHE_KEY]);
            assert.equal(record.saved, Date.now(), 'the second poll changed nothing and still counts');
            unsubscribe();
        });

        test('a status the store withdraws is not left for the next page', async () => {
            // The store stops to show a status that several polls did not confirm. The page
            // after it must not bring that status back.
            mock.timers.enable({ apis: ['setInterval', 'Date'] });
            const storage = fakeStorage();
            bodies.push({ body: healthy() });
            const store = storeWith(storage, 1000);
            const unsubscribe = store.subscribe(() => {});
            await settle();
            assert.ok(CACHE_KEY in storage.items);

            for (let i = 0; i < 6; i += 1) {
                mock.timers.tick(1000);
                await settle();
            }

            assert.equal(store.current(), null);
            assert.deepEqual(storage.items, {});
            unsubscribe();
        });

        test('a kept status is withdrawn on the schedule of the answer that gave it', async () => {
            // The record carries the time of that answer, and the store counts from it. Thus a
            // kept status that no poll of this page confirms goes at the time it would have gone
            // on the page that kept it, and not five polls after this page opened.
            mock.timers.enable({ apis: ['setInterval', 'Date'] });
            const storage = fakeStorage(kept(healthy(), 800));
            const store = storeWith(storage, 1000);
            const seen = [];
            // Every request of this store fails, because bodies is empty.
            const unsubscribe = store.subscribe((status) => seen.push(status));
            await settle();
            assert.equal(seen[0].slots_busy, 2, 'the kept status, with no response to confirm it');

            for (let i = 0; i < 4; i += 1) {
                mock.timers.tick(1000);
                await settle();
            }
            assert.equal(store.current().slots_busy, 2, 'our own failures are not news of the runner');

            mock.timers.tick(1000);
            await settle();
            assert.equal(store.current(), null);
            assert.deepEqual(storage.items, {});
            unsubscribe();
        });
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
        let module = null;
        try {
            window.document.body.innerHTML = noticeBoxHtml({ note: 'Standing note.', showqueue: true });
            global.fetch = () => Promise.resolve({
                ok: true, status: 200, json: () => Promise.resolve(healthy({ slots_busy: 5 })),
            });

            module = await loadPageModule('bootstrap');
            await settle();

            assert.match(window.document.getElementById('runnerstatus').textContent, /5 of 16 slots busy/);
        } finally {
            module?.stopPageStore();
            delete global.fetch;
            await teardownDom(window);
        }
    });

    test('it fills the box from the status the page before left, with no response at all', async () => {
        // The property of the kept status, through sessionStorage and the box, as a page load
        // reaches it. The tests above make the same point against a storage of their own.
        const window = setupDom();
        let module = null;
        try {
            // No reader: setupDom renders no atlas-reader tag, as a page for a reader who is not
            // signed in does not.
            window.sessionStorage.setItem(CACHE_KEY, JSON.stringify({
                url: STATUS_URL, reader: '', saved: Date.now() - 2000, status: healthy({ slots_busy: 7 }),
            }));
            window.document.body.innerHTML = noticeBoxHtml({ showqueue: true });
            // A request that never answers: what the box shows here it shows from the record.
            global.fetch = () => new Promise(() => {});

            module = await loadPageModule('kept');

            assert.match(window.document.getElementById('runnerstatus').textContent, /7 of 16 slots busy/);
            assert.equal(window.document.getElementById('sitenotice').classList.contains('sitenotice-noline'), false);
        } finally {
            // In the finally, and not after the assertions: a failure here would otherwise leave
            // the poll of this copy of the module running for the remainder of the test run.
            module?.stopPageStore();
            delete global.fetch;
            await teardownDom(window);
        }
    });
});

describe('the box', () => {
    let window;

    beforeEach(() => {
        window = setupDom();
        window.document.body.innerHTML = noticeBoxHtml({ note: 'Standing note about the data.' });
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

    test('the queue line is drawn only when the counts are of use to this reader', () => {
        const line = () => window.document.getElementById('runnerstatus').textContent;

        renderInto(box(), healthy());
        assert.equal(line(), '', 'no attribute, and this reader has no task in the queue');

        box().setAttribute('data-showqueue', '');
        renderInto(box(), healthy({ slots_busy: 4 }));
        assert.match(line(), /4 of 16 slots busy/);
    });

    test('the response tells whether this reader waits on the queue', () => {
        // The attribute marks the queue page alone. On each other page the answer comes with the
        // status, and thus it follows the queue within one poll.
        const line = () => window.document.getElementById('runnerstatus').textContent;

        renderInto(box(), healthy({ user_queued_task_count: 2 }));
        assert.match(line(), /2 of 16 slots busy/, 'a reader with queued tasks gets the counts');

        renderInto(box(), healthy({ user_queued_task_count: 0, written: 'later' }));
        assert.equal(line(), '', 'a reader whose tasks all completed stops to get them');
    });

    test('an outage carries a mark as well as a colour', () => {
        renderInto(box(), { stale: true, status_age_seconds: 3600 });

        const mark = box().querySelector('.sitenotice-warnmark');
        // The attribute, and not the property: the mark is an SVG element, and only HTML elements
        // have the `hidden` property. A test of the property passes on an expando value.
        assert.equal(mark.hasAttribute('hidden'), false, 'the colours must not be the only sign of an outage');
        assert.equal(mark.getAttribute('aria-hidden'), 'true', 'the sentence next to it already says this');
        // the sentence is the same sentence, with the mark or without it
        assert.match(window.document.getElementById('runnerstatus').textContent, /not currently processing jobs/);
    });

    test('an unchanged sentence is not written again', () => {
        // The sentence is in a live region, and a second write makes a screen reader speak it
        // again. In an outage the age changes at each poll, and the words stay the same for an
        // hour.
        renderInto(box(), { stale: true, status_age_seconds: 7200 });
        const text = window.document.querySelector('.sitenotice-runnertext');
        const node = text.firstChild;

        renderInto(box(), { stale: true, status_age_seconds: 7260 });

        assert.equal(text.firstChild, node, 'the same text node is there, untouched');
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

        assert.equal(box().querySelector('.sitenotice-warnmark').hasAttribute('hidden'), true);
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
