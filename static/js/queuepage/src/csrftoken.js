// Reading the CSRF cookie. This used to come from DRF's rest_framework/js/csrf.js, which the
// vendored copy of DRF's base template loaded on every page -- and which is written against
// jQuery, so it was one of the things keeping jQuery on the site.

/** Return the value of a cookie, or null when it is not set. */
export function getCookie(name) {
    const prefix = `${name}=`;

    for (const cookie of document.cookie.split(';')) {
        const trimmed = cookie.trim();
        if (trimmed.startsWith(prefix)) {
            return decodeURIComponent(trimmed.slice(prefix.length));
        }
    }

    return null;
}

/** Return the header a same-origin unsafe request needs, ready to spread into a fetch init. */
export function csrfHeader() {
    return { 'X-CSRFToken': getCookie('csrftoken') };
}
