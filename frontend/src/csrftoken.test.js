'use strict';

import assert from 'node:assert/strict';
import { test, describe, beforeEach, afterEach } from 'node:test';

import { csrfHeader, getCookie } from './csrftoken.js';
import { setupDom, teardownDom } from './testing.js';

describe('csrftoken', () => {
    let window;

    beforeEach(() => {
        window = setupDom();
    });

    afterEach(async () => {
        await teardownDom(window);
    });

    test('returns the value of the named cookie', () => {
        document.cookie = 'csrftoken=abc123';
        assert.equal(getCookie('csrftoken'), 'abc123');
    });

    test('returns null for a cookie that is not set', () => {
        assert.equal(getCookie('csrftoken'), null);
    });

    test('finds the cookie among others, whatever the spacing', () => {
        document.cookie = 'sessionid=s1';
        document.cookie = 'csrftoken=tok';
        document.cookie = 'theme=dark';
        assert.equal(getCookie('csrftoken'), 'tok');
    });

    test('does not mistake a cookie whose name ends with the wanted name', () => {
        document.cookie = 'xcsrftoken=wrong';
        assert.equal(getCookie('csrftoken'), null);
    });

    test('decodes a percent-encoded value', () => {
        document.cookie = 'csrftoken=' + encodeURIComponent('a b/c');
        assert.equal(getCookie('csrftoken'), 'a b/c');
    });

    test('csrfHeader carries the token under the header Django reads', () => {
        document.cookie = 'csrftoken=tok';
        assert.deepEqual(csrfHeader(), { 'X-CSRFToken': 'tok' });
    });
});
