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

/** Seconds a task at this queue position is likely to wait, or null if that cannot be said. */
export function estimateWaitSeconds({ queuepos, ownqueuepositions, requesttype, runnerstatus }) {
    if (queuepos == null || runnerstatus == null || runnerstatus.stale) {
        return null;
    }

    // per request type: an IMGZIP task retrieves up to a thousand images where an FP task fits one
    // light curve, so the two are not interchangeable. A type the server has not seen enough of
    // recently is absent from this object, and gets no estimate rather than a borrowed one.
    //
    // Negated rather than tested for a positive, so an absent type and a NaN are turned away by
    // the same comparison.
    const typicalruntime = runnerstatus.typical_runtime_seconds?.[requesttype];
    if (!(typicalruntime > 0)) {
        return null;
    }

    // Positions strictly ahead of this one. The endpoint reports the user's whole queued set, not
    // the page on screen, which is what makes this a count of their queue rather than of the rows
    // that happen to be rendered.
    const ownahead = ownqueuepositions.filter((position) => position < queuepos).length;
    const otherahead = queuepos - ownahead;

    // distinct_queued_users can lag the queue by up to one status write, and a queue with work in
    // it always has at least one user, so the floor of 1 keeps a stale zero from dividing by nothing
    const concurrency = Math.max(1, Math.min(runnerstatus.numslots || 1, runnerstatus.distinct_queued_users || 1));

    // the user's own tasks and everybody else's are worked through concurrently, so the wait is the
    // longer of the two, not their sum
    return Math.max(ownahead, otherahead / concurrency) * typicalruntime;
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
    if (seconds == null || !isFinite(seconds)) {
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
    if (seconds == null || !isFinite(seconds) || seconds < 0) {
        return null;
    }

    if (seconds < 60) {
        return Math.round(seconds) + 's';
    }

    if (seconds < 3600) {
        return Math.floor(seconds / 60) + 'm ' + String(Math.round(seconds % 60)).padStart(2, '0') + 's';
    }

    return Math.floor(seconds / 3600) + 'h ' + String(Math.floor((seconds % 3600) / 60)).padStart(2, '0') + 'm';
}
