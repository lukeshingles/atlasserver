// Reading the CSRF cookie, without jQuery: the fetch calls of the queue page send the token in a
// header, and this is the one place that reads it.

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
