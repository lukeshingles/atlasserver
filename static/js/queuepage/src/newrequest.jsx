'use strict';

import React from "react"
import { csrfHeader } from "csrftoken";

let submission_in_progress = false;

// how far back the earliest date of a new request defaults to. The reset button names it too.
const DEFAULT_MJD_MIN_DAYS = 30;

function getDefaultMjdMin() {
    return (mjdFromDate(new Date()) - DEFAULT_MJD_MIN_DAYS).toFixed(5);
}

function errortext(value) {
    // DRF error values can be strings, lists of strings, or nested objects (a field validator
    // that raises a dict produces e.g. {"mjd_min": {"mjd_min": "..."}}). Rendering an object as
    // a React child throws and unmounts the page, so flatten everything to a string.
    if (Array.isArray(value)) {
        return value.map(errortext).join(' ');
    }
    if (value != null && typeof value === 'object') {
        return Object.values(value).map(errortext).join(' ');
    }
    return String(value);
}

/*
 * Fields whose value is remembered across page loads, and how to read one back.
 *
 * This was fifteen separate localStorage.getItem calls in get_defaultstate() and a matching setItem
 * in all fifteen onChange handlers. One table, read here and written in setField.
 *
 * `clearedOnSubmit` marks the ones a successful submission forgets: they describe the request that
 * was just made. use_reduced and send_email are not among them -- they are standing preferences,
 * and clearing them turned "email me when completed" back on for someone who had turned it off.
 */
const STORED_FIELDS = {
    radeclist: { fallback: '', clearedOnSubmit: true },
    mjd_min: { fallback: getDefaultMjdMin, clearedOnSubmit: true },
    mjd_max: { fallback: '', clearedOnSubmit: true },
    comment: { fallback: '', clearedOnSubmit: true },
    radec_epoch_year: { fallback: '', clearedOnSubmit: true },
    propermotion_ra: { fallback: 0., clearedOnSubmit: true },
    propermotion_dec: { fallback: 0., clearedOnSubmit: true },
    enable_stack_rock: { fallback: false, clearedOnSubmit: true },
    enable_propermotion: { fallback: false, clearedOnSubmit: true },
    use_reduced: { fallback: false },
    send_email: { fallback: true },
};

/*
 * Every one of the three below is guarded, because localStorage throws rather than answering when
 * a browser is set to refuse site data. These values are a convenience: a visitor who has turned
 * it off gets the defaults on every visit, which is a working form. An exception is not -- the
 * read runs inside the useState initialiser of the first render, so it would propagate out of
 * render, and with no error boundary above it React tears the whole queue page down: no rows, no
 * pager, no form, no message. theme.js and runnerstatus.js guard the same call for the same reason.
 */

/** Remember a field's value for the next visit. localStorage stringifies whatever it is given. */
function storeValue(name, value) {
    try {
        localStorage.setItem(name, value);
    } catch (err) {
        // not carried to the next visit; this one still holds it in React state
    }
}

/** Forget a remembered value, so that the next visit takes the field's default. */
function forgetValue(name) {
    try {
        localStorage.removeItem(name);
    } catch (err) {
        // nothing was stored, so there is nothing to forget
    }
}

function storedValue(name) {
    const field = STORED_FIELDS[name];
    let stored = null;
    try {
        stored = localStorage.getItem(name);
    } catch (err) {
        // read as "nothing remembered", which is what the fallbacks below are for
    }

    // a checkbox round-trips as the string "true"/"false"; which fields those are is already said
    // by the type of their fallback, so it does not need saying twice in the table
    if (typeof field.fallback === 'boolean') {
        return stored == null ? field.fallback : stored === 'true';
    }
    if (stored != null) {
        return stored;
    }

    return typeof field.fallback === 'function' ? field.fallback() : field.fallback;
}

function defaultFormValues() {
    return Object.fromEntries(Object.keys(STORED_FIELDS).map((name) => [name, storedValue(name)]));
}

/** Render an MJD as the ISO timestamp shown under the input. */
function mjdCaption(strmjd, blankcaption) {
    if (strmjd === '') {
        return blankcaption;
    }

    try {
        const isostr = dateFromMJD(parseFloat(strmjd)).toISOString();
        return isostr.includes('.') ? isostr.split('.')[0] + 'Z' : isostr;
    } catch (err) {
        console.log('error', err, err.message);
        return 'error';
    }
}

export function NewRequest({ allow_stack_rock, fetchData }) {
    const [values, setValues] = React.useState(defaultFormValues);
    const [showradechelp, setShowradechelp] = React.useState(false);
    const [errors, setErrors] = React.useState({});
    const [httperror, setHttperror] = React.useState('');
    const [submitting, setSubmitting] = React.useState(false);

    // was one binding per field, each duplicating the setState-then-setItem pair.
    //
    // Written here rather than from an effect on `values`: an effect also runs on mount, which
    // persisted the *defaults* before the user had touched anything -- and mjd_min's default is
    // "30 days ago", so once stored it was read back verbatim on every later visit and stopped
    // moving. Only a field the user actually changed is remembered.
    const setField = (name) => (event) => {
        const input = event.target;
        const value = input.type === 'checkbox' ? input.checked : input.value;
        setValues((previous) => ({ ...previous, [name]: value }));
        storeValue(name, value);
    };

    // the captions are derived from the MJD values, so they are computed during render rather than
    // stored: as state they had to be recomputed by hand after a reset, and were forgotten once
    const mjd_min_isoformat = mjdCaption(values.mjd_min, '(leave blank to fetch earliest)');
    const mjd_max_isoformat = mjdCaption(values.mjd_max, '(leave blank to fetch latest)');

    function resetForm() {
        for (const [name, field] of Object.entries(STORED_FIELDS)) {
            if (field.clearedOnSubmit) {
                forgetValue(name);
            }
        }
        setValues(defaultFormValues());
    }

    async function submit() {
        const datadict = {
            radeclist: values.radeclist,
            mjd_min: values.mjd_min === '' ? null : values.mjd_min,
            mjd_max: values.mjd_max === '' ? null : values.mjd_max,
            use_reduced: values.use_reduced,
            send_email: values.send_email,
            comment: values.comment,
            request_type: (allow_stack_rock && values.enable_stack_rock) ? 'SSOSTACK' : 'FP',
        };

        if (values.enable_propermotion) {
            datadict['radec_epoch_year'] = values.radec_epoch_year;
            datadict['propermotion_ra'] = values.propermotion_ra;
            datadict['propermotion_dec'] = values.propermotion_dec;
        }

        fetch(api_url_base,
            {
                credentials: "same-origin",
                method: "POST",
                body: JSON.stringify(datadict),
                headers: {
                    ...csrfHeader(),
                    'Accept': 'application/json',
                    'Content-Type': 'application/json',
                },
            })
            .then((response) => {
                submission_in_progress = false;
                setSubmitting(false);
                setHttperror('');
                console.log('New task: HTTP response ', response.status);

                if (response.status == 201) {
                    console.log("New task: successful creation", response.status);
                    setErrors({});
                    resetForm();
                    response.json().then(data => {
                        data.forEach((task) => {
                            console.log('Created new task', task.id);
                            newtaskids.push(task.id);
                        })
                    });
                    window.history.pushState({}, document.title, api_url_base);
                    fetchData(true);
                }
                else if (response.status == 400) {
                    response.json().then(data => {
                        console.log('New task: errors returned', data);
                        setErrors(data);
                    });
                }
                else {
                    console.log("New task: Error on submission: ", response.status);
                    setHttperror('Request failed (HTTP ' + response.status + '). You may need to log in again.');
                };
            })
            .catch(error => {
                submission_in_progress = false;
                console.log('New task HTTP request failed', error);
                setSubmitting(false);
                setHttperror('HTTP request failed. Check internet connection and server are online.');
            });
    }

    function onSubmit(event) {
        event.preventDefault();
        if (submission_in_progress) {
            console.log('New task: Submission already in progress');
            return;
        }

        console.log('New task: Submitting', api_url_base);
        submission_in_progress = true;
        setSubmitting(true);
        submit();
    }

    const formcontent = [];

    formcontent.push(
        <ul key="ulradec">
            <li><label htmlFor="id_radeclist">RA Dec / MPC names:</label>
                <textarea name="radeclist" cols="" rows="3" required id="id_radeclist" value={values.radeclist} onChange={setField('radeclist')}></textarea>
                {/* a button, not an <a> without an href: this toggles a panel rather than
                    navigating, and a hrefless link is not focusable, so it could not be reached
                    by keyboard. type="button" matters here — inside a form, a button submits. */}
                &nbsp;<button type="button" className="linkbutton" aria-expanded={showradechelp} aria-controls="radec_help" onClick={() => setShowradechelp(!showradechelp)}>Help</button>
                {showradechelp ? <div id="radec_help" style={{ display: 'block', clear: 'right', fontSize: 'small' }} className="collapse">Each line should consist of a right ascension and a declination coordinate (J2000) in decimal or sexagesimal notation (RA/DEC separated by a space or a comma) or 'mpc ' and a Minor Planet Center object name (e.g. 'mpc Makemake'). Limit of 100 objects per submission. If requested, email notification will be sent only after all targets in the list have been processed.</div> : null}
            </li>
            {'radeclist' in errors ? <ul className="errorlist"><li>{errortext(errors['radeclist'])}</li></ul> : ''}
        </ul>
    );

    formcontent.push(
        <div key="propermotion_checkbox" id="propermotion_checkboxdiv" style={{ width: '100%' }}>
            <label style={{ width: '100%' }}>
                <input type="checkbox" checked={values.enable_propermotion} onChange={setField('enable_propermotion')} style={{ position: 'static', display: 'inline', width: '5em' }} /> Proper motion
            </label>
        </div>);
    if (values.enable_propermotion) {
        formcontent.push(
            <div key="propermotion_panel" id="propermotion_panel" className="optionpanel">
                <p key="propermotiondesc" style={{ fontSize: 'small' }}>If the star is moving, the J2000 coordinates above are correct for a specified epoch along with proper motions in RA (angle) and Dec in milliarcseconds. The epoch of ATLAS observations varies from 2015.5 to the present. Note: these are angular velocities, not rates of coordinate change.</p>
                <ul key="propermotion_inputs">
                    <li key="radec_epoch_year"><label htmlFor="id_radec_epoch_year">Epoch year:</label><input type="number" name="radec_epoch_year" step="0.1" id="id_radec_epoch_year" value={values.radec_epoch_year} onChange={setField('radec_epoch_year')} /></li>
                    <li key="propermotion_ra"><label htmlFor="id_propermotion_ra">PM RA [mas/yr]</label><input type="number" name="propermotion_ra" step="any" id="id_propermotion_ra" value={values.propermotion_ra} onChange={setField('propermotion_ra')} /></li>
                    <li key="propermotion_dec"><label htmlFor="id_propermotion_dec">PM Dec [mas/yr]</label><input type="number" name="propermotion_dec" step="any" id="id_propermotion_dec" value={values.propermotion_dec} onChange={setField('propermotion_dec')} /></li>
                </ul>
            </div>
        );
    }

    if (allow_stack_rock) {
        formcontent.push(
            <div key="stack_rock" id="stack_rock" style={{ width: '100%' }}>
                <label style={{ width: '100%' }}>
                    <input type="checkbox" checked={values.enable_stack_rock} onChange={setField('enable_stack_rock')} style={{ position: 'static', display: 'inline', width: '5em' }} /> Get stack of SS object images
                </label>
            </div>);

        if (values.enable_stack_rock) {
            formcontent.push(
                <div key="stackrock_panel" id="stackrock_panel" className="optionpanel">
                    <p key="stackrockdesc" style={{ fontSize: 'small' }}>Perform a shift &amp; stack operation for the MPC object entered above.</p>
                </div>
            );
        }
    }

    formcontent.push(
        <ul key="ulmjdoptions">
            <li key="mjd_min">
                <label htmlFor="id_mjd_min">MJD min:</label><input type="number" name="mjd_min" step="any" id="id_mjd_min" value={values.mjd_min} onChange={setField('mjd_min')} />
                {/* the emoji is the whole of this control, so without a label there is nothing
                    for a screen reader (or a hover) to say about it */}
                <button type="button" className="btn resetbutton" title={'Reset to ' + DEFAULT_MJD_MIN_DAYS + ' days before today'} aria-label={'Reset MJD min to ' + DEFAULT_MJD_MIN_DAYS + ' days before today'} onClick={() => { forgetValue('mjd_min'); setValues((previous) => ({ ...previous, mjd_min: getDefaultMjdMin() })); }}>↩️</button>
                <p className="inputisodate" id='id_mjd_min_isoformat'>{mjd_min_isoformat}</p>
            </li>
            <li key="mjd_max">
                <label htmlFor="id_mjd_max">MJD max:</label><input type="number" name="mjd_max" step="any" id="id_mjd_max" value={values.mjd_max} onChange={setField('mjd_max')} />
                <p className="inputisodate" id='id_mjd_max_isoformat'>{mjd_max_isoformat}</p>
                {'mjd_max' in errors ? <ul className="errorlist"><li>{errortext(errors['mjd_max'])}</li></ul> : ''}
            </li>
            <li key="comment"><label htmlFor="id_comment">Comment:</label><input type="text" name="comment" maxLength="300" id="id_comment" value={values.comment} onChange={setField('comment')} /></li>

            <li key="use_reduced"><input type="checkbox" name="use_reduced" id="id_use_reduced" checked={values.use_reduced} onChange={setField('use_reduced')} /><label htmlFor="id_use_reduced" >Use reduced (input) instead of difference images (<a href="../faq/">FAQ</a>)</label></li>
            <li key="send_email"><input type="checkbox" name="send_email" id="id_send_email" checked={values.send_email} onChange={setField('send_email')} /><label htmlFor="id_send_email">Email me when completed</label></li>
        </ul>
    );

    // radeclist and mjd_max are shown next to their own inputs above. Everything else the API
    // rejects (comment, mjd_min, radec_epoch_year, propermotion_*, ...) is shown here, so that
    // a validation error can never leave the form looking like it simply did nothing.
    const shownerrorkeys = ['radeclist', 'mjd_max'];
    const remainingerrors = Object.entries(errors).filter(
        ([key]) => !shownerrorkeys.includes(key));

    if (remainingerrors.length > 0) {
        formcontent.push(
            <ul key="othererrors" className="errorlist">
                {remainingerrors.map(([key, value]) => (
                    <li key={key}>{key == 'non_field_errors' ? '' : key + ': '}{errortext(value)}</li>
                ))}
            </ul>
        );
    }

    // a <button> rather than an <input type="submit">, which cannot contain anything: the spinner has
    // to be an element inside the control, and an input's label is an attribute
    formcontent.push(
        <button key="submitbutton" type="submit" id="submitrequest"
            className={submitting ? 'btn btn-primary submitting' : 'btn btn-primary'}>
            {submitting
                ? <span className="spinner-border spinner-border-sm submitspinner" aria-hidden="true"></span>
                : null}
            {submitting ? 'Requesting...' : 'Request'}
        </button>);
    if (httperror != '') {
        formcontent.push(<p key="httperror" className="errors">{httperror}</p>);
    }

    return (
        <div key="newrequestcontainer" id="newrequestcontainer">
            <div key="newrequestsource" className="newrequest" id="newrequestsource">
                <div key="newtask" className="task">
                    <h2 key="newtaskheader">New request</h2>
                    <form key="newtaskform" id="newrequest" onSubmit={onSubmit}>
                        {formcontent}
                    </form>
                </div>
            </div>
        </div>);
}
