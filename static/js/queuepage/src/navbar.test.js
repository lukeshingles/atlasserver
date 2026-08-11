// Tests for static/js/navbar.js: the stuck class and the geometry of the indicator under the
// navigation.
//
// Under jsdom, because all three inputs have to be controlled and a real browser gives none of them
// up: whether the sentinel is on screen (an IntersectionObserver callback), which side of the
// collapse breakpoint the window is (a media query), and where the links are (layout, which jsdom
// does not do at all -- every rect is zero, so the boxes below are stubbed in).

import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { after, before, describe, test } from 'node:test';
import { JSDOM } from 'jsdom';

const SRC = dirname(fileURLToPath(import.meta.url));
const NAVBAR_JS = join(SRC, '..', '..', 'navbar.js');

// the shape of navbar.html that navbar.js cares about: the wrapper it puts the sentinel in, the
// left-hand list, and one link marked as the current page
const MARKUP = `
  <div class="wrapper">
    <nav class="navbar navbar-expand-lg">
      <div class="container">
        <a class="navbar-brand" href="/"><svg class="atlaslogo"></svg></a>
        <div class="collapse navbar-collapse">
          <ul class="navbar-nav me-auto">
            <li class="nav-item"><a class="nav-link" href="/">Home</a></li>
            <li class="nav-item"><a class="nav-link active" href="/faq/">FAQ</a></li>
            <li class="nav-item"><a class="nav-link" href="/stats/">Stats</a></li>
          </ul>
          <ul class="navbar-nav">
            <li class="nav-item"><a class="nav-link" href="/login/">Log in</a></li>
          </ul>
        </div>
      </div>
    </nav>
    <div class="container">content</div>
  </div>`;

// where the stubbed layout puts things: the list starts at x=100, and each link is 80 wide
const NAV_LEFT = 100;
const LINK_WIDTH = 80;

let source;

before(async () => {
  source = await readFile(NAVBAR_JS, 'utf8');
});

/**
 * Load navbar.js into a fresh window.
 *
 * `wide` is which side of the collapse breakpoint the window is on. The returned observe() fires the
 * IntersectionObserver callback the script registered, which is how a test says "the sentinel has
 * scrolled off" without a viewport to scroll.
 */
async function load({ wide = true, markup = MARKUP } = {}) {
  const dom = new JSDOM(`<!doctype html><html><body>${markup}</body></html>`, {
    runScripts: 'outside-only',
    url: 'http://testserver/'
  });
  const window = dom.window;

  window.matchMedia = (query) => ({ media: query, matches: wide, addEventListener: () => {}, removeEventListener: () => {} });

  const observed = [];
  window.IntersectionObserver = class {
    constructor(callback) {
      this.callback = callback;
    }
    observe(target) {
      observed.push({ target, callback: this.callback });
    }
    disconnect() {}
  };

  // the script uses it to re-measure on resize; nothing here resizes, so it only has to exist
  window.ResizeObserver = class {
    observe() {}
    disconnect() {}
  };
  window.requestAnimationFrame = (fn) => window.setTimeout(fn, 0);

  const nav = window.document.querySelector('.navbar-nav.me-auto');
  const links = nav ? [...nav.querySelectorAll('.nav-link')] : [];

  // jsdom lays nothing out, so the boxes are stated rather than measured
  if (nav) {
    nav.getBoundingClientRect = () => ({ left: NAV_LEFT, right: NAV_LEFT + 400, width: 400, top: 0, bottom: 40, height: 40 });
    links.forEach((link, index) => {
      const left = NAV_LEFT + index * LINK_WIDTH;
      link.getBoundingClientRect = () => ({ left, right: left + LINK_WIDTH, width: LINK_WIDTH, top: 0, bottom: 40, height: 40 });
    });
  }

  await new Promise((resolve) => {
    if (window.document.readyState === 'loading') {
      window.document.addEventListener('DOMContentLoaded', resolve);
    } else {
      resolve();
    }
  });

  window.eval(source);
  // the script enables the transition a frame after its first measurement
  await new Promise((resolve) => window.setTimeout(resolve, 5));

  const navbar = window.document.querySelector('nav.navbar');

  return {
    window,
    navbar,
    nav,
    links,
    stuck: () => navbar.classList.contains('navbar-stuck'),
    sentinel: () => window.document.querySelector('.navbar-sentinel'),
    /** Fire the registered callback as though the sentinel had scrolled out of (or back into) view. */
    observe: (isIntersecting) => observed.forEach((o) => o.callback([{ isIntersecting }])),
    geometry: () => ({
      x: nav.style.getPropertyValue('--navindicator-x'),
      width: nav.style.getPropertyValue('--navindicator-width'),
      opacity: nav.style.getPropertyValue('--navindicator-opacity')
    }),
    hover: (link) => link.dispatchEvent(new window.MouseEvent('mouseenter')),
    focus: (link) => link.dispatchEvent(new window.FocusEvent('focus')),
    leave: () => nav.dispatchEvent(new window.MouseEvent('mouseleave')),
    close: () => window.close()
  };
}

describe('navbar.js', () => {
  const open = [];
  const start = async (options) => {
    const page = await load(options);
    open.push(page);
    return page;
  };
  after(() => open.forEach((page) => page.close()));

  describe('the stuck class', () => {
    test('a sentinel is placed immediately before the navbar and takes no space', async () => {
      const page = await start();
      const sentinel = page.sentinel();

      assert.ok(sentinel, 'no sentinel was inserted');
      assert.equal(sentinel.nextElementSibling, page.navbar);
      assert.equal(sentinel.parentElement.className, 'wrapper');
    });

    test('it is absent while the sentinel is on screen and present once it is not', async () => {
      const page = await start();
      assert.equal(page.stuck(), false, 'stuck before anything scrolled');

      page.observe(false);
      assert.equal(page.stuck(), true, 'not stuck after the sentinel left the viewport');

      page.observe(true);
      assert.equal(page.stuck(), false, 'still stuck after scrolling back to the top');
    });
  });

  describe('the indicator', () => {
    test('is created inside the left-hand list, which becomes its host', async () => {
      const page = await start();
      const bar = page.nav.querySelector('.navindicator');

      assert.ok(bar, 'no indicator was added');
      assert.equal(page.nav.classList.contains('navindicator-host'), true);
      assert.equal(bar.getAttribute('aria-hidden'), 'true', 'the bar duplicates state links already carry');
      assert.equal(page.window.document.querySelectorAll('.navindicator').length, 1, 'the right-hand list has one too');
    });

    test('rests on the current page', async () => {
      const page = await start();

      // the active link is the second, so one link width along from the list's left edge
      assert.deepEqual(page.geometry(), { x: `${LINK_WIDTH}px`, width: `${LINK_WIDTH}px`, opacity: '1' });
    });

    test('follows the pointer and returns to the current page', async () => {
      const page = await start();

      page.hover(page.links[2]);
      assert.equal(page.geometry().x, `${2 * LINK_WIDTH}px`);

      page.leave();
      assert.equal(page.geometry().x, `${LINK_WIDTH}px`, 'did not return to the active link');
    });

    test('follows keyboard focus too, so tabbing through moves it', async () => {
      const page = await start();

      page.focus(page.links[0]);

      assert.equal(page.geometry().x, '0px');
    });

    test('the transition is enabled only after the first measurement', async () => {
      const page = await start();

      // by now the frame has passed; the class is what main.css hangs the transition on, so the bar
      // does not slide in from the left edge on every page load
      assert.equal(page.nav.classList.contains('navindicator-animated'), true);
    });

    test('is hidden below the collapse breakpoint, where the links are a stacked column', async () => {
      const page = await start({ wide: false });

      assert.equal(page.geometry().opacity, '0');

      page.hover(page.links[2]);
      assert.equal(page.geometry().opacity, '0', 'hovering brought it back at a width that has no bar');
    });

    test('stays hidden on a page that is not in the navigation', async () => {
      const page = await start({ markup: MARKUP.replace(' active', '') });

      assert.equal(page.geometry().opacity, '0');
    });
  });

  test('a page with no navbar is left alone', async () => {
    const page = await start({ markup: '<div class="wrapper"><p>no navigation here</p></div>' });

    assert.equal(page.sentinel(), null);
    assert.equal(page.window.document.querySelectorAll('.navindicator').length, 0);
  });
});
