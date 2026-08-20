/*
The note in the site notice box, and the control that removes it.

The note is on every page, and it gives the same words at each visit. A permanent note has one
result: the readers who come most often are the readers who stop to see it first. Thus a reader can
remove the note, and it stays away until its words change.

This file stores a hash of the note, and not the fact of a removal. New words give a new hash, and
thus a new note comes back and the reader reads it once more. That is the purpose of a note that an
operator can edit. This file does not touch the runner sentence in the same box. That sentence
gives the condition at this moment, and no reader removes it.

The server renders the note, and this file shows the control. Thus a page without JavaScript keeps
the note and shows no control that could do nothing. navbar.html shows the theme control in the
same way.
*/

(function () {
  'use strict';

  var STORAGE_KEY = 'atlas-notice-dismissed';

  /*
  The FNV-1a hash of the text of the note, as a hexadecimal string.

  Any function that changes with the text is sufficient. This one is eight lines and needs no other
  code. A cryptographic digest would need SubtleCrypto, which is asynchronous and which a browser
  supplies only on a secure origin. This code makes no security decision. If two notes give one
  hash, the result is a new note that stays away, and no more than that.
  */
  function hashOf(text) {
    var hash = 0x811c9dc5;
    for (var i = 0; i < text.length; i += 1) {
      hash ^= text.charCodeAt(i);
      // Math.imul gives the multiplication of the FNV algorithm. A product of hash and 16777619
      // is larger than the 53 bits that a number holds exactly, and Math.imul keeps 32 bits.
      hash = Math.imul(hash, 0x01000193);
    }
    return (hash >>> 0).toString(16);
  }

  // A browser that refuses site data makes localStorage give an exception, and not null. Thus
  // both functions below contain the exception. A reader who refuses site data sees the note at
  // each page load, which is what the site did before this file.
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
      // The note comes back at the next page load. It is away from this page, as the reader asked.
    }
  }

  /**
   * Remove the note, and the control that removes it, from the box.
   *
   * The box collapses when both of its sentences are empty. main.css uses the class that this
   * function sets, and it does not examine the contents of the box. A browser without :has()
   * ignores such an examination, and it would then show an empty box on each page always.
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

    // The words, and only the words. Each group of space characters becomes one space. To put
    // notice.txt on three lines changes textContent, and it changes nothing that a reader sees.
    // Without this step, such a change would give a new note to each reader who removed it.
    var text = note.textContent.replace(/\s+/g, ' ').trim();

    // A note with no words in it is already away. The control would offer to remove nothing, and
    // the box must collapse and not show an empty box.
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
