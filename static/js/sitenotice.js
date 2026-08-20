/*
The control that removes the note in the site notice box.

The note is on every page, and it gives the same words at each visit. A note that is always there
gets less attention at each visit, and the reader who comes most often reads it least. Thus a
reader can remove the note.

A click stores the version of the note in a cookie. The server reads the cookie, and it does not
render the note again until the words change; see context_processors.sitenotice. New words give a
new version, and thus a new note comes back and the reader reads it once more. Because the server
omits a removed note, no page shows the note and then removes it after the first paint.

The server renders the control hidden, and this file shows it. Thus a page without JavaScript
shows the note, and it shows no control that does not work. This file does not touch the runner
sentence in the same box. That sentence gives the condition at this moment, and no reader removes
it.
*/

(function () {
  'use strict';

  function init() {
    var box = document.getElementById('sitenotice');
    if (box == null) {
      return;
    }

    // The server omits the note and the control when the cookie names the current version, and
    // when the note has no words. Then there is nothing to do here.
    var note = box.querySelector('.sitenotice-note');
    var button = box.querySelector('.sitenotice-dismiss');
    if (note == null || button == null) {
      return;
    }

    button.hidden = false;
    button.addEventListener('click', function () {
      // A cookie, and not localStorage, so that the server can read the choice and omit the note
      // from the next page. The value is one short version string, kept for one year.
      document.cookie = 'atlas-notice-dismissed=' + button.getAttribute('data-notice-version')
        + '; path=/; max-age=31536000; samesite=lax';
      note.remove();
      button.remove();
      // The first part of the collapse. js/runnerstatus.min.js keeps sitenotice-noline correct.
      // See main.css.
      box.classList.add('sitenotice-nonote');
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
