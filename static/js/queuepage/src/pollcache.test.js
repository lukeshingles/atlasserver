'use strict';

import assert from 'node:assert/strict';
import { test, describe } from 'node:test';

import { NOT_MODIFIED, PollCache } from './pollcache.js';

const HEADERS = { 'Accept': 'application/json' };
const URL_A = 'https://example.com/queue/';
const URL_B = 'https://example.com/queue/?cursor=abc';

describe('PollCache', () => {
    test('sends no If-None-Match on the very first poll', () => {
        const cache = new PollCache();
        assert.deepEqual(cache.requestHeaders(URL_A, HEADERS), HEADERS);
    });

    test('sends If-None-Match once a page has been fetched and rendered', () => {
        const cache = new PollCache();
        cache.storeEtag(URL_A, '"etag-1"');
        cache.storeBody(URL_A, { results: [] });

        assert.equal(cache.requestHeaders(URL_A, HEADERS)['If-None-Match'], '"etag-1"');
    });

    test('withholds If-None-Match while no rendered copy is held', () => {
        // a 304 carries no body, so answering one with nothing cached would blank the page
        const cache = new PollCache();
        cache.storeEtag(URL_A, '"etag-1"');

        assert.equal(cache.requestHeaders(URL_A, HEADERS)['If-None-Match'], undefined);
    });

    test('keeps etags per page URL', () => {
        // paging back and forth must not offer one page's etag for another
        const cache = new PollCache();
        cache.storeEtag(URL_A, '"etag-a"');
        cache.storeBody(URL_A, { results: [] });
        cache.storeEtag(URL_B, '"etag-b"');
        cache.storeBody(URL_B, { results: [] });

        assert.equal(cache.requestHeaders(URL_A, HEADERS)['If-None-Match'], '"etag-a"');
        assert.equal(cache.requestHeaders(URL_B, HEADERS)['If-None-Match'], '"etag-b"');
    });

    test('a body held for one page does not enable conditional requests for another', () => {
        // if the user navigates while a fetch is in flight, the response belongs to the URL it was
        // requested from, not to whatever the address bar says when it lands. Storing it under the
        // new page's key would offer that page an If-None-Match for content never fetched for it.
        const cache = new PollCache();
        cache.storeEtag(URL_B, '"etag-b"');
        cache.storeBody(URL_A, { results: [] });

        assert.equal(cache.requestHeaders(URL_B, HEADERS)['If-None-Match'], undefined);
    });

    test('classifies a 304 as not modified', () => {
        const cache = new PollCache();
        assert.equal(cache.noteResponse(304), NOT_MODIFIED);
    });

    test('classifies a 200 as ordinary', () => {
        const cache = new PollCache();
        assert.equal(cache.noteResponse(200), null);
    });

    test('refreshes the stored etag when the page changes', () => {
        const cache = new PollCache();
        cache.storeEtag(URL_A, '"etag-1"');
        cache.storeBody(URL_A, { results: [] });
        cache.storeEtag(URL_A, '"etag-2"');

        assert.equal(cache.requestHeaders(URL_A, HEADERS)['If-None-Match'], '"etag-2"');
    });

    test('a 304 does not discard the etag', () => {
        // a 304 carries no body and no tag of its own, so the caller stores nothing for it
        const cache = new PollCache();
        cache.storeEtag(URL_A, '"etag-1"');
        cache.storeBody(URL_A, { results: [] });
        cache.noteResponse(304);

        assert.equal(cache.requestHeaders(URL_A, HEADERS)['If-None-Match'], '"etag-1"');
    });

    test('a response without an ETag leaves the previous one in place', () => {
        const cache = new PollCache();
        cache.storeEtag(URL_A, '"etag-1"');
        cache.storeBody(URL_A, { results: [] });
        cache.storeEtag(URL_A, null);

        assert.equal(cache.getEtag(URL_A), '"etag-1"');
    });

    test('does not mutate the caller\'s header object', () => {
        const cache = new PollCache();
        cache.storeEtag(URL_A, '"etag-1"');
        cache.storeBody(URL_A, { results: [] });

        cache.requestHeaders(URL_A, HEADERS);

        assert.deepEqual(HEADERS, { 'Accept': 'application/json' });
    });
});
