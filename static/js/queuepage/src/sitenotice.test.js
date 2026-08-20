// Tests for static/js/sitenotice.js. That file shows the control that removes the note, and a
// click stores the version of the note in a cookie. The server reads the cookie and omits the
// note; see context_processors.sitenotice and its Django tests. Thus these tests cover the one
// page condition with work to do: the page where the note is present.

import assert from 'node:assert/strict';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { after, describe, test } from 'node:test';

import { loadClassicScript } from './testing.js';

const SRC = dirname(fileURLToPath(import.meta.url));
const SITENOTICE_JS = join(SRC, '..', '..', 'sitenotice.js');

// The box in the form that sitenotice.html renders it, with the note present. The file under test
// must not change the runner sentence.
const PAGE = `<!doctype html><html><body>
  <div class="sitenotice sitenotice-noline" id="sitenotice">
    <button type="button" class="btn-close sitenotice-dismiss" aria-label="Dismiss this notice" data-notice-version="version123" hidden></button>
    <p class="sitenotice-note">Words about the data.</p>
    <p class="sitenotice-runner" id="runnerstatus" role="status" aria-live="off"><svg class="sitenotice-warnmark" aria-hidden="true" hidden></svg><span class="sitenotice-runnertext">Task runner: 2 of 16 slots busy.</span></p>
  </div>
</body></html>`;

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

    assert.equal(window.document.querySelector('.sitenotice-dismiss').hidden, false);
    assert.notEqual(window.document.querySelector('.sitenotice-note'), null);
  });

  test('a click stores the version in a cookie and removes the note', async () => {
    const window = await load();

    window.document.querySelector('.sitenotice-dismiss').click();

    assert.match(window.document.cookie, /atlas-notice-dismissed=version123/);
    assert.equal(window.document.querySelector('.sitenotice-note'), null);
    assert.equal(window.document.querySelector('.sitenotice-dismiss'), null,
      'a control that removes nothing is not left in the page');
    assert.equal(
      window.document.getElementById('sitenotice').classList.contains('sitenotice-nonote'), true,
      'the box must be able to collapse');
  });

  test('the runner sentence is not this file\'s to touch', async () => {
    // That sentence gives the condition at this moment, and no reader removes it.
    const window = await load();

    window.document.querySelector('.sitenotice-dismiss').click();

    assert.match(window.document.getElementById('runnerstatus').textContent, /2 of 16 slots busy/);
  });

  test('a page whose server omitted the note is left alone', async () => {
    // the condition after a removal: the box holds only the runner sentence
    const window = await load(`<!doctype html><html><body>
      <div class="sitenotice sitenotice-noline sitenotice-nonote" id="sitenotice">
        <p class="sitenotice-runner" id="runnerstatus" role="status" aria-live="off"><svg class="sitenotice-warnmark" aria-hidden="true" hidden></svg><span class="sitenotice-runnertext"></span></p>
      </div></body></html>`);

    assert.equal(window.document.querySelector('.sitenotice-dismiss'), null);
    assert.notEqual(window.document.getElementById('runnerstatus'), null);
  });

  test('a page without the box is left alone', async () => {
    const window = await load('<!doctype html><html><body><p>No box here.</p></body></html>');

    assert.equal(window.document.body.textContent.trim(), 'No box here.');
  });
});
