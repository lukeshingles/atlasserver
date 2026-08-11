/*
Replaces Django REST framework's rest_framework/js/default.js on the browsable API.

That file drives the page through Bootstrap 3's jQuery plugins -- $.fn.tooltip, $.fn.tab,
$.fn.modal -- and Bootstrap 5 has no jQuery interface at all, so with Bootstrap 5 loaded the
original throws on its first call and the tab setup below it never runs, leaving the POST form
hidden. The same behaviour is expressed here against Bootstrap 5's own API.

Bootstrap 5 also renamed the attributes it acts on, so DRF's data-toggle/data-target/data-dismiss
are rewritten to data-bs-*. Its listeners are delegated from the document rather than bound to each
element, so renaming them after DRF's markup is in place is enough for them to be found.

jQuery is still loaded for DRF's own ajax-form.js and csrf.js, which do not touch Bootstrap.

Not carried over: default.js opens an #errorModal on load, which only rest_framework/admin.html
contains, and this site does not use the AdminRenderer.
*/

(function () {
  'use strict';

  var ATTRIBUTE_RENAMES = {
    'data-toggle': 'data-bs-toggle',
    'data-target': 'data-bs-target',
    'data-dismiss': 'data-bs-dismiss'
  };

  function readCookie(name) {
    var match = document.cookie.match(new RegExp('(?:^|;\\s*)' + name + '=([^;]*)'));
    return match ? decodeURIComponent(match[1]) : null;
  }

  function renameBehaviourAttributes() {
    Object.keys(ATTRIBUTE_RENAMES).forEach(function (oldname) {
      document.querySelectorAll('[' + oldname + ']').forEach(function (element) {
        element.setAttribute(ATTRIBUTE_RENAMES[oldname], element.getAttribute(oldname));
        element.removeAttribute(oldname);
      });
    });
  }

  /* Bootstrap 5 finds the tabs of a group by .nav-link, and styles them by it; DRF's markup has
     bare anchors inside .nav-tabs > li. Without this the switcher is unstyled and clicking a tab
     does nothing. */
  function classifyTabs() {
    document.querySelectorAll('.form-switcher > li').forEach(function (item) {
      item.classList.add('nav-item');
    });
    document.querySelectorAll('.form-switcher > li > a').forEach(function (link) {
      link.classList.add('nav-link');
      link.setAttribute('role', 'tab');
    });
  }

  /* Which of HTML form / raw data is shown, remembered across pages in a cookie as DRF does it.
     Each switcher is resolved separately: a page with both a POST and a PUT form has two. */
  function restoreSelectedTab() {
    var selected = (readCookie('tabstyle') || '').replace(/[^a-z-]/g, '');

    document.querySelectorAll('.form-switcher').forEach(function (switcher) {
      var tab = (selected && switcher.querySelector('a[name="' + selected + '"]')) ||
        switcher.querySelector('a');

      if (tab) {
        bootstrap.Tab.getOrCreateInstance(tab).show();
      }
    });

    document.querySelectorAll('.form-switcher a[data-bs-toggle="tab"]').forEach(function (tab) {
      tab.addEventListener('click', function () {
        document.cookie = 'tabstyle=' + this.name + '; path=/';
      });
    });
  }

  function init() {
    renameBehaviourAttributes();
    classifyTabs();

    if (window.prettyPrint) {
      prettyPrint();  // JSON syntax highlighting, from DRF's prettify-min.js
    }

    // Bootstrap 3 initialised tooltips from a data attribute; Bootstrap 5 requires each one
    document.querySelectorAll('.js-tooltip').forEach(function (element) {
      new bootstrap.Tooltip(element, { delay: 1000, container: 'body' });
    });

    restoreSelectedTab();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
