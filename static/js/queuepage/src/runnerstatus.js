'use strict';

// The task runner status, as it reaches every page.
//
// One module owns the poll of /taskrunnerstatus.json, the wording of the line it produces, and the
// update of the box that shows it. The queue page reads the same response for its wait estimates,
// so it subscribes here instead of polling the endpoint a second time.
//
// It imports nothing. A page that carries no import map can therefore load it as
// <script type="module">, which is what puts the status on the pages that run no other JavaScript.

export const POLL_MS = 60000;

// where base.html and the browsable API template put the endpoint URL. A meta tag rather than a
// global, because {% url %} must supply the script prefix and only a template knows it.
const URL_META = 'meta[name="atlas-runnerstatus-url"]';

/**
 * Describe an age in seconds in the largest unit that still reads as a number of things.
 *
 * The outage line gives an age, and "6401 minutes ago" is the one thing a reader of that sentence
 * has to work out for themselves: whether this started moments ago or days ago.
 */
export function describeAge(seconds) {
    const units = [['day', 86400], ['hour', 3600], ['minute', 60]];
    for (const [name, size] of units) {
        if (seconds >= size) {
            // floor, not round: an age is a count of completed units, and rounding up would
            // cross the boundary the unit was chosen by — 3599 seconds would come out as
            // "60 minutes ago" instead of "59 minutes ago"
            const count = Math.floor(seconds / size);
            return count + ' ' + name + (count == 1 ? '' : 's') + ' ago';
        }
    }
    return 'less than a minute ago';
}

/*
 * Fields of the runner status that move on their own and are not read for their value.
 *
 * A denylist rather than a list of the fields that matter: a field added to the endpoint later is
 * then compared by default, so the worst it can do is cost a re-render. Under an allowlist it would
 * be silently invisible to every reader, with nothing failing to say so.
 */
const RUNNERSTATUS_VOLATILE_FIELDS = ['written', 'pid', 'running_taskids', 'status_age_seconds'];

/**
 * Whether two runner status responses say the same thing to everything that reads one.
 *
 * The store publishes only a response that differs by this measure, so it decides both whether the
 * box is rewritten and whether the queue page re-renders. Neither leaves a mark that an assertion
 * on the page could tell from an unchanged one, which is why this is exported and tested directly.
 */
export function runnerStatusEqual(previous, next) {
    if (previous == null || next == null) {
        return previous === next;
    }

    // The age advances on every poll by construction, so comparing it always would make this gate
    // useless. It is rendered in one place, the outage line -- and that is exactly the case where
    // every other field has stopped moving, because a runner that is not writing its status file is
    // not changing any of them. Excluded when the runner is healthy, compared when it is not.
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
        // typical_runtime_seconds is the one nested value: a handful of request types to a number
        // each. Serialising is enough to compare it, because the server builds it by iterating the
        // declared request types, so a given set of medians has one spelling.
        if (JSON.stringify(before) !== JSON.stringify(after)) {
            return false;
        }
    }

    return true;
}

/**
 * The sentence for a status, or null when the runner has nothing to report.
 *
 * A string rather than markup, so that the one caller which touches the DOM is renderInto below.
 *
 * `showQueue` is whether the queue counts are of use to this reader. The outage sentence is for
 * everybody, because it changes what to expect of every page. How many slots are busy is not: it
 * is an answer to "when does my task start", so a reader with nothing queued, on a page that is
 * not the queue, is told nothing at all.
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
        // an idle runner with an empty queue is the normal case and needs no commentary
        return null;
    }

    // during the hourly maintenance sweep the slot counts are frozen (nothing is dispatched or
    // reaped while it runs), so say what is happening rather than reporting numbers that are
    // temporarily meaningless
    const activity = status.maintenance
        ? 'maintenance sweep in progress;'
        : status.slots_busy + ' of ' + status.numslots + ' slots busy,';

    return 'Task runner: ' + activity + ' ' + status.queued_task_count + ' unfinished '
        + (status.queued_task_count == 1 ? 'task' : 'tasks') + ' from all users in the queue.';
}

/** Whether the page is on screen. A hidden tab is not polled; see subscribe below. */
function pollingPaused() {
    // typeof, because the store also runs under `node --test` with no DOM at all. The queue page
    // pauses on its own inactivity timer as well, which is a global no other page defines, so a
    // visible but idle tab now keeps polling.
    return typeof document !== 'undefined' && document.hidden;
}

/**
 * A poller of one status URL, and the readers it reports to.
 *
 * Exported for the tests, which drive a store of their own with a stubbed fetch and a short
 * interval. Pages use the one below, which every subscriber shares.
 */
export function createStore({ url, poll = POLL_MS }) {
    let status = null;
    // when the endpoint was last actually reachable
    let lastgoodfetch = null;
    let interval = null;
    const listeners = new Set();

    function publish(next) {
        // The response is a freshly parsed object every poll, so its identity always differs. On
        // the queue page this state belongs to TaskPage, and taking it unconditionally re-renders
        // the whole page -- every row's estimate, two URL parses -- to redraw an unchanged line.
        if (runnerStatusEqual(status, next)) {
            return;
        }

        status = next;
        // over a copy, so that a listener which unsubscribes from inside its own call cannot
        // shorten the set that is being walked
        for (const listener of [...listeners]) {
            listener(status);
        }
    }

    function refresh() {
        // a stale runner is reported with HTTP 503 and a body, so the status is read from the
        // body rather than from the status code
        return fetch(url, { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
            .then(response => response.json())
            .then(body => {
                lastgoodfetch = Date.now();
                publish(body);
            })
            .catch(() => {
                // not being able to reach this endpoint says nothing about the runner, so do not
                // claim an outage on the strength of our own failed request — but a status we have
                // not been able to re-confirm for several polls is no longer worth asserting
                // either, in either direction: a frozen "3 slots busy" line outlives a dead
                // runner, and a frozen outage line outlives a recovered one.
                if (lastgoodfetch != null && (Date.now() - lastgoodfetch) > poll * 5) {
                    publish(null);
                }
            });
    }

    function refreshIfVisible() {
        if (!pollingPaused()) {
            refresh();
        }
    }

    /** Stop polling, and forget the last reading. */
    function stop() {
        clearInterval(interval);
        interval = null;
        if (typeof document !== 'undefined') {
            document.removeEventListener('visibilitychange', refreshIfVisible);
        }
        // A store with no readers has no way to know how old its answer will be when somebody
        // subscribes again, so it keeps none. The next subscriber gets null and a fresh request,
        // which is what a first subscriber gets. A page that draws the box never reaches this: its
        // renderer stays subscribed for the life of the page.
        status = null;
        lastgoodfetch = null;
    }

    /**
     * Report every status to `listener` until the returned function is called.
     *
     * The listener is called at once with the current status, so a subscriber which arrives after
     * the first response needs no separate way to ask for it, and one which arrives before gets
     * null -- the value React holds until its first response either way.
     */
    function subscribe(listener) {
        listeners.add(listener);

        if (interval == null) {
            // The mount fetch runs whether or not polling is paused: it is the one that fills the
            // box, not a repeat of it. A tab opened in the background would otherwise show no
            // outage line until the interval first fired.
            refresh();
            interval = setInterval(() => { if (!pollingPaused()) { refresh(); } }, poll);
            // A hidden tab is not polled, so what it shows is as old as the moment it was hidden.
            // The reader who comes back to it is the one person looking at that line, and an
            // outage that started, or ended, while they were away is exactly what it gets wrong.
            if (typeof document !== 'undefined') {
                document.addEventListener('visibilitychange', refreshIfVisible);
            }
        }

        listener(status);

        let subscribed = true;
        return () => {
            // once only: a second call must not stop a poll that a later subscriber has restarted
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

// The page's own store, built on first use. Lazy, because the URL comes from the document.
let pagestore = null;

function pageStore() {
    if (pagestore == null) {
        const meta = document.querySelector(URL_META);
        // No meta tag means no page told us where the endpoint is. Answering null for ever is the
        // right reading of that: a path written here instead would be missing the script prefix.
        pagestore = meta != null
            ? createStore({ url: meta.content })
            : { subscribe: (listener) => { listener(null); return () => {}; }, refresh: () => {}, stop: () => {}, current: () => null };
    }

    return pagestore;
}

/** Report every status of this page's runner to `listener`. Returns the unsubscribe function. */
export function subscribe(listener) {
    return pageStore().subscribe(listener);
}

/**
 * The warning mark that goes in front of the outage sentence.
 *
 * Drawn here rather than written into the template, because it belongs to a state the template
 * cannot know. It is aria-hidden: the sentence beside it already says that the runner is down, and
 * a screen reader announcing "warning" first would only delay it. What the mark is for is the
 * reader who cannot tell the yellow panel from the grey one, and the page printed in black.
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

/** Put a status into the box: the runner line, and the panel the whole box becomes when stale. */
export function renderInto(box, status) {
    const line = box.querySelector('.sitenotice-runner');
    if (line == null) {
        return;
    }

    // Whether this page's reader is one of the people the queue counts are for; see runnerMessage.
    // Read off the box, because the server is what knows it -- who is signed in, and what they
    // have queued.
    const message = runnerMessage(status, { showQueue: box.hasAttribute('data-showqueue') });
    const stale = status != null && Boolean(status.stale);

    // Text nodes and one drawn element, never innerHTML: the sentence is built here, and the box
    // also holds the standing note, which this must not be able to rewrite.
    if (message == null) {
        line.textContent = '';
    } else if (stale) {
        line.replaceChildren(warningMark(box.ownerDocument), message);
    } else {
        line.textContent = message;
    }

    // on the box rather than on the line, so the warning panel wraps the notice as well
    box.classList.toggle('stale', stale);
}

/** Keep `box` showing the runner status. Returns the unsubscribe function. */
export function start(box) {
    return subscribe((status) => renderInto(box, status));
}

// Every page that draws the box gets the status; a page without one (a test harness, an email
// body) starts nothing. The same guard shape as tasklist.jsx uses for #taskpage.
const sitenotice = typeof document !== 'undefined' ? document.getElementById('sitenotice') : null;
if (sitenotice != null) {
    start(sitenotice);
}
