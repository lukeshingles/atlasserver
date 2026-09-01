import datetime
import shutil
import typing as t
from pathlib import Path
from typing import override

from django.conf import settings

# the project uses the default user model, and django-stubs types against the concrete class
from django.contrib.auth.models import User
from django.core.cache import caches
from django.db import models
from django.db import transaction
from django.db.models import Min
from django.db.models.functions import Replace
from django.db.models.functions import Trim
from django.db.models.lookups import Exact
from django.urls import get_script_prefix
from django.urls import reverse
from django.utils import timezone

from atlasserver.forcephot.misc import country_code_to_name
from atlasserver.forcephot.misc import datetime_to_mjd
from atlasserver.forcephot.misc import resultplotdatajs_cachekey


def get_mjd_min_default() -> float:
    return round(datetime_to_mjd(datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=30)), 5)


# marks a memoised value that has not been computed yet. None is a meaningful result for every
# value memoised below, so it cannot double as the "not computed" marker.
UNSET: t.Final = object()

# The whitespace a name may be made of and still count as no target. SQL and Python must agree on
# it exactly -- TRIM() strips only spaces where str.strip() strips much more -- or the constraint
# accepts a name the runner reads as absent and dispatches by coordinates the constraint let be
# NULL. Not all of Unicode's whitespace: each character is another nested REPLACE in a stored
# constraint, and what matters is that both sides derive from this one constant.
MPC_NAME_WHITESPACE: t.Final = " \t\n\r\v\f\u00a0"


def _space_normalised(field: str) -> Trim:
    """Return the field with MPC_NAME_WHITESPACE collapsed to spaces and then trimmed."""
    expression: t.Any = field
    for character in MPC_NAME_WHITESPACE:
        if character != " ":
            expression = Replace(expression, models.Value(character), models.Value(" "))

    return Trim(expression)


# "the mpc_name column holds no target", for the check constraint below and for the callers that
# have to draw the same line in a query. Not a bare == "": a name of nothing but whitespace is not
# a target, but it is truthy, so it satisfied the plain test and then reached the runner, which
# interpolates it into ssforce.sh.
#
# Migration 0006 spells this out again rather than importing it: a migration has to keep working
# when the model moves on. The two must stay identical in deconstructed form, or makemigrations
# reads the difference as model drift and asks for another migration.
BLANK_MPC_NAME = models.Q(Exact(_space_normalised("mpc_name"), models.Value("")))


class Task(models.Model):
    class RequestType(models.TextChoices):
        FP = "FP", "Forced Photometry Data"
        IMGZIP = "IMGZIP", "Image Zip"
        IMGSTACK = "SSOSTACK", "Solar System object image stack"

    timestamp = models.DateTimeField(default=timezone.now)
    starttimestamp = models.DateTimeField(null=True, blank=True, default=None)
    finishtimestamp = models.DateTimeField(null=True, blank=True, default=None)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)

    # the task must specify either Minor Planet Center object name (overrides RA and Dec)
    # or RA and Dec in floating-point degrees
    mpc_name = models.CharField(
        null=True,
        blank=True,
        default=None,
        max_length=300,
        verbose_name="Minor Planet Center object name (overrides RA/Dec)",
    )

    ra = models.FloatField(null=True, blank=True, default=None, verbose_name="Right Ascension (degrees)")
    dec = models.FloatField(null=True, blank=True, default=None, verbose_name="Declination (degrees)")

    mjd_min = models.FloatField(null=True, blank=True, default=get_mjd_min_default, verbose_name="MJD min")
    mjd_max = models.FloatField(null=True, blank=True, default=None, verbose_name="MJD max")
    comment = models.CharField(default=None, null=True, blank=True, max_length=300)
    use_reduced = models.BooleanField("Use reduced images instead of difference images", default=False)
    send_email = models.BooleanField("Email me when completed", default=True)
    from_api = models.BooleanField(default=False)
    country_code = models.CharField(default=None, null=True, blank=True, max_length=2)
    region = models.CharField(default=None, null=True, blank=True, max_length=256)
    error_msg = models.CharField(
        null=True, blank=True, default=None, max_length=512, verbose_name="Error messages during execution"
    )
    is_archived = models.BooleanField(default=False)
    # optional completion callback for API clients, which are not sent result emails and would
    # otherwise have to poll. Validated on submission and again before the request is sent, see
    # atlasserver.forcephot.webhooks.
    callback_url = models.URLField(
        null=True,
        blank=True,
        default=None,
        max_length=500,
        verbose_name="Completion callback URL (API only)",
    )
    radec_epoch_year = models.DecimalField(
        null=True, blank=True, max_digits=7, decimal_places=1, verbose_name="Epoch year"
    )
    propermotion_ra = models.FloatField(null=True, blank=True, verbose_name="Proper motion RA (mas/yr)")
    propermotion_dec = models.FloatField(null=True, blank=True, verbose_name="Proper motion Dec (mas/yr)")
    # How many times the runner has started this task; zero until the first attempt. The timestamps
    # describe only the attempt that produced the result, so this is the one record that a task
    # needed more than one. See taskrunner.main.mark_started.
    attempt_count = models.IntegerField(default=0, verbose_name="Execution attempts")

    queuepos_relative = models.IntegerField(null=True, blank=True, default=None, verbose_name="Queue position")
    userqueuedtasks_on_submit = models.IntegerField(
        null=True, blank=True, default=None, verbose_name="User queued tasks when submitted", editable=False
    )

    parent_task = models.ForeignKey(
        "self",
        related_name="imagerequest",
        # CASCADE rather than SET_NULL: an image request has no meaning without the task whose
        # results it was made from, so it goes when that goes
        on_delete=models.CASCADE,
        null=True,
        default=None,
        limit_choices_to={"request_type": "FP"},
    )

    request_type = models.CharField(max_length=8, choices=RequestType.choices, default=RequestType.FP)

    task_modified_datetime = models.DateTimeField(auto_now=True)

    id: int
    user_id: int
    parent_task_id: int | None

    # per-instance memoisation slots, declared as class attributes so that no __init__ override is
    # needed. Model instances do not outlive a request, so a stale entry is not a concern.
    _localresultfile_cache: t.Any = UNSET
    _imagerequest_cache: t.Any = UNSET

    class Meta:
        constraints = [
            # Exactly one of the two forms: an MPC name with no coordinates, or both coordinates
            # with no name. The serializer applies the same rule, but was the only thing that did,
            # so the admin and the shell could create a task the runner would dispatch for nothing.
            # NULL rather than falsiness, because RA 0 / Dec 0 are real coordinates.
            models.CheckConstraint(
                condition=(
                    (models.Q(mpc_name__isnull=False) & ~BLANK_MPC_NAME & models.Q(ra__isnull=True, dec__isnull=True))
                    | (
                        (models.Q(mpc_name__isnull=True) | BLANK_MPC_NAME)
                        & models.Q(ra__isnull=False, dec__isnull=False)
                    )
                ),
                name="task_target_is_mpcname_or_radec",
            ),
            # And mpc_name is never whitespace alone: NULL, "" or a real name. save() normalises,
            # but bulk_create, bulk_update and update() do not go through it, so the guarantee has
            # to live here. It is what lets every reader test the field for truth rather than
            # re-deriving what counts as blank -- which five of them were doing.
            models.CheckConstraint(
                condition=models.Q(mpc_name__isnull=True) | models.Q(mpc_name="") | ~BLANK_MPC_NAME,
                name="task_mpc_name_not_blank",
            ),
        ]
        indexes = [
            # the task list: filter on (is_archived, user), order by (-timestamp, -id)
            models.Index(fields=["is_archived", "user", "-timestamp", "-id"], name="task_userlist_idx"),
            # the task runner's queue scan and the queuepos aggregate: unfinished and not archived,
            # ordered by queue position
            models.Index(fields=["finishtimestamp", "is_archived", "queuepos_relative"], name="task_queue_idx"),
            # Task._imagerequest_task(): the live image request belonging to a parent task
            models.Index(fields=["parent_task", "is_archived", "id"], name="task_imagereq_idx"),
            # the hourly maintenance sweeps, which select by type and finish time
            models.Index(fields=["request_type", "finishtimestamp"], name="task_maint_idx"),
            # the usage statistics views, which window on submission time
            models.Index(fields=["timestamp"], name="task_timestamp_idx"),
        ]

    def __str__(self) -> str:
        """Return a string representation of the task (as seen in the admin panel list of tasks)."""
        user = self.user
        targetstr = " " + self.describe_target()

        if self.finishtimestamp:
            status = "finished"
        elif self.starttimestamp:
            status = "running"
        else:
            status = "queued"

        strtask = (
            f"Task {self.id:d}: {self.timestamp:%Y-%m-%d %H:%M:%S %Z} {user.username} ({user.email})"
            + (f" '{country_code_to_name(self.country_code)}'" if self.country_code else "")
            + f"{' API' if self.from_api else ''} {self.request_type}"
            + targetstr
            + f" {'redimg' if self.use_reduced else 'diffimg'} {status} {' archived' if self.is_archived else ''}"
        )

        # tested against the value rather than against the timestamps it is derived from, so the
        # condition cannot drift from the one inside waittime()/runtime()
        if (waittime := self.waittime()) is not None:
            strtask += f" waittime: {waittime:.0f}s"
        if (runtime := self.runtime()) is not None:
            strtask += f" runtime: {runtime:.0f}s"

        strtask += f" queuedtasks_on_submit: {self.userqueuedtasks_on_submit}"

        return strtask

    @override
    def save(self, *args: t.Any, **kwargs: t.Any) -> None:
        """Normalise mpc_name, then save as usual.

        Whitespace around a name is not part of it, and a name of nothing but whitespace is not a
        name -- the check constraint refuses to store one. Doing it here means readers can test the
        field for truth: it is falsy exactly when the task has no MPC target.

        MPC_NAME_WHITESPACE rather than str.strip(), so this and the constraint agree on the set.
        """
        if self.mpc_name is not None:
            self.mpc_name = self.mpc_name.strip(MPC_NAME_WHITESPACE)

        super().save(*args, **kwargs)

    def describe_target(self) -> str:
        """Return the target of this task as one short string, for logs and mail.

        One definition: the result email used to print "RA None Dec None" for every MPC task,
        because it read the coordinate columns and nothing else.
        """
        if self.mpc_name:
            return f"MPC[{self.mpc_name}]"
        if self.ra is not None and self.dec is not None:
            return f"RA Dec: {self.ra:09.4f} {self.dec:09.4f}"

        # a task with no target at all could be created in the admin panel
        return "RA Dec: (none)"

    def public_url(self) -> str:
        """Return the absolute URL of this task's page, for a message sent from outside a request.

        The task runner sends the result email and the completion callback, and it has no request
        to build a URL from. SITE_ORIGIN is the same authority the verification mail uses. The
        script prefix comes from settings when no request has set one, which is the runner's case;
        inside a request, reverse() already carries it.
        """
        path = reverse("task-detail", args=[self.id])
        if get_script_prefix() == "/":
            path = settings.PATHPREFIX + path

        # development has no origin configured; a wrong host in a development mail is harmless
        return (settings.SITE_ORIGIN or "http://localhost:8000") + path

    def localresultfileprefix(self, use_parent: bool = False) -> str:
        """Return the relative path prefix for the job (no file extension)."""
        # the id column, so that a child answers without loading its parent row
        int_id = int(self.parent_task_id) if use_parent and self.parent_task_id else int(self.id)
        return f"results/job{int_id:05d}"

    def localresultfile(self) -> str | None:
        """Return the relative path to the FP data file if the job is finished, and the file exists.

        The answer is memoised on the instance: serialising one task asks for it twice (once for
        the result URL and once for the PDF plot URL), and each miss costs a filesystem stat.
        """
        if self._localresultfile_cache is UNSET:
            resultfile = None
            if self.finishtimestamp:
                relpath = f"{self.localresultfileprefix()}.txt"
                if Path(settings.STATIC_ROOT, relpath).exists():
                    resultfile = relpath
            self._localresultfile_cache = resultfile

        return self._localresultfile_cache

    @property
    def localresultpreviewimagefile(self) -> str | None:
        """Return the relative path to the preview image if it exists, otherwise None.

        An image request shares its parent's preview, so this can resolve to another row's file.
        This refuses an archived parent, because the views filter only on the id they were asked
        for: a live image request's id would otherwise reach the parent's image.
        """
        if self.finishtimestamp and not (self.parent_task is not None and self.parent_task.is_archived):
            imagefile = f"{self.localresultfileprefix(use_parent=True)}.jpg"
            if Path(settings.STATIC_ROOT, imagefile).exists():
                return imagefile

        return None

    @property
    def localresultpdfplotfile(self) -> str | None:
        """Return the full local path to the PDF plot file if the job is finished."""
        return f"{self.localresultfileprefix()}.pdf" if self.finishtimestamp else None

    @property
    def localresultimagezipfile(self) -> Path | None:
        """Return the relative path to this image request's zip if it exists, otherwise None.

        Named for this task. An image request written before that was named for its parent, so
        that name is tried second, for the rows that finished under the old rule and have not yet
        been swept.
        """
        for prefix in (self.localresultfileprefix(), self.localresultfileprefix(use_parent=True)):
            imagezipfile = Path(f"{prefix}.zip")
            if Path(settings.STATIC_ROOT, imagezipfile).exists():
                return imagezipfile

        return None

    def inputfile(self) -> Path:
        """Return the private path of this image request's input: a copy of the parent's data file.

        Outside STATIC_ROOT, which the web server serves without asking Django. The copy is what
        lets the parent's own file go with the parent: the runner reads this one, so nothing that
        outlives a deleted task is left at a public path for it.
        """
        return Path(settings.TASK_INPUTS_DIR, f"job{self.id:05d}.txt")

    def copy_parent_datafile(self) -> bool:
        """Copy the parent's data file to this image request's input path. Return whether it existed.

        Called once the row has an id. A parent with no data file cannot have images fetched for
        it; the runner reports that when it dispatches the request, so nothing is raised here.
        """
        if self.parent_task is None or not (source := self.parent_task.localresultfile()):
            return False

        target = self.inputfile()
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(Path(settings.STATIC_ROOT, source), target)
        except FileNotFoundError:
            # gone between the existence check in localresultfile() and here; the caller holds
            # the parent's row lock, so only the maintenance sweep can do that
            return False

        return True

    @property
    def localresultimagestackfile(self) -> Path | None:
        """Return the full local path to the image stack FITS file if it exists, otherwise None."""
        imagstackfile = Path(f"{self.localresultfileprefix()}.fits")
        return imagstackfile if Path(settings.STATIC_ROOT, imagstackfile).exists() else None

    def live_imagerequests(self) -> "list[Task]":
        """Return the image requests of this task that are not archived, oldest first.

        Answered from the prefetch when the caller made one (see prefetch_imagerequests), so a page
        or a maintenance batch costs one query for all of its tasks rather than one each. Memoised
        otherwise: one serialisation asks three times, and delete_result_files asks again.

        Ordered by id, so that every caller agrees on which request is the first one. A parent can
        carry more than one live image request, for example from a double-clicked button.
        """
        prefetched = getattr(self, "prefetched_imagerequests", None)
        if prefetched is not None:
            return list(prefetched)

        if self._imagerequest_cache is UNSET:
            self._imagerequest_cache = list(Task.live().filter(parent_task_id=self.id).order_by("id"))

        return self._imagerequest_cache

    def _imagerequest_task(self) -> "Task | None":
        """Return the image request that this task reports, or None if it has none."""
        live = self.live_imagerequests()

        return live[0] if live else None

    def live_imagerequest_ids(self) -> list[int]:
        """Return the ids of the image requests that still read this task's data file."""
        return [imagerequest.id for imagerequest in self.live_imagerequests()]

    @property
    def imagerequest_task_id(self) -> int | None:
        """Return the image request task id associated with this forced photometry task, or None."""
        imagerequest = self._imagerequest_task()
        return imagerequest.id if imagerequest is not None else None

    @property
    def imagerequest_finished(self) -> bool | None:
        """Return whether this task's image request has finished, or None if it has none."""
        imagerequest = self._imagerequest_task()
        return bool(imagerequest.finishtimestamp) if imagerequest is not None else None

    @staticmethod
    def live() -> "models.QuerySet[Task]":
        """Return the tasks that have not been archived: everything a reader may still be shown.

        delete() archives a finished task instead of removing it, so archived means the owner
        deleted it. One definition, because every reader has to agree. A reader that misses this
        rule serves data the owner asked to have removed, and a writer that misses it keeps a file
        for an image request that no reader can reach.

        Two readers apply the same predicate on a queryset of their own, because DRF hands them
        one: ForcePhotTaskViewSet.list filters its queryset, and retrieve tests the instance that
        get_object() returns.
        """
        return Task.objects.filter(is_archived=False)

    @staticmethod
    def queued() -> "models.QuerySet[Task]":
        """Return the tasks that are waiting or running: everything the queue positions cover.

        One definition, because both ends of the range are read together -- Task.queuepos subtracts
        the minimum while forcephot.queue assigns from the maximum -- and the task runner scans the
        same set. Changing what counts as queued in one place only would give a submitted task a
        position measured against a differently scoped baseline.
        """
        return Task.live().filter(finishtimestamp__isnull=True)

    @staticmethod
    def min_queuepos_relative() -> int:
        """Return the lowest queue position currently assigned, or 0 if the queue is empty.

        After completing a job, the next job might not have queuepos_relative=0 until a queue order
        refresh is done, so queuepos_relative=1 could have queuepos 0 (is next).

        This does not depend on any particular task, so a caller serialising many tasks should call
        it once rather than once per task (see ForcePhotTaskSerializer.min_queuepos_relative).
        """
        minqueuepos = Task.queued().aggregate(Min("queuepos_relative"))["queuepos_relative__min"]

        return 0 if minqueuepos is None else int(minqueuepos)

    @property
    def queuepos(self) -> int | None:
        if self.finishtimestamp or self.queuepos_relative is None:
            return None

        return self.queuepos_relative - Task.min_queuepos_relative()

    def is_owned_by(self, user: t.Any) -> bool:
        """Return whether this user may act on the task: its owner, or any staff member.

        One definition, because three places apply it -- ForcePhotPermission, the image request
        view, and the serializer when it decides who may read back the submitter's callback URL.
        Each used to spell it out again, so a change to one of them reached only that one.

        user is Any because the type checkers disagree about it: django-stubs declares is_staff on
        the concrete User, while ty reads Django's own source, where request.user is typed as
        AbstractBaseUser. AUTH_USER_MODEL is swappable in principle and is not swapped here.
        """
        return bool(
            user is not None
            and getattr(user, "is_authenticated", False)
            and (getattr(user, "is_staff", False) or self.user_id == user.pk)
        )

    def finished(self) -> bool:
        return bool(self.finishtimestamp)

    # None rather than a float sentinel for "not applicable yet", so a caller that forgets to check
    # gets a TypeError rather than a number that quietly propagates. It also crosses the API
    # boundary: NaN is not part of JSON, and the renderer emits it as a bare token that JSON.parse
    # rejects, so one unstarted task would fail a whole response.
    def waittime(self) -> float | None:
        """Return how long the task waited before starting, or None if it has not started."""
        if self.starttimestamp and self.timestamp:
            # Floored at zero, but only across the sub-second gap the runner's own rounding opens:
            # it writes starttimestamp truncated to the whole second while timestamp keeps its
            # microseconds, so a task dispatched the moment it arrives computes a small negative.
            # Anything below that is clock skew between the two hosts and is left visible rather
            # than flattened into a plausible-looking zero.
            waited = (self.starttimestamp - self.timestamp).total_seconds()
            return 0.0 if -1.0 < waited < 0.0 else waited

        return None

    def runtime(self) -> float | None:
        """Return how long the task took to run, or None if it has not finished running."""
        if self.finishtimestamp and self.starttimestamp:
            return (self.finishtimestamp - self.starttimestamp).total_seconds()

        return None

    @property
    def username(self) -> str:
        return self.user.username

    @staticmethod
    def prefetch_imagerequests() -> "models.Prefetch[str, models.QuerySet[Task, Task]]":
        """Return the prefetch that lets live_imagerequests() answer without a query per task."""
        return models.Prefetch(
            "imagerequest",
            queryset=Task.live().order_by("id"),
            to_attr="prefetched_imagerequests",
        )

    def new_imagerequest(self, user: User, *, from_api: bool) -> "Task":
        """Return an unsaved IMGZIP task that retrieves the images behind this finished FP task.

        Here rather than in the view, so that the decision of which fields a child inherits sits
        next to the field declarations it reads. This used to be model_to_dict(exclude=["id"]) in
        the view, which took every editable field and then corrected the ones that must not carry
        over one at a time — so each field added to Task was inherited by default, and
        callback_url (the parent submitter's completion webhook, fired for a task they never
        created) was one of them. Anything not named here is left at the model default on purpose.
        """
        return Task(
            user=user,
            parent_task_id=self.id,
            request_type=Task.RequestType.IMGZIP,
            timestamp=datetime.datetime.now(datetime.UTC).replace(microsecond=0),
            # the images to fetch are the observations the parent reported, so the image request
            # describes the same target over the same window
            mpc_name=self.mpc_name,
            ra=self.ra,
            dec=self.dec,
            mjd_min=self.mjd_min,
            mjd_max=self.mjd_max,
            radec_epoch_year=self.radec_epoch_year,
            propermotion_ra=self.propermotion_ra,
            propermotion_dec=self.propermotion_dec,
            # decides which image directory the remote script reads, so it has to match
            use_reduced=self.use_reduced,
            comment=self.comment,
            # the origin of this request, not of the parent: the caller knows which authenticator
            # accepted the request. from_api selects the maintenance sweep that collects the row
            # (31 days for API tasks, 183 otherwise); send_email=False below already stops the
            # result email that from_api would otherwise suppress
            from_api=from_api,
            send_email=False,
            # the parent's location, kept when the caller cannot place the new request itself
            country_code=self.country_code,
            region=self.region,
        )

    def _unlink_results(self, extensions: t.Iterable[str]) -> None:
        """Remove these result files of this task, whether or not they are on disk."""
        for ext in extensions:
            Path(settings.STATIC_ROOT, self.localresultfileprefix() + ext).unlink(missing_ok=True)

    def delete_result_files(self) -> None:
        """Delete the result files of this task. Every file belongs to one task.

        An image request works from its own copy of the parent's data file (see inputfile) and
        writes a zip named for itself, so nothing here is read by another row. A zip named for the
        parent is a legacy name from before that rule; it goes with the parent.

        Split out of delete() so that the maintenance sweep can reclaim the files of many tasks and
        then update or remove their rows in one statement, instead of one write per task.
        """
        if self.request_type == "IMGZIP":
            self._unlink_results([".zip"])
            self.inputfile().unlink(missing_ok=True)

            # A zip written under the parent's name, from before an image request had its own.
            # Reclaimed here once no other live image request of the parent is still served from
            # it -- a row without a zip of its own is. Otherwise it would stay at its static path
            # until the parent went. One query, and only while such a file exists; the parent row
            # itself is not loaded, so a maintenance batch stays at its fixed query count.
            legacyzip = Path(f"{self.localresultfileprefix(use_parent=True)}.zip")
            if self.parent_task_id is not None and Path(settings.STATIC_ROOT, legacyzip).exists():
                siblings = Task.live().filter(parent_task_id=self.parent_task_id).exclude(id=self.id)
                still_served = any(
                    not Path(settings.STATIC_ROOT, f"{sibling.localresultfileprefix()}.zip").exists()
                    for sibling in siblings
                )
                if not still_served:
                    Path(settings.STATIC_ROOT, legacyzip).unlink(missing_ok=True)
        else:
            # The .jpg and the .txt go with the row too. localresultpreviewimagefile refuses an
            # archived parent, so nothing renders the preview once this row is archived, and each
            # image request holds its own copy of the data file. The web server serves results
            # from STATIC_URL without Django, so a file kept past its row would stay readable by
            # every link that was published before.
            self._unlink_results([".pdf", ".fits", ".jpg", ".txt", ".zip"])

    def forget_derived_cache(self) -> None:
        """Drop the cached plot data generated from this task's result file."""
        caches["taskderived"].delete(resultplotdatajs_cachekey(self.id))

    # returns None rather than Model.delete's (count, per-type counts): a finished task is archived
    # instead of deleted, so there is no honest count to report for that branch
    def delete(  # type: ignore[override]  # ty: ignore[invalid-method-override]
        self, using: str | None = None, keep_parents: bool = False
    ) -> None:
        with transaction.atomic(using=using):
            # This row locked first, before any file goes. RequestImages holds the same lock on
            # a parent while it copies the parent's data file for a new image request, so that
            # copy is complete, or not started, by the time the file is reclaimed here.
            if self.pk is not None:
                list(Task.objects.select_for_update().filter(pk=self.pk).values_list("id", flat=True))

            # cleanup associated files when removing a task object from the database
            self.delete_result_files()

            # keep finished jobs in the database but mark them as archived and hide them from the
            # website
            if self.finished():
                self.is_archived = True
                # only the one column changed, so there is no reason to rewrite every other one
                self.save(update_fields=["is_archived", "task_modified_datetime"])
                self.forget_derived_cache()
            else:
                super().delete(using=using, keep_parents=keep_parents)


class PendingEmailVerification(models.Model):
    """Marks an account that registered and has not yet proved its address.

    This exists because "inactive" alone cannot say why. Unchecking is_active is equally how an
    administrator disables an account -- Django's own help text recommends it in place of deleting
    -- so a resend path that treats every inactive account as unverified hands a disabled one a way
    back in. The previous answer inferred the difference from a null last_login, which is wrong for
    any account that was disabled before it ever logged in, or that only ever used an API token.

    A table this project owns, rather than a column on auth_user: the user model is
    django.contrib.auth's and adding to it means either a custom user model or a sidecar column
    Django cannot query through. A row here is created with the account and deleted the moment the
    address is confirmed, so its presence is the whole state -- and existing accounts have no row,
    which is correct, because none of them came from the verification flow.
    """

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="pending_verification")
    created = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        """Return a description for the admin changelist."""
        return f"awaiting verification since {self.created:%Y-%m-%d}: {self.user}"
