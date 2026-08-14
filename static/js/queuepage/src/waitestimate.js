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
 * `ownqueuepositions` is the positions of the caller's other queued tasks, or null while that is
 * not yet known — the two are different answers and an empty array only means the first.
 */
export function estimateWaitSeconds({ queuepos, ownqueuepositions, requesttype, runnerstatus }) {
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
    if (ownqueuepositions == null) {
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

    // How many tasks run at once: the slot count, but never more than the number of users sharing
    // the queue, because dispatch takes at most one task per user at a time.
    //
    // Both are required rather than defaulted. A runner that has not been restarted since this
    // field was added still writes a fresh status file without it, so `stale` is false and the
    // absence is silent -- and defaulting it to 1 would multiply every estimate by up to the slot
    // count, which is the "invented estimate" case this module refuses.
    const numslots = runnerstatus.numslots;
    const queuedusers = runnerstatus.distinct_queued_users;
    if (!(numslots > 0) || !(queuedusers > 0)) {
        return null;
    }
    const concurrency = Math.min(numslots, queuedusers);

    // Positions strictly ahead of this one. The endpoint reports the user's whole queued set, not
    // the page on screen, which is what makes this a count of their queue rather than of the rows
    // that happen to be rendered.
    const ownahead = ownqueuepositions.filter((position) => position < queuepos).length;
    const otherahead = queuepos - ownahead;

    // Whole dispatch passes, not a fraction of one. With fifteen other users' tasks ahead and
    // sixteen ways of running them, all fifteen start in the same pass as this one, so it waits
    // for none of them -- a fraction here would report most of a run time for a task about to
    // start. The user's own tasks are serialised one per pass, so they count individually.
    const passesahead = Math.max(ownahead, Math.floor(otherahead / concurrency));

    if (passesahead === 0) {
        // Nothing has to finish first, so the only question is whether a slot is free now. If every
        // slot is busy the task waits for one to come free, and nothing reported here says how far
        // through those tasks are -- so say nothing, and let the position chip carry it.
        return runnerstatus.slots_busy < numslots ? 0 : null;
    }

    return passesahead * typicalruntime;
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
