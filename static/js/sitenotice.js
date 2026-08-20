/*
The control that folds the note in the site notice box, and unfolds it.

The note is on every page, and it gives the same words at each visit. A note that is always there
gets less attention at each visit, and the reader who comes most often reads it least. Thus a
reader can fold the note away. A folded note is not a removed note: the same control brings it
back, on this page and without a new request.

A click that folds the note stores the version of the note in a cookie, for the whole site. The
server reads the cookie, and it renders the note folded until the words change; see
context_processors.sitenotice. New words give a new version, and thus the note opens again and the
reader reads it once more. Because the server renders a folded note folded, no page shows the note
and then folds it after the first paint. A click that unfolds the note takes the cookie away, and
it takes the cookie of the old removal control away with it, so that the server does not fold the
note again on the next page.

The server renders the control hidden, and this file shows it. Thus a page without JavaScript
shows the note in the condition the server rendered it, and it shows no control that does not
work. This file does not touch the runner sentence in the same box. That sentence gives the
condition at this moment, and no reader folds it away.
*/

(function () {
  'use strict';

  function init() {
    var box = document.getElementById('sitenotice');
    if (box == null) {
      return;
    }

    // The server omits the note and the control when the note has no words. Then there is
    // nothing to do here.
    var note = box.querySelector('.sitenotice-note');
    var button = box.querySelector('.sitenotice-toggle');
    if (note == null || button == null) {
      return;
    }

    button.hidden = false;
    button.addEventListener('click', function () {
      // main.css hides the note under this class, and it turns the chevron of the control.
      var collapsed = box.classList.toggle('sitenotice-collapsed');
      button.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
      button.setAttribute('aria-label', collapsed ? 'Expand this notice' : 'Collapse this notice');

      // A cookie, and not localStorage, so that the server can read the choice and render the
      // next page in the same condition. The value is one short version string, kept for one
      // year. To unfold sets max-age=0, which is how a script takes a cookie away.
      if (collapsed) {
        document.cookie = 'atlas-notice-collapsed=' + button.getAttribute('data-notice-version')
          + '; path=/; max-age=31536000; samesite=lax';
      } else {
        document.cookie = 'atlas-notice-collapsed=; path=/; max-age=0; samesite=lax';
        // The cookie of the control that this one replaced. That control removed the note, and
        // the server folds the note for its cookie too. An unfold must override that choice.
        document.cookie = 'atlas-notice-dismissed=; path=/; max-age=0; samesite=lax';
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
