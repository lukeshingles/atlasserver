'use strict';

// Human-readable ages for the task runner status banner.
//
// Kept free of React and of browser globals so that it can be tested directly with `node --test`
// (the same reasoning as pollcache.js).

/**
 * Describe an age in seconds in the largest unit that still reads as a number of things.
 *
 * The outage banner used to say "6401 minutes ago", which is the one thing a reader of that
 * sentence has to work out for themselves: whether this started moments ago or days ago.
 */
export function describeAge(seconds) {
    const units = [['day', 86400], ['hour', 3600], ['minute', 60]];
    for (const [name, size] of units) {
        if (seconds >= size) {
            // floor, not round: an age is a count of completed units, and rounding up would
            // cross the boundary the unit was chosen by — 3599 seconds would come out as
            // "60 minutes ago" instead of "59 minutes ago"
            const count = Math.floor(seconds / size);
            return count + ' ' + name + (count == 1 ? '' : 's') + ' ago';
        }
    }
    return 'less than a minute ago';
}
