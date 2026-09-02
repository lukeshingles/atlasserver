'use strict';

// The task runner status, as it reaches every page.
//
// This module does four things:
//
// - it polls /taskrunnerstatus.json;
// - it writes the sentence that gives the status;
// - it puts that sentence into the site notice box;
// - it keeps the last status for the next page, so that the box is filled before the first
//   response of that page arrives. See createCache.
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

// Where those templates put the reader that the page was rendered for, which is empty for a
// reader who is not signed in. See createCache: the status that a page keeps for the next page
// holds a count of this reader's own queued tasks.
const READER_META = 'meta[name="atlas-reader"]';

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
 * Fields of the status that change on their own. No reader renders from their values, but the
 * queue page reads the write time beside the list of running task ids, to tell a task that runs
 * from one that was started and given back; so an unchanged poll carries these fields onto the
 * kept status (see publish), and the page reads them at its next render. The list of ids is not
 * among them: a change to it is a change, and two lists with the same ids compare equal below.
 *
 * This is a list of the fields to ignore, and not a list of the fields that are of use. A field
 * that the endpoint gains later is thus compared by default. Such a field can then cost an
 * unnecessary render, which is a small cost. In a list of the fields to compare, a new field would
 * be invisible to every reader, and nothing would fail to show the mistake.
 */
const RUNNERSTATUS_VOLATILE_FIELDS = ['written', 'pid', 'status_age_seconds'];

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
export function pollingPaused() {
    // Use typeof for both names. The store also runs under `node --test`, which has no document.
    // A direct reference to a name that does not exist gives a ReferenceError.
    if (typeof document === 'undefined') {
        return false;
    }

    return document.hidden || (typeof window !== 'undefined' && window.user_is_active === false);
}

// Where a page leaves its last status for the page after it. See createCache.
const CACHE_KEY = 'atlas-runnerstatus';

/**
 * sessionStorage, or null where there is none.
 *
 * A browser that is set to refuse site data throws from this name rather than giving null, and
 * node, which runs the tests, has no such name at all. theme.js makes the same allowance for
 * localStorage.
 */
function browserStorage() {
    try {
        return typeof sessionStorage !== 'undefined' ? sessionStorage : null;
    } catch (error) {
        return null;
    }
}

/**
 * A status as it stands `seconds` later.
 *
 * The outage sentence gives the age of the status file, and that age grows while no page of this
 * site is open. To report the age as it was kept would move the start of the outage forward by the
 * time the reader spent on the page before, once for each page they open.
 *
 * That sentence is the one reader of the field, and thus a status that is not stale is returned as
 * it is. runnerStatusEqual ignores the age of a status that is not stale for the same reason.
 */
function advanceAge(status, seconds) {
    if (!status.stale || typeof status.status_age_seconds !== 'number') {
        return status;
    }

    // To one decimal, which is what the endpoint gives. A value with more digits than the one it
    // replaces would say that this number is measured more finely than it is.
    return { ...status, status_age_seconds: Math.round((status.status_age_seconds + seconds) * 10) / 10 };
}

/**
 * The place where one page leaves the status for the next one.
 *
 * The box is on every page, and each page load starts with no status and asks for one. The answer
 * takes about a second, and the box is empty for that second. A reader who moves between pages
 * thus watches the sentence go and come back, and a reader who is told of an outage on one page
 * has to wait to be told again on the next.
 *
 * So a page keeps its last answer, and the page after it starts from that answer instead of from
 * nothing. The kept status is shown for as long as the page that kept it would have shown it: one
 * poll interval. Beyond that it is discarded, and the new page starts empty as before, because a
 * sentence that no poll has confirmed for longer than that is a sentence an open tab would have
 * replaced by now.
 *
 * sessionStorage, and not localStorage: this is a snapshot of a moment and not a preference of the
 * reader. It belongs to the tab that took it, it is gone when that tab is closed, and a browser
 * that opens the site again after an hour has nothing to start from, which is the correct amount.
 *
 * One field of the response is about the reader and not about the task runner:
 * user_queued_task_count, which decides whether this reader is shown the queue counts. A tab keeps
 * its sessionStorage across a sign-out and a sign-in, and thus that field would otherwise reach
 * the page of another reader: the counts of the queue would show to somebody who signed out, and
 * they would be missing from the first page of somebody who signed in. So the record names the
 * reader it was kept for, and a page reads only a record with its own name on it. The counts
 * themselves are the same for everybody, and the queue page gives them to any reader who asks.
 *
 * This is not the first status that a page could have. The server renders the box, and it could
 * render the status into it, which would fill the box of the first page of a session as well. That
 * is a larger change than the one this fixes: the endpoint's answer holds a count for this reader
 * and a set of medians, and the context processor of the box is written to keep those off the
 * render of every page. So the first page of a session waits for its answer, as every page did
 * before, and each page after it starts from the answer that page got.
 *
 * `storage` is a parameter so that the tests can pass their own. `url` guards against a status
 * kept by another endpoint on the same origin, such as a second instance under another prefix,
 * and `reader` against a status kept for another reader of this browser.
 */
export function createCache({ storage = browserStorage(), url, reader }) {
    function clear() {
        try {
            storage?.removeItem(CACHE_KEY);
        } catch (error) {
            console.debug('Could not discard the kept task runner status', error);
        }
    }

    /**
     * The kept status and the time it was confirmed, or null when there is nothing to start from.
     *
     * `maxage` is in milliseconds. The age of the record decides, and not the age of the status
     * file: those are two different things, and only the outage sentence gives the second one.
     */
    function read(maxage) {
        let record = null;
        try {
            // No storage gives undefined here, which reads as no record, as an empty one does.
            const text = storage?.getItem(CACHE_KEY);
            record = text == null ? null : JSON.parse(text);
        } catch (error) {
            // Refused storage, or a value that another version of this file wrote. Neither is
            // worth a message: the page then does what it did before this cache existed.
            record = null;
        }

        // A record that this file did not write fails one of these: a value of any other shape
        // has no `url` in it, and thus no `url` that is this one.
        if (record == null || record.url !== url || record.reader !== reader
            || record.status == null || typeof record.status !== 'object') {
            return null;
        }

        // A negative age means the clock moved back between the two pages, and thus that the age
        // of this record is not known. Such a record is left with the ones that are too old, as is
        // a `saved` that is not a number, which gives NaN here and fails both comparisons.
        //
        // Nothing is removed: the next answer overwrites this record, and until that answer comes
        // each read makes this same judgement of it.
        const elapsed = Date.now() - record.saved;
        if (!(elapsed >= 0 && elapsed <= maxage)) {
            return null;
        }

        return { saved: record.saved, status: advanceAge(record.status, elapsed / 1000) };
    }

    /** Keep `status`, as confirmed by a response that arrived at `at`. */
    function write(status, at) {
        try {
            storage?.setItem(CACHE_KEY, JSON.stringify({ url, reader, saved: at, status }));
        } catch (error) {
            // A storage that is full or refused must not stop the poll. The cost of a failure
            // here is the empty second that this cache is here to remove.
            console.debug('Could not keep the task runner status for the next page', error);
        }
    }

    return { read, write, clear };
}

/**
 * A poll of one status URL, and the readers that the poll reports to.
 *
 * This function is exported for the tests. Each test makes its own store, with a test fetch
 * function and a short interval. A page uses the store below, which all of its readers share.
 */
export function createStore({ url, poll = POLL_MS, reader = '', storage = browserStorage() }) {
    const cache = createCache({ storage, url, reader });
    // What the page before this one last heard, which is what the box drew as that page closed.
    // Thus the box of this page is filled at its first paint, and the response that arrives a
    // moment later is usually equal to this status and redraws nothing. See createCache.
    //
    // One poll interval, which is a rule about this box and not the freshness of the endpoint.
    // The endpoint declares its own, and it is shorter: a browser may answer a request of its own
    // from its cache for one write interval of the task runner. This window is the longer one
    // because it asks a different question, which is how long an open tab would have gone on
    // showing this sentence without a new answer.
    const kept = cache.read(poll);
    let status = kept == null ? null : kept.status;
    // The time of the last request that reached the endpoint. A kept status carries the time of
    // the request that gave it, and so an endpoint that cannot be reached from this page
    // withdraws that status at the same time as it would have on the page that kept it.
    let lastgoodfetch = kept == null ? null : kept.saved;
    let interval = null;
    // A count of the questions that this store asked.
    let generation = 0;
    // The number of the newest question with a body in its answer. A failed request does not
    // change this count, because a failure gives no status to report. stop() sets it to the
    // current count of questions, which cancels each request that is in progress.
    let answered = 0;
    const listeners = new Set();

    function publish(next) {
        // Each poll parses the response into a new object, and thus each response is a different
        // object. On the queue page this value is the state of TaskPage. To accept every response
        // would render the full page again, with the estimate of each row, to draw one unchanged
        // sentence.
        if (runnerStatusEqual(status, next)) {
            // the same thing, said again later: the kept object learns the newer write time in
            // place, without a round of the readers
            if (status != null) {
                for (const field of RUNNERSTATUS_VOLATILE_FIELDS) {
                    if (field in next) {
                        status[field] = next[field];
                    }
                }
            }
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
        // Thus the store keeps the answer with the highest number, and it discards an older
        // answer. Without these numbers, the store keeps the older status for a full poll. That
        // status also changes the wait estimate of each row on the queue page.
        const asked = (generation += 1);

        // The endpoint answers 200 for an outage too, and carries the state in the body, in
        // `stale` and `running`. Thus the store reads the status from the body, and not from the
        // HTTP status code. It reads the body under any other code as well, so a release that
        // still answers 503, or a request that a proxy answered with JSON of its own, is handled
        // the same way: a body that says nothing about the runner draws no sentence.
        return fetch(url, {
            credentials: 'same-origin',
            headers: { 'Accept': 'application/json' },
            cache: fresh ? 'no-cache' : 'default',
        })
            .then(response => response.json())
            .then(body => {
                // The comparison is with the newest answer, and not with the newest question. A
                // later request that fails gives no status, and thus it must not discard this one.
                if (asked <= answered) {
                    return;
                }
                answered = asked;
                lastgoodfetch = Date.now();
                publish(body);
                // After the readers of this page, because no reader of this page waits on it: the
                // record is for the next one. It is written at each confirmation and not at each
                // change, because a status that holds its value while a reader spends ten minutes
                // on one page must not become too old for the page they open next. The time in it
                // is the time of this answer, which is the time the next page judges its age from.
                cache.write(body, lastgoodfetch);
            })
            .catch(error => {
                if (asked <= answered) {
                    return;
                }
                // A failed request of our own tells us nothing about the task runner. Thus this
                // code reports no outage. But the store must also stop to give a status that
                // several polls did not confirm. An unchanged "3 slots busy" sentence can stay
                // after a task runner stops. An unchanged outage sentence can stay after a task
                // runner starts again.
                console.debug('Could not read the task runner status', error);
                if (lastgoodfetch != null && (Date.now() - lastgoodfetch) > poll * 5) {
                    // Also from the store for the next page. A status this old is one that this
                    // page stopped showing, and the next page must not bring it back.
                    cache.clear();
                    publish(null);
                }
            });
    }

    /*
     * Ask again when the tab becomes visible.
     *
     * This function tests document.hidden alone, and it does not call pollingPaused(). A reader
     * who brings the tab to the front is at the page, whatever the inactivity timer of the queue
     * page reports. That timer measures mouse and touch events, and a change of tab is neither.
     * A test of the timer here would thus skip the request for each tab that was hidden for more
     * than two minutes, which is the condition that most needs a new status.
     */
    function refreshIfVisible() {
        if (typeof document !== 'undefined' && !document.hidden) {
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
        // The count of answers moves to the count of questions, which cancels each request that
        // is in progress. Without that step, a request that started before the stop arrives after
        // it, and it puts back the status that stop() just discarded.
        answered = generation;
        status = null;
        lastgoodfetch = null;
        // The store for the next page is left as it is, and this is the difference between the
        // two. It holds the time of the answer in it, and thus the page that reads it can tell
        // how old that answer is. That is the one thing this store cannot do with `status`.
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
        // A page with no reader tag is a page rendered for nobody in particular, which reads as
        // the reader who is not signed in. A page that gives one and a page that gives none thus
        // do not share a record, which is the safe way round.
        const reader = document.querySelector(READER_META);
        // No meta tag means that no page gave the address of the endpoint. This store then gives
        // null always. A path in this file would be wrong, because it would have no script prefix.
        pagestore = meta != null
            ? createStore({ url: meta.content, reader: reader == null ? '' : reader.content })
            : { subscribe: (listener) => { listener(null); return () => {}; }, refresh: () => {}, stop: () => {}, current: () => null };
    }

    return pagestore;
}

/** Report each status of the task runner to `listener`. Return the function that cancels this. */
export function subscribe(listener) {
    return pageStore().subscribe(listener);
}

/**
 * Put a status into the box. This gives the runner sentence, and the warning colours of the box.
 *
 * This function writes no text when the sentence is the one on the screen. The runner sentence is
 * in a live region, and a second write of it makes a screen reader speak it again. In an outage,
 * the age changes at each poll, but the words stay the same for an hour. A write at each poll
 * would thus speak one sentence sixty times.
 */
export function renderInto(box, status) {
    const line = box.querySelector('.sitenotice-runner');
    const text = line == null ? null : line.querySelector('.sitenotice-runnertext');
    if (line == null || text == null) {
        return;
    }

    /*
     * Whether the queue counts are of use to the reader of this page; see runnerMessage.
     *
     * The queue page sets the attribute, because it is the page about the queue. On each other
     * page, the response tells whether this reader has a task in the queue. Thus the answer
     * follows the queue within one poll. A reader whose tasks all complete stops to get the
     * counts. A reader who submits from another tab starts to get them.
     */
    const showQueue = box.hasAttribute('data-showqueue')
        || (status != null && status.user_queued_task_count > 0);
    const message = runnerMessage(status, { showQueue });
    const stale = status != null && Boolean(status.stale);
    const drawn = message == null ? '' : message;

    // The classes and the mark come first, and before the test below, because the box is drawn
    // from them. A box that opens empty and stays empty must also get the class that collapses
    // it. To set a class to the value that it has changes nothing, and thus this costs one
    // comparison. The mark is in the template, hidden, with the other icons of the site; it
    // belongs to the outage sentence and shows with it.
    box.classList.toggle('stale', stale);
    // The second part of the collapse. The template sets sitenotice-nonote. See main.css.
    box.classList.toggle('sitenotice-noline', message == null);
    const mark = line.querySelector('.sitenotice-warnmark');
    if (mark != null) {
        // The attribute, and not the `hidden` property. The mark is an SVG element, and only HTML
        // elements have that property. An assignment to it makes a plain value on the object, and
        // the attribute that hides the mark stays.
        mark.toggleAttribute('hidden', !(stale && message != null));
    }

    if (text.textContent === drawn) {
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

    // One write of text, and never innerHTML. This function makes the sentence, and the box also
    // holds the note. This function must not be able to change that note.
    text.textContent = drawn;
}

/**
 * Keep the status of the task runner in `box`. Return the function that cancels this.
 *
 * The box arrives with the colours the server rendered into it, and this leaves them alone until
 * it has a status of its own. A store with no kept record reports null to a new reader, and null
 * is "not known yet" and not "the runner is well": drawing it would take the warning colours off
 * an outage the server had already told the reader about, and put them back a second later when
 * the first response arrived. That is the flash this avoids, and it is worst on the first page of
 * a session, which is the one page with no record to start from.
 *
 * A null after a status is the other thing: the store withdrawing an answer that repeated polls
 * could not confirm. That one is drawn, which empties the box -- see the withdrawal in refresh().
 */
export function start(box) {
    let known = false;
    return subscribe((status) => {
        if (status == null && !known) {
            return;
        }
        known = known || status != null;
        renderInto(box, status);
    });
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
