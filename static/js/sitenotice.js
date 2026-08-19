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

  /**
   * Take the note, and the control that dismissed it, out of the box.
   *
   * The box collapses on its own once both of its lines are empty. main.css keys that off the
   * class set here rather than off the shape of the box, because a browser without :has() would
   * otherwise keep an empty panel on every page for ever.
   */
  function removeNote(box, note, button) {
    note.remove();
    button.remove();
    box.classList.add('sitenotice-nonote');
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

    // The words, and only the words. Runs of white space collapse to one space first. Rewrapping
    // notice.txt onto three lines changes textContent and changes nothing a reader can see, and
    // without this it would read as a new notice and come back for everybody who dismissed it.
    var text = note.textContent.replace(/\s+/g, ' ').trim();

    // A notice with no words in it is already dismissed. The control would offer to put away what
    // is not there, and the box must collapse rather than draw an empty panel.
    if (text === '') {
      removeNote(box, note, button);
      return;
    }

    var hash = hashOf(text);

    if (storedHash() === hash) {
      removeNote(box, note, button);
      return;
    }

    button.hidden = false;
    button.addEventListener('click', function () {
      storeHash(hash);
      removeNote(box, note, button);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
