import datetime
import typing as t
from multiprocessing import Process
from pathlib import Path

import astrocalc.coords.unit_conversion
import fundamentals.logs
import julian
import pycountry
from django.http import Http404
from django.utils.log import AdminEmailHandler

from atlasserver import plot_atlas_fp

dictcountrycodes = {
    "A2": "Satellite Provider",
    "O1": "Other Country",
    "AD": "Andorra",
    "AE": "United Arab Emirates",
    "AF": "Afghanistan",
    "AG": "Antigua and Barbuda",
    "AI": "Anguilla",
    "AL": "Albania",
    "AM": "Armenia",
    "AO": "Angola",
    "AP": "Asia/Pacific Region",
    "AQ": "Antarctica",
    "AR": "Argentina",
    "AS": "American Samoa",
    "AT": "Austria",
    "AU": "Australia",
    "AW": "Aruba",
    "AX": "Aland Islands",
    "AZ": "Azerbaijan",
    "BA": "Bosnia and Herzegovina",
    "BB": "Barbados",
    "BD": "Bangladesh",
    "BE": "Belgium",
    "BF": "Burkina Faso",
    "BG": "Bulgaria",
    "BH": "Bahrain",
    "BI": "Burundi",
    "BJ": "Benin",
    "BL": "Saint Barthelemey",
    "BM": "Bermuda",
    "BN": "Brunei Darussalam",
    "BO": "Bolivia",
    "BQ": "Bonaire, Saint Eustatius and Saba",
    "BR": "Brazil",
    "BS": "Bahamas",
    "BT": "Bhutan",
    "BV": "Bouvet Island",
    "BW": "Botswana",
    "BY": "Belarus",
    "BZ": "Belize",
    "CA": "Canada",
    "CC": "Cocos (Keeling) Islands",
    "CD": "Congo, The Democratic Republic of the",
    "CF": "Central African Republic",
    "CG": "Congo",
    "CH": "Switzerland",
    "CI": "Cote d'Ivoire",
    "CK": "Cook Islands",
    "CL": "Chile",
    "CM": "Cameroon",
    "CN": "China",
    "CO": "Colombia",
    "CR": "Costa Rica",
    "CU": "Cuba",
    "CV": "Cape Verde",
    "CW": "Curacao",
    "CX": "Christmas Island",
    "CY": "Cyprus",
    "CZ": "Czech Republic",
    "DE": "Germany",
    "DJ": "Djibouti",
    "DK": "Denmark",
    "DM": "Dominica",
    "DO": "Dominican Republic",
    "DZ": "Algeria",
    "EC": "Ecuador",
    "EE": "Estonia",
    "EG": "Egypt",
    "EH": "Western Sahara",
    "ER": "Eritrea",
    "ES": "Spain",
    "ET": "Ethiopia",
    "EU": "Europe",
    "FI": "Finland",
    "FJ": "Fiji",
    "FK": "Falkland Islands (Malvinas)",
    "FM": "Micronesia, Federated States of",
    "FO": "Faroe Islands",
    "FR": "France",
    "GA": "Gabon",
    "GB": "United Kingdom",
    "GD": "Grenada",
    "GE": "Georgia",
    "GF": "French Guiana",
    "GG": "Guernsey",
    "GH": "Ghana",
    "GI": "Gibraltar",
    "GL": "Greenland",
    "GM": "Gambia",
    "GN": "Guinea",
    "GP": "Guadeloupe",
    "GQ": "Equatorial Guinea",
    "GR": "Greece",
    "GS": "South Georgia and the South Sandwich Islands",
    "GT": "Guatemala",
    "GU": "Guam",
    "GW": "Guinea-Bissau",
    "GY": "Guyana",
    "HK": "Hong Kong",
    "HM": "Heard Island and McDonald Islands",
    "HN": "Honduras",
    "HR": "Croatia",
    "HT": "Haiti",
    "HU": "Hungary",
    "ID": "Indonesia",
    "IE": "Ireland",
    "IL": "Israel",
    "IM": "Isle of Man",
    "IN": "India",
    "IO": "British Indian Ocean Territory",
    "IQ": "Iraq",
    "IR": "Iran, Islamic Republic of",
    "IS": "Iceland",
    "IT": "Italy",
    "JE": "Jersey",
    "JM": "Jamaica",
    "JO": "Jordan",
    "JP": "Japan",
    "KE": "Kenya",
    "KG": "Kyrgyzstan",
    "KH": "Cambodia",
    "KI": "Kiribati",
    "KM": "Comoros",
    "KN": "Saint Kitts and Nevis",
    "KP": "Korea, Democratic People's Republic of",
    "KR": "Korea, Republic of",
    "KW": "Kuwait",
    "KY": "Cayman Islands",
    "KZ": "Kazakhstan",
    "LA": "Lao People's Democratic Republic",
    "LB": "Lebanon",
    "LC": "Saint Lucia",
    "LI": "Liechtenstein",
    "LK": "Sri Lanka",
    "LR": "Liberia",
    "LS": "Lesotho",
    "LT": "Lithuania",
    "LU": "Luxembourg",
    "LV": "Latvia",
    "LY": "Libyan Arab Jamahiriya",
    "MA": "Morocco",
    "MC": "Monaco",
    "MD": "Moldova, Republic of",
    "ME": "Montenegro",
    "MF": "Saint Martin",
    "MG": "Madagascar",
    "MH": "Marshall Islands",
    "MK": "Macedonia",
    "ML": "Mali",
    "MM": "Myanmar",
    "MN": "Mongolia",
    "MO": "Macao",
    "MP": "Northern Mariana Islands",
    "MQ": "Martinique",
    "MR": "Mauritania",
    "MS": "Montserrat",
    "MT": "Malta",
    "MU": "Mauritius",
    "MV": "Maldives",
    "MW": "Malawi",
    "MX": "Mexico",
    "MY": "Malaysia",
    "MZ": "Mozambique",
    "NA": "Namibia",
    "NC": "New Caledonia",
    "NE": "Niger",
    "NF": "Norfolk Island",
    "NG": "Nigeria",
    "NI": "Nicaragua",
    "NL": "Netherlands",
    "NO": "Norway",
    "NP": "Nepal",
    "NR": "Nauru",
    "NU": "Niue",
    "NZ": "New Zealand",
    "OM": "Oman",
    "PA": "Panama",
    "PE": "Peru",
    "PF": "French Polynesia",
    "PG": "Papua New Guinea",
    "PH": "Philippines",
    "PK": "Pakistan",
    "PL": "Poland",
    "PM": "Saint Pierre and Miquelon",
    "PN": "Pitcairn",
    "PR": "Puerto Rico",
    "PS": "Palestinian Territory",
    "PT": "Portugal",
    "PW": "Palau",
    "PY": "Paraguay",
    "QA": "Qatar",
    "RE": "Reunion",
    "RO": "Romania",
    "RS": "Serbia",
    "RU": "Russian Federation",
    "RW": "Rwanda",
    "SA": "Saudi Arabia",
    "SB": "Solomon Islands",
    "SC": "Seychelles",
    "SD": "Sudan",
    "SE": "Sweden",
    "SG": "Singapore",
    "SH": "Saint Helena",
    "SI": "Slovenia",
    "SJ": "Svalbard and Jan Mayen",
    "SK": "Slovakia",
    "SL": "Sierra Leone",
    "SM": "San Marino",
    "SN": "Senegal",
    "SO": "Somalia",
    "SR": "Suriname",
    "SS": "South Sudan",
    "ST": "Sao Tome and Principe",
    "SV": "El Salvador",
    "SX": "Sint Maarten",
    "SY": "Syrian Arab Republic",
    "SZ": "Swaziland",
    "TC": "Turks and Caicos Islands",
    "TD": "Chad",
    "TF": "French Southern Territories",
    "TG": "Togo",
    "TH": "Thailand",
    "TJ": "Tajikistan",
    "TK": "Tokelau",
    "TL": "Timor-Leste",
    "TM": "Turkmenistan",
    "TN": "Tunisia",
    "TO": "Tonga",
    "TR": "Turkey",
    "TT": "Trinidad and Tobago",
    "TV": "Tuvalu",
    "TW": "Taiwan",
    "TZ": "Tanzania, United Republic of",
    "UA": "Ukraine",
    "UG": "Uganda",
    "UM": "United States Minor Outlying Islands",
    "US": "United States",
    "UY": "Uruguay",
    "UZ": "Uzbekistan",
    "VA": "Holy See (Vatican City State)",
    "VC": "Saint Vincent and the Grenadines",
    "VE": "Venezuela",
    "VG": "Virgin Islands, British",
    "VI": "Virgin Islands, U.S.",
    "VN": "Vietnam",
    "VU": "Vanuatu",
    "WF": "Wallis and Futuna",
    "WS": "Samoa",
    "XX": "Unknown",
    "YE": "Yemen",
    "YT": "Mayotte",
    "ZA": "South Africa",
    "ZM": "Zambia",
    "ZW": "Zimbabwe",
}


def splitradeclist(data, form=None):
    from rest_framework import serializers

    from atlasserver.forcephot.serializers import ForcePhotTaskSerializer

    if "radeclist" not in data:
        return [data]
    # multi-add functionality with a list of RA,DEC coords
    datalist = []

    converter = astrocalc.coords.unit_conversion(log=fundamentals.logs.emptyLogger())

    # if an RA and Dec were specified directly in their fields, add them to the list
    if "ra" in data and data["ra"] and "dec" in data and data["dec"]:
        newrow = data.copy()
        newrow["ra"] = converter.ra_sexegesimal_to_decimal(ra=newrow["ra"])
        newrow["dec"] = converter.dec_sexegesimal_to_decimal(dec=newrow["dec"])
        del newrow["radeclist"]
        datalist.append(newrow)

    lines = data["radeclist"].split("\n")

    if len(lines) > 100:
        raise serializers.ValidationError({"radeclist": f"Number of lines ({len(lines)}) is above the limit of 100"})
        # lines = lines[:1]

    for index, line in enumerate(lines, 1):
        if line[:4] in ["mpc_", "MPC_", "mpc ", "MPC "]:
            if not (mpc_name := line[4:].strip()):
                raise serializers.ValidationError({"radeclist": f"Error on line {index}: MPC name is blank"})
            newrow = data.copy()
            newrow["mpc_name"] = mpc_name
            newrow["ra"] = None
            newrow["dec"] = None
            ForcePhotTaskSerializer.validate_mpc_name(
                None, mpc_name, prefix=f"Error on line {index}: ", field="radeclist"
            )
            datalist.append(newrow)

            serializer = ForcePhotTaskSerializer(data=newrow, many=False)
            serializer.is_valid(raise_exception=True)
            continue

        if "," in line:
            row = line.split(",")
        elif len(line.split()) == 6:  # handle '00 52 20.21 +56 34 03.9' style of RA Dec
            rowspacesplit = line.split()
            row = [" ".join(rowspacesplit[:3]), " ".join(rowspacesplit[3:6])]
        else:
            row = line.split()

        if row:
            if len(row) < 2:
                raise serializers.ValidationError(
                    {
                        "radeclist": (
                            f"Error on line {index}: Could not find two columns. Separate RA and Dec by a comma or a"
                            " space. For MPC object names, start the line with 'mpc ', e.g., 'mpc Makemake'"
                        )
                    }
                )
            try:
                newrow = data.copy()
                newrow["ra"] = converter.ra_sexegesimal_to_decimal(ra=row[0])
                newrow["dec"] = converter.dec_sexegesimal_to_decimal(dec=row[1])
                newrow["radeclist"] = [""]
                ForcePhotTaskSerializer.validate_ra(
                    None, newrow["ra"], prefix=f"Error on line {index}: ", field="radeclist"
                )
                ForcePhotTaskSerializer.validate_dec(
                    None, newrow["dec"], prefix=f"Error on line {index}: ", field="radeclist"
                )
                serializer = ForcePhotTaskSerializer(data=newrow, many=False)
                serializer.is_valid(raise_exception=True)
                datalist.append(newrow)

            except (OSError, IndexError) as err:
                raise serializers.ValidationError({"radeclist": f"Error on line {index}: {err}"}) from err

    return datalist


def datetime_to_mjd(dt: datetime.datetime) -> float:
    return julian.to_jd(dt) - 2400000.5


def make_pdf_plot_worker(
    localresultfile: Path,
    taskid: int,
    taskcomment: str = "",
    logprefix: str = "",
    logfunc: None | t.Callable[[t.Any], t.Any] = None,
) -> Path | None:
    localresultdir = localresultfile.parent
    pdftitle = f"Task {taskid}"
    # if taskcomment:
    #     pdftitle += ':' + taskcomment

    localresultfiles = [Path(localresultfile)]
    plotfilepaths_requested = [f.with_suffix(".pdf") for f in localresultfiles]

    plotfilepaths = None
    try:
        myplotter = plot_atlas_fp.plotter(
            log=fundamentals.logs.emptyLogger(),
            resultFilePaths=localresultfiles,
            outputPlotPaths=plotfilepaths_requested,
            # outputDirectory=str(localresultdir),
            objectName=pdftitle,
            plotType="pdf",
        )

        plotfilepaths = myplotter.plot()

    except Exception as ex:
        if logfunc:
            logfunc(f"{logprefix}ERROR: plot_atlas_fp caused exception: {ex}")
        plotfilepaths = [None for _ in plotfilepaths_requested]

    localresultfile, plotfilepath, plotfilepath_requested = (
        localresultfiles[0],
        plotfilepaths[0],
        plotfilepaths_requested[0],
    )

    if plotfilepath_requested.exists():
        if logfunc and plotfilepath == plotfilepath_requested:
            logfunc(f"{logprefix}Created plot file {Path(plotfilepath).relative_to(localresultdir)}")
        elif logfunc:
            logfunc(
                f"{logprefix}plot_atlas_fp returned an error but the PDF file {plotfilepath_requested.relative_to(localresultdir)} exists"
            )
        return plotfilepath_requested

    if logfunc:
        logfunc(f"{logprefix}Failed to create PDF plot from {Path(localresultfile).relative_to(localresultdir)}")
    return None


def make_pdf_plot(*args, separate_process=False, **kwargs):
    if separate_process:
        proc = Process(target=make_pdf_plot_worker, args=args, kwargs=kwargs)

        proc.start()
        proc.join()
    else:
        make_pdf_plot_worker(*args, **kwargs)


def country_code_to_name(country_code):
    # return dictcountrycodes.get(country_code, 'Unknown')

    if country_code == "XX" or not country_code:
        return "Unknown"

    if c := pycountry.countries.get(alpha_2=country_code):
        return c.name

    return "Unknown"


def country_region_to_name(country_code, region_code):
    region_name = "Unknown"

    if region_code:
        fullcode = f"{country_code}-{region_code}"
        if subdiv := pycountry.subdivisions.get(code=fullcode):
            region_name = subdiv.name

    return f"{region_name}, {country_code_to_name(country_code)}"


class AdminEmailHandlerNo404(AdminEmailHandler):
    """Custom Handler that ignores 404 errors instead of sending them by email."""

    def handle(self, record):
        if record.exc_info:
            _exc_type, exc_value, _tb = record.exc_info
            if isinstance(exc_value, Http404):
                return
        super().handle(record)
