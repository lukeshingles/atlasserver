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

# How long a forked PDF render may run before it is killed. Generous: a large result file
# legitimately takes a while, and the point is to bound a hang, not to police slow plots.
PDF_PLOT_TIMEOUT_SECONDS: t.Final = 120.0

# grace between SIGTERM and SIGKILL for that process
TERMINATE_GRACE_SECONDS: t.Final = 5.0


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
    # deferred: plot_atlas_fp imports matplotlib, and matplotlib imports numpy. This function only
    # ever runs in the process forked by make_pdf_plot (or in the task runner's own per-task
    # process), so a module-scope import would put both of them permanently in every web worker
    # instead -- misc is imported by views for splitradeclist and the country helpers.
    from atlasserver import plot_atlas_fp

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


def run_process_with_timeout(proc: Process, timeout: float) -> bool:
    """Start `proc` and wait for it, killing it if it overruns. Return False if it was killed."""
    proc.start()
    proc.join(timeout)

    timed_out = proc.is_alive()
    if timed_out:
        # SIGTERM first so the child can unwind; SIGKILL only for one that ignores it, which would
        # otherwise leave this thread waiting on the join below exactly as an unbounded wait did
        proc.terminate()
        proc.join(TERMINATE_GRACE_SECONDS)
        if proc.is_alive():
            proc.kill()
            proc.join()

    proc.close()
    return not timed_out


def make_pdf_plot(*args, separate_process: bool = False, timeout: float = PDF_PLOT_TIMEOUT_SECONDS, **kwargs) -> bool:
    """Render a task's PDF plot. Return False if it had to be killed for exceeding `timeout`.

    matplotlib has to run in its own process or it crashes the caller, and that process is also the
    only place a time limit can be imposed: plot_atlas_fp is third-party code run against a
    user-supplied result file, and an unbounded join() here holds a mod_wsgi worker thread for as
    long as it takes, so enough slow plots exhaust the pool.

    `timeout` is ignored without `separate_process`, because there is nothing to interrupt: the
    task runner calls it that way from a process that is already per-task and already capped.
    """
    if not separate_process:
        make_pdf_plot_worker(*args, **kwargs)
        return True

    return run_process_with_timeout(Process(target=make_pdf_plot_worker, args=args, kwargs=kwargs), timeout)


# GeoIP codes that are not ISO country codes, so pycountry does not know them. They were the only
# entries the hand-maintained country table held that pycountry does not supply, and rows recorded
# with them are already in the database -- without these they would all read as "Unknown".
GEOIP_NON_ISO_COUNTRIES: t.Final = {
    "A2": "Satellite Provider",
    "O1": "Other Country",
    "AP": "Asia/Pacific Region",
    "EU": "Europe",
}


def country_code_to_name(country_code):
    if country_code == "XX" or not country_code:
        return "Unknown"

    if name := GEOIP_NON_ISO_COUNTRIES.get(country_code):
        return name

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
