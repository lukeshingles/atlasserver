/*
The two parts of the navbar that need measuring rather than styling.

1. .navbar-stuck, added once the bar has scrolled to the top of the window. The stickiness itself is
   CSS; this only says when the bar has left its place in the page, which CSS cannot express.

2. The indicator under the navigation, which marks the current page and follows the pointer. Its
   position and width are whichever link it is on, so they have to be read off the rendered page;
   main.css owns everything else about it.

Both are enhancements to a navbar that works without them: the bar is sticky either way, and the
current page is underlined until this file replaces the underline with something better.
*/

(function () {
  'use strict';

  // matches the navbar-expand-lg breakpoint, below which the links are a stacked column
  var expanded = window.matchMedia('(min-width: 992px)');

  /*
  Watch for the navbar leaving the top of the page.

  Via a one-pixel sentinel above the bar rather than a scroll handler: a scroll handler runs on every
  frame of every scroll to answer a question that changes twice a page, and getting the answer means
  measuring the bar, which forces layout. The sentinel is pulled back out of the flow by its own
  negative margin, so it occupies no space.
  */
  function watchStuck(navbar) {
    if (!window.IntersectionObserver) {
      return;
    }

    var sentinel = document.createElement('div');
    sentinel.className = 'navbar-sentinel';
    navbar.parentNode.insertBefore(sentinel, navbar);

    new IntersectionObserver(function (entries) {
      navbar.classList.toggle('navbar-stuck', !entries[0].isIntersecting);
    }).observe(sentinel);
  }

  /** Put the indicator under `link`, or hide it when there is nothing to mark. */
  function indicator(nav) {
    var active = nav.querySelector('.nav-link.active');
    var bar = document.createElement('span');
    bar.className = 'navindicator';
    bar.setAttribute('aria-hidden', 'true');
    nav.classList.add('navindicator-host');
    nav.appendChild(bar);

    function moveTo(link) {
      // below the breakpoint the bar is display-less and the underline marks the page instead, so
      // there is nothing to measure and the numbers would be wrong by the time it matters again
      if (!link || !expanded.matches) {
        nav.style.setProperty('--navindicator-opacity', '0');
        return;
      }

      var navbox = nav.getBoundingClientRect();
      var linkbox = link.getBoundingClientRect();

      nav.style.setProperty('--navindicator-x', linkbox.left - navbox.left + 'px');
      nav.style.setProperty('--navindicator-width', linkbox.width + 'px');
      nav.style.setProperty('--navindicator-opacity', '1');
    }

    var settle = function () {
      moveTo(active);
    };

    nav.querySelectorAll('.nav-link').forEach(function (link) {
      link.addEventListener('mouseenter', function () {
        moveTo(link);
      });
      // focus as well as hover, so tabbing through the navigation moves the bar too
      link.addEventListener('focus', function () {
        moveTo(link);
      });
    });

    nav.addEventListener('mouseleave', settle);
    nav.addEventListener('focusout', settle);

    settle();

    // the transition is enabled a frame after the first measurement, so the bar is already under the
    // current page on load rather than sliding in from the left edge of the navigation
    requestAnimationFrame(function () {
      nav.classList.add('navindicator-animated');
    });

    // The link widths change with the window, with the breakpoint, and once a web font replaces the
    // fallback it was first measured in -- all of which move the thing the bar is supposed to be
    // under. ResizeObserver covers the first two by watching the navigation itself.
    if (window.ResizeObserver) {
      new ResizeObserver(settle).observe(nav);
    } else {
      window.addEventListener('resize', settle);
    }

    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(settle);
    }
  }

  function init() {
    var navbar = document.querySelector('nav.navbar');
    if (!navbar) {
      return;
    }

    watchStuck(navbar);

    // the left-hand list, which is the one holding the pages; the right-hand list is the account
    // and theme controls, and the current page is never one of those
    var nav = navbar.querySelector('.navbar-nav.me-auto');
    if (nav) {
      indicator(nav);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
