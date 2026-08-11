/*
Adds a copy button to each <pre class="codeblock"> on the API guide.

The button is created here rather than written into the template so that the markup stays plain
<pre><code>, and so that a page without JavaScript shows the examples with their frame and no
control that cannot work. It sits in a wrapper alongside the <pre> rather than inside it: the block
scrolls sideways for long lines, and a button within that box would scroll away with the code.

Nothing is added at all where navigator.clipboard is missing, which is any page served over plain
http to a host other than localhost -- the API is unavailable there, so the button could only fail.
*/

(function () {
  'use strict';

  var RESTORE_LABEL_AFTER_MS = 2000;

  function copyButton(code) {
    var button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn btn-sm btn-outline-secondary codeblock-copy';
    button.textContent = 'Copy';
    // polite, so the change of label is announced rather than read over whatever has focus
    button.setAttribute('aria-live', 'polite');
    button.setAttribute('aria-label', 'Copy code to clipboard');

    var restore = null;
    button.addEventListener('click', function () {
      navigator.clipboard.writeText(code.textContent).then(
        function () {
          button.textContent = 'Copied!';
        },
        function () {
          // the browser can refuse the write (permissions, or a document without focus), and a
          // button that silently does nothing is worse than one that says so
          button.textContent = 'Press Ctrl-C';
        }
      );

      clearTimeout(restore);
      restore = setTimeout(function () {
        button.textContent = 'Copy';
      }, RESTORE_LABEL_AFTER_MS);
    });

    return button;
  }

  function enhance(block) {
    var code = block.querySelector('code');
    if (!code) {
      return;
    }

    var wrapper = document.createElement('div');
    wrapper.className = 'codeblock-wrap';
    block.parentNode.insertBefore(wrapper, block);
    wrapper.appendChild(block);
    wrapper.appendChild(copyButton(code));
  }

  function init() {
    if (!navigator.clipboard) {
      return;
    }

    document.querySelectorAll('pre.codeblock').forEach(enhance);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
