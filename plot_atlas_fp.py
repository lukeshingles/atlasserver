#!/usr/bin/env python
# encoding: utf-8
"""
*Plot data returned from the ATLAS Forced Photometry service*

:Author:
    David Young

:Date Created:
    December  3, 2020

Usage:
    plot_atlas_fp plot <resultsPath> [<mjdMin> <mjdMax> --n=<objectName> --o=<outputDirectory>]
    plot_atlas_fp stack <binDays> <resultsPath> [<mjdMin> <mjdMax> --n=<objectName> --o=<outputDirectory>]

Commands:
    plot                    plot the photometry
    stack                   stack photometry across epochs (and same filter) and plot

Options:
    resultsPath             path to results file or directory of results files returned by the ATLAS FP service
    mjdMin                  min mjd to plot
    mjdMax                  max mjd to plot
    binDays                 time-range of bins to stack the within (days)

    --n=<objectName>        give a name for the object you are plotting (for plot title and filename). This is ignored if `resultsPath` is a directory
    --o=<outputDirectory>   path to the directory to output the plots to. Default is CWD.

    -h, --help              show this help message
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
from fundamentals import fmultiprocess
from astrocalc.times import conversions
from os.path import expanduser
from astropy.stats import sigma_clip, mad_std
from operator import itemgetter
from fundamentals.stats import rolling_window_sigma_clip
from fundamentals.renderer import list_of_dictionaries
import matplotlib.ticker as ticker
mpl.use('svg')


def main(arguments=None):
    """
    *The main function used when ``plot_atlas_fp.py`` is run as a single script from the cl*
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

    resultsPath = a["resultsPath"]
    objectName = a["nFlag"]
    stacked = a["stack"]
    mjdMin = a["mjdMin"]
    mjdMax = a["mjdMax"]
    outputDirectory = a["oFlag"]
    binDays = a["binDays"]

    # DO WE HAVE A SINGLE FILE OR DIRECTORY OF FILES
    if os.path.isfile(resultsPath):
        resultFilePaths = [resultsPath]
    elif os.path.isdir(resultsPath):
        # GENERATE A LIST OF FILE PATHS
        resultFilePaths = []
        for d in os.listdir(resultsPath):
            filepath = os.path.join(resultsPath, d)
            if os.path.isfile(filepath) and os.path.splitext(filepath)[1] == ".txt":
                resultFilePaths.append(filepath)

    log.info("""starting plotter""")
    myplotter = plotter(
        log=log, resultFilePaths=resultFilePaths, outputDirectory=outputDirectory, mjdMin=mjdMin, mjdMax=mjdMax, stackBinSize=binDays, objectName=objectName, plotType="pdf")
    plotPaths = myplotter.plot()

    plotPaths[:] = [p for p in plotPaths if p]
    plotPaths = "\n".join(plotPaths)
    print(f'Your plots can be found here:\n{plotPaths}')

    return


class plotter():
    """
    Plotter object used to plot results returned from the ATLAS Forced Photometry service

    **Key Arguments:**
        - `log` -- logger
        - `resultFilePaths` -- a list of result filepaths
        - `outputDirectory` -- path to the directory to output the plots to. Default is *False* (CWD).
        - `outputPlotPaths` -- a list of plot file paths. Default is *False*. If set this overrides the `outputDirectory`. Must be same length as `resultFilePaths`
        - `mjdMin` -- min mjd to plot. Default **False**
        - `mjdMax` -- max mjd to plot. Default **False**
        - `stackBinSize` -- stack photometry (in same filter) in time-range bin of this size (days) Default **False** (don't stack)
        - `objectName` -- give a name for the object you are plotting (for plot title and filename). ignored if `resultsPath` is a directory. Default **False**
        - `plotType` -- pdf, png, jpg. Default **PNG**

    **Usage:**

    ```python
    from plot_atlas_fp import plotter
    myplotter = plotter(
        log=log,
        resultFilePaths=resultFilePaths,
        outputPlotPaths=['/path/to/plot1.pdf','/path/to/plot2.pdf'...],
        mjdMin=mjdMin,
        mjdMax=mjdMax,
        stackBinSize=binDays,
        objectName=objectName,
        plotType="png"
    )
    plotPaths = myplotter.plot()
    ```
    """

    def __init__(
            self,
            log,
            resultFilePaths=[],
            outputDirectory=False,
            outputPlotPaths=False,
            mjdMin=False,
            mjdMax=False,
            stackBinSize=False,
            objectName=False,
            plotType="png"
    ):
        self.log = log
        self.resultFilePaths = resultFilePaths
        self.mjdMin = mjdMin
        self.mjdMax = mjdMax
        self.stackBinSize = stackBinSize
        self.objectName = objectName
        self.plotType = plotType
        self.outputPlotPaths = outputPlotPaths

        if plotType not in ("pdf", "png", "jpg"):
            self.log.error('plotType must be png, pdf or jpg')
            raise TypeError('plotType must be png, pdf or jpg')

        if not outputDirectory:
            outputDirectory = os.getcwd()
        home = expanduser("~")
        outputDirectory = outputDirectory.replace("~", home)
        if outputDirectory[-1] != "/":
            outputDirectory += "/"
        self.outputDirectory = outputDirectory

        # TEST OUT FOLDER EXISTS - CREATE IF NOT
        if not os.path.exists(outputDirectory) and outputPlotPaths is False:
            os.makedirs(outputDirectory)

        # CLEAR OBJECT NAME IF MORE THAN ONE RESULT FILE
        if len(resultFilePaths) > 1 or self.objectName == False:
            self.objectName = ""

        # MJD TO FLOAT
        if mjdMin:
            self.mjdMin = float(mjdMin)
        if mjdMax:
            self.mjdMax = float(mjdMax)

        # CHECK outputPlotPaths AND resultFilePaths SAME LENGTH
        if outputPlotPaths != False and len(outputPlotPaths) != len(resultFilePaths):
            log.error(
                'outputPlotPaths and resultFilePaths are not the same length' % locals())
            raise AssertionError(
                'outputPlotPaths and resultFilePaths are not the same length')

        self.firstPlot = True

        return None

    def __repr__(self):
        return "<plotter Object>"

    def close(self):
        del self
        return None

    def plot(self):
        """generate the plots from the data

        **Return:**
            - `plotPaths` -- list of paths to the output plots
        """
        self.log.info('starting the ``plot`` method')

        self.log.info("""setup figure""")
        # SETUP THE INITIAL FIGURE FOR THE PLOT (ONLY ONCE)
        fig = plt.figure(
            num=None,
            figsize=(10, 10),
            dpi=100,
            facecolor=None,
            edgecolor=None,
            frameon=True)
        mpl.rc('ytick', labelsize=18)
        mpl.rc('xtick', labelsize=18)
        mpl.rcParams.update({'font.size': 22})

        # FORMAT THE AXES
        ax = fig.add_axes(
            [0.1, 0.1, 0.8, 0.8],
            polar=False,
            frameon=True)
        ax.set_xlabel('MJD', labelpad=20)
        ax.set_yticks([2.2])

        # RHS AXIS TICKS
        plt.setp(ax.xaxis.get_majorticklabels(),
                 rotation=45, horizontalalignment='right')
        ax.xaxis.set_major_formatter(mtick.FormatStrFormatter('%5.0f'))

        y_formatter = mpl.ticker.FormatStrFormatter("%2.1f")
        ax.yaxis.set_major_formatter(y_formatter)
        ax.xaxis.grid(False)

        # ADD SECOND Y-AXIS
        ax2 = ax.twinx()
        ax2.yaxis.set_major_formatter(y_formatter)
        ax2.set_ylabel('Flux ($\mu$Jy)', rotation=-90.,  labelpad=27)
        ax2.grid(False)

        # ADD SECOND X-AXIS
        ax3 = ax.twiny()
        ax3.grid(True)
        plt.setp(ax3.xaxis.get_majorticklabels(),
                 rotation=45, horizontalalignment='left')

        # CONVERTER TO CONVERT MJD TO DATE
        converter = conversions(
            log=self.log
        )

        # CREATE A LOOKUP DICT FOR OUTPUT PATHS
        if self.outputPlotPaths:
            self.outputLookupDict = {}
            for r, o in zip(self.resultFilePaths, self.outputPlotPaths):
                self.outputLookupDict[r] = o

        self.log.info("""starting multiprocessing""")
        # plotPaths = fmultiprocess(log=self.log, function=self.plot_single_result,
        #                           inputArray=self.resultFilePaths, poolSize=False, timeout=7200, fig=fig, converter=converter, ax=ax)
        plotPaths = [self.plot_single_result(fpFile=fpFile, fig=fig, converter=converter, ax=ax) for fpFile in self.resultFilePaths]
        self.log.info("""finished multiprocessing""")

        self.log.info('completed the ``plot`` method')
        return plotPaths

    def read_and_sigma_clip_data(
            self,
            fpFile,
            clippingSigma=2.2):
        """*clean up rouge data from the files by performing some basic clipping*

        **Key Arguments:**

        - `fpFile` -- path to single force photometry file
        - `clippingSigma` -- the level at which to clip flux data

        **Return:**

        - `epochs` -- sigma clipped and cleaned epoch data
        """
        self.log.info('starting the ``read_and_sigma_clip_data`` function')

        mjdMin = self.mjdMin
        mjdMax = self.mjdMax

        # CLEAN UP FILE FOR EASIER READING
        with codecs.open(fpFile, encoding='utf-8', mode='r') as readFile:
            content = readFile.read()
        fpData = content.replace("###", "").replace(" ", ",").replace(
            ",,", ",").replace(",,", ",").replace(",,", ",").replace(",,", ",").splitlines()

        # PARSE DATA WITH SOME FIXED CLIPPING
        oepochs = []
        cepochs = []
        csvReader = csv.DictReader(
            fpData, dialect='excel', delimiter=',', quotechar='"')

        for row in csvReader:
            for k, v in row.items():
                try:
                    row[k] = float(v)
                except:
                    pass
            # REMOVE VERY HIGH ERROR DATA POINTS & POOR CHI SQUARED
            if row["duJy"] > 4000 or row["chi/N"] > 100:
                continue
            if mjdMin and mjdMax:
                if row["MJD"] < mjdMin or row["MJD"] > mjdMax:
                    continue
            if row["F"] == "c":
                cepochs.append(row)
            if row["F"] == "o":
                oepochs.append(row)

        # SORT BY MJD
        cepochs = sorted(cepochs, key=itemgetter('MJD'), reverse=False)
        oepochs = sorted(oepochs, key=itemgetter('MJD'), reverse=False)

        # SIGMA-CLIP THE DATA WITH A ROLLING WINDOW
        cdataFlux = []
        cdataFlux[:] = [row["uJy"] for row in cepochs]
        odataFlux = []
        odataFlux[:] = [row["uJy"] for row in oepochs]

        maskList = []
        for flux in [cdataFlux, odataFlux]:
            fullMask = rolling_window_sigma_clip(
                log=self.log,
                array=flux,
                clippingSigma=clippingSigma,
                windowSize=11)
            maskList.append(fullMask)

        try:
            cepochs = [e for e, m in zip(
                cepochs, maskList[0]) if m == False]
        except:
            cepochs = []

        try:
            oepochs = [e for e, m in zip(
                oepochs, maskList[1]) if m == False]
        except:
            oepochs = []

        self.log.debug('completed the ``read_and_sigma_clip_data`` function')
        return cepochs + oepochs

    def plot_single_result(
            self,
            fpFile,
            fig,
            converter,
            ax):
        """*plot single result*

        **Key Arguments:**
            - `fpFile` -- path to single force photometry file
            - `fig` -- the matplotlib figure to use for the plot
            - `converter` -- converter to switch mjd to ut-date
            - `ax` -- plot axis

        **Return:**
            - `filePath` -- path to the output PDF plot
        """
        self.log.info('starting the ``plot_single_result`` method')

        ax2 = get_twin_axis(ax, "x")
        ax3 = get_twin_axis(ax, "y")

        # SOME INTIAL SETUP
        objectName = self.objectName
        # ax = fig.gca()
        epochs = self.read_and_sigma_clip_data(fpFile)

        # c = cyan, o = arange
        magnitudes = {
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

        # STACK PHOTOMETRY IF REQUIRED
        if self.stackBinSize:
            magnitudes = self.stack_photometry(
                magnitudes, binningDays=self.stackBinSize, fpFile=fpFile)

        # ATLAS OBJECT NAME LABEL AS TITLE
        if objectName and len(objectName):
            fig.text(0.1, 1.02, objectName, ha="left", fontsize=40)

        # ADD MAGNITUDES AND LIMITS FOR EACH FILTER
        handles = []

        # SET AXIS LIMITS FOR MAGNTIUDES
        upperMag = -99999999999
        lowerMag = 99999999999

        # DETERMINE THE TIME-RANGE OF DETECTION FOR THE SOURCE
        mjdList = magnitudes['o']['mjds'] + \
            magnitudes['c']['mjds'] + magnitudes['I']['mjds']
        if len(mjdList) == 0:
            self.log.error(f'{fpFile} does not contain enough data')
            return None
        lowerDetectionMjd = min(mjdList)
        upperDetectionMjd = max(mjdList)

        # DETERMIN MAGNITUDE RANGE
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
            errMask = np.array(magnitudes['o']['magErrs'])
            np.putmask(errMask, errMask > 30, 30)

            if max(np.array(magnitudes['o']['mags']) + errMask) > upperMag:
                upperMag = max(
                    np.array(magnitudes['o']['mags']) + errMask)
                upperMagIndex = np.argmax((
                    magnitudes['o']['mags']) + errMask)

            if min(np.array(magnitudes['o']['mags']) - errMask) < lowerMag:
                lowerMag = min(
                    np.array(magnitudes['o']['mags']) - errMask)
                lowerMagIndex = np.argmin((
                    magnitudes['o']['mags']) - errMask)

        if len(magnitudes['c']['mjds']):
            cyanMag = ax.errorbar(magnitudes['c']['mjds'], magnitudes['c']['mags'], yerr=magnitudes[
                'c']['magErrs'], color='#2aa198', fmt='o', mfc='#2aa198', mec='#2aa198', zorder=1, ms=12., alpha=0.8, linewidth=1.2, label='c-band mag ', capsize=10)
            # ERROBAR CAP THICKNESS
            cyanMag[1][0].set_markeredgewidth('0.7')
            cyanMag[1][1].set_markeredgewidth('0.7')
            handles.append(cyanMag)
            errMask = np.array(magnitudes['c']['magErrs'])
            np.putmask(errMask, errMask > 30, 30)

            if max(np.array(magnitudes['c']['mags']) + errMask) > upperMag:
                upperMag = max(
                    np.array(magnitudes['c']['mags']) + errMask)
                upperMagIndex = np.argmax((
                    magnitudes['c']['mags']) + errMask)

            if min(np.array(magnitudes['c']['mags']) - errMask) < lowerMag:
                lowerMag = min(
                    np.array(magnitudes['c']['mags']) - errMask)
                lowerMagIndex = np.argmin(
                    (magnitudes['c']['mags']) - errMask)

        if self.firstPlot:
            plt.legend(handles=handles, prop={
                'size': 13.5}, bbox_to_anchor=(0.95, 1.2), loc=0, borderaxespad=0., ncol=4, scatterpoints=1)

        # SET THE TEMPORAL X-RANGE
        allMjd = magnitudes['o']['mjds'] + magnitudes['c']['mjds']
        xmin = min(allMjd) - 5.
        xmax = max(allMjd) + 5.
        mjdRange = xmax - xmin
        ax.set_xlim([xmin, xmax])
        ax.set_ylim([lowerMag - deltaMag, upperMag + deltaMag])

        # PLOT THE MAGNITUDE SCALE
        axisUpperFlux = upperMag
        axisLowerFlux = 1e-29
        axisLowerMag = -2.5 * math.log10(axisLowerFlux) + 23.9
        if axisUpperFlux > 0.:
            axisUpperMag = -2.5 * math.log10(axisUpperFlux) + 23.9
        else:
            axisUpperMag = None

        if axisUpperMag:
            ax.set_ylabel('Apparent Magnitude', labelpad=15)
            magLabels = [20., 17.0, 15.0, 14.0, 13.5, 13.0]
            if axisUpperMag < 14:
                magLabels = [20.0, 16.0, 15.0, 14.0,
                             13.0, 12.5, 12.0, 11.5, 11.0]
            elif axisUpperMag < 17:
                magLabels = [20.,
                             18.0, 17.0, 16.5, 16.0, 15.5, 15.0]
            elif axisUpperMag < 18:
                magLabels = [20., 19.5, 19.0, 18.5,
                             18.0, 17.5, 17.0, 16.5, 16.0, 15.5, 15.0]
            magFluxes = [pow(10, old_div(-(m - 23.9), 2.5)) for m in magLabels]

            ax.yaxis.set_major_locator(ticker.FixedLocator((magFluxes)))
            ax.yaxis.set_major_formatter(ticker.FixedFormatter((magLabels)))
        else:
            ax.set_yticks([])

        # ADD SECOND Y-AXIS
        ax2.set_ylim([lowerMag - deltaMag, upperMag + deltaMag])

        # RELATIVE TIME SINCE DISCOVERY
        lower, upper = ax.get_xlim()
        utLower = converter.mjd_to_ut_datetime(mjd=lower, datetimeObject=True)
        utUpper = converter.mjd_to_ut_datetime(mjd=upper, datetimeObject=True)
        ax3.set_xlim([utLower, utUpper])

        if mjdRange > 365:
            ax3.xaxis.set_major_formatter(dates.DateFormatter('%b %d %y'))
        else:
            ax3.xaxis.set_major_formatter(dates.DateFormatter('%b %d'))

        # SAVE PLOT TO FILE
        if objectName and len(objectName):
            objectName = objectName + "_"
        else:
            objectName = os.path.splitext(os.path.basename(fpFile))[0] + "_"
        if self.stackBinSize:
            stacked = "_stacked"
        else:
            stacked = ""
        if self.outputPlotPaths:
            filePath = self.outputLookupDict[fpFile]
        else:
            filePath = f"{self.outputDirectory}{objectName}atlas_fp_lightcurve{stacked}.{self.plotType}"
        plt.savefig(filePath, bbox_inches='tight', transparent=False,
                    pad_inches=0.1, optimize=True, progressive=True)

        try:
            cyanMag.remove()
        except:
            pass

        try:
            orangeMag.remove()
        except:
            pass

        self.firstPlot = False

        self.log.info(f'finished plotting {objectName}')

        self.log.debug('completed the ``plot_single_result`` method')
        return filePath

    def stack_photometry(
            self,
            magnitudes,
            fpFile,
            binningDays=1.):
        """*stack the photometry for the given temporal range*

        **Key Arguments:**
            - `magnitudes` -- dictionary of photometry divided into filter sets
            - `fpFile` -- path to the file containing the original force photometry data
            - `binningDays` -- the binning to use (in days)

        **Return:**
            - `summedMagnitudes` -- the stacked photometry
        """
        self.log.debug('starting the ``stack_photometry`` method')

        # IF WE WANT TO 'STACK' THE PHOTOMETRY
        summedMagnitudes = {
            'c': {'mjds': [], 'mags': [], 'magErrs': [], 'n': []},
            'o': {'mjds': [], 'mags': [], 'magErrs': [], 'n': []},
            'I': {'mjds': [], 'mags': [], 'magErrs': [], 'n': []},
        }

        # MAGNITUDES/FLUXES ARE DIVIDED IN UNIQUE FILTER SETS - SO ITERATE OVER
        # FILTERS
        allData = []
        for fil, data in list(magnitudes.items()):
            # WE'RE GOING TO CREATE FURTHER SUBSETS FOR EACH UNQIUE MJD (FLOORED TO AN INTEGER)
            # MAG VARIABLE == FLUX (JUST TO CONFUSE YOU)
            distinctMjds = {}
            for mjd, flx, err in zip(data["mjds"], data["mags"], data["magErrs"]):
                # DICT KEY IS THE UNIQUE INTEGER MJD
                key = str(int(math.floor(mjd / float(binningDays))))
                # FIRST DATA POINT OF THE NIGHTS? CREATE NEW DATA SET
                if key not in distinctMjds:
                    distinctMjds[key] = {
                        "mjds": [mjd],
                        "mags": [flx],
                        "magErrs": [err]
                    }
                # OR NOT THE FIRST? APPEND TO ALREADY CREATED LIST
                else:
                    distinctMjds[key]["mjds"].append(mjd)
                    distinctMjds[key]["mags"].append(flx)
                    distinctMjds[key]["magErrs"].append(err)

            # ALL DATA NOW IN MJD SUBSETS. SO FOR EACH SUBSET (I.E. INDIVIDUAL
            # NIGHTS) ...
            for k, v in list(distinctMjds.items()):
                # GIVE ME THE MEAN MJD
                meanMjd = old_div(sum(v["mjds"]), len(v["mjds"]))
                summedMagnitudes[fil]["mjds"].append(meanMjd)
                # GIVE ME THE MEAN FLUX
                meanFLux = old_div(sum(v["mags"]), len(v["mags"]))
                summedMagnitudes[fil]["mags"].append(meanFLux)
                # GIVE ME THE COMBINED ERROR
                combError = sum(v["magErrs"]) / len(v["magErrs"]
                                                    ) / math.sqrt(len(v["magErrs"]))
                summedMagnitudes[fil]["magErrs"].append(combError)
                # GIVE ME NUMBER OF DATA POINTS COMBINED
                n = len(v["mjds"])
                summedMagnitudes[fil]["n"].append(n)
                allData.append({
                    'MJD': f'{meanMjd:0.2f}',
                    'uJy': f'{meanFLux:0.2f}',
                    'duJy': f'{combError:0.2f}',
                    'F': fil,
                    'n': n
                })

        # WRITE STACK PHOTOMETRY TO FILE
        objectName = self.objectName
        if objectName and len(objectName):
            objectName = objectName + "_"
        else:
            objectName = os.path.splitext(os.path.basename(fpFile))[0] + "_"
        if binningDays:
            stacked = f"stacked_{binningDays}_days"
        else:
            stacked = ""
        filePath = f"{self.outputDirectory}{objectName}atlas_fp_{stacked}.txt"

        # CHECK FOR EMPTY DATASETS
        if not len(allData):
            return summedMagnitudes

        # SORT FULL DATASET BY MJD
        allData = sorted(allData, key=itemgetter('MJD'))

        header = """
# MJD (average of the points included, after clipping)
# uJy (average of the points included after clipping)
# duJy (error on the average point after clipping)
# F (filter)
# n (number of points included in the stacked detection, after
# clipping)
        """
        dataSet = list_of_dictionaries(
            log=self.log,
            listOfDictionaries=allData,
            # use re.compile('^[0-9]{4}-[0-9]{2}-[0-9]{2}T') for mysql
            reDatetime=False
        )
        csvData = dataSet.csv(filepath=filePath)

        # DEFINE NAME OF TEMPORARY DUMMY FILE
        dummy_file = filePath + '.bak'
        # OPEN ORIGINAL FILE IN READ MODE AND DUMMY FILE IN WRITE MODE
        with open(filePath, 'r') as read_obj, open(dummy_file, 'w') as write_obj:
            # WRITE GIVEN LINE TO THE DUMMY FILE
            write_obj.write(header.strip() + '\n')
            # READ LINES FROM ORIGINAL FILE ONE BY ONE AND APPEND THEM TO THE
            # DUMMY FILE
            for line in read_obj:
                write_obj.write(line)
        # REMOVE ORIGINAL FILE
        os.remove(filePath)
        # RENAME DUMMY FILE AS THE ORIGINAL FILE
        os.rename(dummy_file, filePath)

        self.log.debug('completed the ``stack_photometry`` method')
        return summedMagnitudes


def get_twin(ax, axis):

    for sibling in siblings:
        if sibling.bbox.bounds == ax.bbox.bounds and sibling is not ax:
            return sibling
    return None


def get_twin_axis(ax, axis):
    assert axis in ("x", "y")
    for other_ax in ax.figure.axes:
        if other_ax is ax:
            siblings = getattr(ax, f"get_shared_{axis}_axes")().get_siblings(ax)
            for sibling in siblings:
                if sibling.bbox.bounds == ax.bbox.bounds and sibling is not ax:
                    return sibling
    return None

if __name__ == '__main__':
    main()
