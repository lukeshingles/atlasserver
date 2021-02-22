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


def country_code_to_name(country_code):
    dictcodes = {'A2': 'Satellite Provider', 'O1': 'Other Country', 'AD': 'Andorra', 'AE': 'United Arab Emirates', 'AF': 'Afghanistan', 'AG': 'Antigua and Barbuda', 'AI': 'Anguilla', 'AL': 'Albania', 'AM': 'Armenia', 'AO': 'Angola', 'AP': 'Asia/Pacific Region', 'AQ': 'Antarctica', 'AR': 'Argentina', 'AS': 'American Samoa', 'AT': 'Austria', 'AU': 'Australia', 'AW': 'Aruba', 'AX': 'Aland Islands', 'AZ': 'Azerbaijan', 'BA': 'Bosnia and Herzegovina', 'BB': 'Barbados', 'BD': 'Bangladesh', 'BE': 'Belgium', 'BF': 'Burkina Faso', 'BG': 'Bulgaria', 'BH': 'Bahrain', 'BI': 'Burundi', 'BJ': 'Benin', 'BL': 'Saint Barthelemey', 'BM': 'Bermuda', 'BN': 'Brunei Darussalam', 'BO': 'Bolivia', 'BQ': 'Bonaire, Saint Eustatius and Saba', 'BR': 'Brazil', 'BS': 'Bahamas', 'BT': 'Bhutan', 'BV': 'Bouvet Island', 'BW': 'Botswana', 'BY': 'Belarus', 'BZ': 'Belize', 'CA': 'Canada', 'CC': 'Cocos (Keeling) Islands', 'CD': 'Congo, The Democratic Republic of the', 'CF': 'Central African Republic', 'CG': 'Congo', 'CH': 'Switzerland', 'CI': "Cote d'Ivoire", 'CK': 'Cook Islands', 'CL': 'Chile', 'CM': 'Cameroon', 'CN': 'China', 'CO': 'Colombia', 'CR': 'Costa Rica', 'CU': 'Cuba', 'CV': 'Cape Verde', 'CW': 'Curacao', 'CX': 'Christmas Island', 'CY': 'Cyprus', 'CZ': 'Czech Republic', 'DE': 'Germany', 'DJ': 'Djibouti', 'DK': 'Denmark', 'DM': 'Dominica', 'DO': 'Dominican Republic', 'DZ': 'Algeria', 'EC': 'Ecuador', 'EE': 'Estonia', 'EG': 'Egypt', 'EH': 'Western Sahara', 'ER': 'Eritrea', 'ES': 'Spain', 'ET': 'Ethiopia', 'EU': 'Europe', 'FI': 'Finland', 'FJ': 'Fiji', 'FK': 'Falkland Islands (Malvinas)', 'FM': 'Micronesia, Federated States of', 'FO': 'Faroe Islands', 'FR': 'France', 'GA': 'Gabon', 'GB': 'United Kingdom', 'GD': 'Grenada', 'GE': 'Georgia', 'GF': 'French Guiana', 'GG': 'Guernsey', 'GH': 'Ghana', 'GI': 'Gibraltar', 'GL': 'Greenland', 'GM': 'Gambia', 'GN': 'Guinea', 'GP': 'Guadeloupe', 'GQ': 'Equatorial Guinea', 'GR': 'Greece', 'GS': 'South Georgia and the South Sandwich Islands', 'GT': 'Guatemala', 'GU': 'Guam', 'GW': 'Guinea-Bissau', 'GY': 'Guyana', 'HK': 'Hong Kong', 'HM': 'Heard Island and McDonald Islands', 'HN': 'Honduras', 'HR': 'Croatia', 'HT': 'Haiti', 'HU': 'Hungary', 'ID': 'Indonesia', 'IE': 'Ireland', 'IL': 'Israel', 'IM': 'Isle of Man', 'IN': 'India', 'IO': 'British Indian Ocean Territory', 'IQ': 'Iraq', 'IR': 'Iran, Islamic Republic of', 'IS': 'Iceland', 'IT': 'Italy', 'JE': 'Jersey', 'JM': 'Jamaica', 'JO': 'Jordan', 'JP': 'Japan', 'KE': 'Kenya', 'KG': 'Kyrgyzstan', 'KH': 'Cambodia', 'KI': 'Kiribati', 'KM': 'Comoros', 'KN': 'Saint Kitts and Nevis', 'KP': "Korea, Democratic People's Republic of", 'KR': 'Korea, Republic of', 'KW': 'Kuwait', 'KY': 'Cayman Islands', 'KZ': 'Kazakhstan', 'LA': "Lao People's Democratic Republic", 'LB': 'Lebanon', 'LC': 'Saint Lucia', 'LI': 'Liechtenstein', 'LK': 'Sri Lanka', 'LR': 'Liberia', 'LS': 'Lesotho', 'LT': 'Lithuania', 'LU': 'Luxembourg', 'LV': 'Latvia', 'LY': 'Libyan Arab Jamahiriya', 'MA': 'Morocco', 'MC': 'Monaco', 'MD': 'Moldova, Republic of', 'ME': 'Montenegro', 'MF': 'Saint Martin', 'MG': 'Madagascar', 'MH': 'Marshall Islands', 'MK': 'Macedonia', 'ML': 'Mali', 'MM': 'Myanmar', 'MN': 'Mongolia', 'MO': 'Macao', 'MP': 'Northern Mariana Islands', 'MQ': 'Martinique', 'MR': 'Mauritania', 'MS': 'Montserrat', 'MT': 'Malta', 'MU': 'Mauritius', 'MV': 'Maldives', 'MW': 'Malawi', 'MX': 'Mexico', 'MY': 'Malaysia', 'MZ': 'Mozambique', nan: 'Namibia', 'NC': 'New Caledonia', 'NE': 'Niger', 'NF': 'Norfolk Island', 'NG': 'Nigeria', 'NI': 'Nicaragua', 'NL': 'Netherlands', 'NO': 'Norway', 'NP': 'Nepal', 'NR': 'Nauru', 'NU': 'Niue', 'NZ': 'New Zealand', 'OM': 'Oman', 'PA': 'Panama', 'PE': 'Peru', 'PF': 'French Polynesia', 'PG': 'Papua New Guinea', 'PH': 'Philippines', 'PK': 'Pakistan', 'PL': 'Poland', 'PM': 'Saint Pierre and Miquelon', 'PN': 'Pitcairn', 'PR': 'Puerto Rico', 'PS': 'Palestinian Territory', 'PT': 'Portugal', 'PW': 'Palau', 'PY': 'Paraguay', 'QA': 'Qatar', 'RE': 'Reunion', 'RO': 'Romania', 'RS': 'Serbia', 'RU': 'Russian Federation', 'RW': 'Rwanda', 'SA': 'Saudi Arabia', 'SB': 'Solomon Islands', 'SC': 'Seychelles', 'SD': 'Sudan', 'SE': 'Sweden', 'SG': 'Singapore', 'SH': 'Saint Helena', 'SI': 'Slovenia', 'SJ': 'Svalbard and Jan Mayen', 'SK': 'Slovakia', 'SL': 'Sierra Leone', 'SM': 'San Marino', 'SN': 'Senegal', 'SO': 'Somalia', 'SR': 'Suriname', 'SS': 'South Sudan', 'ST': 'Sao Tome and Principe', 'SV': 'El Salvador', 'SX': 'Sint Maarten', 'SY': 'Syrian Arab Republic', 'SZ': 'Swaziland', 'TC': 'Turks and Caicos Islands', 'TD': 'Chad', 'TF': 'French Southern Territories', 'TG': 'Togo', 'TH': 'Thailand', 'TJ': 'Tajikistan', 'TK': 'Tokelau', 'TL': 'Timor-Leste', 'TM': 'Turkmenistan', 'TN': 'Tunisia', 'TO': 'Tonga', 'TR': 'Turkey', 'TT': 'Trinidad and Tobago', 'TV': 'Tuvalu', 'TW': 'Taiwan', 'TZ': 'Tanzania, United Republic of', 'UA': 'Ukraine', 'UG': 'Uganda', 'UM': 'United States Minor Outlying Islands', 'US': 'United States', 'UY': 'Uruguay', 'UZ': 'Uzbekistan', 'VA': 'Holy See (Vatican City State)', 'VC': 'Saint Vincent and the Grenadines', 'VE': 'Venezuela', 'VG': 'Virgin Islands, British', 'VI': 'Virgin Islands, U.S.', 'VN': 'Vietnam', 'VU': 'Vanuatu', 'WF': 'Wallis and Futuna', 'WS': 'Samoa', 'XX': 'Unknown', 'YE': 'Yemen', 'YT': 'Mayotte', 'ZA': 'South Africa', 'ZM': 'Zambia', 'ZW': 'Zimbabwe'}
    
    return dictcodes.get(country_code, 'Unknown')