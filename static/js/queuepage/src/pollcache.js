'use strict';

// Conditional-request bookkeeping for the task list poll.
//
// The queue page re-fetches the task list every couple of seconds per open tab, and most of the
// time nothing has changed. Remembering the ETag of each page and sending it back as
// If-None-Match lets the server answer 304 without serialising anything.
//
// Kept free of React and of browser globals so that it can be tested directly with `node --test`.

// distinguishes "the server said nothing changed" from "the request produced no usable data"
export const NOT_MODIFIED = Symbol('not modified');

export class PollCache {
    constructor() {
        this.etags = {};
        this.bodies = {};
    }

    /** Remember the rendered state of a page, so a later 304 has something to fall back on. */
    storeBody(url, body) {
        this.bodies[url] = body;
    }

    getBody(url) {
        return this.bodies[url];
    }

    hasBody(url) {
        return url in this.bodies;
    }

    storeEtag(url, etag) {
        if (etag) {
            this.etags[url] = etag;
        }
    }

    getEtag(url) {
        return this.etags[url];
    }

    /**
     * Return the headers to send for a poll of the given URL.
     *
     * If-None-Match is only sent when a rendered copy of that exact page is already held: a 304
     * carries no body, so without one there would be nothing left to display.
     */
    requestHeaders(url, baseheaders) {
        const headers = { ...baseheaders };
        if (url in this.etags && this.hasBody(url)) {
            headers['If-None-Match'] = this.etags[url];
        }
        return headers;
    }

    /**
     * Classify a response: NOT_MODIFIED for a 304, otherwise null (the caller reads the body).
     *
     * This stores nothing. The caller stores the tag with storeEtag beside the body it describes,
     * once it has decided to apply that body: a tag held without its body makes the next poll ask
     * for a 304 that the cache cannot restore.
     */
    noteResponse(status) {
        return status === 304 ? NOT_MODIFIED : null;
    }
}
