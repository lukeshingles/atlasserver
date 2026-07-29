import datetime
import itertools
import json
import tempfile
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core import mail as django_mail
from django.core.cache import caches
from django.test import override_settings
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.serializers import ValidationError

from atlasserver.forcephot.misc import splitradeclist
from atlasserver.forcephot.models import Task
from atlasserver.forcephot.serializers import ForcePhotTaskSerializer
from atlasserver.forcephot.views import calculate_queue_positions


class TaskQueueTests(TestCase):
    def setUp(self) -> None:
        self.users = [
            User.objects.create_user(username=f"user{i}", email=f"user{i}@example.com", password=None) for i in range(3)
        ]

    def test_tasklist_requires_login(self) -> None:
        response = self.client.get(reverse("task-list"))
        # anonymous users are redirected to the login page by the custom exception handler
        assert response.status_code in {302, 403}

    def test_queue_positions_are_round_robin(self) -> None:
        for user in self.users:
            for _ in range(3):
                Task.objects.create(user=user, ra=10.0, dec=20.0)

        calculate_queue_positions()

        queued = list(Task.objects.filter(finishtimestamp__isnull=True).order_by("queuepos_relative"))
        positions = [task.queuepos_relative for task in queued]
        assert positions == list(range(9)), positions

        # each pass of three consecutive positions must contain exactly one task per user
        userids = {user.pk for user in self.users}
        for passtasks in itertools.batched(queued, 3, strict=True):
            assert {task.user_id for task in passtasks} == userids


class EmailChangeTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="emailchanger", email="old@example.com", password="testpassword123"
        )

    def test_requires_login(self) -> None:
        response = self.client.get(reverse("email_change"))
        assert response.status_code == 302
        assert reverse("login") in response["Location"]

    def test_change_email(self) -> None:
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("email_change"), {"password": "testpassword123", "new_email": "new@example.com"}
        )
        assert response.status_code == 200
        self.user.refresh_from_db()
        assert self.user.email == "new@example.com"

    def test_wrong_password_rejected(self) -> None:
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("email_change"), {"password": "wrongpassword", "new_email": "new@example.com"}
        )
        assert response.status_code == 200
        self.user.refresh_from_db()
        assert self.user.email == "old@example.com"

    def test_invalid_email_rejected(self) -> None:
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("email_change"), {"password": "testpassword123", "new_email": "not-an-email"}
        )
        assert response.status_code == 200
        self.user.refresh_from_db()
        assert self.user.email == "old@example.com"


class TaskCoordValidationTests(TestCase):
    def test_zero_coordinates_are_accepted(self) -> None:
        # RA 0 and Dec 0 are valid positions, so they must not be treated as "not specified"
        for ra, dec in ((0.0, 0.0), (0.0, 20.0), (10.0, 0.0), (359.9, -0.0)):
            serializer = ForcePhotTaskSerializer(data={"ra": ra, "dec": dec})
            assert serializer.is_valid(), (ra, dec, serializer.errors)

    def test_missing_dec_is_rejected(self) -> None:
        serializer = ForcePhotTaskSerializer(data={"ra": 10.0})
        assert not serializer.is_valid()
        assert "dec" in serializer.errors

    def test_missing_ra_is_rejected(self) -> None:
        serializer = ForcePhotTaskSerializer(data={"dec": 10.0})
        assert not serializer.is_valid()
        assert "ra" in serializer.errors

    def test_no_target_is_rejected(self) -> None:
        serializer = ForcePhotTaskSerializer(data={})
        assert not serializer.is_valid()
        assert "non_field_errors" in serializer.errors

    def test_mjd_max_must_be_positive(self) -> None:
        # the task runner treats a falsy mjd_max as "not given", so a 0 upper bound would widen
        # the request to the whole archive rather than narrowing it
        for mjd_max in (0, -500):
            serializer = ForcePhotTaskSerializer(data={"ra": 1.0, "dec": 2.0, "mjd_min": None, "mjd_max": mjd_max})
            assert not serializer.is_valid(), mjd_max
            assert "mjd_max" in serializer.errors, mjd_max

    def test_mjd_max_older_than_the_default_window_is_allowed(self) -> None:
        # an explicit mjd_min of None means "no lower bound", so an archival upper bound is valid
        serializer = ForcePhotTaskSerializer(data={"ra": 1.0, "dec": 2.0, "mjd_min": None, "mjd_max": 57000.0})
        assert serializer.is_valid(), serializer.errors


def splitradeclist_rejects(data: dict) -> bool:
    """Return whether splitradeclist raises a ValidationError for the given request data."""
    try:
        splitradeclist(data)
    except ValidationError:
        return True

    return False


class SplitRaDecListTests(TestCase):
    def test_one_hundred_coords_with_trailing_newline(self) -> None:
        # a trailing newline must not push a full list of 100 targets over the limit
        radeclist = "".join(f"{10.0 + i:.4f} 20.0\n" for i in range(100))
        assert len(splitradeclist({"radeclist": radeclist})) == 100

    def test_over_the_limit_is_rejected(self) -> None:
        radeclist = "".join(f"{10.0 + i:.4f} 20.0\n" for i in range(101))
        assert splitradeclist_rejects({"radeclist": radeclist})

    def test_crlf_line_endings(self) -> None:
        # browsers normalise textarea contents to CRLF, which must not end up in the Dec value
        datalist = splitradeclist({"radeclist": "10.0,20.0\r\n11.0,21.0\r\n"})
        assert len(datalist) == 2
        assert [round(row["dec"], 4) for row in datalist] == [20.0, 21.0]

    def test_blank_lines_are_skipped(self) -> None:
        assert len(splitradeclist({"radeclist": "\n10.0 20.0\n\n11.0 21.0\n\n"})) == 2

    def test_blank_radeclist_is_rejected(self) -> None:
        # an empty result would otherwise be serialised as an empty list and answered with 201
        assert splitradeclist_rejects({"radeclist": "\n  \n"})

    def test_non_string_radeclist_is_rejected(self) -> None:
        assert splitradeclist_rejects({"radeclist": ["10.0 20.0"]})

    def test_zero_ra_and_dec_fields_are_kept(self) -> None:
        datalist = splitradeclist({"ra": 0.0, "dec": 0.0, "radeclist": "10.0 20.0"})
        assert len(datalist) == 2


class PaginationTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="pager", email="p@example.com", password=None)
        now = timezone.now()
        for i in range(10):
            Task.objects.create(user=self.user, ra=1.0, dec=2.0, timestamp=now + datetime.timedelta(seconds=i))

    def get_json(self, url, **params):
        response = self.client.get(url, params, HTTP_ACCEPT="application/json")
        assert response.status_code == 200, response.status_code
        return response.json()

    def test_second_page_with_default_ordering(self) -> None:
        self.client.force_login(self.user)
        page1 = self.get_json(reverse("task-list"))
        assert page1["next"] is not None
        page2 = self.get_json(page1["next"])
        assert page2["pagefirsttaskposition"] == 6

    def test_pagesize_is_bounded(self) -> None:
        # without max_page_size, ?pagesize=100000 serialises the user's whole task history
        Task.objects.bulk_create([Task(user=self.user, ra=1.0, dec=2.0) for _ in range(200)])
        self.client.force_login(self.user)

        assert len(self.get_json(reverse("task-list"), pagesize=100000)["results"]) == 100
        assert len(self.get_json(reverse("task-list"), pagesize=20)["results"]) == 20

    def test_walking_backwards_with_tied_ordering_values(self) -> None:
        # tasks from one radeclist submission share a timestamp, so paging back under
        # ?ordering=timestamp exercises offset cursors: the pk tiebreaker keeps tied rows in a
        # deterministic order, and the position arithmetic must count the offset correctly
        Task.objects.all().delete()
        now = timezone.now()
        for batch in range(3):
            stamp = now + datetime.timedelta(minutes=batch)
            for _ in range(9):
                Task.objects.create(user=self.user, ra=1.0, dec=2.0, timestamp=stamp)
        self.client.force_login(self.user)

        page = self.get_json(reverse("task-list"), pagesize=5, ordering="timestamp")
        allids: list[int] = []
        pages = [page]
        while page["next"]:
            page = self.get_json(page["next"])
            pages.append(page)
        for forwardpage in pages:
            # forward positions must count the offset cursors that tied values produce
            assert forwardpage["pagefirsttaskposition"] == len(allids), (forwardpage["pagefirsttaskposition"], allids)
            allids.extend(row["id"] for row in forwardpage["results"])
        assert len(allids) == 27

        # walking back via the previous links must revisit the same pages with correct positions
        page = pages[-1]
        for _ in range(10):
            if not page.get("previous"):
                break
            page = self.get_json(page["previous"])
            ids = [row["id"] for row in page["results"]]
            assert ids, "a previous page must never be empty"
            startpos = allids.index(ids[0])
            assert allids[startpos : startpos + len(ids)] == ids, (startpos, ids)
            assert page["pagefirsttaskposition"] == startpos, (page["pagefirsttaskposition"], startpos)

        assert page["results"][0]["id"] == allids[0], "the backward walk must reach the first page"

    def test_second_page_with_timestamp_ordering(self) -> None:
        # ordering is not necessarily by id, and the page position used to be counted with
        # int(current_position), which raises ValueError for a timestamp cursor
        self.client.force_login(self.user)
        page1 = self.get_json(reverse("task-list"), ordering="timestamp")
        assert page1["next"] is not None
        page2 = self.get_json(page1["next"])
        assert page2["pagefirsttaskposition"] == 6
        assert len(page2["results"]) == 4


class TaskStrTests(TestCase):
    def test_str_without_a_target(self) -> None:
        # a task with no mpc_name, ra or dec can be created in the admin panel, and formatting
        # None with a float format spec used to raise TypeError
        user = User.objects.create_user(username="nocoords", email="n@example.com", password=None)
        assert "RA Dec" in str(Task.objects.create(user=user))


class TaskDeleteFileTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="deleter", email="d@example.com", password=None)

    def make_finished_task(self, staticroot: Path, **kwargs) -> Task:
        task = Task.objects.create(user=self.user, ra=1.0, dec=2.0, finishtimestamp=timezone.now(), **kwargs)
        resultfile = Path(staticroot, f"{task.localresultfileprefix()}.txt")
        resultfile.parent.mkdir(parents=True, exist_ok=True)
        resultfile.touch()
        return task

    def test_datafile_kept_while_an_image_request_needs_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, override_settings(STATIC_ROOT=tmpdir):
            parent = self.make_finished_task(Path(tmpdir))
            Task.objects.create(user=self.user, ra=1.0, dec=2.0, request_type="IMGZIP", parent_task=parent)

            parent.delete()

            # the queued image request rsyncs this file to the compute host as its input
            assert Path(tmpdir, f"{parent.localresultfileprefix()}.txt").exists()

    def test_datafile_removed_once_the_image_request_is_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, override_settings(STATIC_ROOT=tmpdir):
            parent = self.make_finished_task(Path(tmpdir))
            Path(tmpdir, f"{parent.localresultfileprefix()}.jpg").touch()
            imagerequest = Task.objects.create(
                user=self.user, ra=1.0, dec=2.0, request_type="IMGZIP", parent_task=parent
            )

            parent.delete()
            assert Path(tmpdir, f"{parent.localresultfileprefix()}.txt").exists()

            imagerequest.delete()

            # nothing else revisits the parent, so its files must be collected here
            assert not Path(tmpdir, f"{parent.localresultfileprefix()}.txt").exists()
            assert not Path(tmpdir, f"{parent.localresultfileprefix()}.jpg").exists()

    def test_datafile_kept_while_another_image_request_remains(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, override_settings(STATIC_ROOT=tmpdir):
            parent = self.make_finished_task(Path(tmpdir))
            first = Task.objects.create(user=self.user, ra=1.0, dec=2.0, request_type="IMGZIP", parent_task=parent)
            Task.objects.create(user=self.user, ra=1.0, dec=2.0, request_type="IMGZIP", parent_task=parent)

            parent.delete()
            first.delete()

            assert Path(tmpdir, f"{parent.localresultfileprefix()}.txt").exists()

    def test_datafile_deleted_when_no_image_request_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, override_settings(STATIC_ROOT=tmpdir):
            task = self.make_finished_task(Path(tmpdir))

            task.delete()

            assert not Path(tmpdir, f"{task.localresultfileprefix()}.txt").exists()


class TaskUpdateTests(TestCase):
    def setUp(self) -> None:
        self.owner = User.objects.create_user(username="owner", email="o@example.com", password=None)
        self.staffuser = User.objects.create_user(
            username="staffer", email="s@example.com", password=None, is_staff=True
        )

    def test_staff_update_does_not_take_ownership(self) -> None:
        task = Task.objects.create(user=self.owner, ra=1.0, dec=2.0)
        self.client.force_login(self.staffuser)

        response = self.client.patch(
            reverse("task-detail", args=[task.id]),
            data=json.dumps({"ra": 1.0, "dec": 2.0, "comment": "edited by staff"}),
            content_type="application/json",
            HTTP_ACCEPT="application/json",
        )

        assert response.status_code == 200, response.status_code
        task.refresh_from_db()
        assert task.user_id == self.owner.pk
        assert task.comment == "edited by staff"

    def test_partial_update_keeps_the_existing_target(self) -> None:
        # a PATCH used to be judged only on the fields it changed, so changing just the comment
        # was rejected for having neither an mpc_name nor coordinates
        task = Task.objects.create(user=self.owner, ra=1.0, dec=2.0)
        self.client.force_login(self.owner)

        response = self.client.patch(
            reverse("task-detail", args=[task.id]),
            data=json.dumps({"comment": "just a comment"}),
            content_type="application/json",
            HTTP_ACCEPT="application/json",
        )

        assert response.status_code == 200, response.content
        task.refresh_from_db()
        assert task.comment == "just a comment"
        assert task.ra == 1.0


class RequestImagesTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="imgreq", email="i@example.com", password=None)

    def test_get_is_not_allowed(self) -> None:
        # creating a task from a GET handler is not CSRF protected
        task = Task.objects.create(user=self.user, ra=1.0, dec=2.0, finishtimestamp=timezone.now())
        self.client.force_login(self.user)

        response = self.client.get(reverse("requestimages", args=[task.id]), HTTP_ACCEPT="application/json")

        assert response.status_code == 405, response.status_code
        assert not Task.objects.filter(parent_task_id=task.id).exists()

    def test_post_creates_an_image_request(self) -> None:
        task = Task.objects.create(user=self.user, ra=1.0, dec=2.0, finishtimestamp=timezone.now())
        with tempfile.TemporaryDirectory() as tmpdir, override_settings(STATIC_ROOT=tmpdir):
            resultfile = Path(tmpdir, f"{task.localresultfileprefix()}.txt")
            resultfile.parent.mkdir(parents=True, exist_ok=True)
            resultfile.touch()

            self.client.force_login(self.user)
            response = self.client.post(reverse("requestimages", args=[task.id]), HTTP_ACCEPT="application/json")

        assert response.status_code == 302, response.status_code
        imagerequest = Task.objects.get(parent_task_id=task.id)
        assert imagerequest.request_type == "IMGZIP"
        assert imagerequest.user_id == self.user.pk
        assert imagerequest.finishtimestamp is None

    def test_url_used_by_the_frontend_has_a_trailing_slash(self) -> None:
        # APPEND_SLASH answers a slashless POST with a 301, and a browser retries a redirected
        # POST as a GET, which this endpoint no longer accepts
        task = Task.objects.create(user=self.user, ra=1.0, dec=2.0, finishtimestamp=timezone.now())
        frontendpath = Path(settings.BASE_DIR, "static/js/queuepage/src/tasklist.jsx").read_text()
        assert "'requestimages/'" in frontendpath, "tasklist.jsx must post to the slashed URL"

        self.client.force_login(self.user)
        response = self.client.post(f"/queue/{task.id}/requestimages", HTTP_ACCEPT="application/json")

        assert response.status_code == 301, response.status_code
        assert response["Location"] == reverse("requestimages", args=[task.id])

    def test_post_requires_ownership(self) -> None:
        other = User.objects.create_user(username="other", email="o2@example.com", password=None)
        task = Task.objects.create(user=other, ra=1.0, dec=2.0, finishtimestamp=timezone.now())
        self.client.force_login(self.user)

        response = self.client.post(reverse("requestimages", args=[task.id]), HTTP_ACCEPT="application/json")

        assert response.status_code in {302, 403}, response.status_code
        assert not Task.objects.filter(parent_task_id=task.id).exists()


class TaskCreateLimitTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="submitter", email="s2@example.com", password=None)

    def test_radeclist_cannot_overshoot_the_task_limit(self) -> None:
        from atlasserver.forcephot.views import MAX_USER_TASKS

        Task.objects.bulk_create(
            [
                Task(user=self.user, ra=1.0, dec=2.0, timestamp=timezone.now() - datetime.timedelta(seconds=1))
                for _ in range(MAX_USER_TASKS - 1)
            ]
        )
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("task-list"),
            data=json.dumps({"radeclist": "".join(f"{10.0 + i:.4f} 20.0\n" for i in range(10))}),
            content_type="application/json",
            HTTP_ACCEPT="application/json",
        )

        assert response.status_code == 400, response.status_code
        assert Task.objects.filter(user_id=self.user.pk).count() == MAX_USER_TASKS - 1


class TaskCreateResponseTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="creator", email="c@example.com", password=None)

    def post_tasks(self, payload):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("task-list"),
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_ACCEPT="application/json",
        )
        assert response.status_code == 201, response.content
        return response.json()

    def test_created_tasks_report_their_queue_position(self) -> None:
        # serializer.data used to be materialised (and cached) before the queue position and
        # userqueuedtasks_on_submit updates were applied, so both were always null in the response
        results = self.post_tasks({"radeclist": "150.0 20.0\n151.0 21.0\n152.0 22.0\n"})

        assert len(results) == 3
        assert [row["userqueuedtasks_on_submit"] for row in results] == [0, 1, 2]
        assert [row["queuepos"] for row in results] == [0, 1, 2]

        for row in results:
            task = Task.objects.get(id=row["id"])
            assert task.userqueuedtasks_on_submit == row["userqueuedtasks_on_submit"]
            assert task.queuepos == row["queuepos"]

    def test_single_task_creation(self) -> None:
        # RA 0 / Dec 0 doubles as the end-to-end check that zero coordinates survive the API
        result = self.post_tasks({"ra": 0.0, "dec": 0.0})
        assert result["userqueuedtasks_on_submit"] == 0
        assert result["queuepos"] == 0

        task = Task.objects.get(id=result["id"])
        assert task.ra == 0.0
        assert task.dec == 0.0


class ResultPlotDataTests(TestCase):
    HEADER = "###MJD m dm uJy duJy F err chi/N RA Dec x y maj min phi apfit mag5sig Sky Obs"

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="plotter", email="pl@example.com", password=None)
        # the taskderived cache is file-based and outlives the test database, so an entry from a
        # previous run (whose task received the same id) would poison the ragged-file assertion
        caches["taskderived"].clear()

    def datarow(self, index: int) -> str:
        return (
            f"{59000.0 + index:.5f} 18.0 0.05 {100 + index} 10 o 0 1.0 10.0 20.0 "
            f"100 100 2 2 0 -0.5 19.0 18.0 01a{index:05d}o0235o"
        )

    def test_ragged_file_returns_empty_data_and_is_not_cached(self) -> None:
        # a diagnostic line mixed into the output must not 500 the endpoint, and the empty
        # response must not be cached forever: once the file is fixed, the plot must appear
        task = Task.objects.create(user=self.user, ra=1.0, dec=2.0, finishtimestamp=timezone.now())
        with tempfile.TemporaryDirectory() as tmpdir, override_settings(STATIC_ROOT=tmpdir):
            resultfile = Path(tmpdir, f"{task.localresultfileprefix()}.txt")
            resultfile.parent.mkdir(parents=True, exist_ok=True)
            resultfile.write_text(self.HEADER + "\nWARNING: bad line\n" + self.datarow(0) + "\n")

            response = self.client.get(reverse("resultplotdatajs", args=[task.id]))
            assert response.status_code == 200
            assert response.content == b""

            # the view appends the plotting script from STATIC_ROOT: the minified bundle
            # normally, the source file when DEBUG is on (as it is in the CI settings)
            Path(tmpdir, "js", "queuepage", "src").mkdir(parents=True)
            Path(tmpdir, "js", "lightcurveplotly.min.js").write_text("// plot script\n")
            Path(tmpdir, "js", "queuepage", "src", "lightcurveplotly.js").write_text("// plot script\n")
            resultfile.write_text(self.HEADER + "\n" + self.datarow(0) + "\n" + self.datarow(1) + "\n")

            response = self.client.get(reverse("resultplotdatajs", args=[task.id]))
            assert response.status_code == 200
            assert b"jslcdata.push" in response.content, response.content[:200]


class TaskRunnerEmailTests(TestCase):
    """Tests for the batch result email.

    The task is marked finished before the email is sent, so batching must not rely on the
    finishing task still looking unfinished, and a send failure must not propagate.
    """

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="mailer", email="m@example.com", password=None)
        self.stamp = timezone.now()
        django_mail.outbox.clear()

    def make_batch_task(self, finished: bool) -> Task:
        return Task.objects.create(
            user=self.user,
            ra=1.0,
            dec=2.0,
            timestamp=self.stamp,
            send_email=True,
            finishtimestamp=timezone.now() if finished else None,
        )

    def test_email_waits_for_the_rest_of_the_batch(self) -> None:
        from atlasserver.taskrunner import main as taskrunner_main

        finishing = self.make_batch_task(finished=True)
        self.make_batch_task(finished=False)

        taskrunner_main.send_email_if_needed(task=finishing, logfunc=lambda _msg: None)

        assert not django_mail.outbox

    def test_email_sent_once_the_batch_is_complete(self) -> None:
        from atlasserver.taskrunner import main as taskrunner_main

        first = self.make_batch_task(finished=True)
        finishing = self.make_batch_task(finished=True)

        taskrunner_main.send_email_if_needed(task=finishing, logfunc=lambda _msg: None)

        assert len(django_mail.outbox) == 1
        body = django_mail.outbox[0].body
        assert django_mail.outbox[0].to == ["m@example.com"]
        for task in (first, finishing):
            assert f"Task {task.id}:" in body

    def test_send_failure_does_not_propagate(self) -> None:
        # an exception here used to kill the worker before finishtimestamp was written,
        # so the task was re-dispatched and the remote job re-run forever
        import smtplib

        from atlasserver.taskrunner import main as taskrunner_main

        finishing = self.make_batch_task(finished=True)
        logged: list[str] = []

        with mock.patch(
            "django.core.mail.message.EmailMessage.send",
            side_effect=smtplib.SMTPRecipientsRefused({"m@example.com": (550, b"nope")}),
        ):
            taskrunner_main.send_email_if_needed(task=finishing, logfunc=logged.append)

        assert any("could not send email" in line for line in logged), logged


class LogoutTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="leaver", email="l@example.com", password="pw12345678")

    def test_logout_link_is_a_post_form(self) -> None:
        # Django's LogoutView has only accepted POST since 5.0, so a plain link returns HTTP 405
        self.client.force_login(self.user)
        body = self.client.get(reverse("task-list"), HTTP_ACCEPT="text/html").content.decode()

        assert 'action="/logout/" method="post"' in body, "the log out control must be a POST form"
        assert self.client.get(reverse("logout")).status_code == 405

    def test_logout_returns_to_the_index_page(self) -> None:
        # a "next" field would win over LOGOUT_REDIRECT_URL and bounce the anonymous user to a login form
        self.client.force_login(self.user)

        response = self.client.post(reverse("logout"), follow=True)

        assert response.redirect_chain == [(reverse("index"), 302)], response.redirect_chain
        assert not response.wsgi_request.user.is_authenticated


class ApiGuideTests(TestCase):
    def test_documented_code_blocks_are_valid_python(self) -> None:
        # users copy these snippets straight out of the page
        import ast
        import html as htmlmodule
        import re

        page = Path(settings.BASE_DIR, "atlasserver/forcephot/templates/apiguide.html").read_text()
        blocks = re.findall(r"<pre><code>(.*?)</code></pre>", page, flags=re.DOTALL)
        assert blocks, "no code blocks found in the API guide"

        for index, block in enumerate(blocks):
            source = htmlmodule.unescape(block)
            try:
                ast.parse(source)
            except SyntaxError as exc:
                msg = f"API guide code block {index} does not parse: {exc}"
                raise AssertionError(msg) from exc


class ContentNegotiationTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="apiclient", email="a@example.com", password=None)
        self.client.force_login(self.user)

    def test_wildcard_accept_gets_json(self) -> None:
        # a script that sets no Accept header sends */*, and used to receive an HTML page
        response = self.client.get(reverse("task-list"), HTTP_ACCEPT="*/*")
        assert response.status_code == 200
        assert response["Content-Type"].startswith("application/json"), response["Content-Type"]

    def test_wildcard_accept_keeps_validation_errors(self) -> None:
        response = self.client.post(
            reverse("task-list"),
            data=json.dumps({"ra": "notanumber", "dec": 11}),
            content_type="application/json",
            HTTP_ACCEPT="*/*",
        )
        assert response.status_code == 400
        assert "ra" in response.json(), response.content

    def test_browser_still_gets_html(self) -> None:
        response = self.client.get(
            reverse("task-list"),
            HTTP_ACCEPT="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        )
        assert response.status_code == 200
        assert response["Content-Type"].startswith("text/html"), response["Content-Type"]


class AllowedHostsTests(TestCase):
    def test_unknown_host_is_rejected(self) -> None:
        # django.contrib.sites is not installed, so the password reset email takes its domain from
        # the Host header: accepting any host hands an attacker the reset link
        User.objects.create_user(username="victim", email="victim@example.com", password="pw12345678")

        response = self.client.post(
            reverse("password_reset"), {"email": "victim@example.com"}, HTTP_HOST="evil.example.com"
        )

        assert response.status_code == 400, response.status_code
        assert not django_mail.outbox

    def test_known_host_is_accepted(self) -> None:
        User.objects.create_user(username="victim2", email="victim2@example.com", password="pw12345678")

        response = self.client.post(
            reverse("password_reset"), {"email": "victim2@example.com"}, HTTP_HOST="fallingstar-data.com"
        )

        assert response.status_code == 302, response.status_code
        assert len(django_mail.outbox) == 1
        assert "fallingstar-data.com" in django_mail.outbox[0].body


class TaskRunnerResultFileTests(TestCase):
    def test_remove_ssostack_resultfiles(self) -> None:
        from atlasserver.taskrunner import main as taskrunner_main

        with tempfile.TemporaryDirectory() as tmpdir:
            resultsdir = Path(tmpdir)
            for ext in (".fits", ".jpg", ".txt"):
                (resultsdir / f"job00042{ext}").touch()

            with mock.patch.object(taskrunner_main.settings, "RESULTS_DIR", resultsdir):
                taskrunner_main.remove_task_resultfiles(taskid=42, request_type="SSOSTACK", logfunc=lambda _msg: None)

            assert not list(resultsdir.glob("job00042.*"))

    def test_remove_fp_resultfiles(self) -> None:
        # the .jpg and .pdf used to be left behind, and nothing can reach them once the row is gone
        from atlasserver.taskrunner import main as taskrunner_main

        with tempfile.TemporaryDirectory() as tmpdir:
            resultsdir = Path(tmpdir)
            for ext in (".txt", ".jpg", ".pdf"):
                (resultsdir / f"job00042{ext}").touch()
            (resultsdir / "job00420.txt").touch()  # a different task, must be left alone

            with mock.patch.object(taskrunner_main.settings, "RESULTS_DIR", resultsdir):
                taskrunner_main.remove_task_resultfiles(taskid=42, request_type="FP", logfunc=lambda _msg: None)

            assert not list(resultsdir.glob("job00042.*"))
            assert (resultsdir / "job00420.txt").exists()
