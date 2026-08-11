// Tests for the NewRequest form, added alongside its conversion from a class component to hooks.
// The 1,400 lines of JSX in this directory had no tests at all, which is what made converting them
// feel risky; these cover the behaviour that conversion could plausibly break -- form state, the
// remembered values, the derived MJD captions, and how a rejected submission is reported.
import assert from 'node:assert/strict';
import test, { after, beforeEach, describe } from 'node:test';

import { flush, importComponent, loadReact, render, setupDom, setValue, teardownDom } from './testing.js';

let window;
let NewRequest;
// not a static import: react-dom must not be loaded until setupDom has run. See loadReact.
let React;
let ReactDOM;

function jsonResponse(status, body) {
    return Promise.resolve({ status, json: () => Promise.resolve(body) });
}

describe('NewRequest', () => {
    beforeEach(async () => {
        if (window) {
            await teardownDom(window);
        }
        window = setupDom();
        ({ React, ReactDOM } = await loadReact());
        ({ NewRequest } = await importComponent('newrequest.jsx'));
    });

    after(async () => {
        if (window) {
            await teardownDom(window);
        }
    });

    const renderForm = (props = {}) =>
        render(ReactDOM, React, React.createElement(NewRequest, { allow_stack_rock: false, fetchData: () => {}, ...props }));

    test('renders the coordinate field and the submit button', async () => {
        const { container } = await renderForm();

        assert.ok(container.querySelector('#id_radeclist'));
        assert.equal(container.querySelector('#submitrequest').value, 'Request');
    });

    test('typing a coordinate updates the field and is remembered', async () => {
        const { container } = await renderForm();

        await setValue(container.querySelector('#id_radeclist'), '150.0 20.0');

        assert.equal(container.querySelector('#id_radeclist').value, '150.0 20.0');
        assert.equal(window.localStorage.getItem('radeclist'), '150.0 20.0');
    });

    test('a remembered coordinate list is restored on the next visit', async () => {
        window.localStorage.setItem('radeclist', 'mpc Makemake');

        const { container } = await renderForm();

        assert.equal(container.querySelector('#id_radeclist').value, 'mpc Makemake');
    });

    test('the MJD caption follows the MJD input', async () => {
        const { container } = await renderForm();

        await setValue(container.querySelector('#id_mjd_min'), '59000');

        // 59000 is 2020-05-31. The caption is derived during render rather than stored, so it
        // cannot fall out of step with the value the way the old copy in state could.
        assert.match(container.querySelector('#id_mjd_min_isoformat').textContent, /^2020-05-31T/);
    });

    test('an empty MJD max explains what blank means', async () => {
        const { container } = await renderForm();

        assert.equal(container.querySelector('#id_mjd_max_isoformat').textContent, '(leave blank to fetch latest)');
    });

    test('the reset button puts MJD min back to the default', async () => {
        const { container } = await renderForm();
        await setValue(container.querySelector('#id_mjd_min'), '12345');
        // asserted before the reset: without it this test passes even when setValue does nothing,
        // because the untouched default is also not '12345'
        assert.equal(container.querySelector('#id_mjd_min').value, '12345');

        container.querySelector('.resetbutton').click();
        await flush();

        assert.notEqual(container.querySelector('#id_mjd_min').value, '12345');
        assert.match(container.querySelector('#id_mjd_min_isoformat').textContent, /^\d{4}-\d{2}-\d{2}T/);
    });

    test('the proper motion panel appears only when its box is ticked', async () => {
        const { container } = await renderForm();
        assert.equal(container.querySelector('#propermotion_panel'), null);

        await setValue(container.querySelector('#propermotion_checkboxdiv input'), true);

        assert.ok(container.querySelector('#propermotion_panel'));
        assert.ok(container.querySelector('#id_radec_epoch_year'));
    });

    test('the SSO stack option is offered only when the page allows it', async () => {
        const withoutit = await renderForm({ allow_stack_rock: false });
        assert.equal(withoutit.container.querySelector('#stack_rock'), null);

        const withit = await renderForm({ allow_stack_rock: true });
        assert.ok(withit.container.querySelector('#stack_rock'));
    });

    test('the help panel toggles and reports its state to assistive technology', async () => {
        const { container } = await renderForm();
        const help = container.querySelector('button.linkbutton');
        assert.equal(help.getAttribute('aria-expanded'), 'false');

        help.click();
        await flush();

        assert.equal(help.getAttribute('aria-expanded'), 'true');
        assert.ok(container.querySelector('#radec_help'));
    });

    test('a field error is shown next to its field', async () => {
        global.fetch = () => jsonResponse(400, { radeclist: ['Enter a valid coordinate.'] });
        const { container } = await renderForm();

        container.querySelector('#newrequest').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
        await flush(50);

        assert.match(container.textContent, /Enter a valid coordinate\./);
    });

    test('an error on a field with no input of its own is still shown', async () => {
        // otherwise a rejected submission leaves the form looking like it simply did nothing
        global.fetch = () => jsonResponse(400, { comment: 'Too long.' });
        const { container } = await renderForm();

        container.querySelector('#newrequest').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
        await flush(50);

        assert.match(container.textContent, /comment: Too long\./);
    });

    test('a nested error object is flattened rather than crashing the page', async () => {
        // rendering an object as a React child throws, which used to unmount the whole queue page
        global.fetch = () => jsonResponse(400, { mjd_max: { mjd_max: 'must be greater than mjd_min' } });
        const { container } = await renderForm();

        container.querySelector('#newrequest').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
        await flush(50);

        assert.match(container.textContent, /must be greater than mjd_min/);
    });

    test('a successful submission clears the form and the remembered values', async () => {
        global.fetch = () => jsonResponse(201, [{ id: 7 }]);
        window.localStorage.setItem('radeclist', '150.0 20.0');
        window.localStorage.setItem('comment', 'keep me?');
        let refetched = false;
        const { container } = await renderForm({ fetchData: () => { refetched = true; } });

        container.querySelector('#newrequest').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
        await flush(50);

        assert.equal(container.querySelector('#id_radeclist').value, '');
        assert.equal(container.querySelector('#id_comment').value, '');
        assert.ok(refetched, 'the task list was not refreshed after a successful submission');
        assert.deepEqual(global.newtaskids, [7]);
    });

    test('a network failure is reported rather than silently dropped', async () => {
        global.fetch = () => Promise.reject(new Error('offline'));
        const { container } = await renderForm();

        container.querySelector('#newrequest').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
        await flush(50);

        assert.match(container.textContent, /Check internet connection/);
    });
});
