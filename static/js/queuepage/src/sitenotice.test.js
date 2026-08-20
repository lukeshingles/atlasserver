// Tests for static/js/sitenotice.js. That file shows the control that removes the note, and it
// stores which note the reader removed.
//
// These tests use jsdom, and not a browser, because they must control two inputs. The first is
// the value that an earlier visit put in storage. The second is a browser that refuses site data,
// which gives an exception at each access.

import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { after, before, describe, test } from 'node:test';
import { JSDOM } from 'jsdom';

const SRC = dirname(fileURLToPath(import.meta.url));
const SITENOTICE_JS = join(SRC, '..', '..', 'sitenotice.js');

const NOTE = 'Forced photometry is now available from the Southern Telescopes.';

// The box, in the form that sitenotice.html renders. It holds the hidden control, the note, and
// the runner sentence. The file under test must not change that sentence.
const box = (note) => `
  <div class="sitenotice" id="sitenotice">
    <button type="button" class="btn-close sitenotice-dismiss" aria-label="Dismiss this notice" hidden></button>
    <p class="sitenotice-note">${note}</p>
    <p class="sitenotice-runner" id="runnerstatus" role="status">Task runner: 2 of 16 slots busy.</p>
  </div>`;

let source;
// Each load opens a window, and an open jsdom window holds the node event loop after the tests
// finish. theme.test.js and navbar.test.js keep the same list.
const open = [];

before(async () => {
  source = await readFile(SITENOTICE_JS, 'utf8');
});

after(() => {
  open.forEach((window) => window.close());
});

/**
 * Load sitenotice.js into a fresh window.
 *
 * `stored` is the value that an earlier visit left in localStorage. `storage` is 'working' or
 * 'unavailable'. With 'unavailable', each access gives an exception, as in a browser that refuses
 * site data.
 */
async function load({ note = NOTE, stored = null, storage = 'working' } = {}) {
  const dom = new JSDOM(`<!doctype html><html><body>${box(note)}</body></html>`, {
    runScripts: 'outside-only',
    url: 'http://testserver/',
  });
  const window = dom.window;
  open.push(window);

  if (storage === 'unavailable') {
    const reject = () => {
      throw new window.DOMException('The operation is insecure.', 'SecurityError');
    };
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      value: { getItem: reject, setItem: reject, removeItem: reject },
    });
  } else if (stored !== null) {
    window.localStorage.setItem('atlas-notice-dismissed', stored);
  }

  // jsdom sends DOMContentLoaded one tick after it makes the window, and until then readyState
  // is "loading". The script would then wait for an event that has already occurred.
  await new Promise((resolve) => {
    if (window.document.readyState === 'loading') {
      window.document.addEventListener('DOMContentLoaded', resolve);
    } else {
      resolve();
    }
  });

  window.eval(source);

  const query = (selector) => window.document.querySelector(selector);
  return {
    window,
    note: () => query('.sitenotice-note'),
    noteText: () => (query('.sitenotice-note') || {}).textContent,
    runner: () => query('.sitenotice-runner').textContent,
    button: () => query('.sitenotice-dismiss'),
    dismiss: () => query('.sitenotice-dismiss').click(),
    stored: () => window.localStorage.getItem('atlas-notice-dismissed'),
    collapsed: () => query('#sitenotice').classList.contains('sitenotice-nonote'),
  };
}

describe('the standing note', () => {
  test('the control is revealed, because this file is what makes it do anything', async () => {
    const page = await load();

    assert.equal(page.button().hidden, false);
    assert.notEqual(page.note(), null, 'the note is there until it is dismissed');
  });

  test('dismissing takes the note out of the page and remembers it', async () => {
    const page = await load();

    page.dismiss();

    assert.equal(page.note(), null);
    assert.notEqual(page.stored(), null, 'a note dismissed for one page load is not dismissed');
  });

  test('a note that was dismissed before does not come back', async () => {
    const first = await load();
    first.dismiss();

    const second = await load({ stored: first.stored() });

    assert.equal(second.note(), null);
  });

  test('a new notice is read once more, whatever was dismissed before', async () => {
    // This is why the file stores the note, and not the removal. An edited note gives new
    // information, and no reader has seen it.
    const first = await load();
    first.dismiss();

    const second = await load({ note: 'The southern telescopes are offline this week.', stored: first.stored() });

    assert.notEqual(second.note(), null);
    assert.equal(second.button().hidden, false);
  });

  test('the runner line is not this file\'s to touch', async () => {
    // That sentence gives the condition at this moment, and no reader removes it.
    const page = await load();

    page.dismiss();

    assert.match(page.runner(), /2 of 16 slots busy/);
  });

  test('a browser that refuses site data still shows the note and still dismisses it', async () => {
    const page = await load({ storage: 'unavailable' });

    assert.equal(page.button().hidden, false, 'the control must work for this page at least');
    page.dismiss();
    assert.equal(page.note(), null, 'the dismissal is honoured, it just is not remembered');
  });

  test('the control goes with the note, so the box has nothing left to draw', async () => {
    // The box collapses on the two classes, and not on the shape of its contents. A browser
    // without :has() would keep an empty box on each page always.
    const page = await load();

    page.dismiss();

    assert.equal(page.button(), null, 'a control that dismisses nothing is not left behind');
    assert.equal(page.collapsed(), true);
  });

  test('rewrapping the notice is not a new notice', async () => {
    // The same words on three lines. textContent changes, and what the reader sees does not.
    const first = await load({ note: 'The southern telescopes are offline this week.' });
    first.dismiss();

    const second = await load({
      note: '\n      The southern telescopes\n      are offline this week.\n    ',
      stored: first.stored(),
    });

    assert.equal(second.note(), null, 'the reader dismissed these words already');
  });

  test('a notice with no words in it is already dismissed', async () => {
    // This is what an operator leaves when they empty notice.txt to remove the note.
    const page = await load({ note: '\n  ' });

    assert.equal(page.note(), null);
    assert.equal(page.button(), null);
    assert.equal(page.collapsed(), true);
  });

  test('a page without the box is left alone', async () => {
    // Each page loads this file. A page whose template removed the box has no note in it.
    const dom = new JSDOM('<!doctype html><html><body><p>No box here.</p></body></html>', {
      runScripts: 'outside-only',
      url: 'http://testserver/',
    });
    await new Promise((resolve) => setTimeout(resolve, 0));

    dom.window.eval(source);

    assert.equal(dom.window.document.body.textContent.trim(), 'No box here.');
  });
});
