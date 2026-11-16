// The wait estimate arithmetic.
//
// These are the cases that separate the model in estimateWaitSeconds from the obvious
// tasks-ahead-over-slots formula. The first is the one that matters most in practice: a radeclist
// submission creates up to 100 tasks at once, the runner runs at most one task per user at a time,
// and so those tasks are worked through one after another however many slots are free.
//
// A plain import, with no DOM and no React: that is what keeping these functions out of the
// component module buys (the same reasoning as pollcache.test.js).
import assert from 'node:assert/strict';
import test, { describe } from 'node:test';

import { estimateWaitSeconds, formatDuration, formatWaitEstimate } from './waitestimate.js';

const status = (overrides = {}) => ({
    stale: false,
    maintenance: false,
    numslots: 16,
    slots_busy: 0,
    distinct_queued_users: 1,
    typical_runtime_seconds: { FP: 60, IMGZIP: 900 },
    // an all-FP queue unless a test says otherwise, which is what this server's queue mostly is
    queued_by_request_type: { FP: 500 },
    ...overrides,
});

/** A bulk submission: every task ahead of this one belongs to the same user, and is the same type. */
const ownqueue = (count, requesttype = 'FP') =>
    Array.from({ length: count }, (unused, index) => ({ position: index, requesttype }));

/** The estimate for a task at `queuepos` that is the only one of the user's in the queue. */
const soleTaskAt = (queuepos, statusoverrides = {}, requesttype = 'FP') => estimateWaitSeconds({
    queuepos,
    ownqueued: [{ position: queuepos, requesttype }],
    runnerstatus: status(statusoverrides),
});

describe('estimateWaitSeconds with several slots for one user', () => {
    // the task runner writes the maximum number of slots of one user to its status
    const sharing = (overrides = {}) => status({ numslots: 24, max_user_slots: 8, ...overrides });

    test('the own tasks ahead use the slots of one user', () => {
        // 100 own tasks ahead and 8 slots for one user: 12 full passes of 8 start before this
        // task. One slot gives 100 passes, and all 24 slots give 4.
        const seconds = estimateWaitSeconds({
            queuepos: 100,
            ownqueued: ownqueue(101),
            runnerstatus: sharing(),
        });

        assert.equal(seconds, 12 * 60);
    });

    test('fewer own tasks ahead than the slots of one user are no wait', () => {
        const seconds = estimateWaitSeconds({ queuepos: 7, ownqueued: ownqueue(8), runnerstatus: sharing() });

        assert.equal(seconds, 0);
    });

    test('the own tasks ahead have the mean price of their types', () => {
        // 16 own tasks ahead, 8 FP and 8 IMGZIP: two passes at the mean of 60 and 900 seconds
        const own = [...ownqueue(8, 'IMGZIP'), ...ownqueue(9).map((task) => ({ ...task, position: task.position + 8 }))];
        const seconds = estimateWaitSeconds({ queuepos: 16, ownqueued: own, runnerstatus: sharing() });

        assert.equal(seconds, 2 * (60 + 900) / 2);
    });

    test('the tasks of the other users use all the slots', () => {
        // 100 tasks of other users ahead, and 5 users can fill the 24 slots: 4 full passes
        assert.equal(soleTaskAt(100, { numslots: 24, max_user_slots: 8, distinct_queued_users: 5 }), 4 * 60);
    });

    test('two users cannot fill more than their two maximums', () => {
        // 2 users with 8 slots each use 16 of the 24 slots: 100 tasks ahead are 6 full passes
        assert.equal(soleTaskAt(100, { numslots: 24, max_user_slots: 8, distinct_queued_users: 2 }), 6 * 60);
    });

    test('the slots of one user are not more than the slots of the task runner', () => {
        const seconds = estimateWaitSeconds({
            queuepos: 100,
            ownqueued: ownqueue(101),
            runnerstatus: sharing({ numslots: 4 }),
        });

        assert.equal(seconds, 25 * 60);
    });

    test('the wait is the longer of the own tasks and the tasks of the other users', () => {
        // 16 own tasks ahead are 2 passes. 200 tasks of other users ahead are 8 passes of 24.
        const own = ownqueue(17).map((task) => ({ ...task, position: task.position + 200 }));
        const seconds = estimateWaitSeconds({
            queuepos: 216,
            ownqueued: own,
            runnerstatus: sharing({ distinct_queued_users: 30 }),
        });

        assert.equal(seconds, 8 * 60);
    });

    test('the tasks of the user that have started use slots of the user', () => {
        // 8 tasks of the user are in the 8 slots of one user, and the tasks of other users wait.
        // The task at the front waits for one of the 8 to finish: one pass at the price of the queue.
        const waiting = { queuepos: 0, ownqueued: ownqueue(1), runnerstatus: sharing({ distinct_queued_users: 5 }) };

        assert.equal(estimateWaitSeconds({ ...waiting, ownstarted: 8 }), 60);
        assert.equal(estimateWaitSeconds({ ...waiting, ownstarted: 7 }), 0);
        assert.equal(estimateWaitSeconds(waiting), 0);
    });

    test('the started tasks and the own tasks ahead fill the passes together', () => {
        // 5 tasks in slots and 11 own tasks ahead are 2 full passes of 8
        const seconds = estimateWaitSeconds({
            queuepos: 11,
            ownqueued: ownqueue(12),
            ownstarted: 5,
            runnerstatus: sharing(),
        });

        assert.equal(seconds, 2 * 60);
    });

    test('there is no estimate when a pass of started tasks has no price', () => {
        const seconds = estimateWaitSeconds({
            queuepos: 0,
            ownqueued: ownqueue(1),
            ownstarted: 8,
            runnerstatus: sharing({ queued_by_request_type: undefined }),
        });

        assert.equal(seconds, null);
    });

    test('a status with no maximum gives one slot to each user', () => {
        // a task runner that does not write the field runs one task of each user at a time
        const seconds = estimateWaitSeconds({ queuepos: 10, ownqueued: ownqueue(11), runnerstatus: status() });

        assert.equal(seconds, 10 * 60);
    });
});

describe('estimateWaitSeconds', () => {
    test('a bulk submission is serialised, not divided by the free slots', () => {
        // 40 of the user's own tasks ahead, every slot free. They run one at a time, so the wait is
        // 40 run times -- not the 40/16 = 2.5 that dividing by the slot count would promise.
        const seconds = estimateWaitSeconds({
            queuepos: 40,
            ownqueued: ownqueue(41),
            runnerstatus: status({ distinct_queued_users: 1 }),
        });

        assert.equal(seconds, 40 * 60);
    });

    test('other users\' tasks ahead drain in whole passes', () => {
        // 40 tasks ahead, none of them this user's, 20 users sharing 16 ways of running them: two
        // full passes clear 32 of them and this task goes in the third alongside the rest
        assert.equal(soleTaskAt(40, { distinct_queued_users: 20 }), 2 * 60);
    });

    test('a task in the first pass waits for nobody ahead of it', () => {
        // 15 other users' tasks ahead and 16 ways of running them: all 16 start together, so this
        // one is not behind any of them. A fraction of a pass here would report most of a run time
        // for a task about to start.
        assert.equal(soleTaskAt(15, { distinct_queued_users: 20 }), 0);
    });

    test('the first pass is still estimated when every slot is busy', () => {
        // optimistic, since the task then waits for a slot to free -- but withholding on a full
        // queue would make the figure appear and vanish between polls as slots_busy moves, across
        // the last few positions, which is the stretch a waiting user watches hardest
        assert.equal(soleTaskAt(15, { distinct_queued_users: 20, slots_busy: 16 }), 0);
    });

    test('concurrency is bounded by the number of users, not the slot count', () => {
        // 16 slots but only 2 users with work: at most one task per user runs, so two at a time
        assert.equal(soleTaskAt(8, { distinct_queued_users: 2 }), (8 / 2) * 60);
    });

    test('the wait is the longer of the two, since they happen at once', () => {
        // 4 of the user's own ahead (4 x 60 = 240s serialised) and 32 others at 16 at a time
        // (2 x 60 = 120s). The user's own queue is the binding constraint.
        const seconds = estimateWaitSeconds({
            queuepos: 36,
            ownqueued: [0, 1, 2, 3].map((position) => ({ position, requesttype: 'FP' }))
                .concat([{ position: 36, requesttype: 'FP' }]),
            runnerstatus: status({ distinct_queued_users: 20 }),
        });

        assert.equal(seconds, 4 * 60);
    });

    test('the next task waits for nothing', () => {
        assert.equal(soleTaskAt(0), 0);
    });

    test('the user\'s own tasks ahead are priced by their own type', () => {
        // a radeclist submission is one request type, so the tasks this one is serialised behind
        // are IMGZIP too
        const seconds = estimateWaitSeconds({
            queuepos: 3,
            ownqueued: ownqueue(4, 'IMGZIP'),
            runnerstatus: status({ distinct_queued_users: 1 }),
        });

        assert.equal(seconds, 3 * 900);
    });

    test('other users\' tasks ahead are priced by what is actually queued', () => {
        // 32 tasks ahead, none of them this user's, and the queue is all IMGZIP. Dispatch orders
        // one global queue, so this FP task waits on their 900s run times -- pricing the passes by
        // its own 60s median would promise two minutes for most of an hour's wait.
        const seconds = soleTaskAt(32, {
            distinct_queued_users: 20,
            queued_by_request_type: { IMGZIP: 32 },
        });

        assert.equal(seconds, 2 * 900);
    });

    test('a mixed queue is priced by the weighted mean of what is in it', () => {
        // three quarters FP at 60s and one quarter IMGZIP at 900s: (3 x 60 + 900) / 4 = 270
        const seconds = soleTaskAt(32, {
            distinct_queued_users: 20,
            queued_by_request_type: { FP: 300, IMGZIP: 100 },
        });

        assert.equal(seconds, 2 * 270);
    });

    test('a queued type with no runtime is left out of the weighting', () => {
        // a type is unpriced by being rare, so it is only ever a minority of the queue -- and
        // letting it decide would mean work sitting behind a task could blank the estimate in
        // front of it
        assert.equal(soleTaskAt(32, {
            distinct_queued_users: 20,
            queued_by_request_type: { FP: 300, SSOSTACK: 4 },
        }), 2 * 60);
    });

    test('a runner too old to report what is queued offers no estimate', () => {
        assert.equal(soleTaskAt(32, {
            distinct_queued_users: 20,
            queued_by_request_type: undefined,
        }), null);
    });

    test('the composition is not needed while nothing is ahead but the user\'s own tasks', () => {
        // no full pass of other users' work to price, so an unpriceable queue cannot matter
        const seconds = estimateWaitSeconds({
            queuepos: 3,
            ownqueued: ownqueue(4),
            runnerstatus: status({ distinct_queued_users: 1, queued_by_request_type: undefined }),
        });

        assert.equal(seconds, 3 * 60);
    });

    test('nothing is estimated when the runner is stale', () => {
        assert.equal(soleTaskAt(5, { stale: true }), null);
    });

    test('nothing is estimated during the maintenance sweep', () => {
        // the sweep blocks the runner's dispatch loop, so nothing starts for its duration and the
        // slot figures it reports are frozen
        assert.equal(soleTaskAt(5, { maintenance: true }), null);
    });

    test('nothing is estimated before the first status response', () => {
        assert.equal(estimateWaitSeconds({
            queuepos: 5, ownqueued: [{ position: 5, requesttype: 'FP' }], runnerstatus: null,
        }), null);
    });

    test('nothing is estimated before the queue positions are known', () => {
        // null, not [] -- an empty array means the user has nothing else queued, and treating "not
        // asked yet" as that collapses a bulk submitter's estimate to the divide-by-slots form
        assert.equal(estimateWaitSeconds({
            queuepos: 40, ownqueued: null, runnerstatus: status(),
        }), null);
    });

    test('the waiting task\'s own type does not affect when it starts', () => {
        // its own run time says when it finishes, not when it begins -- so a type the server has
        // too few samples of is no reason to withhold the wait
        assert.equal(soleTaskAt(5, {}, 'SSOSTACK'), 5 * 60);
    });

    test('the user\'s own mixed types ahead are each priced as themselves', () => {
        // one task per user at a time, so an image request submitted first is waited through in
        // full before the light curve behind it starts
        const seconds = estimateWaitSeconds({
            queuepos: 2,
            ownqueued: [
                { position: 0, requesttype: 'IMGZIP' },
                { position: 1, requesttype: 'FP' },
                { position: 2, requesttype: 'FP' },
            ],
            runnerstatus: status({ distinct_queued_users: 1 }),
        });

        assert.equal(seconds, 900 + 60);
    });

    test('an own task ahead that cannot be priced withholds the estimate', () => {
        // unlike a rare type elsewhere in the queue, this one is directly in the way
        const seconds = estimateWaitSeconds({
            queuepos: 1,
            ownqueued: [{ position: 0, requesttype: 'SSOSTACK' }, { position: 1, requesttype: 'FP' }],
            runnerstatus: status({ distinct_queued_users: 1 }),
        });

        assert.equal(seconds, null);
    });

    test('nothing is estimated when the runner reported no runtimes at all', () => {
        assert.equal(soleTaskAt(5, { typical_runtime_seconds: {} }), null);
    });

    test('nothing is estimated for a task with no queue position yet', () => {
        assert.equal(estimateWaitSeconds({
            queuepos: null, ownqueued: [], runnerstatus: status(),
        }), null);
    });

    test('a runner too old to report its queued users offers no estimate', () => {
        // the runner is a separate long-lived process, so it can still be writing a fresh status
        // file without this field. Defaulting it to 1 would multiply every estimate by up to the
        // slot count, which is the invented estimate this module refuses.
        const seconds = estimateWaitSeconds({
            queuepos: 40,
            ownqueued: [{ position: 40, requesttype: 'FP' }],
            runnerstatus: { stale: false, numslots: 16, slots_busy: 0, typical_runtime_seconds: { FP: 60 } },
        });

        assert.equal(seconds, null);
    });

    test('a runner reporting no slots offers no estimate', () => {
        // nothing runs at all, so there is no rate to divide by -- unlike a zero user count, which
        // is only ever a lagging snapshot
        assert.equal(soleTaskAt(4, { numslots: 0 }), null);
    });

    test('a queued-user count of zero is a lagging snapshot, not a missing field', () => {
        // the status file is written every 15s and read every 60, so it can still describe the
        // empty queue this task was submitted into. Refusing here would blank every row of a bulk
        // submission for over a minute -- the case the module exists for.
        assert.equal(soleTaskAt(4, { distinct_queued_users: 0 }), 4 * 60);
    });
});

describe('formatWaitEstimate', () => {
    test('estimates are hedged and coarse, never a bare number of seconds', () => {
        assert.equal(formatWaitEstimate(null), null);
        assert.equal(formatWaitEstimate(30), 'under a minute');
        assert.equal(formatWaitEstimate(240), '~4 min');
        assert.equal(formatWaitEstimate(2 * 3600), '~2 hours');
        assert.equal(formatWaitEstimate(5 * 3600), 'over 4 hours');
    });

    test('every band rounds up', () => {
        // an estimate that expires while the task is still queued reads as a broken promise in a
        // way that one that comes good early does not, so no band is allowed to round down
        assert.equal(formatWaitEstimate(241), '~5 min');
        assert.equal(formatWaitEstimate(91 * 60), '~2 hours');
        assert.equal(formatWaitEstimate(2 * 3600 + 1), '~3 hours');
    });

    test('the ladder is monotone across every boundary', () => {
        // one second must never move the estimate downwards: this string is recomputed as the
        // queue moves, and a figure that grows as the wait shrinks reads as broken
        let previous = -1;
        for (let seconds = 0; seconds <= 5 * 3600; seconds += 7) {
            const estimate = estimateOrder(formatWaitEstimate(seconds));
            assert.ok(estimate >= previous,
                `${seconds}s reads as a shorter wait than the second before it`);
            previous = estimate;
        }
    });
});

/** Rank the possible outputs so the monotonicity check above can compare two of them. */
function estimateOrder(text) {
    if (text === 'under a minute') {
        return 0;
    }
    if (text === 'over 4 hours') {
        return Number.MAX_SAFE_INTEGER;
    }
    const value = parseFloat(text.replace('~', ''));
    return text.includes('hour') ? value * 60 : value;
}

describe('formatDuration', () => {
    test('durations read as durations', () => {
        assert.equal(formatDuration(null), null);
        assert.equal(formatDuration(40), '40s');
        assert.equal(formatDuration(130), '2m 10s');
        assert.equal(formatDuration(3860), '1h 04m');
    });

    test('a fraction of a second cannot carry a unit to 60', () => {
        // the serializer sends one decimal place, so these arrive in practice. Rounding the minutes
        // and the seconds separately lets them disagree: 119.6 reads as "1m 60s".
        assert.equal(formatDuration(59.6), '1m 00s');
        assert.equal(formatDuration(119.6), '2m 00s');
        assert.equal(formatDuration(3599.7), '1h 00m');
    });

    test('a negative duration is declined rather than rendered', () => {
        assert.equal(formatDuration(-0.3), null);
    });
});
