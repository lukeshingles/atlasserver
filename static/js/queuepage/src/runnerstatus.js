'use strict';

// The task runner status, as it reaches every page.
//
// This module does three things:
//
// - it polls /taskrunnerstatus.json;
// - it writes the sentence that gives the status;
// - it puts that sentence into the site notice box.
//
// The queue page reads the same response for its wait estimates. It subscribes to this module, so
// the two readers share one request each minute.
//
// This module imports no other module. A page with no import map can thus load it as
// <script type="module">. This is how the status gets to a page that runs no other JavaScript.

export const POLL_MS = 60000;

// Where base.html and the browsable API template put the URL of the endpoint. A meta tag, and not
// a global value, because {% url %} must supply the script prefix. Only a template knows it.
const URL_META = 'meta[name="atlas-runnerstatus-url"]';

/**
 * Describe an age in seconds. Use the largest unit that gives a small number.
 *
 * The outage sentence gives an age. A reader of "6401 minutes ago" must do the arithmetic to learn
 * whether the outage started a moment ago or some days ago.
 */
export function describeAge(seconds) {
    const units = [['day', 86400], ['hour', 3600], ['minute', 60]];
    for (const [name, size] of units) {
        if (seconds >= size) {
            // Round down, and do not round up. An age is a count of complete units. A value
            // that rounds up crosses the boundary that selected the unit. For example, 3599
            // seconds would give "60 minutes ago" in the place of "59 minutes ago".
            const count = Math.floor(seconds / size);
            return count + ' ' + name + (count == 1 ? '' : 's') + ' ago';
        }
    }
    return 'less than a minute ago';
}

/*
 * Fields of the status that change on their own. No reader uses their values.
 *
 * This is a list of the fields to ignore, and not a list of the fields that are of use. A field
 * that the endpoint gains later is thus compared by default. Such a field can then cost an
 * unnecessary render, which is a small cost. In a list of the fields to compare, a new field would
 * be invisible to every reader, and nothing would fail to show the mistake.
 */
const RUNNERSTATUS_VOLATILE_FIELDS = ['written', 'pid', 'running_taskids', 'status_age_seconds'];

/**
 * Whether two status responses tell every reader the same thing.
 *
 * The store publishes a response only when this function reports a difference. The result thus
 * controls two things: whether the box changes, and whether the queue page renders again. Neither
 * result is visible in the page, so this function is exported and has its own tests.
 */
export function runnerStatusEqual(previous, next) {
    if (previous == null || next == null) {
        return previous === next;
    }

    // The age increases at each poll. To compare it always would report a difference always, and
    // this function would then be of no use. One sentence gives the age: the outage sentence. In
    // an outage, the task runner does not write its status file, and thus every other field holds
    // its value. Compare the age when the status is stale, and ignore it when the status is fresh.
    if ((previous.stale || next.stale) && previous.status_age_seconds !== next.status_age_seconds) {
        return false;
    }

    const fields = new Set([...Object.keys(previous), ...Object.keys(next)]
        .filter(field => !RUNNERSTATUS_VOLATILE_FIELDS.includes(field)));

    for (const field of fields) {
        const before = previous[field];
        const after = next[field];
        if (before === after) {
            continue;
        }
        // typical_runtime_seconds is the one field with a value in it: a few request types, each
        // with a number. A comparison of the two JSON texts is sufficient. The server builds the
        // field from the declared request types in one order, so one set of medians gives one text.
        if (JSON.stringify(before) !== JSON.stringify(after)) {
            return false;
        }
    }

    return true;
}

/**
 * The sentence for a status, or null when the task runner has nothing to report.
 *
 * This function returns a string, and not markup. Thus renderInto below is the one function that
 * changes the page.
 *
 * `showQueue` tells whether the queue counts are of use to this reader. Every reader gets the
 * outage sentence, because an outage changes what to expect of every page. Not every reader gets
 * the queue counts. Those counts answer the question "when does my task start". A reader with no
 * queued task, on a page that is not the queue page, gets no sentence at all.
 */
export function runnerMessage(status, { showQueue = true } = {}) {
    if (status == null) {
        return null;
    }

    if (status.stale) {
        const age = status.status_age_seconds != null
            ? ' It last reported ' + describeAge(status.status_age_seconds) + '.'
            : '';
        return 'The task runner is not currently processing jobs.' + age
            + ' Queued tasks will start once it is back; there is no need to submit them again.';
    }

    if (!status.queued_task_count || !showQueue) {
        // An idle task runner with an empty queue is the usual condition. It needs no sentence.
        return null;
    }

    // During the hourly maintenance operation, the slot counts hold their values. The task runner
    // starts no task and completes no task while that operation runs. Thus the sentence names the
    // operation, and does not give numbers that have no meaning at that time.
    const activity = status.maintenance
        ? 'maintenance sweep in progress;'
        : status.slots_busy + ' of ' + status.numslots + ' slots busy,';

    return 'Task runner: ' + activity + ' ' + status.queued_task_count + ' unfinished '
        + (status.queued_task_count == 1 ? 'task' : 'tasks') + ' from all users in the queue.';
}

/**
 * Whether to skip the poll at this time.
 *
 * Do not poll for a hidden tab. Do not poll for a tab whose reader is away. The queue page has a
 * timer that measures inactivity and gives the result as `user_is_active`. The poll on that page
 * used that value before this module became the owner of the poll. No other page sets the value,
 * and thus no other page stops the poll for it.
 */
function pollingPaused() {
    // Use typeof for both names. The store also runs under `node --test`, which has no document.
    // A direct reference to a name that does not exist gives a ReferenceError.
    if (typeof document === 'undefined') {
        return false;
    }

    return document.hidden || (typeof window !== 'undefined' && window.user_is_active === false);
}

/**
 * A poll of one status URL, and the readers that the poll reports to.
 *
 * This function is exported for the tests. Each test makes its own store, with a test fetch
 * function and a short interval. A page uses the store below, which all of its readers share.
 */
export function createStore({ url, poll = POLL_MS }) {
    let status = null;
    // The time of the last request that reached the endpoint.
    let lastgoodfetch = null;
    let interval = null;
    // A count of the requests. An answer uses it to find whether its question is the newest.
    let generation = 0;
    const listeners = new Set();

    function publish(next) {
        // Each poll parses the response into a new object, and thus each response is a different
        // object. On the queue page this value is the state of TaskPage. To accept every response
        // would render the full page again, with the estimate of each row, to draw one unchanged
        // sentence.
        if (runnerStatusEqual(status, next)) {
            return;
        }

        status = next;
        // One value for the full round, and a copy of the set. A reader can cancel its
        // subscription during its own call. Such a call makes the set shorter, and it can also
        // stop the store. Then stop() sets `status` to null, and the readers after it in the round
        // must not get that null.
        const published = status;
        for (const listener of [...listeners]) {
            tell(listener, published);
        }
    }

    /** Give one status to one reader. Contain a failure of that reader. */
    function tell(listener, published) {
        // An exception from a reader is a fault in that reader. Such an exception would go to the
        // catch of the fetch below, which reads every exception as an endpoint that it cannot
        // reach. The readers after the fault would not get this status. They would not get a later
        // status either, because the store ignores a poll that is equal to the last one. The box
        // is the first reader and the queue page is the second. A fault in the box would thus hold
        // every wait estimate at its value for the life of the page.
        try {
            listener(published);
        } catch (error) {
            console.error('A task runner status reader failed', error);
        }
    }

    /**
     * Ask the endpoint. Report the answer while it is the answer to the most recent question.
     *
     * `fresh` makes the browser ignore its cache. The response is cacheable for one write interval
     * of the task runner. That cache stops several tabs that open together from several requests.
     * But a reader who comes back to a tab must get the status at that moment. Such a reader must
     * not get an answer from before the tab became hidden.
     */
    function refresh({ fresh = false } = {}) {
        // The number of this question. Two requests can overlap, because the interval and a
        // return to the tab can occur in the same second. The two answers can arrive in any order.
        // Thus the store discards the answer to a question that it asked again. Without this
        // number, the store keeps the older value for a full poll. That value also changes the
        // wait estimate of each row on the queue page.
        const asked = (generation += 1);

        // The endpoint reports a stale task runner with HTTP status 503 and a body. Thus the store
        // reads the status from the body, and not from the HTTP status code.
        return fetch(url, {
            credentials: 'same-origin',
            headers: { 'Accept': 'application/json' },
            cache: fresh ? 'no-cache' : 'default',
        })
            .then(response => response.json())
            .then(body => {
                if (asked !== generation) {
                    return;
                }
                lastgoodfetch = Date.now();
                publish(body);
            })
            .catch(error => {
                if (asked !== generation) {
                    return;
                }
                // A failed request of our own tells us nothing about the task runner. Thus this
                // code reports no outage. But the store must also stop to give a status that
                // several polls did not confirm. An unchanged "3 slots busy" sentence can stay
                // after a task runner stops. An unchanged outage sentence can stay after a task
                // runner starts again.
                console.debug('Could not read the task runner status', error);
                if (lastgoodfetch != null && (Date.now() - lastgoodfetch) > poll * 5) {
                    publish(null);
                }
            });
    }

    function refreshIfVisible() {
        if (!pollingPaused()) {
            refresh({ fresh: true });
        }
    }

    /** Stop the poll, and discard the last status. */
    function stop() {
        clearInterval(interval);
        interval = null;
        if (typeof document !== 'undefined') {
            document.removeEventListener('visibilitychange', refreshIfVisible);
        }
        // A store with no reader cannot know the age of its answer at the time of the next
        // subscription. Thus it keeps no answer. The next reader gets null and a new request,
        // which is what the first reader gets. A page that draws the box does not come here,
        // because the box keeps its subscription for the life of the page.
        //
        // The question number also increases, which discards each request that is in progress.
        // Without that step, a request that started before the stop arrives after it, and it puts
        // back the status that stop() just discarded.
        generation += 1;
        status = null;
        lastgoodfetch = null;
    }

    /**
     * Report each status to `listener`, until a call of the returned function.
     *
     * This function calls the listener immediately with the current status. Thus a reader that
     * subscribes after the first response needs no other way to get that response. A reader that
     * subscribes before the first response gets null, which is the value React holds until its
     * first response.
     */
    function subscribe(listener) {
        listeners.add(listener);

        if (interval == null) {
            // This first request runs even when the poll is paused. It fills the box, and it is
            // not a repeat of an earlier request. Without it, a tab that opens in the background
            // would show no outage sentence until the first interval.
            refresh();
            interval = setInterval(() => { if (!pollingPaused()) { refresh(); } }, poll);
            // A hidden tab gets no poll, and thus its sentence has the age of the moment when the
            // tab became hidden. The reader who comes back to that tab is the one person who looks
            // at the sentence. An outage that started, or stopped, in that time makes it wrong.
            if (typeof document !== 'undefined') {
                document.addEventListener('visibilitychange', refreshIfVisible);
            }
        }

        // Through tell(), because the start of this module makes this call. An exception here
        // would stop the full module, and the queue page imports the module for its estimates.
        tell(listener, status);

        let subscribed = true;
        return () => {
            // One call only. A second call must not stop a poll that a later reader started.
            if (!subscribed) {
                return;
            }
            subscribed = false;
            listeners.delete(listener);
            if (listeners.size === 0) {
                stop();
            }
        };
    }

    return { subscribe, refresh, stop, current: () => status };
}

// The store of this page. It is made at the first use, because the URL comes from the document.
let pagestore = null;

function pageStore() {
    if (pagestore == null) {
        const meta = document.querySelector(URL_META);
        // No meta tag means that no page gave the address of the endpoint. This store then gives
        // null always. A path in this file would be wrong, because it would have no script prefix.
        pagestore = meta != null
            ? createStore({ url: meta.content })
            : { subscribe: (listener) => { listener(null); return () => {}; }, refresh: () => {}, stop: () => {}, current: () => null };
    }

    return pagestore;
}

/** Report each status of the task runner to `listener`. Return the function that cancels this. */
export function subscribe(listener) {
    return pageStore().subscribe(listener);
}

/**
 * The warning mark in front of the outage sentence.
 *
 * This function draws the mark, and the template does not hold it, because the mark belongs to a
 * condition that the template cannot know. The mark has aria-hidden. The sentence next to it
 * already reports that the task runner stopped, and a screen reader that speaks "warning" first
 * would only delay that sentence. The mark is for the reader who cannot see the difference between
 * the yellow box and the grey box.
 */
function warningMark(document) {
    const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('class', 'sitenotice-warnmark');
    svg.setAttribute('viewBox', '0 0 16 16');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');

    const triangle = document.createElementNS(ns, 'path');
    triangle.setAttribute('d', 'M8 2.4 14.8 13.8H1.2z');
    triangle.setAttribute('fill', 'none');
    triangle.setAttribute('stroke', 'currentColor');
    triangle.setAttribute('stroke-width', '1.4');
    triangle.setAttribute('stroke-linejoin', 'round');

    const bang = document.createElementNS(ns, 'path');
    bang.setAttribute('d', 'M8 6.4v3.2');
    bang.setAttribute('stroke', 'currentColor');
    bang.setAttribute('stroke-width', '1.4');
    bang.setAttribute('stroke-linecap', 'round');

    const dot = document.createElementNS(ns, 'circle');
    dot.setAttribute('cx', '8');
    dot.setAttribute('cy', '11.7');
    dot.setAttribute('r', '0.8');
    dot.setAttribute('fill', 'currentColor');

    svg.append(triangle, bang, dot);
    return svg;
}

/**
 * Put a status into the box. This gives the runner sentence, and the warning colours of the box.
 *
 * This function writes nothing when the sentence and the condition are the ones on the screen. The
 * runner sentence is in a live region, and a second write of it makes a screen reader speak it
 * again. In an outage, the age changes at each poll, but the words stay the same for an hour. A
 * write at each poll would thus speak one sentence sixty times.
 */
export function renderInto(box, status) {
    const line = box.querySelector('.sitenotice-runner');
    if (line == null) {
        return;
    }

    // Whether the queue counts are of use to the reader of this page; see runnerMessage. The
    // value comes from the box, because the server knows it: who has signed in, and what that
    // reader has in the queue.
    const message = runnerMessage(status, { showQueue: box.hasAttribute('data-showqueue') });
    const stale = status != null && Boolean(status.stale);
    const drawn = message == null ? '' : message;

    // These two classes come first, and before the test below, because the box is drawn from
    // them. A box that opens empty and stays empty must also get the class that collapses it. To
    // set a class to the value that it has changes nothing, and thus this costs one comparison.
    box.classList.toggle('stale', stale);
    // The second part of the collapse. js/sitenotice.js sets sitenotice-nonote. See main.css.
    box.classList.toggle('sitenotice-noline', message == null);

    if (line.textContent === drawn) {
        return;
    }

    /*
     * What a screen reader speaks, and what it does not speak.
     *
     * An outage changes what to expect of every page, and thus a screen reader speaks the outage
     * sentence. The queue counts answer the question "when does my task start", and this box is on
     * every page. Spoken each minute over the work of the reader, those counts interrupt a reader
     * who did not ask for them. This code sets the attribute before the text. A screen reader
     * reads a live region in the condition that the region had at the change of its content.
     */
    line.setAttribute('aria-live', stale ? 'polite' : 'off');

    // Text nodes and one drawn element, and never innerHTML. This function makes the sentence,
    // and the box also holds the note. This function must not be able to change that note.
    if (message == null) {
        line.textContent = '';
    } else if (stale) {
        line.replaceChildren(warningMark(box.ownerDocument), message);
    } else {
        line.textContent = message;
    }

}

/** Keep the status of the task runner in `box`. Return the function that cancels this. */
export function start(box) {
    return subscribe((status) => renderInto(box, status));
}

// Each page that draws the box gets the status. A page with no box starts no poll. A test
// harness and the body of an email are such pages. tasklist.jsx makes the same test for #taskpage.
const sitenotice = typeof document !== 'undefined' ? document.getElementById('sitenotice') : null;
const stopbootstrap = sitenotice != null ? start(sitenotice) : () => {};

/**
 * Cancel the subscription above.
 *
 * This function is for the tests. A test loads this module with a document of its own. Without
 * this function, the poll would continue for the remainder of the test run. A page does not call
 * it, because the page shows the box for as long as it is open.
 */
export function stopPageStore() {
    stopbootstrap();
}
