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
// * jslcdata - an array of filter arrays. Each point is
//              [mjd, flux / uJy, flux error / uJy, magnitude, magnitude error] - e.g.
//              jslcdata.push([[55973.492, 5730, 127, 20.4057, 0.024156]]);
//              The magnitude is negative where the flux is. A point whose error is null is an
//              upper limit rather than a detection, and its value is the depth of the image: a
//              point can be a measurement in flux and a limit in magnitude, which is why each
//              unit has its own error.
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
"use strict";
(function () {

    // Need to set the div ID from the global data
    var locallcdivname = lcdivname;
    //var lightcurve = $(locallcdivname);

    var divid = locallcdivname.replace('#', '');
    var plotdiv = document.getElementById(divid);

    // The data script is fetched asynchronously, so by the time it runs the plot may have been
    // unmounted (which deletes these globals and removes the div). Bail out instead of throwing.
    if (!jslimitsglobal[locallcdivname] || !jslcdataglobal[locallcdivname] || !plotdiv) {
        return;
    }

    // Always refer to the external data via the global variable and lcdivname.

    var pad = 20.0; // i.e. 5 percent
    var xpadding = (jslimitsglobal[locallcdivname]["today"] - jslimitsglobal[locallcdivname]["xmin"]) / pad;
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

    /*
    A colour from the palette above, with an alpha channel added.

    An error bar takes its transparency from the alpha channel of its colour, because Plotly 3
    dropped the `opacity` property that an error bar had before. A marker keeps its own `opacity`.
    */
    function withalpha(hexcolor, alpha) {
        var red = parseInt(hexcolor.slice(1, 3), 16);
        var green = parseInt(hexcolor.slice(3, 5), 16);
        var blue = parseInt(hexcolor.slice(5, 7), 16);
        return 'rgba(' + red + ', ' + green + ', ' + blue + ', ' + alpha + ')';
    }

    /*
    The unit the visitor has selected, which the queue page writes onto the plot div.

    The name of the div is the default, so a page that does not offer the button plots what it
    always plotted.
    */
    function selectedunit() {
        if (plotdiv.dataset.unit === 'mag' || plotdiv.dataset.unit === 'flux') {
            return plotdiv.dataset.unit;
        }
        return locallcdivname.includes("flux") ? 'flux' : 'mag';
    }

    /*
    Draw the plot in the unit that is selected now.

    Plotly.react is given the whole plot again, so this is also how the plot is redrawn when the
    visitor presses the flux/magnitude button. It reads the theme colours each time, so a redraw
    after a change of theme uses the colours in force.
    */
    function draw() {
        var unit = selectedunit();
        var ismag = unit === 'mag';
        // the faintest and brightest magnitude drawn, which is what a magnitude plot is ranged on
        var faintest = null;
        var brightest = null;

        // Backgrounds, text and gridlines for the light or dark theme, from theme.js, which reads them
        // off the same Bootstrap custom properties the page around the plot is coloured from. Plotly
        // paints into a canvas, so these cannot come from the stylesheet the way the rest does. The
        // series colours above are the data and are the same in either theme.
        //
        // The fallback is for a page without theme.js: Plotly then draws with its own defaults, which
        // are a readable plot on a light background.
        var theme = (window.atlasTheme && window.atlasTheme.plotlyColors()) || { axis: {}, hoverlabel: {} };

        // So... Flot wanted [[x, y, error], [x, y, error], ...]
        // Plotly wants [x, x, ...], [y, y, ...], [error, error, ...]. Should be easy to convert,
        // but it's a bit of a pain!

        // All the lightcurve data
        var data = [];

        for (var filter = 0; filter < jslcdataglobal[locallcdivname].length; filter++) {
            var filterpoints = jslcdataglobal[locallcdivname][filter];
            if (!filterpoints) {
                continue;
            }

            var filtercolor = colors[jslabelsglobal[locallcdivname][filter]['color']];
            var filterlabel = jslabelsglobal[locallcdivname][filter]['label'];

            /*
            The series to draw for this filter.

            A flux plot draws the filter as one series. A magnitude plot draws two, because a
            negative flux is a different measurement from a positive one of the same size. The
            result file marks such a point with a negative magnitude; here its points carry a
            label with a leading "-" and are drawn as diamonds, which is the convention the
            results page describes.
            */
            var serieslist = ismag ? [false, true] : [false];

            /*
            One legend entry for the filter, carried by the first of its traces that has points.

            A filter is drawn as up to four traces -- detections and limits, each of them positive
            and negative -- and they are one thing to the reader. Naming them separately would put
            the filter in the legend several times over, once with a leading "-".
            */
            var legendshown = false;

            for (var series = 0; series < serieslist.length; series++) {
                var isnegative = serieslist[series];
                // a leading "-" marks the negative fluxes, as the results page describes
                var serieslabel = isnegative ? '-' + filterlabel : filterlabel;

                // All the filter data
                var detx = [];
                var dety = [];
                var dete = [];
                var nondetx = [];
                var nondety = [];

                for (var lc = 0; lc < filterpoints.length; lc++) {
                    // Split out the dets and non-dets into separate arrays
                    if (filterpoints[lc] && filterpoints[lc].length > 0) {
                        var pointmjd = filterpoints[lc][0];
                        var pointflux = filterpoints[lc][1];
                        var pointmag = filterpoints[lc][3];

                        /*
                        The file writes a negative magnitude for a negative flux, so the sign of
                        the magnitude is what puts the point in one series or the other. It is the
                        size that the axis shows.

                        A magnitude of exactly zero is the file's way of saying it has none, which
                        it writes where the flux is zero. Zero is not a magnitude this survey can
                        measure -- it would be one of the brightest objects in the sky -- and
                        drawing it would pull the axis down and flatten every real point.
                        */
                        if (ismag && (pointmag === 0 || (pointmag < 0) !== isnegative)) {
                            continue;
                        }

                        var pointy = ismag ? Math.abs(pointmag) : pointflux;
                        if (ismag) {
                            faintest = faintest === null ? pointy : Math.max(faintest, pointy);
                            brightest = brightest === null ? pointy : Math.min(brightest, pointy);
                        }
                        // a limit has no error, which is what tells it from a detection
                        var pointerror = ismag ? filterpoints[lc][4] : filterpoints[lc][2];

                        if (pointerror != null) {
                            // It's a det
                            detx.push(pointmjd);
                            dety.push(pointy);
                            dete.push(pointerror);
                        }
                        else {
                            // It's a non-det
                            nondetx.push(pointmjd);
                            nondety.push(pointy);
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
                        color: withalpha(filtercolor, 0.4)
                    },
                    type: 'scatter',
                    mode: 'markers',
                    // the name is what the hover label reads, so a negative point still says so
                    name: serieslabel,
                    legendgroup: filterlabel,
                    marker: {
                        color: filtercolor,
                        opacity: 0.4,
                        symbol: isnegative ? 'diamond' : 'circle',
                        line: {
                            width: 0,
                            color: 'black'
                        },
                        size: markersize
                    }
                };

                var tracenondets = {
                    x: nondetx,
                    y: nondety,
                    type: 'scatter',
                    mode: 'markers',
                    name: serieslabel,
                    // the group is also what makes one click on the legend hide the whole filter
                    legendgroup: filterlabel,
                    marker: {
                        color: filtercolor,
                        opacity: 0.4,
                        // a bar with an arrow below it, which is how an upper limit is drawn
                        symbol: 'arrow-bar-down',
                        line: {
                            width: 0,
                            color: filtercolor
                        },
                        size: arrowsize
                    }
                };

                /*
                A series with no points is left out, rather than pushed as an empty trace.

                An empty trace draws nothing but still takes a legend entry, so keeping it would
                name every filter twice over -- and in a magnitude plot would promise a negative
                series for a filter whose fluxes are all positive.
                */
                if (detx.length > 0) {
                    tracedets.showlegend = !legendshown;
                    legendshown = true;
                    data.push(tracedets);
                }
                if (nondetx.length > 0) {
                    tracenondets.showlegend = !legendshown;
                    legendshown = true;
                    data.push(tracenondets);
                }
            }
        }

        /*
        A magnitude runs the other way round from a flux, so a magnitude plot is drawn with the
        axis reversed and the bright end at the top.

        The limits in the data file are fluxes. A magnitude plot therefore finds its own limits
        from the points it draws, because those fluxes do not convert to the range it needs.
        */
        var ylabel = ismag ? 'AB Mag' : 'Flux / \u00B5Jy';

        /*
        A magnitude plot is ranged on the magnitudes, not on the error bars.

        A flux near zero carries a magnitude error of tens or hundreds of magnitudes, so letting
        Plotly find the range would fit those bars and press every real point into a line. The
        bars are still drawn; they simply run off the top and bottom of the plot.

        The range descends because a magnitude does: the bright end belongs at the top.
        */
        var magpadding = faintest === null ? 0 : (faintest - brightest) / 20 + 0.05;
        var yaxis = ismag
            ? (faintest === null
                ? { autorange: 'reversed' }
                : { range: [faintest + magpadding, brightest - magpadding], autorange: false })
            : { range: [ymin, ymax], autorange: false };

        // Object.assign merges the theme's axis colours in beside the ranges, formats and titles, which
        // are what this file has to say about an axis; the two name no property in common.
        //
        // An axis title is an object with a `text` property. Plotly discards a plain string, which
        // leaves the axis with no label.
        var layout = {
            showlegend: true,
            // Every filter at the epoch under the pointer, in one label, instead of whichever marker the
            // pointer happens to be over. The two filters are observed within about an hour of each other,
            // so their points sit close enough together that reading one without the other is the harder
            // way to use this plot.
            hovermode: 'x unified',
            paper_bgcolor: theme.paper_bgcolor,
            plot_bgcolor: theme.plot_bgcolor,
            font: theme.font,
            legend: theme.legend,
            hoverlabel: theme.hoverlabel,
            yaxis: Object.assign({
                tickformat: ".1f",
                hoverformat: ".2f",
                title: { text: ylabel }
            }, yaxis, theme.axis),
            xaxis: Object.assign({
                tickformat: ".0f",
                hoverformat: ".5f",
                range: [xmin, xmax],
                title: { text: "mjd" }
            }, theme.axis),
            // b: 50 gives the x axis title a row of its own. At 30 the title overlaps the tick
            // labels, because the plot height is fixed and Plotly does not expand a set margin.
            margin: { l: 70, r: 0, b: 50, t: 30 },
            height: lcplotheight,
        }

        // 2018-10-11 KWS Add another x axis if not forced photometry
        if (!(locallcdivname.includes("forced"))) {
            layout["xaxis2"] = Object.assign({
                tickformat: ".0f",
                overlaying: "x",
                zeroline: false,
                side: "top",
                hoverformat: ".5f",
                range: [x2min, x2max],
                title: { text: "days since earliest detection" }
            }, theme.axis)
        }

        Plotly.react(divid, data, layout, { displayModeBar: false, responsive: true });
    }

    /*
    Offer the redraw to the queue page, under the id of the div, and draw the plot for the first
    time. The queue page deletes this entry when it unmounts the plot, along with the data globals.
    */
    window.atlasLightcurves = window.atlasLightcurves || {};
    window.atlasLightcurves[divid] = draw;

    draw();

})();
