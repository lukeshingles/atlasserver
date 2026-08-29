'use strict';

import React from "react"
import ReactDOM from 'react-dom';
import { subscribe } from "runnerstatus";
import { csrfHeader } from "csrftoken";
import { NewRequest } from "newrequest";
import { NOT_MODIFIED, PollCache } from "pollcache";
import { estimateWaitSeconds, formatDuration, formatWaitEstimate } from "waitestimate";

function debug_log(...args) {
    // uncomment for debugging in development
    // console.log(...args);
}

// How often each thing is polled.
//
// The full task list used to be re-fetched every 2 seconds. Most of what a waiting user is watching
// is their queue position, and queuepositions.json answers that with two indexed queries and a few
// hundred bytes instead of a page of serialised tasks, so that is what runs on the short interval.
// A task dropping out of the queue positions response means it has finished, which triggers an
// immediate full fetch, so "my task is done" still appears within one short interval.
const TASKLIST_POLL_MS = 6000;
const QUEUEPOS_POLL_MS = 2000;

const SITE_TITLE = 'ATLAS Forced Photometry';

/*
How often to look for finished tasks while the tab is not being looked at.

Everything else stops when the tab is hidden -- see pollingPaused() -- which is the right policy for
the task list, a full serialisation nobody is reading. But it also means a queue left open in a
background tab can never say that anything has finished, and waiting is what this page is for. So one
request a minute, to queuepositions, which is the cheap endpoint (a dictionary of ids to positions,
no serialisation) and the one the code already treats as the signal that a task has left the queue.

Deliberately much slower than QUEUEPOS_POLL_MS: nobody is watching, so this only has to be quick
enough that the count is right by the time they look back.
*/
const AWAY_POLL_MS = 60000;

/*
How many queue positions ticks to let pass between requests once the answer is that nothing is queued.

Stopping outright was wrong: work can appear from somewhere this page cannot see -- another tab, or
the API -- and a view showing a finished task or the Running/Finished filter never gains a row that
would start the poll again. But asking every two seconds on behalf of a user with an empty queue is
the request this guard exists to avoid, so it slows to a fifth of the rate rather than stopping.
*/
const EMPTY_QUEUE_TICKS = 5;

function pageTitle(taskid, finishedwhileaway) {
    // matches the server-rendered <title>, so a client-side navigation does not leave the tab
    // claiming to show something else
    const title = (taskid != null ? 'Task ' + taskid : 'Task Queue') + ' – ' + SITE_TITLE;

    // in front, because a tab shows the beginning of its title and little else
    return finishedwhileaway > 0 ? '(' + finishedwhileaway + ') ' + title : title;
}

function pollingPaused() {
    return document[hidden] || !user_is_active;
}

/**
 * Whether the queuepositions response can be expected to say anything about this task.
 *
 * Only this user's own tasks appear in it, so a staff member looking at somebody else's task must not
 * be counted as "no longer queued". A task whose position has not been assigned yet is not in the
 * response either, and counting it would fire a full fetch on every tick, which is worse than not
 * having the endpoint at all.
 *
 * Used to decide whether the queuepositions poll is worth making at all: with none of the rows on
 * screen trackable, its answer could not change anything.
 */
function tracksQueuePosition(task) {
    return task.user_id == user_id && task.finishtimestamp == null && task.queuepos != null;
}

/**
 * Put a number on the navbar's Queue badge, or take the badge away at zero.
 *
 * The badge is rendered by the server, which is right for every other page -- each navigation
 * re-renders it -- but this page never navigates: submitting, cancelling and finishing all happen
 * without a page load, so the badge it was drawn with goes stale the moment anything changes.
 *
 * A direct DOM call because the navbar is not React's, the same reason the row animations are; and
 * from the queuepositions response because that is the user's whole queued set rather than the page
 * of rows on screen, so it is the count the badge wants.
 */
function updateQueueBadge(count) {
    const badge = document.querySelector('.queuecount');
    if (!badge) {
        return;
    }

    const number = badge.querySelector('.queuecount-number');
    if (number) {
        number.textContent = count;
    }
    badge.hidden = count == 0;
}

/**
 * Run fn every ms milliseconds, skipping the ticks where polling is paused.
 *
 * The pause belongs to the repeating polls and to nothing else, so it is applied here, at the
 * only place that repeats. It used to sit inside the functions being polled, where it also
 * caught the many callers that are not polls at all — a click on a task, a filter, a page, a
 * delete, a history navigation — and silently dropped them, leaving the URL and the heading on
 * the new page while the tasks on screen stayed those of the old one, with nothing to correct it
 * until the mouse moved again (two minutes without a mousemove counts as "away").
 */
function pollInterval(fn, ms) {
    return setInterval(() => { if (!pollingPaused()) { fn(); } }, ms);
}

/*
 * Row show/hide animations.
 *
 * These were jQuery's slideUp/slideDown/hide, which set inline styles on a node React owns: React
 * does not know the element was hidden, so a re-render that reused the node left it stuck. The
 * class is applied to the same node but drives a CSS transition instead (see main.css), and is
 * always removed again, so the only inline state is the one the stylesheet manages.
 *
 * Kept as direct DOM calls rather than component state because both callers act on a *sibling*
 * row from a callback (a delete animates the row that is going away; a new task animates a row
 * that has only just mounted), which is awkward to express as state on the row itself.
 */
const ROW_TRANSITION_MS = 200;

function taskRow(taskid) {
    return document.getElementById('task-' + taskid);
}

function collapseRow(taskid) {
    taskRow(taskid)?.classList.add('task-collapsed');
}

function expandRow(taskid) {
    taskRow(taskid)?.classList.remove('task-collapsed');
}

/* How long the highlight on a just-created row lasts. Longer than the row's own show, because it is
   there to be noticed after the movement has finished and the eye has arrived. */
const ROW_FLASH_MS = 1600;

function revealRow(taskid) {
    const row = taskRow(taskid);
    if (!row) {
        return;
    }
    row.classList.add('task-collapsed');
    // next frame, so the browser has laid the row out collapsed and has something to animate from
    requestAnimationFrame(() => requestAnimationFrame(() => row.classList.remove('task-collapsed')));

    // and a tint that fades out, so the row you just made is findable in a list of others. Removed
    // again, so nothing is left on a node React owns -- the same reason the collapse is a class.
    row.classList.add('task-flash');
    setTimeout(() => row.classList.remove('task-flash'), ROW_FLASH_MS);
}

/**
 * A value with a control that copies it, for the coordinates people retype into other tools.
 *
 * The same shape as the button copycode.js adds to the API guide -- a label that reports what
 * happened, and "Press Ctrl-C" when the browser refuses the write -- but rendered rather than
 * inserted, because this value is React's and a node added underneath it would be removed on the
 * next poll.
 *
 * Nothing is rendered where navigator.clipboard is missing, which is any page served over plain http
 * to a host other than localhost: there the button could only ever fail.
 */
const COPY_LABEL_RESET_MS = 2000;

function CopyableValue({ text, label, children }) {
    const [state, setState] = React.useState('idle');
    const timer = React.useRef(null);
    const value = React.useRef(null);

    React.useEffect(() => () => clearTimeout(timer.current), []);

    if (!navigator.clipboard) {
        return children;
    }

    const announce = (next) => {
        setState(next);
        clearTimeout(timer.current);
        timer.current = setTimeout(() => setState('idle'), COPY_LABEL_RESET_MS);
    };

    /*
     * Put the value under the selection, so that the "Press Ctrl-C" the failure path offers has
     * something to copy.
     *
     * Without this the focus is on the button and nothing is selected, so following the instruction
     * copies whatever the user had selected before -- or nothing at all, which is worse than saying
     * nothing. The span below wraps exactly the text the button would have written, so what a
     * keyboard copy produces is what a successful click would have.
     */
    const selectValue = () => {
        const selection = window.getSelection();
        if (!selection || !value.current) {
            return;
        }

        const range = document.createRange();
        range.selectNodeContents(value.current);
        selection.removeAllRanges();
        selection.addRange(range);
    };

    const copy = () => {
        navigator.clipboard.writeText(text).then(
            () => announce('copied'),
            () => {
                selectValue();
                announce('failed');
            });
    };

    return (
        <span className="copyable">
            <span className="copyvalue" ref={value}>{children}</span>
            {/* aria-live, so the change of state is announced rather than read over whatever has
                focus; the title is what the pointer gets, where the live text is not shown */}
            <button type="button" className="copybutton" onClick={copy}
                title={'Copy ' + label} aria-label={'Copy ' + label}>
                {state === 'idle' ? <CopyIcon /> : null}
                <span className="copyfeedback" aria-live="polite">
                    {state === 'copied' ? 'Copied' : null}
                    {state === 'failed' ? 'Press Ctrl-C' : null}
                </span>
            </button>
        </span>
    );
}

function CopyIcon() {
    // two offset rounded rectangles, the usual shorthand for a copy
    return (
        <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor"
            strokeWidth="1.5" aria-hidden="true">
            <rect x="5.5" y="5.5" width="8.5" height="9" rx="1.5" />
            <path d="M10.5 3.2A1.7 1.7 0 0 0 8.8 2H3.7A1.7 1.7 0 0 0 2 3.7v5.1a1.7 1.7 0 0 0 1.2 1.6" />
        </svg>
    );
}

const TaskPlot = React.memo(function TaskPlot({ taskid, taskurl }) {
    const divid = 'plotforcedflux-task-' + taskid;
    // Flux is what the result file holds, so it is what the plot opens in. The choice belongs to
    // one plot and lasts as long as it is on screen.
    const [unit, setUnit] = React.useState('flux');

    React.useEffect(() => {
        debug_log('activating plot', taskid);
        const plot_url = new URL(taskurl);
        plot_url.pathname += 'resultplotdata.js';
        plot_url.search = '';

        // was $.ajax({dataType: 'script'}), which fetches and evals. A <script> element does the
        // same thing, and unlike jQuery's version it is served from the browser's HTTP cache
        // (which the endpoint's ETag is there to make use of) rather than fetched every mount.
        const script = document.createElement('script');
        script.src = plot_url;
        document.head.appendChild(script);

        // taken now, while the div is still in the document. React removes it before the cleanup
        // below runs, and Plotly.purge throws when it is given the id of a div it cannot find.
        const plotnode = document.getElementById(divid);

        return () => {
            debug_log('Unmounting plot for task ', taskid);
            // the node too, not just the globals: jQuery's script transport removed it after
            // evaluating, and without this a session that pages through finished tasks leaves one
            // dead <script> in head per plot it has ever shown
            script.remove();
            // Plotly keeps the drawn traces on the div itself, and a responsive plot keeps a
            // resize listener; purge drops both, which removing the node alone does not. It is
            // given the node rather than the id, which it accepts after the node is detached.
            if (window.Plotly && plotnode) {
                window.Plotly.purge(plotnode);
            }
            const key = '#' + divid;
            delete jslimitsglobal[key];
            delete jslcdataglobal[key];
            delete jslabelsglobal[key];
            if (window.atlasLightcurves) {
                delete window.atlasLightcurves[divid];
            }
        };
    }, [taskid, taskurl, divid]);

    /*
    Redraw in the unit the buttons now ask for.

    This runs after React has written data-unit onto the div, which is where the plot script reads
    the choice from. There is nothing to redraw until that script has arrived: it draws the first
    plot itself, in the unit the div already carries.
    */
    React.useEffect(() => {
        const redraw = window.atlasLightcurves && window.atlasLightcurves[divid];
        if (redraw) {
            redraw();
        }
    }, [unit, divid]);

    return (
        <div className="plotbox">
            <div className="plotunits">
                <div className="btn-group btn-group-sm" role="group" aria-label="Plot units">
                    {[['flux', 'Flux'], ['mag', 'AB Mag']].map(([value, label]) => (
                        // btn-sm on the button itself, not only btn-group-sm on the group: the
                        // task row styles every .btn that is not btn-sm for its big action
                        // buttons, which would paint these white on white
                        <button key={value} type="button"
                            className={'btn btn-sm btn-outline-secondary' + (unit === value ? ' active' : '')}
                            aria-pressed={unit === value}
                            onClick={() => setUnit(value)}>{label}</button>
                    ))}
                </div>
            </div>
            <div id={divid} className="plot" data-unit={unit}
                style={{ width: '100%', height: '300px' }}></div>
        </div>
    );
});

/** Seconds a running task has been going, ticking once a second while it runs. */
function useTimeElapsed(taskdata) {
    const running = taskdata.starttimestamp != null && taskdata.finishtimestamp == null;

    const secondsSinceStart = () =>
        ((new Date().getTime() - new Date(taskdata.starttimestamp).getTime()) / 1000.).toFixed(0);

    const [timeelapsed, setTimeelapsed] = React.useState(() => (running ? secondsSinceStart() : -1));

    React.useEffect(() => {
        if (!running) {
            return undefined;
        }

        // set immediately as well as on the interval, so a row that has just started rendering
        // does not show a stale figure for a second
        setTimeelapsed(secondsSinceStart());
        const interval = setInterval(() => setTimeelapsed(secondsSinceStart()), 1000);

        return () => clearInterval(interval);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [running, taskdata.starttimestamp]);

    return running ? timeelapsed : -1;
}

/**
 * Whether a re-render of a row can be skipped.
 *
 * Replaces the shouldComponentUpdate the class had: the task list is re-fetched every 6 seconds
 * and the 304 branch always stamps a new "last updated" time, so the page re-renders on every
 * poll and would re-render every row with it, whether or not the response differed.
 *
 * The identity check carries the 304 case, where results are re-applied unchanged. A 200 rebuilds
 * every task from JSON, so identity never matches there and the deep compare is what does the
 * work. fetchData and setSingleTaskView are not compared: both are useCallbacks with stable
 * identities, so they cannot differ, and listing them would suggest otherwise.
 *
 * The elapsed-seconds ticker is component state, so it is unaffected by this and keeps running.
 */
function taskPropsEqual(prev, next) {
    return (
        prev.hidePlot === next.hidePlot
        // a string or null, so this stays the cheap check the rest of this function avoids
        && prev.waitestimate === next.waitestimate
        && (prev.taskdata === next.taskdata
            || JSON.stringify(prev.taskdata) === JSON.stringify(next.taskdata))
    );
}

export const Task = React.memo(function Task(props) {
    const [httperror, setHttperror] = React.useState('');

    // The timer used to live in render state, as `interval`, started from
    // getDerivedStateFromProps -- a side effect during render, which React 19 may run more than
    // once or throw away, leaking a timer each time. A timer handle is not state anyway: nothing
    // renders it.
    const timeelapsed = useTimeElapsed(props.taskdata);

    React.useEffect(() => {
        if (newtaskids.includes(props.taskdata.id)) {
            debug_log('showing new task', props.taskdata.id);
            revealRow(props.taskdata.id);
            newtaskids = newtaskids.filter(item => item !== props.taskdata.id);
        }
        // mount only: this animates a row that has just appeared
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    function deleteTask() {
        const task = props.taskdata;
        collapseRow(task.id);
        setTimeout(() => {
            fetch(task.url, {
                credentials: "same-origin",
                method: "DELETE",
                headers: csrfHeader(),
            })
                .then((response) => {
                    if (response.ok) {
                        console.log('Deleted task ', task.id);
                        setHttperror('');
                        // not fetchData(true): "user triggered" re-applies the cached pre-delete
                        // body and scrolls the window to the top, which after deleting the last row
                        // on a page means the viewport jumps away from what the user was looking at
                        props.fetchData();
                        return;
                    }

                    // the row comes back, so without a message the click looks like it simply did
                    // nothing. A 403 here means the task belongs to somebody else; a 401 means the
                    // session has ended, so reload the page, which lands on the login form.
                    console.log('Failed to delete task ', task.id, response.status);
                    if (response.status === 401) {
                        window.location.reload();
                        return;
                    }
                    expandRow(task.id);
                    let message = 'ERROR: could not delete this task (HTTP ' + response.status + ').';
                    if (response.status === 403) {
                        message = 'ERROR: you are not allowed to delete this task.';
                    }
                    setHttperror(message);
                    props.fetchData();
                })
                .catch((err) => {
                    // fetch rejects only on a network-level failure, which has no HTTP status --
                    // jQuery used to report those as status 0, which read as gibberish in a message
                    console.log('Failed to reach the server to delete task ', task.id, err);
                    expandRow(task.id);
                    setHttperror('ERROR: could not reach the server to delete this task.');
                    props.fetchData();
                });
        }, ROW_TRANSITION_MS);
    }

    function requestImages() {
        const request_image_url = new URL(props.taskdata.url);
        // the trailing slash matters: without it APPEND_SLASH answers with a 301, and a browser
        // retries a redirected POST as a GET, which this endpoint does not accept
        request_image_url.pathname += 'requestimages/';
        request_image_url.search = '';

        fetch(request_image_url,
            {
                credentials: "same-origin",
                method: "POST",
                headers: {
                    ...csrfHeader(),
                    'Accept': 'application/json',
                    'Content-Type': 'application/json',
                },
            })
            .then((response) => {
                if (response.status == 200 && response.redirected) {
                    setHttperror('');
                    const newimgtask_id = parseInt(new URL(response.url).searchParams.get('newids'));
                    newtaskids.push(newimgtask_id);
                    debug_log('requestimages created task', newimgtask_id);
                    const new_page_url = new URL(response.url);
                    new_page_url.searchParams.delete('newids');
                    window.history.pushState({}, document.title, new_page_url);
                    props.fetchData(true);
                } else {
                    // the body is not always JSON (e.g. a plain-text 404), and DRF reports its
                    // own errors under 'detail' rather than 'non_field_errors'
                    return response.text().then(text => {
                        let message = response.statusText || ('HTTP ' + response.status);
                        try {
                            const data = JSON.parse(text);
                            message = data["non_field_errors"] || data["detail"] || message;
                        } catch (err) {
                            console.log('requestImages: non-JSON error body', text);
                        }
                        console.log('requestImages: error returned', response.status, message);
                        setHttperror('ERROR: ' + message);
                    });
                }
                return null;
            })
            .catch(error => {
                console.log('requestImages HTTP request failed', error);
                setHttperror('HTTP request failed.');
            });
    }

    const task = props.taskdata;
    // whether the data file this task produced is still on disk. The maintenance sweep
    // reclaims it after a few months, and the serializer answers with a null result_url from
    // then on; everything derived from that file — the download links, the plot, and the
    // image request that reads it to know which observations to fetch — turns on this.
    const hasresultfile = task.result_url != null;
    let statusclass = 'none';
    let buttontext = 'none';
    // the word in the badge beside the task number, and the modifier that colours it and the row's
    // left edge. The four states are the ones the row already distinguished by other means: an error
    // was only visible as red text further down, and a finished task only by not saying anything.
    let statuslabel = '';
    let statusbadge = '';
    if (task.finishtimestamp != null) {
        // "errored" as well as "finished", so the row's left edge can disagree with the plain
        // finished colour without the stylesheet having to look inside the row for the badge
        statusclass = task.error_msg != null ? "finished errored" : "finished";
        buttontext = 'Delete';
        statuslabel = task.error_msg != null ? 'Error' : 'Finished';
        statusbadge = task.error_msg != null ? 'taskbadge-error' : 'taskbadge-finished';
    } else if (task.starttimestamp != null) {
        statusclass = "queued started";
        buttontext = 'Cancel';
        statuslabel = 'Running';
        statusbadge = 'taskbadge-running';
    } else {
        statusclass = "queued notstarted";
        buttontext = 'Cancel';
        statuslabel = 'Queued';
        statusbadge = 'taskbadge-queued';
    }
    debug_log('Task ' + task.id + ' rendered');
    let delbutton = null;
    if (task.user_id == user_id) {
        delbutton = <button className="btn btn-sm btn-danger" onClick={() => deleteTask()}>{buttontext}</button>;
    }
    // rendered only when there is one: React drops a null src, which left an empty <img> box in
    // every row that has no preview, and without alt text there was nothing to announce either
    const previewimage = task.previewimage_url ? (
        <img className="previewimage" src={task.previewimage_url} height="100" loading="lazy"
            alt={'Preview image for task ' + task.id} />
    ) : null;

    let taskbox = [
        <div key="rightside" className="rightside">
            {delbutton}
            {previewimage}
        </div>
    ];

    taskbox.push(
        <div key="tasknum" className="taskheading">
            <a key="tasklink" href={task.url} onClick={(e) => { props.setSingleTaskView(e, task.id, task.url) }}>Task {task.id}</a>
            <span key="taskbadge" className={'badge taskbadge ' + statusbadge}>{statuslabel}</span>
        </div>);

    if (task.parent_task_url) {
        taskbox.push(<p key="imgrequest">Image request for <a key="parent_task_link" href={task.parent_task_url} onClick={(e) => { props.setSingleTaskView(e, task.parent_task_id, task.parent_task_url) }}>Task {task.parent_task_id}</a></p>);
    } else if (task.parent_task_id) {
        taskbox.push(<p key="imgrequest">Image request for Task {task.parent_task_id} (deleted)</p>);
    } else if (task.request_type == 'IMGZIP') {
        taskbox.push(<p key="imgrequest">Image request</p>);
    }

    if (task.request_type == 'IMGZIP') {
        const imagetype = task.use_reduced ? 'reduced' : 'difference';
        taskbox.push(<p key="imgrequestnote">Up to the first 1000 {imagetype} images will be retrieved. The image request and download link may expire after one week.</p>);
    }

    // What the task is, as label and value pairs rather than a run of sentences: down a list of
    // tasks the coordinates, MJD ranges and timestamps are what the eye compares, and as prose they
    // started at a different column in every row. main.css lays the pairs out as a grid and gives
    // the values tabular figures, so the digits line up between rows as well as within one.
    const meta = [];

    if (task.user_id != user_id) {
        meta.push(['user', 'User:', task.username]);
    }

    if (task.comment != null && task.comment != '') {
        meta.push(['comment', 'Comment:', <b>{task.comment}</b>]);
    }

    // task_mpc_name_not_blank keeps whitespace-only names out of the column, so an empty string
    // here means the task has no MPC target rather than one that only looks empty
    if (task.mpc_name != null && task.mpc_name != '') {
        meta.push(['target', 'MPC Object:',
            <CopyableValue text={task.mpc_name} label="object name">{task.mpc_name}</CopyableValue>]);
    } else {
        let radecepoch = '';
        if (task.radec_epoch_year != null) {
            radecepoch = <span>(epoch {task.radec_epoch_year}) </span>;
        }
        // the epoch stays outside the copyable part: what this is for is pasting the position into
        // something else, and the copy control's failure path selects its own contents, so those
        // contents have to be exactly the text the button would otherwise have written
        meta.push(['target', 'RA Dec:',
            <span>{radecepoch}<CopyableValue text={task.ra + ' ' + task.dec} label="coordinates">{task.ra} {task.dec}</CopyableValue></span>]);
        // proper motion components are signed, so testing for > 0 hides half of all real values
        if ((task.propermotion_ra != null && task.propermotion_ra != 0)
            || (task.propermotion_dec != null && task.propermotion_dec != 0)) {
            // the unit moves into the value, so the label column stays as narrow as the longest
            // short label rather than being set by this one
            meta.push(['propermotion', 'Proper motion:',
                <span>{task.propermotion_ra} {task.propermotion_dec} mas/yr</span>]);
        }
    }

    if (task.request_type == "SSOSTACK") {
        meta.push(['imgtype', 'Image:', 'Stacked']);
    } else {
        meta.push(['imgtype', 'Images:', task.use_reduced ? 'Reduced' : 'Difference']);
    }

    if (task.mjd_min != null || task.mjd_max != null) {
        const mjdmin = task.mjd_min != null ? task.mjd_min : "0";
        const mjdmax = task.mjd_max != null ? task.mjd_max : "∞";
        meta.push(['mjdrange', 'MJD request:', <span>[{mjdmin}, {mjdmax}]</span>]);
    }

    // Only when there was more than one, since one is the ordinary case. The timings below cover
    // the attempt that produced the result, so this is what says a result was slow to appear
    // because the task had to be retried.
    if (task.attempt_count > 1) {
        meta.push(['attempts', 'Attempts:', task.attempt_count]);
    }

    meta.push(['queuetime', 'Queued at:', new Date(task.timestamp).toLocaleString()]);
    if (task.finishtimestamp != null) {
        meta.push(['finishtime', 'Finished at:', new Date(task.finishtimestamp).toLocaleString()]);

        // How the two timestamps above break down. Worth a line because it is what tells the user
        // whether a slow result was a busy queue or a slow job -- and it calibrates what to expect
        // from the next request.
        //
        // Either half can be absent (a task cancelled before it started has no run time), so they
        // are assembled rather than formatted as one string. Each is tested on the formatted text
        // rather than on the raw number: formatDuration also declines a value it cannot render, and
        // testing the input instead concatenates its null into the sentence.
        const timing = (label, seconds) => {
            const text = formatDuration(seconds);
            return text != null ? label + ' ' + text : null;
        };

        // The wait is only reported for a task that ran once. starttimestamp moves to each new
        // attempt, so after a retry the span from submission to it covers the earlier attempts and
        // the pauses between them as well as the queue -- and calling that "waited" blames the
        // queue for time the task spent failing, which is the one distinction this line is for.
        // The run time is unaffected: it is the attempt that produced the result either way.
        const timings = [
            task.attempt_count > 1 ? null : timing('waited', task.waittime),
            timing('ran', task.runtime),
        ].filter((part) => part != null);

        if (timings.length > 0) {
            meta.push(['timings', 'Took:', timings.join(' · ')]);
        }
    }

    taskbox.push(
        <dl key="taskmeta" className="taskmeta">
            {meta.map(([key, label, value]) => [
                <dt key={key + '-label'}>{label}</dt>,
                <dd key={key + '-value'}>{value}</dd>
            ])}
        </dl>);

    if (task.finishtimestamp != null) {
        if (task.error_msg != null) {
            taskbox.push(<p key="error_msg" className="taskerror">Error: {task.error_msg}</p>);
        } else {
            // React drops a null href, so before hasresultfile was checked here these rendered
            // as buttons that looked live but did nothing at all when clicked.
            const resultsexpired = (
                <p key="expired">The download link has expired. Delete this task and request again if necessary.</p>);

            if (task.request_type == 'FP') {
                if (hasresultfile) {
                    taskbox.push(<a key="datalink" className="results btn btn-info getdata" href={task.result_url} target="_blank" rel="noopener">Data</a>);
                    taskbox.push(<a key="pdflink" className="results btn btn-info getpdf" href={task.pdfplot_url} target="_blank" rel="noopener">PDF</a>);
                } else {
                    taskbox.push(resultsexpired);
                }
            } else if (task.request_type == 'SSOSTACK') {
                if (hasresultfile) {
                    taskbox.push(<a key="datalink" className="results btn btn-info getdata" href={task.result_url} target="_blank" rel="noopener">Data</a>);
                }
                if (task.result_imagestack_url != null) {
                    taskbox.push(<a key="imgdownload" className="results btn btn-info" href={task.result_imagestack_url} target="_blank" rel="noopener">Stacked image (FITS)</a>);
                } else {
                    taskbox.push(resultsexpired);
                }
            }

            if (task.request_type == 'IMGZIP') {
                if (task.result_imagezip_url != null) {
                    taskbox.push(<a key="imgdownload" className="results btn btn-info" href={task.result_imagezip_url}>Download images (ZIP)</a>);
                } else {
                    taskbox.push(resultsexpired);
                }
            } else if (task.imagerequest_task_id != null) {
                if (task.imagerequest_finished) {
                    taskbox.push(<a key="imgrequest" className="btn btn-primary" href={task.imagerequest_url} onClick={(e) => { props.setSingleTaskView(e, task.imagerequest_task_id, task.imagerequest_url) }}>Images retrieved</a>);
                } else {
                    taskbox.push(<a key="imgrequest" className="btn btn-warning" href={task.imagerequest_url} onClick={(e) => { props.setSingleTaskView(e, task.imagerequest_task_id, task.imagerequest_url) }}>Images requested</a>);
                }
            } else if (task.request_type == 'FP' && user_id == task.user_id && hasresultfile) {
                // hasresultfile: without that file this could only queue a job certain to fail
                taskbox.push(<button key="imgrequest" className="btn btn-info" onClick={() => requestImages()} title="Download FITS and JPEG images for up to the first 1000 observations.">Request {task.use_reduced ? 'reduced' : 'diff'} images</button>);
            }
        }
    } else if (task.starttimestamp != null) {
        // An indeterminate bar, striped and moving: the server reports that a task has started and
        // how long ago, and nothing about how far through it is, so there is no fraction to draw.
        // Bootstrap reads a role="progressbar" with no aria-valuenow as exactly that, and the label
        // carries what is actually known to anyone who cannot see it moving.
        taskbox.push(
            <div key="status" className="taskstatus running">
                Running (started {timeelapsed} seconds ago)
                <div className="progress taskprogress" role="progressbar"
                    aria-label={'Task ' + task.id + ' is running'}>
                    <div className="progress-bar progress-bar-striped progress-bar-animated"></div>
                </div>
            </div>);
    } else if (task.queuepos != null) {
        // the position as a chip rather than inside the sentence, so it can be found down a column
        // of rows. queuepos counts the tasks ahead, so zero is the one that runs next.
        const ahead = task.queuepos == 0 ? 'next' : task.queuepos + ' ahead';
        // Only when the server gave enough to compute one -- a stale runner, or a request type it
        // has too few recent samples of, produces null and nothing is shown. An absent estimate is
        // a smaller failure than an invented one, which is the number the user plans around.
        const estimate = props.waitestimate != null
            ? <>{' '}<span className="taskestimate">{props.waitestimate}<span className="visually-hidden"> estimated wait</span></span></>
            : null;
        taskbox.push(
            <div key="status" className="taskstatus waiting">
                Waiting <span className="badge taskposition">{ahead}<span className="visually-hidden"> in the queue</span></span>{estimate}
            </div>);
    } else {
        // queuepos is null until the queue has been renumbered, and the sentence used to be
        // rendered with the number simply missing: "Waiting ( tasks ahead of this one)"
        taskbox.push(<div key="status" className="taskstatus waiting">Waiting in queue</div>);
    }

    if (httperror != '') {
        taskbox.push(<p key="httperror" className="errors" role="alert">{httperror}</p>);
    }


    // hasresultfile because there is nothing to plot once the data file has been reclaimed.
    // Without it, every expired task reserved a 300px-tall empty box in its row and fetched
    // a resultplotdata.js that the server answers with an empty body.
    if (task.finishtimestamp != null && task.error_msg == null && task.request_type == 'FP'
        && hasresultfile && !props.hidePlot) {
        taskbox.push(<TaskPlot key='plot' taskid={task.id} taskurl={task.url} />);
    }

    return (
        // the inner element is what the row's show and hide animates over: the row is a one-track
        // grid, and a grid item can be given a height smaller than its content while this cannot
        <li key={"task-" + task.id} className={"task " + statusclass} id={"task-" + task.id}>
            <div className="taskinner">{taskbox}</div>
        </li>
    );
}, taskPropsEqual);

let tasklist_api_request_active = false;
// set when a refresh was requested while a request was already in flight; the settled request
// re-fetches so the refresh is delayed rather than lost
let tasklist_refresh_queued = false;
const tasklist_fetchcache = {};
// remembers the ETag of each polled page so that an unchanged page can be answered with a 304
const tasklist_pollcache = new PollCache();
// counts history navigations, so that a response can tell whether one happened while it was in
// flight. Only the scroll depends on this: a forward navigation asks to be taken to the top of
// the new page, but if the user pressed Back or Forward in the meantime the browser has since
// restored a scroll position of its own, and jumping to the top would throw it away.
let historynavigations = 0;

/*
 * The status of the task runner, as the shared poll last reported it.
 *
 * TaskPage holds this status, and no smaller component holds it, because the wait estimates read
 * this response. They read numslots, distinct_queued_users, slots_busy and the typical run times.
 * runnerstatus.js draws the sentence about the task runner in the box above the content, on each
 * page of the site. This page reads the same store, and thus the two share one request each minute.
 */
function useRunnerStatus() {
    const [status, setStatus] = React.useState(null);

    // subscribe() returns the function that cancels the subscription, which this effect must call
    React.useEffect(() => subscribe(setStatus), []);

    return status;
}

const Pager = React.memo(function Pager({ previous, next, taskcount, pagefirsttaskposition, pagetaskcount, updateCursor }) {
    debug_log('Pager rendered');
    if (taskcount == null) {
        return null;
    }

    // the cursors were copied into state by getDerivedStateFromProps, which is only ever a way of
    // keeping a second copy of a prop in step with the first. They are derived during render now.
    const cursorFrom = (url) => (url != null ? new URL(url).searchParams.get('cursor') : null);

    return (
        <div id="paginator" key="paginator">
            <p key="pagedescription">Showing tasks {pagefirsttaskposition + 1}-{pagefirsttaskposition + pagetaskcount} of {taskcount}</p>
            {/* buttons, not links: these run JavaScript rather than navigating, and an <a>
                with no href is not focusable, so the pager could not be reached by keyboard.
                .pagination rather than Bootstrap 3's .pager, which Bootstrap 5 dropped: .page-link
                styles a <button> as readily as a link, so the hand-written rules that used to
                stand in for the missing component are gone from main.css. */}
            <ul key="prevnext" className="pagination">
                {previous != null ? <li key="previous" className="page-item pageprev"><button type="button" className="page-link" onClick={() => updateCursor(cursorFrom(previous))}>&laquo; Newer</button></li> : null}
                {next != null ? <li key="next" className="page-item pagenext"><button type="button" className="page-link" onClick={() => updateCursor(cursorFrom(next))}>Older &raquo;</button></li> : null}
            </ul>
        </div>
    );
});

export function TaskPage() {
    // One object rather than a useState per field: several updates below set three or four of
    // these together, and as separate states each would be its own render.
    const [state, setStateRaw] = React.useState({
        taskcount: null,
        results: null,
        next: null,
        previous: null,
        pagefirsttaskposition: null,
        scrollToTopAfterUpdate: false,
        dataurl: window.location.href,
        tasklist_last_fetch_time: null,
        // component state, not a module variable: setting a module variable from the failure
        // handler changed nothing on screen, because nothing re-rendered. By the time a later
        // poll did render, the variable had already been cleared, so a connection problem was
        // never actually shown to anyone.
        tasklist_api_error: '',
        // how many of the user's tasks have left the queue since the tab was last looked at; see
        // countFinishedWhileAway and pageTitle
        finishedwhileaway: 0,
        /*
         * All the user's queued tasks as {position, requesttype}, ascending by position, or null
         * until the queue positions endpoint has answered. The type is there because these are
         * waited through one at a time, so what each of them is decides how long it takes.
         *
         * Null rather than an empty array, which would mean "this user has nothing else queued" --
         * a different answer, and the one that makes a bulk submitter's estimate wrong by up to the
         * slot count. The rows are on screen before the first positions response lands, so the
         * distinction is visible on every page load. See estimateWaitSeconds.
         *
         * State rather than a ref, unlike queuedIdsRef below, because the wait estimates are
         * rendered from it. It is the whole queued set and not the page on screen: a user with
         * forty queued tasks sees six of them, and it is the other thirty-four that their last
         * task is waiting behind.
         */
        ownqueued: null,
    });

    // The wait estimates read this status, and the site notice box reads it from the same store.
    const runnerstatus = useRunnerStatus();

    /*
     * The current state, readable from asynchronous code.
     *
     * In the class this was `this.state`, which always reads the latest values. A function
     * component's closures capture the values from the render that created them, so a fetch
     * callback, or a poll started once on mount, would otherwise compare against whatever the
     * state was when it was set up -- the class's behaviour has to be reproduced deliberately.
     *
     * Writes go through setState() below, which keeps the two in step. Nothing renders from the
     * ref; it exists only for the asynchronous readers.
     */
    const stateRef = React.useRef(state);

    /*
     * The ids of the user's queued tasks, as the queuepositions endpoint last reported them.
     *
     * That response is the user's whole queued set, which the rendered rows are not: the task list is
     * paginated, and can be filtered or showing a single task, so anything counted from state.results
     * would be counted from a slice. countFinishedWhileAway measures against this, and the navbar
     * badge is drawn from its length.
     *
     * A ref rather than state because nothing renders from it -- the two readers are an interval and a
     * DOM call -- and it must be readable from asynchronous code, like stateRef above.
     */
    const queuedIdsRef = React.useRef(null);

    /*
     * Task ids a queuepositions request has already been made on behalf of, so that a row arriving with
     * a queue position the set above has never heard of prompts exactly one -- see the call site in
     * fetchData, where asking repeatedly would trade requests with the poll for as long as the row is
     * on screen. Ids rather than a count, because which ones have been asked about is the whole point.
     */
    const askedQueuedIdsRef = React.useRef(new Set());

    /*
     * Counts queuepositions requests, so that a response can tell whether it is still the newest one.
     * The poll is on a two-second interval with nothing serialising it, so a request slower than that
     * overlaps the next; without this the older answer landing second would write its obsolete snapshot
     * over the newer one -- rewinding the positions on screen, the navbar badge, and the set the away
     * count measures against, which is the one that matters, since it is then preserved for the whole
     * of the next absence.
     */
    const queueposRequestRef = React.useRef(0);

    /*
     * The same for the away poll, which asks the same endpoint on its own interval and so has the same
     * hazard: a request outliving the minute it was made in overlaps the next, and the older answer
     * landing second would lower the count back to what the queue looked like before -- taking a
     * completion out of the title that the newer answer had just put in, until the tick after. A
     * separate counter, because these are a separate stream of requests: sharing one would have each
     * poll discarding the other's answers.
     */
    const awayRequestRef = React.useRef(0);

    /* Ticks skipped since the queue was last reported empty; see EMPTY_QUEUE_TICKS. */
    const emptyticksRef = React.useRef(0);

    /*
     * The ref is the authority, and is updated before setStateRaw is called.
     *
     * The merge deliberately does not happen inside a setStateRaw(previous => ...) updater: React
     * runs an updater during a later render, not at call time, so a caller that set state and then
     * read the ref got the value from before its own write. fetchQueuePositions does exactly that
     * when it patches the caches, and was writing the pre-update rows back.
     *
     * Computing here instead also keeps the updater out of it entirely, so nothing impure runs
     * during render, and consecutive calls in one tick accumulate because each reads the ref the
     * previous one just wrote.
     */
    const setState = React.useCallback((changes) => {
        const previous = stateRef.current;
        const resolved = typeof changes === 'function' ? changes(previous) : changes;
        if (resolved == null) {
            return;
        }

        stateRef.current = { ...previous, ...resolved };
        setStateRaw(stateRef.current);
    }, []);

    function singleTaskViewTaskId(strurl) {
        const pathext = strurl.toString().replace(
            api_url_base.toString(), '').split('/').filter(el => { return el.length != 0 });

        if (pathext.length == 1 && !isNaN(pathext[0])) {
            return parseInt(pathext[0]);
        } else {
            return null;
        }
    }

    function filterIsActive(filtername, strurl) {
        const started = new URL(strurl).searchParams.get('started');
        if (filtername == null) {
            // strurl, not the ref: both callers pass state.dataurl, and reading the ref here
            // instead meant the two halves of one answer could come from different renders
            return started == null && singleTaskViewTaskId(strurl) == null;
        }
        return filtername == 'started' && started == 'true';
    }

    function filterclass(filtername, strurl) {
        return filterIsActive(filtername, strurl) ? 'btn-primary' : 'btn-link';
    }

    /**
     * Refresh just the queue positions of this user's unfinished tasks.
     *
     * Cheap enough to run on the short interval: two indexed queries and a few hundred bytes,
     * against a page fetch, a prefetch and a full serialisation for the task list. A task that is
     * no longer listed here has finished (or been deleted), which is the transition worth reacting
     * to immediately, so that case falls back to a full fetch straight away.
     */
    const fetchQueuePositions = React.useCallback(() => {
        // no pollingPaused() check: see pollInterval()
        if (stateRef.current.results == null) {
            return;
        }

        /*
         * Skipped only when the last answer said the queue is empty and no row on screen could change
         * that. Anything else -- including not having asked yet -- is worth a request.
         *
         * The response drives the navbar badge and the set the away count measures against, and
         * neither of those is about what is on screen, so gating on the rows was wrong in two ways.
         * Sitting on a view whose rows are all finished (a single task, the Running/Finished filter,
         * an older page) stopped the poll while the user still had tasks running; and opening such a
         * view directly stopped it before it had ever run, leaving the badge to go stale and the away
         * count with no baseline at all.
         *
         * So an unknown queue is asked about once. That costs one cheap request per page load for a
         * user with nothing queued, after which the answer is "empty" and the poll stops until a
         * submission puts a trackable row on screen.
         */
        const queued = queuedIdsRef.current;
        const knownempty = queued != null && queued.length == 0;
        if (knownempty && !stateRef.current.results.some(tracksQueuePosition)) {
            emptyticksRef.current += 1;
            if (emptyticksRef.current % EMPTY_QUEUE_TICKS != 0) {
                return;
            }
        } else {
            emptyticksRef.current = 0;
        }

        const requestnumber = ++queueposRequestRef.current;

        fetch(queuepositions_url,
            {
                credentials: "same-origin",
                headers: { 'Accept': 'application/json' },
                cache: "no-store",
            })
            .then(response => response.status == 200 ? response.json() : null)
            .then(data => {
                // A newer request has been made since this one started, so this answer is already
                // out of date and every write below it would be a rewind. Dropped rather than
                // merged: the newer request is asking the same question of the same endpoint, so
                // there is nothing here it will not also say, and nothing is lost by waiting for it.
                if (requestnumber != queueposRequestRef.current) {
                    debug_log('discarding a queue positions response overtaken by a later request');
                    return;
                }
                if (data == null || data.queuepositions == null || stateRef.current.results == null) {
                    return;
                }

                // The user's whole queued set, which is what this endpoint answers with -- not the
                // page of rows on screen. Recorded for countFinishedWhileAway to measure against and
                // used for the navbar badge, both of which want the total rather than the page.
                const queuedids = Object.keys(data.queuepositions);

                /*
                 * Not while hidden, once there is a baseline to protect. This request can have been in
                 * flight as the tab was left, and a task that finished during that round trip is
                 * already missing from the answer -- so taking it would hide exactly the completion the
                 * away count exists to report.
                 *
                 * Unless there is no baseline yet, which has nothing to lose: a slightly late answer is
                 * the difference between counting and never counting at all.
                 *
                 * The badge is written either way; nothing is lost by it being right early.
                 *
                 * What a late answer must not do is take a task *out* of the set, for the reason above.
                 * Putting one in is safe in a way that is worth using: a task this answer still lists is
                 * queued as of the moment it was taken, so counting it later can only ever report a
                 * completion, never conceal one. Which closes the last hole in "submit, then leave" --
                 * the request the new row asks for can itself be in flight as the tab goes, and dropping
                 * it whole left the count measuring against a set from before the submission, missing
                 * the one task the user was waiting on.
                 */
                if (!document[hidden] || queuedIdsRef.current == null) {
                    queuedIdsRef.current = queuedids;
                } else {
                    const alreadyknown = new Set(queuedIdsRef.current);
                    const added = queuedids.filter(taskid => !alreadyknown.has(taskid));
                    if (added.length > 0) {
                        queuedIdsRef.current = queuedIdsRef.current.concat(added);
                    }
                }
                updateQueueBadge(queuedids.length);

                // tested against state as it stands rather than the committed state: a false
                // positive costs one extra full fetch and a false negative is caught on the next
                // tick, so it does not need to be exact
                if (stateRef.current.results.some(
                    task => tracksQueuePosition(task) && !(String(task.id) in data.queuepositions))) {
                    debug_log('a task left the queue: fetching the full task list');
                    // the pause is re-checked because it can have begun during this request's
                    // round-trip, and the full fetch is exactly the work it exists to skip.
                    // Nothing is lost: the next unpaused tick repeats this comparison.
                    if (!pollingPaused()) {
                        fetchData(false);
                    }
                    return;
                }

                const geturl = window.location.href;

                // functional, because the two polls are phase-locked (6000 is a multiple of 2000):
                // a task list response landing in the same batch would otherwise be overwritten
                // with the rows this request was started with, and its ETag has already been
                // recorded, so the next poll would 304 and never resend them.
                //
                // tasklist_last_fetch_time and tasklist_api_error are deliberately NOT touched
                // here: this endpoint says nothing about whether the task list is reachable, and
                // stamping them would report a stale page as current.
                // sorted so that the elementwise comparison below is meaningful -- the estimate
                // itself counts positions below a given one and does not care about the order
                // sorted so that the elementwise comparison below is meaningful; the estimate
                // itself filters on position and does not care about the order
                const ownqueued = Object.entries(data.queuepositions)
                    .map(([taskid, position]) => ({ position, requesttype: data.queuedtypes?.[taskid] }))
                    .sort((a, b) => a.position - b.position);

                setState(prevstate => {
                    if (prevstate.results == null) {
                        return null;
                    }

                    let changed = false;
                    const newresults = prevstate.results.map(task => {
                        const queuepos = data.queuepositions[String(task.id)];
                        if (queuepos === undefined || queuepos === task.queuepos || task.finishtimestamp != null) {
                            return task;
                        }
                        changed = true;
                        return { ...task, queuepos: queuepos };
                    });

                    // compared rather than assigned, for the reason the rows above are: this runs
                    // every two seconds, and handing back a fresh array each time would re-render
                    // the page on every tick of a queue that has not moved. A null previous is the
                    // first answer, which is always a change.
                    const previousqueued = prevstate.ownqueued;
                    const positionschanged = (
                        previousqueued == null
                        || previousqueued.length != ownqueued.length
                        || ownqueued.some((task, index) => task.position !== previousqueued[index].position
                            || task.requesttype !== previousqueued[index].requesttype));

                    if (!changed && !positionschanged) {
                        return null;
                    }

                    // handing back the previous reference for the half that did not move preserves
                    // its identity exactly as omitting the key would, and setState merges either way
                    return {
                        results: changed ? newresults : prevstate.results,
                        ownqueued: positionschanged ? ownqueued : prevstate.ownqueued,
                    };
                });

                // was the setState callback: the caches have to move with the state, or a
                // user-triggered fetch (a filter, the pager, a delete) re-applies the body held
                // here and visibly rewinds the positions that were just corrected. setState
                // updates stateRef before it returns, so the new results are readable here.
                const cached = tasklist_fetchcache[geturl];
                if (cached != null && cached.results != null && geturl == window.location.href) {
                    const patched = { ...cached, results: stateRef.current.results };
                    tasklist_fetchcache[geturl] = patched;
                    tasklist_pollcache.storeBody(geturl, patched);
                }
            })
            .catch(error => {
                // the full task list poll is what reports a connection problem; a failure here just
                // means the positions are refreshed a few seconds later than they might have been
                debug_log('Queue positions request failed', error);
            });
    }, [setState]);

    /*
     * Count the user's tasks that have left the queue while the tab has not been looked at, for the
     * tab title to report.
     *
     * Runs only while hidden, which is exactly when nothing else runs, and asks the cheap endpoint
     * rather than re-fetching the list: what is wanted is a number, not the rows.
     *
     * Measured against queuedIdsRef -- the user's whole queued set as last seen while the tab was
     * being watched -- rather than the rows on screen, which are one page of a paginated list and
     * could be a filtered one or a single task.
     *
     * The count is recomputed from scratch each time rather than accumulated, and that is what makes
     * it safe to repeat: nothing writes that set while the tab is hidden, so "how many of those are no
     * longer in the queue" is the same answer however many times it is asked. Accumulating would
     * count each finished task again on every tick.
     *
     * What is counted is "left the queue", which a cancelled task does as surely as a completed one:
     * this endpoint answers with positions, so there is nothing in the response to tell the two apart.
     * Cancelling from a second tab, or over the API, therefore shows up in the title as a completion.
     * Accepted rather than fixed -- telling them apart means fetching the rows, which is the cost this
     * whole path exists to avoid, to correct a hint that the tab is about to be told the truth by the
     * poll that resumes the moment it is looked at again.
     */
    const countFinishedWhileAway = React.useCallback(() => {
        const waiting = queuedIdsRef.current;
        if (!document[hidden] || waiting == null || waiting.length == 0) {
            return;
        }

        const requestnumber = ++awayRequestRef.current;

        fetch(queuepositions_url,
            {
                credentials: "same-origin",
                headers: { 'Accept': 'application/json' },
                cache: "no-store",
            })
            .then(response => response.status == 200 ? response.json() : null)
            .then(data => {
                // overtaken by a later away poll, so this is the older of two answers about the same
                // queue; see awayRequestRef
                if (requestnumber != awayRequestRef.current) {
                    debug_log('discarding an away count response overtaken by a later request');
                    return;
                }
                // hidden is re-checked because the tab can have been looked at again during this
                // request's round-trip, the same hazard fetchQueuePositions re-checks the pause for.
                // Without it a response landing just after the user came back would put a count in
                // the title of a tab they are looking at, and handleVisibilityChange has already run
                // and will not run again until the next time they leave and return.
                if (!document[hidden] || data == null || data.queuepositions == null) {
                    return;
                }

                const finished = waiting.filter(taskid => !(taskid in data.queuepositions)).length;
                if (finished != stateRef.current.finishedwhileaway) {
                    setState({ finishedwhileaway: finished });
                }
            })
            .catch(error => debug_log('Away queue positions request failed', error));
    }, [setState]);

    /*
     * Looking at the tab again clears the count -- the polls resume on the same event and will put the
     * real state on screen, which is a better answer than a number in the title -- and takes a fresh
     * set for the next absence to be measured against.
     *
     * Without that refresh the set still holds whatever finished during the absence just ended, so
     * leaving again before the two-second poll had replaced it counted those same tasks a second time
     * and reported a completion that had already been reported.
     *
     * The set is dropped rather than waited on, because the request that replaces it can be in flight
     * when the tab is left again, and a late answer is only allowed to add to a set that exists -- so
     * the tasks reported during the absence just ended would have stayed in it and been reported again.
     * With no set there is nothing to measure against until an answer arrives, and the count says
     * nothing; whichever answer lands first establishes it, hidden or not, for the same reason the
     * write site takes a late one when there is no baseline at all. Silence for one absence is the
     * right way to be wrong here -- the alternative repeats a completion the user has already seen.
     */
    const handleVisibilityChange = React.useCallback(() => {
        if (document[hidden]) {
            return;
        }

        if (stateRef.current.finishedwhileaway != 0) {
            setState({ finishedwhileaway: 0 });
        }
        queuedIdsRef.current = null;
        fetchQueuePositions();
    }, [setState, fetchQueuePositions]);

    const fetchData = React.useCallback((usertriggered, scrolltotop = usertriggered) => {
        // no pollingPaused() check here either, for the reason given on pollInterval().
        // scrolltotop is separate from usertriggered because of history navigations: they are
        // user-triggered (the held copy of the destination page must apply immediately), but
        // the browser restores the old scroll position itself and a scroll to top would fight it.
        // read now, compared at the apply site below: a response is only allowed to scroll to the
        // top if no Back or Forward happened while it was in flight
        const navigationsatstart = historynavigations;

        setState({ dataurl: window.location.href });

        // start by applying a cached version if we have it
        // then send out an HTTP request and update when available
        if (usertriggered) {
            const tasklist_fetchcachematch = (window.location.href in tasklist_fetchcache);
            if (tasklist_fetchcachematch) {
                debug_log('using tasklist_fetchcache before GET response', window.location.href);
                setState(tasklist_fetchcache[window.location.href]);
            } else {
                debug_log('no tasklist_fetchcache for', window.location.href);
            }
        }

        if (tasklist_api_request_active && !usertriggered) {
            // queued rather than dropped: a refresh requested here (e.g. by deleteTask, whose
            // response raced the poll) would otherwise be swallowed, and the in-flight response
            // was serialised before whatever prompted it, so its data is already stale
            debug_log('queueing refresh behind the in-flight GET request');
            tasklist_refresh_queued = true;
            return;
        }

        tasklist_api_request_active = true;
        const get_url = window.location.href;
        debug_log('Fetching task list from', get_url);
        const request_headers = tasklist_pollcache.requestHeaders(get_url, {
            ...csrfHeader(),
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        });
        fetch(get_url,
            {
                credentials: "same-origin",
                method: "GET",
                headers: request_headers,
                // the browser HTTP cache would answer from its own copy and hide the 304 handling
                cache: "no-store",
                redirect: "manual"
            })
            .then((response) => {
                tasklist_api_request_active = false;
                if (tasklist_pollcache.noteResponse(get_url, response.status, response.headers.get('ETag'))
                    === NOT_MODIFIED) {
                    debug_log('Task list unchanged (304)', get_url);
                    setState({ tasklist_api_error: '' });
                    return NOT_MODIFIED;
                }
                if (response.type === "opaqueredirect") {
                    // redirect to login page
                    window.location.href = response.url;
                    console.log('Fetch got a redirection to ', response.url);
                } else {
                    if (response.status != 200) {
                        console.log("Fetch received HTTP status ", response.status);
                    }
                    if (response.status == 401 || response.status == 403) {
                        // the session has gone (expired, or logged out in another tab). The server
                        // answers a JSON request with a real status rather than a redirect, so
                        // nothing navigates on our behalf; without this the page would sit here
                        // showing the pre-logout task list forever, with no error and no way back.
                        setState({ tasklist_api_error: 'Your session has ended. Reloading to sign in again…' });
                        window.location.reload();
                        return null;
                    }
                    if (response.status == 404) {
                        // a handled case (the viewed task was deleted), not a server error: without
                        // this return it fell through to the message below and flashed
                        // "Server error (HTTP 404)" during a perfectly normal navigation
                        window.history.pushState({}, document.title, api_url_base);
                        setState({ scrollToTopAfterUpdate: true });
                        fetchData(true);
                        return null;
                    }
                    if (response.status == 200) {
                        setState({ tasklist_api_error: '' });
                        return response.json();
                    }
                    // cleared only for a response we could actually use: clearing it up front meant
                    // a 500 on every poll left the page looking healthy and merely frozen
                    setState({ tasklist_api_error: 'Server error (HTTP ' + response.status + ')' });
                }
                return null;
            }).catch(error => {
                tasklist_api_request_active = false;
                console.log('Get task list HTTP request failed', error);
                // in state, so that this actually reaches the screen. The "last updated" time is
                // deliberately left where it was: it is what tells the user how stale the page is.
                setState({ tasklist_api_error: 'Connection error' });
            }).then(data => {
                if (tasklist_refresh_queued && !tasklist_api_request_active) {
                    // something asked for a refresh while this request was in flight; this
                    // response predates whatever prompted that, so fetch again right away.
                    // The pause is re-checked at execution time (the tab can have been hidden
                    // since the refresh was queued); a dropped refresh is covered by the first
                    // unpaused poll.
                    tasklist_refresh_queued = false;
                    setTimeout(() => { if (!pollingPaused()) { fetchData(false); } }, 0);
                }
                let statechanges = null;
                if (data === NOT_MODIFIED) {
                    // nothing changed server-side, but the poll did succeed, so the "last updated"
                    // line must still advance or the page looks stalled.
                    //
                    // The held copy is re-applied rather than assumed to be what is on screen:
                    // "unchanged" is a statement about the server's page, not about this
                    // component's results, and the two come apart whenever a navigation replaced
                    // results locally (setSingleTaskView filters the list down to one task). Going
                    // Back from a single task then left the queue page showing that one task with
                    // no paginator, and no poll could ever repair it, because every one of them
                    // answered 304 for a URL whose ETag really had not changed.
                    //
                    // Guarded like the 200 path below: if the user navigated while this request
                    // was in flight, this body belongs to the page they have already left.
                    const held = tasklist_pollcache.getBody(get_url);
                    const restore = (held != null && get_url == window.location.href
                        && stateRef.current.results !== held.results) ? held : null;
                    setState({ ...restore, tasklist_last_fetch_time: new Date() });
                    return;
                }
                if (data != null && data.hasOwnProperty('results')) {
                    if (data.results.length == 0 && new URL(window.location.href).searchParams.get('cursor') != null) {
                        // page is empty. redirect to main page
                        updateCursorRef.current(null);
                    } else {
                        statechanges = data;
                    }
                } else if (data != null && data.hasOwnProperty('id')) {
                    // single task view doesn't put task data inside 'results' list,
                    // so we create a single-item results list
                    statechanges = {
                        results: [data],
                        next: null,
                        previous: null,
                        pagefirsttaskposition: null,
                        taskcount: null,
                    };
                }
                if (statechanges != null) {
                    statechanges['tasklist_last_fetch_time'] = new Date();
                    // keyed off get_url, not window.location.href: if the user navigated while
                    // this request was in flight those differ, and storing the old page's body
                    // under the new page's key would both show the wrong tasks on a later revisit
                    // and let an If-None-Match be sent for a page we do not actually hold
                    tasklist_fetchcache[get_url] = statechanges;
                    // an If-None-Match is only worth sending once a rendered copy exists to fall
                    // back on, since a 304 carries no body
                    tasklist_pollcache.storeBody(get_url, statechanges);
                    if (get_url == window.location.href) {
                        debug_log('Applying results from', get_url);
                        // the flag goes on a copy: statechanges was just stored in both caches,
                        // and setting it on the shared object polluted every later re-application
                        // of the cached body — the eager pre-request restore and the 304 fallback
                        // would scroll an untouched page to the top on a routine poll.
                        //
                        // The counter check is what stops a click's response, resolving after the
                        // user has pressed Back and Forward again onto the same URL, from
                        // discarding the scroll position the browser has just restored.
                        setState(scrolltotop && navigationsatstart == historynavigations
                            ? { ...statechanges, scrollToTopAfterUpdate: true } : statechanges);

                        /*
                         * The rows have arrived, so ask what the whole queue is once, rather than
                         * waiting for the two-second poll to come round.
                         *
                         * Somebody who submits a task and immediately switches tabs -- which is what
                         * waiting for one looks like -- can leave before that first poll has run, and
                         * from then on it is skipped because the tab is hidden. The away count then
                         * has nothing to measure against and never reports anything for that visit.
                         * fetchQueuePositions needs the rows to exist, which is why the call is here
                         * and not beside the fetchData(true) on mount. Named directly rather than
                         * through a ref, as updateCursor below has to be: it is declared above this
                         * one, so it is initialised by the time this body runs.
                         *
                         * Asked for whenever a queued row is one that has not been asked about yet, not
                         * only when there is no set at all: the same "submit, then leave" gap opens on
                         * every later submission, where the set exists but predates the new task, and
                         * the task the user is waiting for is precisely the one missing from it.
                         *
                         * Counted per task id, and only ever once each, rather than by comparing the
                         * rows against the set. Comparing loops: a trackable row the response does not
                         * mention already makes the poll below call fetchData, so "ask again whenever a
                         * queued row is missing from the set" and "fetch the list whenever a queued row
                         * is missing from the response" feed each other for as long as such a row is on
                         * screen -- two requests per round, as fast as the network allows. Asking once
                         * per id cannot: the second round finds nothing new to ask about.
                         */
                        const askedabout = askedQueuedIdsRef.current;
                        const unasked = statechanges.results.filter(
                            task => tracksQueuePosition(task) && !askedabout.has(String(task.id)));
                        if (queuedIdsRef.current == null || unasked.length > 0) {
                            unasked.forEach(task => askedabout.add(String(task.id)));
                            fetchQueuePositions();
                        }
                    } else {
                        debug_log('Not applying results from', get_url, 'location.href', window.location.href);
                        return;
                    }
                }
            });
    }, [setState]);

    /*
     * fetchData and updateCursor call each other. Both are useCallbacks whose only dependency is
     * the stable setState, so each has one identity for the component's lifetime and can simply be
     * named -- fetchData names itself, and updateCursor below names fetchData, because a reference
     * inside a function body resolves when it is called rather than when it is defined.
     *
     * The one exception is the other direction: fetchData reaches updateCursor, which is declared
     * after it, so that call goes through a ref.
     */
    const updateCursor = React.useCallback((new_cursor) => {
        if (new_cursor == new URL(window.location.href).searchParams.get('cursor')) {
            return;
        }
        debug_log('Task list cursor changed to ', new_cursor);

        const new_page_url = new URL(window.location.href);
        if (new_cursor != null) {
            new_page_url.searchParams.set('cursor', new_cursor);
        } else {
            new_page_url.searchParams.delete('cursor');
        }
        new_page_url.searchParams.delete('format');

        window.history.pushState({}, document.title, new_page_url);

        // was a setState callback. The order does not matter: fetchData reads the URL from
        // window.location, which pushState has already changed, not from state.
        setState({ scrollToTopAfterUpdate: true });
        fetchData(true);
    }, [setState]);

    const updateCursorRef = React.useRef(updateCursor);
    updateCursorRef.current = updateCursor;

    function setFilter(filtername) {
        debug_log('changed filter to', filtername);
        const new_page_url = new URL(api_url_base);
        new_page_url.search = '';
        if (filtername != null) {
            new_page_url.searchParams.set(filtername, true);
        }

        if (new_page_url != window.location.href) {
            window.history.pushState({}, document.title, new_page_url);
            const statechanges = { 'scrollToTopAfterUpdate': true, dataurl: new_page_url };
            if (filtername == 'started' && stateRef.current.results != null) {
                statechanges['results'] = stateRef.current.results.filter(task => { return task.starttimestamp != null });
                if (statechanges['results'].length == 0) {
                    // prevent flash of "there are no results" for empty ([] non-null) results list
                    statechanges['results'] = null;
                }
            }
            setState(statechanges);
            fetchData(true);
        }
    }

    const setSingleTaskView = React.useCallback((event, task_id, task_url) => {
        if (event.ctrlKey || event.metaKey || event.shiftKey) {
            return; // let the browser deal with the click natively
        }
        event.preventDefault();
        const new_page_url = api_url_base + task_id + '/';
        window.history.pushState({}, document.title, new_page_url);

        debug_log('Task list changed to single task view for ', new_page_url.toString());

        let newresults = stateRef.current.results.filter(task => { return task.id == task_id });
        if (newresults.length == 0) {
            newresults = null;  // prevent flash of "there are no results" for empty (non-null) results list
        }
        setState({
            results: newresults,
            scrollToTopAfterUpdate: true,
            next: null,
            previous: null,
            pagefirsttaskposition: null,
            taskcount: null,
        });
        fetchData(true);
    }, [setState]);

    /**
     * Re-read the URL after the browser moved through history.
     *
     * Every client-side navigation here (a task, a filter, a page) is a pushState, so Back and
     * Forward change the URL without React hearing about it. dataurl decides the heading, the
     * active filter and whether the new request form is shown, so leaving it behind showed the
     * wrong page entirely.
     */
    React.useEffect(() => {
        function handlePopState() {
            debug_log('History navigation to', window.location.href);
            // No guard comparing state.dataurl against the new URL: dataurl is written by
            // fetchData's asynchronous setState, so a quick Back could find it still equal to the
            // destination and be dropped entirely. A redundant fetch on a spurious popstate is
            // harmless by comparison.
            //
            // The pagination fields are nulled the way setSingleTaskView() nulls them (the Pager
            // only hides itself when taskcount is null): a Forward onto a task detail page that is
            // not held in the cache would otherwise keep the list's "Showing tasks 1-6 of N"
            // paginator, with live page buttons, above a single task until the response lands.
            //
            // fetchData(true, false): user-triggered, so the held copy of the destination page
            // (when there is one) goes up straight away — but no scroll to top, because for a
            // history navigation the browser restores the previous scroll position itself, and
            // Back from a task should land on the list row the user came from.
            //
            // Suppressing the scroll takes all three of these, because it can be asked for from
            // three different points in time: scrollToTopAfterUpdate clears a request already
            // waiting to be consumed by the update effect, the counter stops one arriving later
            // from a fetch that was already in flight (see the apply site in fetchData), and the
            // false argument covers this navigation's own fetch.
            historynavigations += 1;
            setState({
                next: null,
                previous: null,
                pagefirsttaskposition: null,
                taskcount: null,
                scrollToTopAfterUpdate: false,
            });
            fetchData(true, false);
        }

        const fetchinterval = pollInterval(() => fetchData(false), TASKLIST_POLL_MS);
        const queueposinterval = pollInterval(fetchQueuePositions, QUEUEPOS_POLL_MS);
        const awayinterval = setInterval(countFinishedWhileAway, AWAY_POLL_MS);
        document.addEventListener('visibilitychange', handleVisibilityChange);
        window.addEventListener('popstate', handlePopState);
        fetchData(true);

        return () => {
            clearInterval(fetchinterval);
            clearInterval(queueposinterval);
            clearInterval(awayinterval);
            document.removeEventListener('visibilitychange', handleVisibilityChange);
            window.removeEventListener('popstate', handlePopState);
        };
        // mount only, like the componentDidMount this replaces: the callbacks are reached through
        // refs, so this does not need to re-run when they change
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    // was componentDidUpdate
    React.useEffect(() => {
        // the tab title used to keep saying "Task Queue" after a client-side navigation, because
        // every pushState passed the title it already had. Derived from dataurl here so that it is
        // right no matter which navigation path got us here.
        const title = pageTitle(singleTaskViewTaskId(state.dataurl), state.finishedwhileaway);
        if (document.title != title) {
            document.title = title;
        }

        if (state.scrollToTopAfterUpdate) {
            setState({ scrollToTopAfterUpdate: false });
            window.scrollTo(0, 0);
            window.dispatchEvent(new Event('resize'));
        }
    });

    const singletaskmode = singleTaskViewTaskId(state.dataurl) != null;
    let pagehtml = [];
    if (!singletaskmode) {
        pagehtml.push(<div key="header" className="page-header"><h1>Task Queue</h1></div>);
    } else {
        pagehtml.push(<div key="header" className="page-header"><h1>Task {singleTaskViewTaskId(state.dataurl)}</h1></div>);
    }

    if (!singletaskmode || (state.results != null && state.results.length > 0 && state.results[0].user_id == user_id)) {
        // buttons rather than <a> without an href, which is not focusable and so could not be
        // reached by keyboard at all. aria-pressed is what makes the selected one announceable.
        pagehtml.push(
            <ul key="filters" id="taskfilters">
                <li key="all"><button type="button" onClick={() => setFilter(null)} aria-pressed={filterIsActive(null, state.dataurl)} className={'btn ' + filterclass(null, state.dataurl)}>All tasks</button></li>
                <li key="started"><button type="button" onClick={() => setFilter('started')} aria-pressed={filterIsActive('started', state.dataurl)} className={'btn ' + filterclass('started', state.dataurl)}>Running/Finished</button></li>
            </ul>);
    }

    if (state.tasklist_last_fetch_time != null) {
        pagehtml.push(<p key="tasklistfetchstatus" id='tasklistfetchstatus'>Last updated: {state.tasklist_last_fetch_time.toLocaleString()} <span className="errors">{state.tasklist_api_error}</span></p>);
    }

    if (!singletaskmode) {
        const allow_stack_rock = new URL(state.dataurl).searchParams.get('allow_stack_rock') == 'true';

        pagehtml.push(<NewRequest key="newrequest" fetchData={fetchData} allow_stack_rock={allow_stack_rock} />);
    }

    let tasklist;
    if (state.results == null) {
        // Rows the size of the ones about to arrive, rather than a line of text the list then shoves
        // out of the way. aria-hidden on the shapes and the message left as text, so what a screen
        // reader gets is "Loading tasks" and not a description of three empty boxes.
        tasklist = (
            <ul key="ultasklist" className="tasks" aria-busy="true">
                <li key="message" className="visually-hidden" role="status">Loading tasks...</li>
                {[0, 1, 2].map((row) => (
                    // .taskinner as on a real row: li.task carries no padding of its own, because on a
                    // real row the padding belongs inside the grid track that the show and hide
                    // animates, so a placeholder without the wrapper sits flush against its border
                    <li key={'skeleton' + row} className="task taskskeleton" aria-hidden="true">
                        <div className="taskinner">
                            <p className="placeholder-glow"><span className="placeholder col-4"></span></p>
                            <p className="placeholder-glow"><span className="placeholder col-7"></span></p>
                            <p className="placeholder-glow"><span className="placeholder col-6"></span></p>
                        </div>
                    </li>
                ))}
            </ul>);
    } else if (state.results.length == 0) {
        /*
        Two different nothings, which an empty results array does not tell apart on its own.

        Unfiltered, it means the account has no tasks at all: that is what every new account sees
        first, so the message says what to do next rather than only what is absent. The request form
        is beside this on a wide screen and below it on a narrow one, so the sentence names it rather
        than pointing at it.

        Under the Running/Finished filter it means only that nothing has started yet. The queued work
        the user is waiting on is still there, so telling them they have no tasks would be false --
        and inviting them to submit another position invites a duplicate.
        */
        tasklist = filterIsActive('started', state.dataurl) ? (
            <p key="message" className="tasksempty">
                Nothing running or finished yet. Queued tasks appear here once they start.
            </p>) : (
            <p key="message" className="tasksempty">
                No tasks yet. Request a position on the sky and its light curve will appear here.
            </p>);
    } else {
        const pagetaskcount = (state.results != null) ? state.results.length : null;

        // Computed here rather than in the row, which cannot see the user's other queued tasks: the
        // estimate depends on the whole queued set, and a row only knows about itself. Handed down
        // as a string so that the memo comparison in taskPropsEqual stays a primitive check.
        //
        // tracksQueuePosition is the same question this needs answered -- is the queuepositions
        // response about this task? -- so it is reused rather than restated. Without it, somebody
        // else's task (a detail page is public, and staff see every row) would be measured against
        // the viewer's queue, counting the owner's own earlier tasks as other users'.
        const waitEstimateFor = (task) => (!tracksQueuePosition(task) ? null : formatWaitEstimate(estimateWaitSeconds({
            queuepos: task.queuepos,
            ownqueued: state.ownqueued,
            runnerstatus,
        })));

        tasklist = [
            <ul key="ultasklist" className="tasks">
                {state.results.map((task) => (
                    <Task key={task.id} taskdata={task} fetchData={fetchData} setSingleTaskView={setSingleTaskView}
                        hidePlot={pagetaskcount > 10} waitestimate={waitEstimateFor(task)} />))}
            </ul>,
            <Pager key='pager' previous={state.previous} next={state.next} pagefirsttaskposition={state.pagefirsttaskposition} pagetaskcount={pagetaskcount} taskcount={state.taskcount} updateCursor={updateCursor} />
        ];
    }

    pagehtml.push(<div key="tasklist" id="tasklist" className={singletaskmode ? 'singletaskdetail' : null}>{tasklist}</div>);

    return pagehtml;
}


// Guarded so the module can be imported without mounting: the component tests import this file
// for TaskPage, and an unconditional mount ran on import against a container that is not there.
// On the queue page the element always exists.
const container = document.getElementById('taskpage');
if (container) {
    ReactDOM.createRoot(container).render(<TaskPage />);
}
