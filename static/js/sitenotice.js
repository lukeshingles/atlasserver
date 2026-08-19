/*
The standing note in the site notice box, and the control that puts it away.

The note is on every page of the site and says the same thing on every visit, which is the state a
permanent banner ends in: the people who read the site most are the people who stopped seeing it
soonest. So a reader can dismiss it, and it stays dismissed until the text changes.

What is remembered is a hash of the note itself, not the fact of a dismissal. A new note is a
different hash, so it comes back and is read once more -- which is the whole reason for a notice
that can be edited. The runner status line in the same box is untouched: it is about right now, and
nobody dismisses that.

The note is server-rendered and the button is not shown until this file runs, so a page without
JavaScript keeps the note and offers nothing that would do nothing (the same arrangement as the
theme control in navbar.html).
*/

(function () {
  'use strict';

  var STORAGE_KEY = 'atlas-notice-dismissed';

  /*
  FNV-1a over the note text, as a hexadecimal string.

  Any function that changes when the text changes would do; this one is eight lines and has no
  dependencies, where a real digest would mean SubtleCrypto, which is asynchronous and is only
  available on a secure origin. Nothing here is a security decision: the worst a collision can do
  is leave a new note dismissed.
  */
  function hashOf(text) {
    var hash = 0x811c9dc5;
    for (var i = 0; i < text.length; i += 1) {
      hash ^= text.charCodeAt(i);
      // the FNV prime, by shift and add: hash * 16777619 overflows the 53 bits a number holds
      // exactly, and Math.imul is the multiplication that wraps at 32 bits the way the algorithm
      // expects
      hash = Math.imul(hash, 0x01000193);
    }
    return (hash >>> 0).toString(16);
  }

  // localStorage throws rather than returning null where a browser is set to refuse site data, so
  // both readers and writers are guarded. A visitor who has turned it off sees the note on every
  // page load, which is the behaviour of the site before this file existed.
  function storedHash() {
    try {
      return localStorage.getItem(STORAGE_KEY);
    } catch (err) {
      return null;
    }
  }

  function storeHash(hash) {
    try {
      localStorage.setItem(STORAGE_KEY, hash);
    } catch (err) {
      // the note returns on the next page load; it is gone from this one, which is what was asked
    }
  }

  /** Take the note out of the box. The box hides itself once nothing in it has anything to say. */
  function removeNote(note) {
    note.remove();
  }

  function init() {
    var box = document.getElementById('sitenotice');
    if (box == null) {
      return;
    }

    var note = box.querySelector('.sitenotice-note');
    var button = box.querySelector('.sitenotice-dismiss');
    if (note == null || button == null) {
      return;
    }

    // The text as the reader would read it, so that white space in the template cannot change the
    // hash. textContent includes the button's label, which is why the label lives in the button's
    // aria-label rather than in its text.
    var hash = hashOf(note.textContent.trim());

    if (storedHash() === hash) {
      removeNote(note);
      return;
    }

    button.hidden = false;
    button.addEventListener('click', function () {
      storeHash(hash);
      removeNote(note);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
