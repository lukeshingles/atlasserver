'use strict';

// How long a queued task is likely to wait, and how to say it.
//
// Kept free of React and of browser globals so that it can be tested directly with `node --test`
// (the same reasoning as agetext.js and pollcache.js). Nothing here touches the DOM: seconds and
// counts in, a number or a string out.
//
// The obvious formula -- tasks ahead, divided by the number of slots, times how long a task takes
// -- is wrong here, and wrong in the direction that matters. The runner dispatches with
// `.exclude(user_id__in=<users already running>)`, so it runs at most one task per user at a time,
// and forcephot/queue.py numbers the queue in round-robin passes for the same reason: its
// `passnum` is exactly the number of passes a task waits through, before being flattened into the
// queue position this file gets. A radeclist submission creates up to 100 tasks at once, so for
// the user who most needs an estimate almost everything ahead of their last task is their own --
// and those run strictly one after another, no matter how many slots are free. Dividing by the
// slot count would promise minutes and deliver hours.
//
// So the two kinds of task ahead are counted separately: the user's own, which are serialised, and
// everyone else's, which drain in parallel at min(slots, users sharing the queue). The wait is
// whichever of those two takes longer, since they are happening at the same time.
//
// If the dispatch policy in taskrunner/main.py ever changes -- a second concurrent task per user,
// a priority tier -- this is the copy of it that has to change with it.

/**
 * Seconds a task at this queue position is likely to wait, or null if that cannot be said.
 *
 * `ownqueued` is the caller's other queued tasks as {position, requesttype} pairs, or null while
 * that is not yet known — the two are different answers and an empty array only means the first.
 */
export function estimateWaitSeconds({ queuepos, ownqueued, runnerstatus }) {
    // A queue that is not being dispatched from has no wait to report. `maintenance` means the
    // hourly sweep is blocking the runner's loop, so nothing starts for its duration and the slot
    // figures are frozen -- the banner already says so, and a countdown beside it would be against
    // a queue that is not moving.
    if (queuepos == null || runnerstatus == null || runnerstatus.stale || runnerstatus.maintenance) {
        return null;
    }

    // Null until the queue positions endpoint has answered. Treating that as "no other tasks of
    // mine" is the one error this whole module exists to avoid: it collapses a bulk submitter's
    // serialised queue into the divide-by-slots form and under-promises by up to that factor.
    if (ownqueued == null) {
        return null;
    }

    // Nothing here reads the run time of the task being estimated. What it is waiting for is the
    // work ahead of it, so its own type says nothing about when it starts -- only about when it
    // then finishes, which is not what is being reported.
    const runtimes = runnerstatus.typical_runtime_seconds;

    // How many tasks run at once: the slot count, but never more than the number of users sharing
    // the queue, because dispatch takes at most one task per user at a time.
    //
    // Absent is refused, zero is not. A runner predating this field writes a fresh status file
    // without it, so `stale` is false and the absence is silent -- an estimate built on a guessed
    // concurrency would be off by up to the slot count. A present zero is different: the status
    // file is written every 15s and read every 60, so it can still describe an empty queue when
    // this task's own position proves otherwise. One is the floor that case needs, and it is the
    // right answer anyway, since a queue with anything in it has at least one user.
    const numslots = runnerstatus.numslots;
    const queuedusers = runnerstatus.distinct_queued_users;
    // A runner with no slots runs nothing at all, so there is no rate to divide by and no honest
    // finite answer; a queued-user count of zero is only ever the lag described above.
    if (!(numslots > 0) || queuedusers == null) {
        return null;
    }
    const concurrency = Math.max(1, Math.min(numslots, queuedusers));

    // Strictly ahead of this one. The endpoint reports the user's whole queued set, not the page on
    // screen, which is what makes this their queue rather than the rows that happen to be rendered.
    const ownahead = ownqueued.filter((task) => task.position < queuepos);
    const otherahead = queuepos - ownahead.length;

    // The user's own tasks ahead run one at a time, so their run times add up -- each priced by
    // what it actually is. A radeclist submission is one type throughout, but a user who asked for
    // images and then submitted a light curve waits through the image request, and a quarter of an
    // hour of that is not a minute of it.
    let ownseconds = 0;
    for (const task of ownahead) {
        const median = runtimes?.[task.requesttype];
        if (!(median > 0)) {
            // one of this user's own tasks ahead cannot be priced, and it is directly in the way
            return null;
        }
        ownseconds += median;
    }

    // Everybody else's drain in whole passes, not fractions of one. With fifteen other users'
    // tasks ahead and sixteen ways of running them, all fifteen start in the same pass as this
    // one, so it waits for none of them -- a fraction here would report most of a run time for a
    // task about to start.
    //
    // A task in the first pass therefore gets zero, which reads as "under a minute". That is
    // optimistic when every slot is occupied, since it then waits for one to come free; the
    // alternative of withholding whenever slots_busy equals numslots is worse in practice, because
    // slots_busy moves on its own and the figure would appear and vanish from one poll to the next
    // across the last `concurrency` positions -- the stretch a waiting user watches hardest.
    const otherpasses = Math.floor(otherahead / concurrency);
    let otherseconds = 0;
    if (otherpasses > 0) {
        // Priced by what the queue is made of, not by this task's own type. Dispatch orders one
        // global queue across every request type, so the work ahead is whatever was submitted --
        // and an FP task behind a run of IMGZIP tasks waits on their run times, which are longer
        // by orders of magnitude.
        const meanpass = meanQueuedRuntimeSeconds(runnerstatus.queued_by_request_type, runtimes);
        if (meanpass == null) {
            return null;
        }
        otherseconds = otherpasses * meanpass;
    }

    // the two are worked through at the same time, so the wait is the longer of them
    return Math.max(ownseconds, otherseconds);
}

/**
 * How long a dispatch pass takes on average, given what is queued, or null if that cannot be said.
 *
 * Weighted by the number of queued tasks of each type, since a pass is filled from the queue.
 *
 * This is the whole queue rather than the part of it ahead of any particular task, which is the
 * approximation in the estimate: a large batch of one type sitting behind a task still moves the
 * price of the passes in front of it. The composition per position is not something the queue
 * positions endpoint can carry at the rate it is polled, and the figure is rounded to "~N min"
 * regardless, so the error is inside the precision being claimed.
 *
 * A type with no median is left out of the weighting rather than refusing the whole answer. It is
 * only ever a minority of a queue -- a type is unpriced by being rare -- and letting it decide
 * would mean work sitting behind a task could blank the estimate in front of it.
 */
function meanQueuedRuntimeSeconds(queuedbytype, runtimes) {
    if (queuedbytype == null) {
        return null;
    }

    let tasks = 0;
    let seconds = 0;
    for (const [requesttype, count] of Object.entries(queuedbytype)) {
        const median = runtimes?.[requesttype];
        if (!(count > 0) || !(median > 0)) {
            continue;
        }
        tasks += count;
        seconds += count * median;
    }

    return tasks > 0 ? seconds / tasks : null;
}

/**
 * Render an estimate as text, or null when there is nothing worth saying.
 *
 * Coarse on purpose, and always hedged. The input is a median times an integer, so its accuracy is
 * nothing like a second -- and a figure that looks precise is one the user plans an afternoon
 * around. Rounded up throughout, because an estimate that passes while the task is still queued
 * reads as a broken promise in a way that one that comes good early does not.
 */
export function formatWaitEstimate(seconds) {
    if (seconds == null || !Number.isFinite(seconds)) {
        return null;
    }

    if (seconds < 60) {
        // under a minute there is nothing useful to count down, and the row already says "next"
        return 'under a minute';
    }

    if (seconds < 90 * 60) {
        return '~' + Math.ceil(seconds / 60) + ' min';
    }

    // beyond a few hours the estimate is well past the point of being actionable, and naming a
    // number invites a precision that a median over a day of tasks cannot support
    if (seconds >= 4 * 60 * 60) {
        return 'over 4 hours';
    }

    return '~' + Math.ceil(seconds / 3600) + ' hours';
}

/** Seconds as a short duration for the finished-row timings: "40s", "2m 10s", "1h 04m". */
export function formatDuration(seconds) {
    if (seconds == null || !Number.isFinite(seconds) || seconds < 0) {
        return null;
    }

    // Rounded to whole seconds once, before being split into units. Rounding each unit separately
    // lets them disagree: the serializer sends one decimal place, and 119.6s taken as floor(1)
    // minutes and round(59.6) seconds reads as "1m 60s".
    const whole = Math.round(seconds);

    if (whole < 60) {
        return whole + 's';
    }

    if (whole < 3600) {
        return Math.floor(whole / 60) + 'm ' + String(whole % 60).padStart(2, '0') + 's';
    }

    return Math.floor(whole / 3600) + 'h ' + String(Math.floor((whole % 3600) / 60)).padStart(2, '0') + 'm';
}
