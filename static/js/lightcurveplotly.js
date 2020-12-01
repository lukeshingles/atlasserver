// 2018-06-25 KWS Javascript Lightcurve Plotting code using Plotly.
//                The code was originally written for flot, so a
//                conversion needs to be done at the beginning.

// Javascript code to plot the lightcurves.  NOTE that it gets its data
// a data variable in the calling page.  The trick is setting that data
// correctly.
//
// This code is free of HTML tags, with the exception of <DIV>.
//
// The code requires the following data to be set in the calling HTML:
//
// * jslcdata - an array of filter arrays - e.g. for each filter do this...
//              jslcdata.push([[55973.492, 20.4057, 0.024156], [55973.4929, 20.3998, 0.022031]]);
//
// * jslabels - an array of labels - the same length as the array of filters - e.g.
//              jslabels.push("g");
//
// * jslclimits - a dictionary of limit values, currently xmin, xmax, ymin, ymax,
//                discoveryDate and today.

// First of all, setup some global variable based on data min and max to
// setup the padding on the graph and the x2 axis. This is done here, rather
// than in the calling page, because the padding, etc is presentation specific.

// GLOBAL VARIABLES BEGIN

// 2013-02-06 KWS Wrap the entire code in an anonymous function block. This forces
//                everything within here into a different scope.  It means that the
//                plot code can be called multiple times on the same page without
//                worrying about variable name clashes. Needed for window resize.
(function () {

// Need to set the div ID from the global data
var locallcdivname = lcdivname;
//var lightcurve = $(locallcdivname);

// Always refer to the external data via the global variable and lcdivname.

var pad = 20.0; // i.e. 5 percent
var xpadding = (jslimitsglobal[locallcdivname]["today"] - jslimitsglobal[locallcdivname]["xmin"])/pad;
var xmin = jslimitsglobal[locallcdivname]["xmin"] - xpadding;
var xmax = jslimitsglobal[locallcdivname]["xmax"] + xpadding;
var x2min = jslimitsglobal[locallcdivname]["xmin"] - jslimitsglobal[locallcdivname]["discoveryDate"] - xpadding;
var x2max = jslimitsglobal[locallcdivname]["today"] - jslimitsglobal[locallcdivname]["discoveryDate"] + xpadding;
var ymin = jslimitsglobal[locallcdivname]["ymin"];
var ymax = jslimitsglobal[locallcdivname]["ymax"];

// color palette for each data series (up to 20 at the moment)
var colors = ["#6A5ACD", //SlateBlue
              "#008000", //Green
              "#DAA520", //GoldenRod
              "#A0522D", //Sienna
              "#FF69B4", //HotPink
              "#DC143C", //Crimson
              "#708090", //SlateGray
              "#FFD700", //Gold
              "#0000FF", //Blue
              "#4B0082", //Indigo
              "#800080", //Purple
              "#008B8B", //DarkCyan
              "#FF8C00", //Darkorange
              "#A52A2A", //Brown
              "#DB7093", //PaleVioletRed
              "#800000", //Maroon
              "#B22222", //FireBrick
              "#9ACD32", //YellowGreen
              "#FA8072", //Salmon
              "#000000"]; //Black

// Should feed the colors form the calling page - better still, the CSS
var plotColors = { "backgroundColor": "#FFFFFF",
                   "axisColor": "#000000",
                   "tickColor": "#BFBFBF",
                   "shadingColor": "#DDDDDD",
                   "tooltipBackground": "#EEEEFF",
                   "tooltipBorder": "#FFDDDD",
                   "todaylineColor": "FF0000"};


// GLOBAL VARIABLES END


// So... Flot wanted [[x, y, error], [x, y, error], ...]
// Plotly wants [x, x, ...], [y, y, ...], [error, error, ...]. Should be easy to convert,
// but it's a bit of a pain!

// All the lightcurve data
var data = [];

for(filter=0;filter<jslcdataglobal[locallcdivname].length;filter++){
  // All the filter data
  var detx = [];
  var dety = [];
  var dete = [];
  var nondetx = [];
  var nondety = [];
  if (jslcdataglobal[locallcdivname][filter]){

    for(lc=0; lc<jslcdataglobal[locallcdivname][filter].length; lc++){
      // Split out the dets and non-dets into separate arrays
      if (jslcdataglobal[locallcdivname][filter][lc]){
        if (jslcdataglobal[locallcdivname][filter][lc].length > 0){
          if (jslcdataglobal[locallcdivname][filter][lc].length == 3){
            // It's a det
            detx.push(jslcdataglobal[locallcdivname][filter][lc][0]);
            dety.push(jslcdataglobal[locallcdivname][filter][lc][1]);
            dete.push(jslcdataglobal[locallcdivname][filter][lc][2]);
            }
          else {
            // It's a non-det
            nondetx.push(jslcdataglobal[locallcdivname][filter][lc][0]);
            nondety.push(jslcdataglobal[locallcdivname][filter][lc][1]);
            }
          }
        }
      }
      // Add the plot properties

      var tracedets = {
        x: detx,
        y: dety,
        error_y: {
          type: 'data',
          array: dete,
          visible: true,
          width: errorbarsize,
          color: colors[jslabelsglobal[locallcdivname][filter]['color']],
          opacity: 0.4
        },
        type: 'scatter',
        mode: 'markers',
        name: jslabelsglobal[locallcdivname][filter]['label'],
        marker: {
            color: colors[jslabelsglobal[locallcdivname][filter]['color']],
            opacity: 0.4,
            line: {
                width: 0,
                color: 'black'
                },
            size: markersize
            }
        };

        if (jslabelsglobal[locallcdivname][filter]['label'].charAt(0) == "-")
        {
          tracedets['marker']['symbol'] = 'diamond';
        }
        else
        {
          tracedets['marker']['symbol'] = 'circle';
        }

        data.push(tracedets);
     
        var tracenondets = {
        x: nondetx,
        y: nondety,
        type: 'scatter',
        mode: 'markers',
        name: jslabelsglobal[locallcdivname][filter]['label'],
        marker: {
            color: colors[jslabelsglobal[locallcdivname][filter]['color']],
            opacity: 0.4,
            symbol: 'limit-arrow',
            line: {
                width: 0,
                color: colors[jslabelsglobal[locallcdivname][filter]['color']]
                },
            size: arrowsize
            }
        }; 
        data.push(tracenondets);
      }
  }

if (typeof lcplotwidth !== 'undefined')
{
  w = lcplotwidth;
}
else
{
  w = 0.9 * $(locallcdivname).innerWidth();
}

if (locallcdivname.includes("flux"))
{
  yautorange = true;
  ylabel = 'Flux / \u00B5Jy';
}
else
{
  yautorange = 'reversed';
  ylabel = 'AB Mag';
}

var layout = { showlegend: true,
               yaxis: {range:[ymin, ymax],
                       autorange: yautorange,
                       tickformat: ".1f",
                       hoverformat: ".2f",
                       title: ylabel},
                xaxis: { tickformat: ".f",
                         hoverformat: ".5f",
                         range: [xmin, xmax],
                         title: "mjd" },
               margin: {l: 50, r: 0, b: 30, t: 30},
               width: w, //window.innerWidth,
               height: lcplotheight} //window.innerHeight 
               //paper_bgcolor: 'rgba(0,0,0,0)'}
               //plot_bgcolor: 'rgba(0,0,0,0)'}

// 2018-10-11 KWS Add another x axis if not forced photometry
if (!(locallcdivname.includes("forced")))
{
  layout["xaxis2"] = { tickformat: ".f",
                       overlaying: "x",
                       zeroline: false,
                       side: "top",
                       hoverformat: ".5f",
                       range: [x2min, x2max],
                       title: "days since earliest detection" }
}

Plotly.newPlot(locallcdivname.replace('#',''), data, layout, {displayModeBar: false});

// Resize all the lightcurve plots when the window size is changed.
$(window).bind("resize.lcplot", function() {
  Object.keys(jslcdataglobal).forEach(function(key) {
    if (typeof lcplotwidth !== 'undefined')
    {
      w = lcplotwidth;
      // No need to resize - do nothing.
    }
    else
    {
      w = 0.9 * $(locallcdivname).innerWidth();
      Plotly.relayout(key.replace('#',''), {
        width: w, //window.innerWidth,
        height: lcplotheight //window.innerHeight
      })
    }

  });
});

})();
