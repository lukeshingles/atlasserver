// Tests for static/js/sitenotice.js. That file shows the control that folds the note and unfolds
// it. A click that folds stores the version of the note in a cookie, and a click that unfolds
// takes the cookie away. The server reads the cookie and renders the note folded; see
// context_processors.sitenotice and its Django tests. Thus these tests cover the two page
// conditions with work to do: the page where the note is open, and the page where the server
// rendered it folded.

import assert from 'node:assert/strict';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { after, describe, test } from 'node:test';

import { loadClassicScript } from './testing.js';

const SRC = dirname(fileURLToPath(import.meta.url));
const SITENOTICE_JS = join(SRC, '..', '..', 'sitenotice.js');

// The box in the form that sitenotice.html renders it, with the note open. The file under test
// must not change the runner sentence.
const PAGE = `<!doctype html><html><body>
  <div class="sitenotice sitenotice-noline" id="sitenotice">
    <button type="button" class="sitenotice-toggle" aria-controls="sitenotice-note" aria-expanded="true" aria-label="Collapse this notice" data-notice-version="version123" hidden><svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path d="M2.5 10.5 8 5l5.5 5.5"/></svg></button>
    <p class="sitenotice-note" id="sitenotice-note">Words about the data.</p>
    <p class="sitenotice-runner" id="runnerstatus" role="status" aria-live="off"><svg class="sitenotice-warnmark" aria-hidden="true" hidden></svg><span class="sitenotice-runnertext">Task runner: 2 of 16 slots busy.</span></p>
  </div>
</body></html>`;

// The same box as the server renders it for the reader whose cookie folded the note.
const FOLDED_PAGE = PAGE
  .replace('sitenotice sitenotice-noline', 'sitenotice sitenotice-noline sitenotice-collapsed')
  .replace('aria-expanded="true"', 'aria-expanded="false"')
  .replace('aria-label="Collapse this notice"', 'aria-label="Expand this notice"');

// Each load opens a window, and an open jsdom window holds the node event loop after the tests
// finish. theme.test.js and navbar.test.js keep the same list.
const open = [];

after(() => {
  open.forEach((window) => window.close());
});

async function load(html = PAGE) {
  const window = await loadClassicScript({ file: SITENOTICE_JS, html });
  open.push(window);
  return window;
}

describe('the note control', () => {
  test('the control is shown, because this file is what makes it work', async () => {
    const window = await load();

    assert.equal(window.document.querySelector('.sitenotice-toggle').hidden, false);
    assert.notEqual(window.document.querySelector('.sitenotice-note'), null);
  });

  test('a click folds the note and stores the version in a cookie', async () => {
    const window = await load();

    window.document.querySelector('.sitenotice-toggle').click();

    assert.match(window.document.cookie, /atlas-notice-collapsed=version123/);
    assert.equal(
      window.document.getElementById('sitenotice').classList.contains('sitenotice-collapsed'), true,
      'main.css hides the note under this class');
    assert.notEqual(window.document.querySelector('.sitenotice-note'), null,
      'a folded note stays in the page, so that the control can unfold it');
    const button = window.document.querySelector('.sitenotice-toggle');
    assert.equal(button.getAttribute('aria-expanded'), 'false');
    assert.equal(button.getAttribute('aria-label'), 'Expand this notice');
  });

  test('a second click unfolds the note and takes the cookie away', async () => {
    const window = await load();
    const button = window.document.querySelector('.sitenotice-toggle');

    button.click();
    button.click();

    assert.doesNotMatch(window.document.cookie, /atlas-notice-collapsed/,
      'a cookie left behind would fold the note again on the next page');
    assert.equal(
      window.document.getElementById('sitenotice').classList.contains('sitenotice-collapsed'), false);
    assert.equal(button.getAttribute('aria-expanded'), 'true');
    assert.equal(button.getAttribute('aria-label'), 'Collapse this notice');
  });

  test('an unfold takes the cookie of the old removal control away too', async () => {
    // The server folds the note for that cookie as well; see context_processors.sitenotice.
    const window = await load(FOLDED_PAGE);
    window.document.cookie = 'atlas-notice-dismissed=version123; path=/';

    window.document.querySelector('.sitenotice-toggle').click();

    assert.doesNotMatch(window.document.cookie, /atlas-notice-dismissed/);
  });

  test('a note that the server rendered folded unfolds on one click', async () => {
    const window = await load(FOLDED_PAGE);
    const button = window.document.querySelector('.sitenotice-toggle');

    assert.equal(button.hidden, false, 'the control is what unfolds the note');
    button.click();

    assert.equal(
      window.document.getElementById('sitenotice').classList.contains('sitenotice-collapsed'), false);
    assert.equal(button.getAttribute('aria-expanded'), 'true');
    assert.doesNotMatch(window.document.cookie, /atlas-notice-collapsed/);
  });

  test('the runner sentence is not this file\'s to touch', async () => {
    // That sentence gives the condition at this moment, and no reader folds it away.
    const window = await load();

    window.document.querySelector('.sitenotice-toggle').click();

    assert.match(window.document.getElementById('runnerstatus').textContent, /2 of 16 slots busy/);
  });

  test('a page whose server omitted an empty note is left alone', async () => {
    // the condition when notice.txt has no words: the box holds only the runner sentence
    const window = await load(`<!doctype html><html><body>
      <div class="sitenotice sitenotice-noline sitenotice-nonote" id="sitenotice">
        <p class="sitenotice-runner" id="runnerstatus" role="status" aria-live="off"><svg class="sitenotice-warnmark" aria-hidden="true" hidden></svg><span class="sitenotice-runnertext"></span></p>
      </div></body></html>`);

    assert.equal(window.document.querySelector('.sitenotice-toggle'), null);
    assert.notEqual(window.document.getElementById('runnerstatus'), null);
  });

  test('a page without the box is left alone', async () => {
    const window = await load('<!doctype html><html><body><p>No box here.</p></body></html>');

    assert.equal(window.document.body.textContent.trim(), 'No box here.');
  });
});
