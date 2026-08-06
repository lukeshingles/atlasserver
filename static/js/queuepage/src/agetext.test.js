'use strict';

import assert from 'node:assert/strict';
import test from 'node:test';

import { describeAge } from './agetext.js';

test('describeAge', async (t) => {
    await t.test('under a minute', () => {
        assert.equal(describeAge(0), 'less than a minute ago');
        assert.equal(describeAge(59), 'less than a minute ago');
    });

    await t.test('minutes count completed units only', () => {
        assert.equal(describeAge(60), '1 minute ago');
        assert.equal(describeAge(119), '1 minute ago');
        assert.equal(describeAge(120), '2 minutes ago');
        // the boundary that motivated flooring: rounding reported this as "60 minutes ago"
        assert.equal(describeAge(3599), '59 minutes ago');
    });

    await t.test('hours', () => {
        assert.equal(describeAge(3600), '1 hour ago');
        assert.equal(describeAge(7199), '1 hour ago');
        assert.equal(describeAge(7200), '2 hours ago');
        // just under a day is still counted in hours, not rounded up into "24 hours ago"
        assert.equal(describeAge(86399), '23 hours ago');
    });

    await t.test('days', () => {
        assert.equal(describeAge(86400), '1 day ago');
        // the banner text that prompted the helper: 6401 minutes reads as days
        assert.equal(describeAge(6401 * 60), '4 days ago');
    });
});
