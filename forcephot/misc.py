import math
import os

import plot_atlas_fp
import astrocalc.coords.unit_conversion
import fundamentals.logs

from multiprocessing import Process
from pathlib import Path


def date_to_mjd(year, month, day):
    """
    Functions for converting dates to/from JD and MJD. Assumes dates are historical
    dates, including the transition from the Julian calendar to the Gregorian
    calendar in 1582. No support for proleptic Gregorian/Julian calendars.
    :Author: Matt Davis
    :Website: http://github.com/jiffyclub

    Convert a date to Julian Day.

    Algorithm from 'Practical Astronomy with your Calculator or Spreadsheet',
        4th ed., Duffet-Smith and Zwart, 2011.

    Parameters
    ----------
    year : int
        Year as integer. Years preceding 1 A.D. should be 0 or negative.
        The year before 1 A.D. is 0, 10 B.C. is year -9.

    month : int
        Month as integer, Jan = 1, Feb. = 2, etc.

    day : float
        Day, may contain fractional part.

    Returns
    -------
    jd : float
        Julian Day

    Examples
    --------
    Convert 6 a.m., February 17, 1985 to Julian Day

    >>> date_to_jd(1985,2,17.25)
    2446113.75

    """
    if month == 1 or month == 2:
        yearp = year - 1
        monthp = month + 12
    else:
        yearp = year
        monthp = month

    # this checks where we are in relation to October 15, 1582, the beginning
    # of the Gregorian calendar.
    if ((year < 1582) or
        (year == 1582 and month < 10) or
            (year == 1582 and month == 10 and day < 15)):
        # before start of Gregorian calendar
        B = 0
    else:
        # after start of Gregorian calendar
        A = math.trunc(yearp / 100.)
        B = 2 - A + math.trunc(A / 4.)

    if yearp < 0:
        C = math.trunc((365.25 * yearp) - 0.75)
    else:
        C = math.trunc(365.25 * yearp)

    D = math.trunc(30.6001 * (monthp + 1))

    jd = B + C + D + day + 1720994.5
    mjd = jd - 2400000.5

    return mjd


def splitradeclist(data, form=None):
    if 'radeclist' not in data:
        return [data]
    # multi-add functionality with a list of RA,DEC coords
    valid = True
    formdata = data
    datalist = []

    converter = astrocalc.coords.unit_conversion(log=fundamentals.logs.emptyLogger())

    # if an RA and Dec were specified, add them to the list
    if 'ra' in formdata and formdata['ra'] and 'dec' in formdata and formdata['dec']:
        newrow = formdata.copy()
        newrow['ra'] = converter.ra_sexegesimal_to_decimal(ra=newrow['ra'])
        newrow['dec'] = converter.dec_sexegesimal_to_decimal(dec=newrow['dec'])
        newrow['radeclist'] = ['']
        datalist.append(newrow)

    lines = formdata['radeclist'].split('\n')

    if len(lines) > 100:
        valid = False
        if form:
            form.add_error('radeclist', f'Number of lines ({len(lines)}) is above the limit of 100')
        # lines = lines[:1]

    for index, line in enumerate(lines, 1):
        if ',' in line:
            row = line.split(',')
        else:
            row = line.split()

        if row and len(row) < 2:
            valid = False
            if form:
                form.add_error('radeclist', f'Error on line {index}: Could not find two columns. '
                               'Separate RA and Dec by a comma or a space.')
        elif row:
            try:
                newrow = formdata.copy()
                newrow['ra'] = converter.ra_sexegesimal_to_decimal(ra=row[0])
                newrow['dec'] = converter.dec_sexegesimal_to_decimal(dec=row[1])
                newrow['radeclist'] = ['']
                datalist.append(newrow)

            except (IndexError, IOError) as err:
                valid = False
                if form:
                    form.add_error('radeclist', f'Error on line {index}: {err}')

    return datalist if valid else []


def make_pdf_plot_worker(localresultfile, taskid, taskcomment='', logprefix='', logfunc=None):
    localresultdir = localresultfile.parent
    pdftitle = f"Task {taskid}"
    # if taskcomment:
    #     pdftitle += ':' + taskcomment

    localresultfiles = [Path(localresultfile)]
    plotfilepaths_requested = [f.with_suffix('.pdf') for f in localresultfiles]

    plotfilepaths = None
    try:
        myplotter = plot_atlas_fp.plotter(
            log=fundamentals.logs.emptyLogger(),
            resultFilePaths=localresultfiles,
            outputPlotPaths=plotfilepaths_requested,
            # outputDirectory=str(localresultdir),
            objectName=pdftitle,
            plotType="pdf"
        )

        plotfilepaths = myplotter.plot()

    except Exception as ex:
        if logfunc:
            logfunc(logprefix + f'ERROR: plot_atlas_fp caused exception: {ex}')
        plotfilepaths = [None for f in plotfilepaths_requested]

    localresultfile, plotfilepath, plotfilepath_requested = (
        localresultfiles[0], plotfilepaths[0], plotfilepaths_requested[0])

    if os.path.exists(plotfilepath_requested):
        if logfunc and plotfilepath == plotfilepath_requested:
            logfunc(logprefix + f'Created plot file {Path(plotfilepath).relative_to(localresultdir)}')
        elif logfunc:
            logfunc(logprefix + f'plot_atlas_fp returned an error but the PDF file '
                    f'{plotfilepath_requested.relative_to(localresultdir)} exists')
        return plotfilepath_requested

    if logfunc:
        logfunc(logprefix + f'Failed to create PDF plot from {Path(localresultfile).relative_to(localresultdir)}')
    return None


def make_pdf_plot(*args, separate_process=False, **kwargs):
    if separate_process:
        p = Process(target=make_pdf_plot_worker, args=args, kwargs=kwargs)

        p.start()
        p.join()
    else:
        make_pdf_plot_worker(*args, **kwargs)
