// Tests for static/js/theme.js, which decides the data-bs-theme attribute Bootstrap colours the
// page from.
//
// Under jsdom rather than in a browser because both inputs have to be controlled: the system
// preference (a real browser will not let a page change prefers-color-scheme, and the automation
// that emulates it does not dispatch the media query's change event) and localStorage refusing
// writes, which is what a browser set to reject site data does.

import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { after, before, describe, test } from 'node:test';
import { JSDOM } from 'jsdom';

const SRC = dirname(fileURLToPath(import.meta.url));
const THEME_JS = join(SRC, '..', '..', 'theme.js');

// the part of navbar.html the script drives: the three choices, the icon for each, and the wrapper
// it reveals
const NAVBAR = `
  <li class="nav-item dropdown themeswitch d-none">
    <button class="nav-link dropdown-toggle" type="button">
      <svg class="themeicon themeicon-light d-none"></svg>
      <svg class="themeicon themeicon-dark d-none"></svg>
      <svg class="themeicon themeicon-auto d-none"></svg>
    </button>
    <ul class="dropdown-menu">
      <li><button type="button" class="dropdown-item" data-theme-value="light" aria-checked="false">Light</button></li>
      <li><button type="button" class="dropdown-item" data-theme-value="dark" aria-checked="false">Dark</button></li>
      <li><button type="button" class="dropdown-item" data-theme-value="auto" aria-checked="false">Auto</button></li>
    </ul>
  </li>`;

let source;

before(async () => {
  source = await readFile(THEME_JS, 'utf8');
});

/**
 * Load theme.js into a fresh window.
 *
 * `systemDark` is what prefers-color-scheme reports, and the returned setSystemDark() changes it and
 * dispatches the change event, which is the whole point of stubbing matchMedia: jsdom's own
 * implementation always reports false and has nothing to drive.
 *
 * `storage` is 'working', 'rejects-writes' (setItem throws, as when a browser is set to refuse site
 * data) or 'unavailable' (every access throws).
 */
async function load({ systemDark = false, stored = null, storage = 'working' } = {}) {
  const dom = new JSDOM(`<!doctype html><html><body><ul>${NAVBAR}</ul></body></html>`, {
    runScripts: 'outside-only',
    url: 'http://testserver/'
  });
  const window = dom.window;

  const listeners = [];
  let dark = systemDark;
  window.matchMedia = (query) => ({
    media: query,
    get matches() {
      return dark;
    },
    addEventListener: (type, fn) => {
      if (type === 'change') listeners.push(fn);
    },
    removeEventListener: () => {},
    addListener: (fn) => listeners.push(fn)
  });

  const items = new Map();
  if (stored !== null) items.set('atlas-theme', stored);

  if (storage !== 'working') {
    const reject = () => {
      throw new window.DOMException('The quota has been exceeded.', 'QuotaExceededError');
    };
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      value: {
        getItem: (key) => (storage === 'unavailable' ? reject() : (items.has(key) ? items.get(key) : null)),
        setItem: reject,
        removeItem: (key) => (storage === 'unavailable' ? reject() : items.delete(key))
      }
    });
  } else if (stored !== null) {
    window.localStorage.setItem('atlas-theme', stored);
  }

  // jsdom fires DOMContentLoaded a tick after construction, and until then readyState is "loading".
  // theme.js would take that branch and wire the control up on an event that has already gone by,
  // leaving the buttons dead -- in a browser the script is in <head> and the event is still to come.
  await new Promise((resolve) => {
    if (window.document.readyState === 'loading') {
      window.document.addEventListener('DOMContentLoaded', resolve);
    } else {
      resolve();
    }
  });

  window.eval(source);

  return {
    window,
    theme: () => window.document.documentElement.getAttribute('data-bs-theme'),
    click: (mode) =>
      window.document.querySelector(`[data-theme-value="${mode}"]`).click(),
    marked: () => {
      const active = window.document.querySelector('[data-theme-value].active');
      return active && active.getAttribute('data-theme-value');
    },
    visibleIcon: () => {
      const icon = window.document.querySelector('.themeicon:not(.d-none)');
      return icon && icon.getAttribute('class').replace('themeicon themeicon-', '');
    },
    // what would be waiting at the next page load: jsdom's own localStorage when it is working,
    // and the stub's backing map when it is not
    stored: () =>
      storage === 'working'
        ? window.localStorage.getItem('atlas-theme')
        : (items.has('atlas-theme') ? items.get('atlas-theme') : null),
    setSystemDark: (value) => {
      dark = value;
      for (const fn of listeners) fn({ matches: value });
    },
    close: () => window.close()
  };
}

describe('theme.js', () => {
  const open = [];
  const start = async (options) => {
    const page = await load(options);
    open.push(page);
    return page;
  };
  after(() => open.forEach((page) => page.close()));

  describe('at load', () => {
    test('auto takes the system preference', async () => {
      assert.equal((await start({ systemDark: true })).theme(), 'dark');
      assert.equal((await start({ systemDark: false })).theme(), 'light');
    });

    test('a stored choice outranks the system preference', async () => {
      assert.equal((await start({ stored: 'dark', systemDark: false })).theme(), 'dark');
      assert.equal((await start({ stored: 'light', systemDark: true })).theme(), 'light');
    });

    test('a value that is neither light nor dark is treated as auto', async () => {
      assert.equal((await start({ stored: 'chartreuse', systemDark: true })).theme(), 'dark');
    });

    test('the control is revealed, marked, and given the matching icon', async () => {
      const page = await start({ stored: 'dark' });
      assert.equal(page.window.document.querySelector('.themeswitch').classList.contains('d-none'), false);
      assert.equal(page.marked(), 'dark');
      assert.equal(page.visibleIcon(), 'dark');
    });

    test('storage that cannot be read at all still leaves a themed page', async () => {
      const page = await start({ storage: 'unavailable', systemDark: true });
      assert.equal(page.theme(), 'dark');
      assert.equal(page.marked(), 'auto');
    });
  });

  describe('choosing a mode', () => {
    test('applies it, stores it, and moves the mark and the icon', async () => {
      const page = await start({ systemDark: true });

      page.click('light');

      assert.equal(page.theme(), 'light');
      assert.equal(page.stored(), 'light');
      assert.equal(page.marked(), 'light');
      assert.equal(page.visibleIcon(), 'light');
    });

    test('auto clears the stored value and returns to the system preference', async () => {
      const page = await start({ stored: 'light', systemDark: true });

      page.click('auto');

      assert.equal(page.theme(), 'dark');
      assert.equal(page.stored(), null);
      assert.equal(page.marked(), 'auto');
    });
  });

  describe('when the system preference changes', () => {
    test('auto follows it', async () => {
      const page = await start({ systemDark: false });
      assert.equal(page.theme(), 'light');

      page.setSystemDark(true);

      assert.equal(page.theme(), 'dark');
    });

    test('an explicit choice does not follow it', async () => {
      const page = await start({ stored: 'light', systemDark: false });

      page.setSystemDark(true);

      assert.equal(page.theme(), 'light');
    });

    // the reason the mode in force is held in a variable rather than read back from storage: a
    // browser that rejects the write never learns of the click, so asking storage would answer
    // "auto" and the change below would recolour the page against what the visitor just asked for
    test('an explicit choice that could not be stored still does not follow it', async () => {
      const page = await start({ storage: 'rejects-writes', systemDark: true });

      page.click('dark');
      assert.equal(page.theme(), 'dark');
      assert.equal(page.stored(), null, 'the write was rejected, so nothing is stored');

      // to light, so the system now disagrees with the choice: a listener that asked storage what
      // was chosen would be told "auto" and would turn the page light here
      page.setSystemDark(false);

      assert.equal(page.theme(), 'dark');
      assert.equal(page.marked(), 'dark', 'and the control still shows what is in force');
    });
  });
});
