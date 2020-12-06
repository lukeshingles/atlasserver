#!/usr/bin/env python
# encoding: utf-8
"""
*Plot data returned from the ATLAS Forced Photometry service*

:Author:
    David Young

:Date Created:
    December  3, 2020

Usage:
    plot-atlas-fp <fpFile> [--stacked <objectName>]

Options:
    fpFile                path to the results file returned by the ATLAS FP service
    objectName            give a name for the object you are plotting (for plot title and filename)
    -h, --help            show this help message
    -s, --stacked         stack photometry from the smae night (and same filter)
"""
################# GLOBAL IMPORTS ####################
import sys
import os
from fundamentals import tools
import csv
import io
import math
# SUPPRESS MATPLOTLIB WARNINGS
import warnings
warnings.filterwarnings("ignore")
import matplotlib as mpl
import matplotlib.pyplot as plt
from matplotlib import dates
import numpy as np
from past.utils import old_div
import codecs
from fundamentals.download import get_now_datetime_filestamp
import matplotlib.ticker as mtick


def main(arguments=None):
    """
    *The main function used when ``plot-atlas-fp.py`` is run as a single script from the cl*
    """

    # SETUP THE COMMAND-LINE UTIL SETTINGS
    su = tools(
        arguments=arguments,
        docString=__doc__,
        logLevel="ERROR",
        options_first=False,
        projectName=False
    )
    arguments, settings, log, dbConn = su.setup()

    # UNPACK REMAINING CL ARGUMENTS USING `EXEC` TO SETUP THE VARIABLE NAMES
    # AUTOMATICALLY
    a = {}
    for arg, val in list(arguments.items()):
        if arg[0] == "-":
            varname = arg.replace("-", "") + "Flag"
        else:
            varname = arg.replace("<", "").replace(">", "")
        a[varname] = val
        if arg == "--dbConn":
            dbConn = val
            a["dbConn"] = val
        log.debug('%s = %s' % (varname, val,))

    fpFile = a["fpFile"]
    objectName = a["objectName"]
    stacked = a["stackedFlag"]

    epochs = read_and_simga_clip_data(log=log, fpFile=fpFile)

    plotFilePath = plot_lc(log=log, epochs=epochs,
                           objectName=objectName, stacked=stacked)

    print(f"The plot can be found at `{plotFilePath}`")

    return


def plot_lc(
        log,
        epochs,
        objectName=False,
        stacked=False):
    """*plot the lightcurve*

    **Key Arguments**

    - ``log`` -- logger
    - ``epochs`` -- dictionary of lightcurve data-points
    - ``objectName`` -- give a name for the object you are plotting (for plot title and filename). Default **False**
    - ``stacked`` -- stack photometry from the smae night (and same filter). Default **False**

    **Return**

    - ``plotFilePath`` -- path to the generated plot
    """
    log.debug('starting the ``plot_lc`` function')

    if not objectName:
        objectName = ""

    from astrocalc.times import conversions
    # CONVERTER TO CONVERT MJD TO DATE
    converter = conversions(
        log=log
    )

    # c = cyan, o = arange
    magnitudes = {
        'c': {'mjds': [], 'mags': [], 'magErrs': []},
        'o': {'mjds': [], 'mags': [], 'magErrs': []},
        'I': {'mjds': [], 'mags': [], 'magErrs': []},
    }

    # IF WE WANT TO 'STACK' THE PHOTOMETRY
    summedMagnitudes = {
        'c': {'mjds': [], 'mags': [], 'magErrs': []},
        'o': {'mjds': [], 'mags': [], 'magErrs': []},
        'I': {'mjds': [], 'mags': [], 'magErrs': []},
    }

    # SPLIT BY FILTER
    for epoch in epochs:
        if epoch["F"] in ["c", "o", "I"]:
            magnitudes[epoch["F"]]["mjds"].append(epoch["MJD"])
            magnitudes[epoch["F"]]["mags"].append(epoch["uJy"])
            magnitudes[epoch["F"]]["magErrs"].append(epoch["duJy"])

    # STACK OR NON-STACKED?
    if stacked:
        for fil, d in list(magnitudes.items()):
            distinctMjds = {}
            for m, f, e in zip(d["mjds"], d["mags"], d["magErrs"]):
                key = str(int(math.floor(m)))
                if key not in distinctMjds:
                    distinctMjds[key] = {
                        "mjds": [m],
                        "mags": [f],
                        "magErrs": [e]
                    }
                else:
                    distinctMjds[key]["mjds"].append(m)
                    distinctMjds[key]["mags"].append(f)
                    distinctMjds[key]["magErrs"].append(e)

            for k, v in list(distinctMjds.items()):
                summedMagnitudes[fil]["mjds"].append(
                    old_div(sum(v["mjds"]), len(v["mjds"])))
                summedMagnitudes[fil]["mags"].append(
                    old_div(sum(v["mags"]), len(v["mags"])))
                summedMagnitudes[fil]["magErrs"].append(sum(v["magErrs"]) / len(v["magErrs"]
                                                                                ) / math.sqrt(len(v["magErrs"])))
        magnitudes = summedMagnitudes

    # GENERATE THE FIGURE FOR THE PLOT
    fig = plt.figure(
        num=None,
        figsize=(10, 10),
        dpi=100,
        facecolor=None,
        edgecolor=None,
        frameon=True)

    mpl.rc('ytick', labelsize=20)
    mpl.rc('xtick', labelsize=20)
    mpl.rcParams.update({'font.size': 22})

    # FORMAT THE AXES
    ax = fig.add_axes(
        [0.1, 0.1, 0.8, 0.8],
        polar=False,
        frameon=True)
    ax.set_xlabel('MJD', labelpad=20)
    ax.set_ylabel('Apparent Magnitude', labelpad=15)

    # ATLAS OBJECT NAME LABEL AS TITLE
    fig.text(0.1, 1.02, objectName, ha="left", fontsize=40)

    # RHS AXIS TICKS
    plt.setp(ax.xaxis.get_majorticklabels(),
             rotation=45, horizontalalignment='right')

    ax.xaxis.set_major_formatter(mtick.FormatStrFormatter('%5.0f'))

    # ADD MAGNITUDES AND LIMITS FOR EACH FILTER
    handles = []

    # SET AXIS LIMITS FOR MAGNTIUDES
    upperMag = -99
    lowerMag = 99

    # DETERMINE THE TIME-RANGE OF DETECTION FOR THE SOURCE
    mjdList = magnitudes['o']['mjds'] + \
        magnitudes['c']['mjds'] + magnitudes['I']['mjds']

    if len(mjdList) == 0:
        return

    lowerDetectionMjd = min(mjdList)
    upperDetectionMjd = max(mjdList)
    priorLimitsFlavour = None

    postLimitsFlavour = None

    allMags = magnitudes['o']['mags'] + magnitudes['c']['mags']
    magRange = max(allMags) - min(allMags)

    deltaMag = magRange * 0.1

    if len(magnitudes['o']['mjds']):
        orangeMag = ax.errorbar(magnitudes['o']['mjds'], magnitudes['o']['mags'], yerr=magnitudes[
            'o']['magErrs'], color='#FFA500', fmt='o', mfc='#FFA500', mec='#FFA500', zorder=1, ms=12., alpha=0.8, linewidth=1.2,  label='o-band mag ', capsize=10)

        # ERROBAR CAP THICKNESS
        orangeMag[1][0].set_markeredgewidth('0.7')
        orangeMag[1][1].set_markeredgewidth('0.7')
        handles.append(orangeMag)
        if max(np.array(magnitudes['o']['mags']) + np.array(magnitudes['o']['magErrs'])) > upperMag:
            upperMag = max(
                np.array(magnitudes['o']['mags']) + np.array(magnitudes['o']['magErrs']))
            upperMagIndex = np.argmax((
                magnitudes['o']['mags']) + np.array(magnitudes['o']['magErrs']))

        if min(np.array(magnitudes['o']['mags']) - np.array(magnitudes['o']['magErrs'])) < lowerMag:
            lowerMag = min(
                np.array(magnitudes['o']['mags']) - np.array(magnitudes['o']['magErrs']))
            lowerMagIndex = np.argmin((
                magnitudes['o']['mags']) - np.array(magnitudes['o']['magErrs']))

    if len(magnitudes['c']['mjds']):
        cyanMag = ax.errorbar(magnitudes['c']['mjds'], magnitudes['c']['mags'], yerr=magnitudes[
            'c']['magErrs'], color='#2aa198', fmt='o', mfc='#2aa198', mec='#2aa198', zorder=1, ms=12., alpha=0.8, linewidth=1.2, label='c-band mag ', capsize=10)
        # ERROBAR CAP THICKNESS
        cyanMag[1][0].set_markeredgewidth('0.7')
        cyanMag[1][1].set_markeredgewidth('0.7')
        handles.append(cyanMag)
        if max(np.array(magnitudes['c']['mags']) + np.array(magnitudes['c']['magErrs'])) > upperMag:
            upperMag = max(
                np.array(magnitudes['c']['mags']) + np.array(magnitudes['c']['magErrs']))
            upperMagIndex = np.argmax((
                magnitudes['c']['mags']) + np.array(magnitudes['c']['magErrs']))

        if min(np.array(magnitudes['c']['mags']) - np.array(magnitudes['c']['magErrs'])) < lowerMag:
            lowerMag = min(
                np.array(magnitudes['c']['mags']) - np.array(magnitudes['c']['magErrs']))
            lowerMagIndex = np.argmin(
                (magnitudes['c']['mags']) - np.array(magnitudes['c']['magErrs']))

    if len(magnitudes['I']['mjds']):
        cyanMag = ax.errorbar(magnitudes['I']['mjds'], magnitudes['I']['mags'], yerr=magnitudes[
            'I']['magErrs'], color='#dc322f', fmt='o', mfc='#dc322f', mec='#dc322f', zorder=1, ms=12., alpha=0.8, linewidth=1.2, label='I-band mag ', capsize=10)
        # ERROBAR CAP THICKNESS
        cyanMag[1][0].set_markeredgewidth('0.7')
        cyanMag[1][1].set_markeredgewidth('0.7')
        handles.append(cyanMag)
        if max(np.array(magnitudes['I']['mags']) + np.array(magnitudes['I']['magErrs'])) > upperMag:
            upperMag = max(
                np.array(magnitudes['I']['mags']) + np.array(magnitudes['I']['magErrs']))
            upperMagIndex = np.argmax((
                magnitudes['I']['mags']) + np.array(magnitudes['I']['magErrs']))

        if min(np.array(magnitudes['I']['mags']) - np.array(magnitudes['I']['magErrs'])) < lowerMag:
            lowerMag = min(
                np.array(magnitudes['I']['mags']) - np.array(magnitudes['I']['magErrs']))
            lowerMagIndex = np.argmin(
                (magnitudes['I']['mags']) - np.array(magnitudes['I']['magErrs']))

    plt.legend(handles=handles, prop={
               'size': 13.5}, bbox_to_anchor=(0.95, 1.2), loc=0, borderaxespad=0., ncol=4, scatterpoints=1)

    # SET THE TEMPORAL X-RANGE
    allMjd = magnitudes['o']['mjds'] + magnitudes['c']['mjds']
    xmin = min(allMjd) - 5.
    xmax = max(allMjd) + 5.
    ax.set_xlim([xmin, xmax])

    ax.set_ylim([0. - deltaMag, upperMag + deltaMag])
    y_formatter = mpl.ticker.FormatStrFormatter("%2.1f")
    ax.yaxis.set_major_formatter(y_formatter)

    # PLOT THE MAGNITUDE SCALE
    axisUpperFlux = upperMag
    axisLowerFlux = 1e-29

    axisLowerMag = -2.5 * math.log10(axisLowerFlux) + 23.9
    axisUpperMag = -2.5 * math.log10(axisUpperFlux) + 23.9

    ax.set_yticks([2.2])
    import matplotlib.ticker as ticker

    magLabels = [20., 19.5, 19.0, 18.5,
                 18.0, 17.5, 17.0, 16.5, 16.0, 15.5, 15.0]
    magFluxes = [pow(10, old_div(-(m + 48.6), 2.5)) * 1e29 for m in magLabels]

    ax.yaxis.set_major_locator(ticker.FixedLocator((magFluxes)))
    ax.yaxis.set_major_formatter(ticker.FixedFormatter((magLabels)))

    # ADD SECOND Y-AXIS
    ax2 = ax.twinx()
    ax2.set_ylim([0. - deltaMag, upperMag + deltaMag])
    ax2.yaxis.set_major_formatter(y_formatter)

    # RELATIVE TIME SINCE DISCOVERY
    lower, upper = ax.get_xlim()
    utLower = converter.mjd_to_ut_datetime(mjd=lower, datetimeObject=True)
    utUpper = converter.mjd_to_ut_datetime(mjd=upper, datetimeObject=True)

    # ADD SECOND X-AXIS
    ax3 = ax.twiny()
    ax3.set_xlim([utLower, utUpper])
    ax3.grid(True)
    ax.xaxis.grid(False)
    plt.setp(ax3.xaxis.get_majorticklabels(),
             rotation=45, horizontalalignment='left')
    ax3.xaxis.set_major_formatter(dates.DateFormatter('%b %d'))

    ax2.set_ylabel('Flux ($\mu$Jy)', rotation=-90.,  labelpad=27)

    ax2.grid(False)
    # SAVE PLOT TO FILE
    pathToOutputPlotFolder = ""
    title = objectName + " forced photometry lc"
    # Recursively create missing directories
    now = get_now_datetime_filestamp(longTime=False)
    if len(objectName):
        objectName = objectName + "_"
    fileName = f"{objectName}atlas_fp_lightcurve_{now}.pdf"
    plt.savefig(fileName, bbox_inches='tight', transparent=False,
                pad_inches=0.1)

    # CLEAR FIGURE
    plt.clf()

    log.debug('completed the ``plot_lc`` function')
    return fileName


def read_and_simga_clip_data(
        log,
        fpFile,
        clippingSigma=3):
    """*summary of function*

    **Key Arguments:**

    - `log` -- logger
    - `fpFile` -- path to force photometry file
    - `clippingSigma` -- the level at which to clip flux data

    **Return:**

    - `epochs` -- sigma clipped and cleaned epoch data
    """
    log.debug('starting the ``read_and_simga_clip_data`` function')

    # CLEAN UP FILE FOR EASIER READING

    with codecs.open(fpFile, encoding='utf-8', mode='r') as readFile:
        content = readFile.read()
    fpData = content.replace("###", "").replace(" ", ",").replace(
        ",,", ",").replace(",,", ",").replace(",,", ",").replace(",,", ",").splitlines()

    epochs = []
    csvReader = csv.DictReader(
        fpData, dialect='excel', delimiter=',', quotechar='"')
    for row in csvReader:
        for k, v in row.items():
            try:
                row[k] = float(v)
            except:
                pass
        epochs.append(row)

    clipped = 1000
    while clipped > 0:
        clipped = 0
        # WORK OUT STD FOR CLIPPING ROGUE DATA
        fluxes = []
        for e in epochs:
            if e["uJy"] > 49.:
                fluxes.append(e["uJy"])
        std = np.std(fluxes)
        mean = np.mean(fluxes)
        keepEpochs = []
        for epoch in epochs:
            # CLIP SOME ROUGE DATA-POINTS
            if not epoch["uJy"] or epoch["uJy"] < 50. or abs(epoch["uJy"] - mean) > clippingSigma * std:
                clipped += 1
                continue
            keepEpochs.append(epoch)
        epochs = keepEpochs

    log.debug('completed the ``read_and_simga_clip_data`` function')
    return epochs


if __name__ == '__main__':
    main()