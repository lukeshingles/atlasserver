/*
Light/dark mode for the site.

Bootstrap 5.3 reads a data-bs-theme attribute off <html> and recolours everything it styles from
it; this file decides what that attribute should say and keeps the navbar control in step with it.

The choice is one of three: "light", "dark", or -- when nothing is stored -- follow whatever the
operating system asks for through prefers-color-scheme, which is the state the navbar calls "Auto".

It is loaded from <head> without defer, so the attribute is on <html> before the first paint. A
deferred or end-of-body script would let the browser paint a light page first and then repaint it
dark, which is a visible flash on every navigation.

window.atlasTheme is the interface for the parts of the page that are drawn rather than styled:
lightcurveplotly.js asks plotlyColors() for the colours to build a plot with, stats.html asks
themeBokehItem() to colour a bokeh chart before it is built, and the "atlas:theme" event fires
whenever the mode changes so an existing plot or chart can be recoloured in place.
*/

(function () {
  'use strict';

  var STORAGE_KEY = 'atlas-theme';
  var darkMediaQuery = window.matchMedia('(prefers-color-scheme: dark)');

  // localStorage throws rather than returning null when a browser is set to refuse site data, and
  // it is only remembering a preference -- a visitor who has turned it off gets Auto every page
  // load, which is a working site, whereas an exception here leaves <html> without the attribute.
  function storedMode() {
    try {
      var stored = localStorage.getItem(STORAGE_KEY);
      return stored === 'light' || stored === 'dark' ? stored : 'auto';
    } catch (err) {
      return 'auto';
    }
  }

  function storeMode(mode) {
    try {
      if (mode === 'auto') {
        localStorage.removeItem(STORAGE_KEY);
      } else {
        localStorage.setItem(STORAGE_KEY, mode);
      }
    } catch (err) {
      // the preference is not carried to the next page load; chosenMode still holds it for this one
    }
  }

  /*
  The mode in force on this page, and the authority for it.

  Storage is only how the choice reaches the next page load: a browser set to refuse site data
  rejects the write, so asking storage what the visitor chose would answer "auto" however they had
  just set the control -- and the media query listener below would then take an explicit Light or
  Dark to be Auto and recolour the page out from under them at the next change of system preference.
  */
  var chosenMode = storedMode();

  /** The mode as the two values Bootstrap understands, resolving "auto" against the system. */
  function resolve(mode) {
    if (mode === 'light' || mode === 'dark') {
      return mode;
    }
    return darkMediaQuery.matches ? 'dark' : 'light';
  }

  function currentTheme() {
    return document.documentElement.getAttribute('data-bs-theme') === 'dark' ? 'dark' : 'light';
  }

  /*
  Colours for the light curve plots, which Plotly paints into a canvas and so cannot take from the
  stylesheet the way the rest of the page does. Both backgrounds are transparent, so a plot sits on
  whatever the page background is; the text and gridlines are read off the same Bootstrap custom
  properties the surrounding page is coloured from, so there is no second set of values to keep in
  step with the theme. The series colours are the data and belong to lightcurveplotly.js.

  `axis` is separate from the rest because it applies once per axis, and which axes a plot has
  depends on what is being plotted -- see below and lightcurveplotly.js.
  */
  function plotlyColors() {
    var styles = getComputedStyle(document.documentElement);
    var textcolor = styles.getPropertyValue('--bs-body-color').trim() || '#000000';
    var gridcolor = styles.getPropertyValue('--bs-border-color').trim() || '#BFBFBF';

    return {
      paper_bgcolor: 'rgba(0,0,0,0)',
      plot_bgcolor: 'rgba(0,0,0,0)',
      font: { color: textcolor },
      legend: { bgcolor: 'rgba(0,0,0,0)' },
      axis: { gridcolor: gridcolor, zerolinecolor: gridcolor, linecolor: gridcolor },
      // The hover label is the one part that cannot be transparent: it sits over the plot and has to
      // be read against whatever is behind it. --bs-body-bg rather than the tertiary surface, because
      // the plot itself is transparent, so the label's background is what separates it from the row.
      hoverlabel: {
        bgcolor: styles.getPropertyValue('--bs-body-bg').trim() || '#ffffff',
        bordercolor: gridcolor,
        font: { color: textcolor }
      }
    };
  }

  /*
  Recolour light curves that are already on screen. Plotly.relayout redraws with the new colours and
  keeps whatever the visitor has panned or zoomed to, which rebuilding the plot would discard.

  Every graph div Plotly has drawn into carries `data` and `layout` properties, which is what
  separates a live plot from a .plot div whose data file has not arrived yet. The axis colours are
  only sent for the axes that div already has: relayout creates an axis it is given attributes for,
  so naming xaxis2 unconditionally would draw a second x axis on the plots that have one axis.
  */
  function recolourPlots() {
    if (!window.Plotly) {
      return;
    }

    // Not window.atlasLightcurves[divid](), which lightcurveplotly.js publishes to redraw a plot
    // in the other unit: that redraw passes an explicit range and would discard the pan and zoom.


    var colors = plotlyColors();

    document.querySelectorAll('div.plot').forEach(function (div) {
      if (!div.data || !div.layout) {
        return;
      }

      var update = {
        paper_bgcolor: colors.paper_bgcolor,
        plot_bgcolor: colors.plot_bgcolor,
        'font.color': colors.font.color,
        'legend.bgcolor': colors.legend.bgcolor,
        'hoverlabel.bgcolor': colors.hoverlabel.bgcolor,
        'hoverlabel.bordercolor': colors.hoverlabel.bordercolor,
        'hoverlabel.font.color': colors.hoverlabel.font.color
      };

      ['xaxis', 'xaxis2', 'yaxis'].forEach(function (axisname) {
        if (div.layout[axisname]) {
          Object.keys(colors.axis).forEach(function (property) {
            update[axisname + '.' + property] = colors.axis[property];
          });
        }
      });

      window.Plotly.relayout(div, update);
    });
  }

  /*
  Colours for the two stats charts. bokeh paints a chart into a canvas from the properties of the
  models it is given, so -- as with the light curves -- the stylesheet cannot reach it.

  The table is keyed by the bokeh model each set of properties belongs to. The two functions below
  both work from it: one colours the models a response carries, the other the models of a chart
  that is already drawn.

  A figure is itself transparent (see forcephot/views.py) and sits on the page background, so only
  its outline is named here. The colours of the bars and of the sky are the data, and views.py
  keeps them with the data.
  */
  function bokehColors() {
    var styles = getComputedStyle(document.documentElement);
    var textcolor = styles.getPropertyValue('--bs-body-color').trim() || '#212529';
    var linecolor = styles.getPropertyValue('--bs-border-color').trim() || '#DEE2E6';

    var axis = {
      axis_label_text_color: textcolor,
      major_label_text_color: textcolor,
      axis_line_color: linecolor,
      major_tick_line_color: linecolor,
      minor_tick_line_color: linecolor
    };

    return {
      Figure: { outline_line_color: linecolor },
      Title: { text_color: textcolor },
      // the legend sits on a transparent figure, so the page background is what separates it from
      // the bars behind it
      Legend: {
        label_text_color: textcolor,
        background_fill_color: styles.getPropertyValue('--bs-body-bg').trim() || '#FFFFFF',
        border_line_color: linecolor
      },
      // the zero line the two halves of the usage chart are mirrored about
      Span: { line_color: linecolor },
      LinearAxis: axis,
      CategoricalAxis: axis
    };
  }

  /*
  Colour the models in the JSON of a statsusagechart or statscoordchart response.

  bokeh writes a model as an object that gives the name of its type and the properties which
  differ from the defaults. Each later mention of that model is its id alone. Thus the colours go
  on the one object that carries `attributes`.

  The page does this before it calls Bokeh.embed.embed_item, so bokeh draws the chart in the right
  colours the first time.
  */
  function themeBokehItem(item) {
    var colors = bokehColors();

    (function paint(node) {
      if (Array.isArray(node)) {
        node.forEach(paint);
        return;
      }
      if (node === null || typeof node !== 'object') {
        return;
      }
      if (node.type === 'object' && colors[node.name]) {
        node.attributes = Object.assign(node.attributes || {}, colors[node.name]);
      }
      Object.keys(node).forEach(function (key) {
        paint(node[key]);
      });
    })(item);

    return item;
  }

  /*
  Recolour the stats charts that are already on screen. Every chart bokeh has built is a document
  in Bokeh.documents. A change to a property redraws the part of the chart that shows it, and thus
  keeps whatever the visitor has panned or zoomed to.

  setv throws on a property that the model does not have, so a model is only given the properties
  of its own type.
  */
  function recolourBokehCharts() {
    if (!window.Bokeh || !window.Bokeh.documents) {
      return;
    }

    var colors = bokehColors();

    window.Bokeh.documents.forEach(function (doc) {
      doc.all_models.forEach(function (model) {
        if (colors[model.type]) {
          model.setv(colors[model.type]);
        }
      });
    });
  }

  function applyMode(mode) {
    document.documentElement.setAttribute('data-bs-theme', resolve(mode));
    document.dispatchEvent(new CustomEvent('atlas:theme', { detail: { theme: currentTheme() } }));
  }

  /** Mark the chosen entry in the dropdown and show the icon that goes with it. */
  function showActiveMode(mode) {
    document.querySelectorAll('[data-theme-value]').forEach(function (item) {
      var chosen = item.getAttribute('data-theme-value') === mode;
      item.classList.toggle('active', chosen);
      item.setAttribute('aria-checked', chosen ? 'true' : 'false');
    });

    // a class the stylesheet can transition, rather than d-none: the three icons are stacked, and
    // display: none cannot cross-fade into anything
    document.querySelectorAll('.themeicon').forEach(function (icon) {
      icon.classList.toggle('themeicon-shown', icon.classList.contains('themeicon-' + mode));
    });
  }

  /** Adopt `mode` for this page, remember it for the next, and bring the control into step. */
  function setMode(mode) {
    chosenMode = mode;
    storeMode(mode);
    applyMode(mode);
    showActiveMode(mode);
  }

  function init() {
    showActiveMode(chosenMode);

    // the control is hidden in the template, so it never appears on a page where this file did
    // not run and its buttons would do nothing
    document.querySelectorAll('.themeswitch').forEach(function (control) {
      control.classList.remove('d-none');
    });

    document.querySelectorAll('[data-theme-value]').forEach(function (item) {
      item.addEventListener('click', function () {
        setMode(item.getAttribute('data-theme-value'));
      });
    });

    document.addEventListener('atlas:theme', recolourPlots);
    document.addEventListener('atlas:theme', recolourBokehCharts);
  }

  /*
  Follow the system preference as it changes, but only while the mode is "auto": an explicit choice
  outranks the system, and a visitor who has picked light should not be switched when their desktop
  turns dark for the evening.
  */
  function followSystem() {
    if (chosenMode === 'auto') {
      applyMode('auto');
    }
  }

  window.atlasTheme = { current: currentTheme, plotlyColors: plotlyColors, themeBokehItem: themeBokehItem };

  applyMode(chosenMode);

  // the control is further down the page than this script, so it cannot be wired up yet
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  /*
  Registered last, and behind a feature test, because it is the one part of this file that is an
  enhancement: reacting to a change of system preference must not be able to stop the theme being
  applied or the control being revealed, which is what a throw here would do to everything above it.

  MediaQueryList only became an EventTarget in Safari 14. From Safari 12.1 and iOS 13, where
  prefers-color-scheme itself works, addListener is the only spelling there is, and reaching for
  addEventListener throws -- so this is a test of the two methods rather than of a version. Where
  neither exists the theme is still applied and still switchable by hand; it just stops tracking the
  system while the page is open.
  */
  if (darkMediaQuery.addEventListener) {
    darkMediaQuery.addEventListener('change', followSystem);
  } else if (darkMediaQuery.addListener) {
    darkMediaQuery.addListener(followSystem);
  }
})();
