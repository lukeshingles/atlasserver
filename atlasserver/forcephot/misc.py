import datetime
import typing as t
from multiprocessing import Process
from pathlib import Path

import fundamentals.logs
import julian
import pycountry
from astrocalc.coords.unit_conversion import unit_conversion
from django.http import Http404
from django.utils.log import AdminEmailHandler

from atlasserver import plot_atlas_fp


def splitradeclist(data, form=None):
    from rest_framework import serializers

    from atlasserver.forcephot.serializers import ForcePhotTaskSerializer

    if "radeclist" not in data:
        return [data]

    if not isinstance(data["radeclist"], str):
        raise serializers.ValidationError(
            {"radeclist": "radeclist must be a string of newline-separated coordinates or MPC names"}
        )

    # multi-add functionality with a list of RA,DEC coords
    datalist = []

    converter = unit_conversion(log=fundamentals.logs.emptyLogger())

    # if an RA and Dec were specified directly in their fields, add them to the list.
    # RA 0 and Dec 0 are valid, so test for "not given" rather than falsiness
    if data.get("ra") not in (None, "") and data.get("dec") not in (None, ""):
        newrow = data.copy()
        try:
            newrow["ra"] = converter.ra_sexegesimal_to_decimal(ra=newrow["ra"])
            newrow["dec"] = converter.dec_sexegesimal_to_decimal(dec=newrow["dec"])
        except (OSError, IndexError) as err:
            # astrocalc raises IOError for input that it cannot parse
            raise serializers.ValidationError({"ra": f"Could not parse RA/Dec: {err}"}) from err
        del newrow["radeclist"]
        datalist.append(newrow)

    # browsers submit textarea contents with CRLF line endings, so strip the trailing \r from each
    # line. Blank lines are skipped below, so don't let them count towards the limit.
    lines = [line.strip() for line in data["radeclist"].splitlines()]

    linecount = sum(1 for line in lines if line)
    if linecount > 100:
        raise serializers.ValidationError({"radeclist": f"Number of lines ({linecount}) is above the limit of 100"})
        # lines = lines[:1]

    for index, line in enumerate(lines, 1):
        if line[:4] in ["mpc_", "MPC_", "mpc ", "MPC "]:
            if not (mpc_name := line[4:].strip()):
                raise serializers.ValidationError({"radeclist": f"Error on line {index}: MPC name is blank"})
            newrow = data.copy()
            newrow["mpc_name"] = mpc_name
            newrow["ra"] = None
            newrow["dec"] = None
            ForcePhotTaskSerializer.validate_mpc_name(mpc_name, prefix=f"Error on line {index}: ", field="radeclist")
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
                ForcePhotTaskSerializer.validate_ra(newrow["ra"], prefix=f"Error on line {index}: ", field="radeclist")
                ForcePhotTaskSerializer.validate_dec(
                    newrow["dec"], prefix=f"Error on line {index}: ", field="radeclist"
                )
                serializer = ForcePhotTaskSerializer(data=newrow, many=False)
                serializer.is_valid(raise_exception=True)
                datalist.append(newrow)

            except (OSError, IndexError) as err:
                raise serializers.ValidationError({"radeclist": f"Error on line {index}: {err}"}) from err

    if not datalist:
        # otherwise an all-blank radeclist is serialised as an empty list, and the request
        # succeeds with HTTP 201 without having created anything
        raise serializers.ValidationError({"radeclist": "No coordinates or MPC object names were given."})

    return datalist


def resultplotdatajs_cachekey(taskid: int) -> str:
    """Return the cache key holding the generated plot data for a task.

    The suffix is a version marker. Entries written by an earlier release hold a different
    payload, and the cache timeout is long enough (30 days) that they would otherwise be served
    for weeks after a deployment; bumping the suffix makes them be ignored immediately.
    """
    return f"task{taskid}_resultplotdatajs_v2"


def datetime_to_mjd(dt: datetime.datetime) -> float:
    return julian.to_jd(dt) - 2400000.5


def make_pdf_plot_worker(
    localresultfile: Path,
    taskid: int,
    taskcomment: str = "",
    logprefix: str = "",
    logfunc: t.Callable[[t.Any], t.Any] | None = None,
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
            # pyrefly: ignore [bad-argument-type]
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
