import datetime
import itertools
import json
import socket
import subprocess
import tempfile
import threading
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock
from unittest import skipUnless

from django.conf import settings

# the project uses the default user model, and the concrete class is needed for typing
from django.contrib.auth.models import User  # pylint: disable=imported-auth-user
from django.core import mail as django_mail
from django.core.cache import caches
from django.db import connection
from django.db import models
from django.test import override_settings
from django.test import TestCase
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework.serializers import ValidationError

from atlasserver.forcephot.misc import splitradeclist
from atlasserver.forcephot.models import Task
from atlasserver.forcephot.serializers import ForcePhotTaskSerializer
from atlasserver.forcephot.serializers import is_finite_float
from atlasserver.forcephot.views import calculate_queue_positions
from atlasserver.forcephot.webhooks import CallbackUrlError
from atlasserver.forcephot.webhooks import send_task_callback
from atlasserver.forcephot.webhooks import validate_callback_url
from atlasserver.taskrunner import main as taskrunner_main
from atlasserver.taskrunner import status as runnerstatus


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

    def test_structured_values_are_rejected_rather_than_crashing(self) -> None:
        # a JSON body can put a list or an object where a number belongs. float() raises TypeError
        # for those, not ValueError, and is_finite_float() used to let it escape as a 500.
        for badvalue in ([1, 2], {"deg": 1}):
            serializer = ForcePhotTaskSerializer(data={"ra": badvalue, "dec": 2.0})
            assert not serializer.is_valid(), badvalue
            assert "ra" in serializer.errors, (badvalue, serializer.errors)

    def test_is_finite_float_rejects_values_it_cannot_convert(self) -> None:
        for badvalue in (None, "", "abc", [1.0], {"a": 1}, float("nan"), float("inf")):
            assert is_finite_float(badvalue) is False, badvalue

        for goodvalue in (0, 0.0, "1.5", -90):
            assert is_finite_float(goodvalue) is True, goodvalue


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


class TaskListQueryCountTests(TestCase):
    """Pin the number of database queries a task list response costs.

    The queue page polls this endpoint every 2 seconds per open tab, so a per-task query added by
    a new SerializerMethodField multiplies straight into the server's steady-state load. These
    numbers are an upper bound, not a target: lowering them is fine, raising them needs a reason.
    """

    # one page holds settings.REST_FRAMEWORK["PAGE_SIZE"] tasks
    QUERY_BUDGET = 12

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="counter", email="q@example.com", password=None)

    def get_list(self):
        return self.client.get(reverse("task-list"), HTTP_ACCEPT="application/json")

    def test_query_count_does_not_grow_with_page_size(self) -> None:
        # the point of the prefetching is that a page of many tasks costs the same as a page of one
        self.client.force_login(self.user)
        Task.objects.create(user=self.user, ra=1.0, dec=2.0)
        with CaptureQueriesContext(connection) as onetask:
            assert self.get_list().status_code == 200

        for _ in range(20):
            Task.objects.create(user=self.user, ra=1.0, dec=2.0)
        with CaptureQueriesContext(connection) as manytasks:
            assert self.get_list().status_code == 200

        assert len(manytasks) == len(onetask), (
            f"a full page cost {len(manytasks)} queries vs {len(onetask)} for a single task"
            f" — something runs per task:\n" + "\n".join(q["sql"] or "" for q in manytasks)
        )

    def test_query_count_is_within_budget(self) -> None:
        self.client.force_login(self.user)
        for _ in range(20):
            Task.objects.create(user=self.user, ra=1.0, dec=2.0)

        with CaptureQueriesContext(connection) as queries:
            assert self.get_list().status_code == 200

        assert len(queries) <= self.QUERY_BUDGET, f"{len(queries)} queries:\n" + "\n".join(
            q["sql"] or "" for q in queries
        )

    def test_finished_tasks_with_image_requests_do_not_add_queries(self) -> None:
        # imagerequest_task_id, imagerequest_finished and the imagerequest URL all resolve the same
        # relation, and each used to be a separate query
        self.client.force_login(self.user)
        for _ in range(6):
            parent = Task.objects.create(user=self.user, ra=1.0, dec=2.0, finishtimestamp=timezone.now())
            Task.objects.create(
                user=self.user, ra=1.0, dec=2.0, request_type="IMGZIP", parent_task=parent, finishtimestamp=None
            )

        with CaptureQueriesContext(connection) as queries:
            response = self.get_list()

        assert response.status_code == 200
        assert len(queries) <= self.QUERY_BUDGET, f"{len(queries)} queries:\n" + "\n".join(
            q["sql"] or "" for q in queries
        )

    def test_queue_positions_are_still_correct(self) -> None:
        # the queue offset is now computed once per response instead of once per task, so check the
        # values it produces still match the model property
        self.client.force_login(self.user)
        for _ in range(3):
            Task.objects.create(user=self.user, ra=1.0, dec=2.0)
        calculate_queue_positions()
        # a non-zero offset: the lowest assigned position is not 0
        Task.objects.all().update(queuepos_relative=models.F("queuepos_relative") + 5)

        results = self.get_list().json()["results"]

        assert results
        for row in results:
            assert row["queuepos"] == Task.objects.get(id=row["id"]).queuepos
        assert min(row["queuepos"] for row in results) == 0


class TaskListEtagTests(TestCase):
    """The queue page polls every 2 seconds, so an unchanged page must be answerable with a 304.

    The etag used to mix in a wall-clock timestamp and aggregate over every user's tasks, so it
    changed at least once a minute and on any other user's activity. It also has to notice a
    reordering, which calculate_queue_positions() performs with a queryset .update() that does not
    touch the auto_now field.
    """

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="etagger", email="e@example.com", password=None)
        self.other = User.objects.create_user(username="etagother", email="e2@example.com", password=None)
        self.client.force_login(self.user)

    def get_list(self, etag=None):
        headers = {"HTTP_IF_NONE_MATCH": etag} if etag else {}
        return self.client.get(reverse("task-list"), HTTP_ACCEPT="application/json", **headers)

    def test_unchanged_list_returns_304(self) -> None:
        Task.objects.create(user=self.user, ra=1.0, dec=2.0)
        first = self.get_list()
        assert first.status_code == 200
        assert first["ETag"]

        assert self.get_list(etag=first["ETag"]).status_code == 304

    def test_etag_is_quoted(self) -> None:
        # an unquoted ETag is not a valid entity-tag, and intermediaries may reject or rewrite it
        Task.objects.create(user=self.user, ra=1.0, dec=2.0)
        etag = self.get_list()["ETag"]
        assert etag.startswith('"'), etag
        assert etag.endswith('"'), etag

    def test_another_users_activity_does_not_invalidate_the_etag(self) -> None:
        Task.objects.create(user=self.user, ra=1.0, dec=2.0)
        etag = self.get_list()["ETag"]

        Task.objects.create(user=self.other, ra=3.0, dec=4.0)

        assert self.get_list(etag=etag).status_code == 304

    def test_own_new_task_invalidates_the_etag(self) -> None:
        Task.objects.create(user=self.user, ra=1.0, dec=2.0)
        etag = self.get_list()["ETag"]

        Task.objects.create(user=self.user, ra=3.0, dec=4.0)

        assert self.get_list(etag=etag).status_code == 200

    def test_own_task_finishing_invalidates_the_etag(self) -> None:
        task = Task.objects.create(user=self.user, ra=1.0, dec=2.0)
        etag = self.get_list()["ETag"]

        task.finishtimestamp = timezone.now()
        task.save()

        assert self.get_list(etag=etag).status_code == 200

    def test_a_child_image_request_finishing_invalidates_the_etag(self) -> None:
        # imagerequest_finished is rendered on the parent's row, but the child may be on another
        # page, so a per-page-rows etag would miss this
        parent = Task.objects.create(user=self.user, ra=1.0, dec=2.0, finishtimestamp=timezone.now())
        child = Task.objects.create(user=self.user, ra=1.0, dec=2.0, request_type="IMGZIP", parent_task=parent)
        etag = self.get_list()["ETag"]

        child.finishtimestamp = timezone.now()
        child.save()

        assert self.get_list(etag=etag).status_code == 200

    def test_another_users_submission_that_reorders_this_user_invalidates_the_etag(self) -> None:
        """A round-robin renumbering that moves this user's task must not be served from cache.

        This user's two tasks start at positions 0 and 1. When the other user submits, the second
        pass pushes this user's second task to position 2 while the front of the queue stays at 0 —
        so neither the queue offset nor any of this user's own timestamps move on their own.
        calculate_queue_positions() has to write task_modified_datetime for the change to be seen.
        """
        mine = [Task.objects.create(user=self.user, ra=1.0, dec=2.0) for _ in range(2)]
        calculate_queue_positions()
        etag = self.get_list()["ETag"]

        before = Task.objects.get(id=mine[1].id).queuepos_relative
        Task.objects.create(user=self.other, ra=3.0, dec=4.0)
        calculate_queue_positions()

        assert Task.objects.get(id=mine[1].id).queuepos_relative != before, "the test did not reorder anything"
        assert Task.min_queuepos_relative() == 0, "the queue offset moved, so this tests the wrong mechanism"
        assert self.get_list(etag=etag).status_code == 200

    def test_a_direct_reordering_invalidates_the_etag(self) -> None:
        # the queue offset covers a renumbering that moves the front of the queue
        Task.objects.create(user=self.user, ra=1.0, dec=2.0)
        calculate_queue_positions()
        etag = self.get_list()["ETag"]

        Task.objects.filter(user_id=self.user.pk).update(queuepos_relative=models.F("queuepos_relative") + 3)

        assert self.get_list(etag=etag).status_code == 200

    def test_304_costs_fewer_queries_than_a_full_response(self) -> None:
        for _ in range(6):
            Task.objects.create(user=self.user, ra=1.0, dec=2.0)
        etag = self.get_list()["ETag"]

        with CaptureQueriesContext(connection) as notmodified:
            assert self.get_list(etag=etag).status_code == 304
        with CaptureQueriesContext(connection) as full:
            assert self.get_list().status_code == 200

        assert len(notmodified) < len(full), (len(notmodified), len(full))

    def test_detail_view_also_supports_conditional_requests(self) -> None:
        task = Task.objects.create(user=self.user, ra=1.0, dec=2.0)
        response = self.client.get(reverse("task-detail", args=[task.id]), HTTP_ACCEPT="application/json")
        assert response.status_code == 200
        etag = response["ETag"]

        conditional = self.client.get(
            reverse("task-detail", args=[task.id]), HTTP_ACCEPT="application/json", HTTP_IF_NONE_MATCH=etag
        )
        assert conditional.status_code == 304


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


class PermissionResponseTests(TestCase):
    """A refused API request must say so, rather than being redirected to the login page.

    Every 401/403 used to become a 302. jQuery follows redirects, so the queue page received the
    login page with HTTP 200 and ran its *success* handler: a delete that the server had refused was
    reported to the user as having worked.
    """

    def setUp(self) -> None:
        self.owner = User.objects.create_user(username="permowner", email="po@example.com", password=None)
        self.other = User.objects.create_user(username="permother", email="pt@example.com", password=None)
        self.task = Task.objects.create(user=self.owner, ra=1.0, dec=2.0, comment="not yours")

    def test_cross_user_delete_is_refused_with_403(self) -> None:
        self.client.force_login(self.other)

        response = self.client.delete(reverse("task-detail", args=[self.task.id]), HTTP_ACCEPT="application/json")

        assert response.status_code == 403, response.status_code
        assert Task.objects.filter(id=self.task.id).exists()

    def test_cross_user_patch_is_refused_with_403(self) -> None:
        self.client.force_login(self.other)

        response = self.client.patch(
            reverse("task-detail", args=[self.task.id]),
            data=json.dumps({"comment": "hijacked"}),
            content_type="application/json",
            HTTP_ACCEPT="application/json",
        )

        assert response.status_code == 403, response.status_code
        self.task.refresh_from_db()
        assert self.task.comment == "not yours"

    def test_an_unauthenticated_browser_is_still_sent_to_the_login_page(self) -> None:
        response = self.client.get(reverse("task-list"), HTTP_ACCEPT="text/html")

        assert response.status_code == 302, response.status_code
        assert reverse("rest_framework:login") in response["Location"]

    def test_the_next_url_is_url_quoted_not_html_escaped(self) -> None:
        # escape() turns an "&" in the query string into "&amp;", which would send the user
        # somewhere other than where they asked to go
        response = self.client.get(f"{reverse('task-list')}?started=true&pagesize=5", HTTP_ACCEPT="text/html")

        assert response.status_code == 302, response.status_code
        assert "&amp;" not in response["Location"], response["Location"]
        nexturl = urllib.parse.unquote(
            urllib.parse.parse_qs(urllib.parse.urlparse(response["Location"]).query)["next"][0]
        )
        assert nexturl == f"{reverse('task-list')}?started=true&pagesize=5", nexturl

    def test_an_anonymous_api_request_is_not_redirected(self) -> None:
        response = self.client.get(reverse("task-list"), HTTP_ACCEPT="application/json")

        assert response.status_code == 403, response.status_code

    def test_the_queue_page_reloads_itself_when_its_session_has_gone(self) -> None:
        # the poll asks for JSON, so it now gets a real 403 instead of the redirect it used to
        # follow. Nothing navigates on the page's behalf any more, so the polling code has to
        # recognise the status itself or the page sits showing pre-logout tasks forever.
        frontendpath = Path(settings.STATIC_ROOT, "js", "queuepage", "src", "tasklist.jsx").read_text()
        pollbody = frontendpath.split("fetchData(usertriggered)")[1]

        assert "response.status == 401 || response.status == 403" in pollbody, (
            "fetchData must handle an expired session; a 401/403 is no longer a redirect"
        )
        assert "window.location.reload()" in pollbody


class FromApiDetectionTests(TestCase):
    """from_api decides whether a result email is sent, so it must not depend on the Referer header.

    A browser is free not to send one (a no-referrer policy, a privacy extension), and such a
    submission used to be filed as an API request: the user ticked "Email me when completed" and
    never heard anything back.
    """

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="apidetect", email="ad@example.com", password="testpassword123")

    def test_a_session_submission_without_a_referer_is_not_from_api(self) -> None:
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("task-list"),
            data=json.dumps({"ra": 1.0, "dec": 2.0}),
            content_type="application/json",
            HTTP_ACCEPT="application/json",
        )

        assert response.status_code == 201, response.content
        assert Task.objects.get(id=response.json()["id"]).from_api is False

    def test_a_token_submission_is_from_api(self) -> None:
        from rest_framework.authtoken.models import Token

        token = Token.objects.create(user=self.user)

        response = self.client.post(
            reverse("task-list"),
            data=json.dumps({"ra": 1.0, "dec": 2.0}),
            content_type="application/json",
            HTTP_ACCEPT="application/json",
            HTTP_AUTHORIZATION=f"Token {token.key}",
            # a script can send a Referer too, so its presence must not be what decides this
            HTTP_REFERER="https://fallingstar-data.com/forcedphot/queue/",
        )

        assert response.status_code == 201, response.content
        assert Task.objects.get(id=response.json()["id"]).from_api is True


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
        # from_api selects the retention sweep (31 days for API tasks against 183), so an image
        # request must stay a web request however its parent was submitted
        assert imagerequest.from_api is False
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

    def test_a_browser_form_post_renders_a_working_queue_page(self) -> None:
        # a plain (non-JS) form POST receives the queue page via the 201 response, a render path
        # that supplies none of the view context. The URL globals are built inside the template
        # from {% url %} for exactly this reason: when they came from view context, this page
        # polled fetch('') against itself and setFilter threw on new URL('').
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("task-list"),
            data={"ra": "1.0", "dec": "2.0"},
            HTTP_ACCEPT="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        )

        assert response.status_code == 201, response.status_code
        content = response.content.decode()
        assert "const api_url_base = 'http://testserver/queue/'" in content, content[:500]
        assert "const queuepositions_url = 'http://testserver/queuepositions.json'" in content
        assert "const taskrunnerstatus_url = 'http://testserver/taskrunnerstatus.json'" in content

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


@override_settings(DEBUG=False)
class CallbackUrlValidationTests(TestCase):
    """The callback URL is user-supplied and the server fetches it, so this is an SSRF boundary.

    DEBUG is forced off because the validator deliberately relaxes both the scheme and the address
    checks in development, where the callback target is usually on the developer's own machine.
    """

    def assert_rejected(self, url: str) -> None:
        try:
            validate_callback_url(url)
        except CallbackUrlError:
            return
        msg = f"callback_url should have been rejected: {url!r}"
        raise AssertionError(msg)

    def test_public_https_url_is_accepted(self) -> None:
        with mock.patch("socket.getaddrinfo", return_value=[(socket.AF_INET, None, None, "", ("93.184.216.34", 443))]):
            assert validate_callback_url("https://example.com/hook") == "https://example.com/hook"

    def test_plain_http_is_rejected(self) -> None:
        self.assert_rejected("http://example.com/hook")

    def test_non_http_schemes_are_rejected(self) -> None:
        for url in ("file:///etc/passwd", "ftp://example.com/x", "gopher://example.com/", "javascript:alert(1)"):
            self.assert_rejected(url)

    def test_embedded_credentials_are_rejected(self) -> None:
        self.assert_rejected("https://user:password@example.com/hook")

    def test_loopback_is_rejected(self) -> None:
        for host in ("localhost", "127.0.0.1", "[::1]"):
            self.assert_rejected(f"https://{host}/hook")

    def test_private_addresses_are_rejected(self) -> None:
        # the classic SSRF targets: internal services and the cloud instance metadata endpoint
        for address in ("10.0.0.5", "192.168.1.1", "172.16.0.1", "169.254.169.254"):
            self.assert_rejected(f"https://{address}/hook")

    def test_a_public_name_resolving_to_a_private_address_is_rejected(self) -> None:
        # the check is on the resolved address, not on the name
        with mock.patch(
            "socket.getaddrinfo", return_value=[(socket.AF_INET, None, None, "", ("169.254.169.254", 443))]
        ):
            self.assert_rejected("https://sneaky.example.com/hook")

    def test_unresolvable_host_is_rejected(self) -> None:
        with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror("nope")):
            self.assert_rejected("https://does-not-exist.example/hook")

    def test_overlong_url_is_rejected(self) -> None:
        self.assert_rejected("https://example.com/" + "a" * 600)

    def test_empty_url_is_rejected(self) -> None:
        self.assert_rejected("")

    def test_the_api_rejects_a_bad_callback_url(self) -> None:
        user = User.objects.create_user(username="cb", email="cb@example.com", password=None)
        self.client.force_login(user)

        response = self.client.post(
            reverse("task-list"),
            data=json.dumps({"ra": 1.0, "dec": 2.0, "callback_url": "http://169.254.169.254/latest/meta-data/"}),
            content_type="application/json",
            HTTP_ACCEPT="application/json",
        )

        assert response.status_code == 400, response.content
        assert "callback_url" in response.json(), response.content
        assert not Task.objects.filter(user_id=user.pk).exists()


class CallbackSendingTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="cbsend", email="cbs@example.com", password=None)

    def make_task(self, **kwargs):
        return Task.objects.create(
            user=self.user, ra=1.0, dec=2.0, finishtimestamp=timezone.now(), from_api=True, **kwargs
        )

    def test_no_callback_url_sends_nothing(self) -> None:
        with mock.patch("urllib.request.OpenerDirector.open") as opener:
            assert send_task_callback(task=self.make_task(), logfunc=lambda _msg: None) is False
        opener.assert_not_called()

    def test_successful_callback_posts_the_task_result(self) -> None:
        task = self.make_task(callback_url="https://example.com/hook")
        response = mock.MagicMock()
        response.status = 200
        response.__enter__.return_value = response

        with (
            mock.patch("socket.getaddrinfo", return_value=[(socket.AF_INET, None, None, "", ("93.184.216.34", 443))]),
            mock.patch("urllib.request.OpenerDirector.open", return_value=response) as opener,
        ):
            assert send_task_callback(task=task, logfunc=lambda _msg: None) is True

        request = opener.call_args.args[0]
        assert request.method == "POST"
        assert request.full_url == "https://example.com/hook"
        payload = json.loads(request.data)
        assert payload["task_id"] == task.id
        assert payload["success"] is True

    def test_a_failing_endpoint_does_not_raise(self) -> None:
        # the task is already finished and recorded by the time this runs, so a callback failure
        # must never propagate and take the worker down with it
        task = self.make_task(callback_url="https://example.com/hook")
        logged: list[str] = []

        with (
            mock.patch("socket.getaddrinfo", return_value=[(socket.AF_INET, None, None, "", ("93.184.216.34", 443))]),
            mock.patch("urllib.request.OpenerDirector.open", side_effect=urllib.error.URLError("refused")),
        ):
            assert send_task_callback(task=task, logfunc=logged.append) is False

        assert any("failed" in line for line in logged), logged

    def test_a_url_that_became_private_is_not_fetched(self) -> None:
        # the URL passed validation at submission time, but DNS can change afterwards
        task = self.make_task(callback_url="https://example.com/hook")
        logged: list[str] = []

        with (
            mock.patch("socket.getaddrinfo", return_value=[(socket.AF_INET, None, None, "", ("127.0.0.1", 443))]),
            mock.patch("urllib.request.OpenerDirector.open") as opener,
        ):
            assert send_task_callback(task=task, logfunc=logged.append) is False

        opener.assert_not_called()
        assert any("public address" in line for line in logged), logged

    def capture_callback_payload(self, task) -> dict:
        """Run notify_finished() against a stubbed endpoint and return the JSON it posted."""
        captured: dict = {}

        def fake_open(request, timeout=None):
            captured["payload"] = json.loads(request.data)
            response = mock.MagicMock()
            response.status = 200
            response.__enter__.return_value = response
            return response

        with (
            mock.patch("socket.getaddrinfo", return_value=[(socket.AF_INET, None, None, "", ("93.184.216.34", 443))]),
            mock.patch("urllib.request.OpenerDirector.open", side_effect=fake_open),
        ):
            taskrunner_main.notify_finished(task=task, logfunc=lambda _msg: None)

        return captured["payload"]

    def test_a_finished_task_reports_success_and_a_finish_time(self) -> None:
        # do_task() records the result with a queryset update, which leaves the in-memory instance
        # untouched. The callback is built from that instance, so until mark_finished() applied the
        # same values to it, every callback said finishtimestamp: null and success: true.
        task = Task.objects.create(
            user=self.user, ra=1.0, dec=2.0, from_api=True, callback_url="https://example.com/hook"
        )

        taskrunner_main.mark_finished(task=task, error_msg=None)
        payload = self.capture_callback_payload(task)

        assert payload["success"] is True
        assert payload["error_msg"] is None
        assert payload["finishtimestamp"] is not None
        task.refresh_from_db()
        assert task.finishtimestamp is not None
        assert task.queuepos_relative is None

    def test_a_failed_task_reports_the_failure(self) -> None:
        task = Task.objects.create(
            user=self.user, ra=1.0, dec=2.0, from_api=True, callback_url="https://example.com/hook"
        )

        taskrunner_main.mark_finished(task=task, error_msg="No data returned")
        payload = self.capture_callback_payload(task)

        assert payload["success"] is False, payload
        assert payload["error_msg"] == "No data returned"
        assert payload["finishtimestamp"] is not None
        assert Task.objects.get(id=task.id).error_msg == "No data returned"

    def test_notify_finished_still_emails_when_the_callback_fails(self) -> None:
        task = Task.objects.create(
            user=self.user,
            ra=1.0,
            dec=2.0,
            finishtimestamp=timezone.now(),
            from_api=False,
            send_email=True,
            callback_url="https://example.com/hook",
        )
        django_mail.outbox.clear()

        with mock.patch("atlasserver.taskrunner.main.send_task_callback", side_effect=RuntimeError("boom")):
            taskrunner_main.notify_finished(task=task, logfunc=lambda _msg: None)

        assert len(django_mail.outbox) == 1, "an exploding callback must not suppress the result email"


class QueuePositionsEndpointTests(TestCase):
    """The cheap endpoint the queue page can poll instead of re-fetching the whole task list."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="qp", email="qp@example.com", password=None)
        self.other = User.objects.create_user(username="qpother", email="qp2@example.com", password=None)

    def test_requires_login(self) -> None:
        response = self.client.get(reverse("queuepositions"))
        assert response.status_code == 302
        assert reverse("login") in response["Location"]

    def test_reports_positions_matching_the_task_list(self) -> None:
        # the other user's tasks are interleaved by the round robin, so this user's positions are
        # not simply 0..n and must agree with what the task list itself would report
        other_task = Task.objects.create(user=self.other, ra=1.0, dec=2.0)
        mine = [Task.objects.create(user=self.user, ra=1.0, dec=2.0) for _ in range(2)]
        calculate_queue_positions()
        self.client.force_login(self.user)

        data = self.client.get(reverse("queuepositions")).json()

        assert set(data["queuepositions"]) == {str(task.id) for task in mine}, data
        assert str(other_task.id) not in data["queuepositions"], "another user's task must not be reported"
        for task in mine:
            task.refresh_from_db()  # the positions were assigned after these instances were built
            assert data["queuepositions"][str(task.id)] == task.queuepos

    def test_finished_tasks_are_omitted(self) -> None:
        Task.objects.create(user=self.user, ra=1.0, dec=2.0, finishtimestamp=timezone.now())
        queued = Task.objects.create(user=self.user, ra=1.0, dec=2.0)
        calculate_queue_positions()
        self.client.force_login(self.user)

        data = self.client.get(reverse("queuepositions")).json()

        assert list(data["queuepositions"]) == [str(queued.id)]

    def test_is_cheaper_than_the_task_list(self) -> None:
        for _ in range(6):
            Task.objects.create(user=self.user, ra=1.0, dec=2.0)
        calculate_queue_positions()
        self.client.force_login(self.user)

        with CaptureQueriesContext(connection) as positions:
            assert self.client.get(reverse("queuepositions")).status_code == 200
        with CaptureQueriesContext(connection) as tasklist:
            assert self.client.get(reverse("task-list"), HTTP_ACCEPT="application/json").status_code == 200

        assert len(positions) < len(tasklist), (len(positions), len(tasklist))


class TaskRunnerStatusTests(TestCase):
    """Both the runner and the view read STATUS_PATH through atlasserver.taskrunner.status.

    The view must not import atlasserver.taskrunner.main (which runs django.setup() and pulls in
    pandas), so patching the shared module is what makes one patch cover both sides.
    """

    def test_missing_status_file_reports_not_running(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            mock.patch.object(runnerstatus, "STATUS_PATH", Path(tmpdir, "nothing.json")),
        ):
            response = self.client.get(reverse("taskrunnerstatus"))

        assert response.status_code == 503
        assert response.json()["running"] is False

    def test_fresh_status_file_reports_running(self) -> None:
        user = User.objects.create_user(username="statuser", email="st@example.com", password=None)
        Task.objects.create(user=user, ra=1.0, dec=2.0)

        with tempfile.TemporaryDirectory() as tmpdir:
            statuspath = Path(tmpdir, "taskrunner_status.json")
            with mock.patch.object(runnerstatus, "STATUS_PATH", statuspath):
                taskrunner_main.write_status(procs_taskids={0: 42}, numslots=16)
                response = self.client.get(reverse("taskrunnerstatus"))

        assert response.status_code == 200, response.content
        body = response.json()
        assert body["running"] is True
        assert body["stale"] is False
        assert body["slots_busy"] == 1
        assert body["running_taskids"] == [42]
        assert body["numslots"] == 16
        assert body["maintenance"] is False
        assert body["queued_task_count"] == 1
        assert body["oldest_queued_task_time"] is not None

    def test_a_maintenance_snapshot_is_reported_as_such(self) -> None:
        # written by the sweep's heartbeat: the slot fields are frozen while the sweep blocks the
        # runner's loop, so the flag is what tells a reader not to trust them
        with tempfile.TemporaryDirectory() as tmpdir:
            statuspath = Path(tmpdir, "taskrunner_status.json")
            with mock.patch.object(runnerstatus, "STATUS_PATH", statuspath):
                taskrunner_main.write_status(procs_taskids={0: 42}, numslots=16, maintenance=True)
                response = self.client.get(reverse("taskrunnerstatus"))

        assert response.status_code == 200, response.content
        body = response.json()
        assert body["running"] is True
        assert body["maintenance"] is True

    def test_old_status_file_reports_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            statuspath = Path(tmpdir, "taskrunner_status.json")
            stale_written = timezone.now() - datetime.timedelta(hours=1)
            statuspath.write_text(json.dumps({"written": stale_written.isoformat(), "slots_busy": 0}))

            with mock.patch.object(runnerstatus, "STATUS_PATH", statuspath):
                response = self.client.get(reverse("taskrunnerstatus"))

        assert response.status_code == 503
        assert response.json()["stale"] is True

    def test_unusable_status_files_report_stale_rather_than_raising(self) -> None:
        # this endpoint exists to say that the runner is in trouble, so it must not be the thing
        # that raises. Every one of these used to be a 500 (and an email to the admins).
        unusable = {
            "no written key": json.dumps({"pid": 1, "slots_busy": 0}),
            "naive written": json.dumps({"written": "2026-07-31T10:00:00"}),
            "unparseable written": json.dumps({"written": "the other day"}),
            "not an object": json.dumps([1, 2, 3]),
            "not json at all": "half a fi",
        }

        for description, contents in unusable.items():
            with tempfile.TemporaryDirectory() as tmpdir:
                statuspath = Path(tmpdir, "taskrunner_status.json")
                statuspath.write_text(contents)

                with mock.patch.object(runnerstatus, "STATUS_PATH", statuspath):
                    response = self.client.get(reverse("taskrunnerstatus"))

            assert response.status_code == 503, f"{description}: {response.content!r}"
            assert response.json()["stale"] is True, description
            assert response.json()["running"] is False, description


class OpenApiSchemaTests(TestCase):
    def test_schema_generates_without_errors(self) -> None:
        # the schema is derived from the serializer, so this is what stops the published API
        # documentation from drifting away from the code
        response = self.client.get(reverse("schema"))

        assert response.status_code == 200, response.content

    def test_schema_describes_the_queue_endpoint(self) -> None:
        import yaml

        schema = yaml.safe_load(self.client.get(reverse("schema")).content)

        assert "/queue/" in schema["paths"], sorted(schema["paths"])
        properties = schema["components"]["schemas"]["ForcePhotTask"]["properties"]
        for field in ("ra", "dec", "mjd_min", "mjd_max", "callback_url"):
            assert field in properties, sorted(properties)


class AtlasCommandTests(TestCase):
    """The remote shell command is assembled by string concatenation and run over ssh.

    Nothing else checks it, and a mistake here is only visible in production, so the pieces that
    have needed a comment to get right are pinned: the 0-valued MJD bound that truthiness used to
    drop, and the SSOSTACK site list whose quoting decides whether the remote shell sees one
    argument or five.
    """

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="cmd", email="cmd@example.com", password=None)

    def make_task(self, **kwargs) -> Task:
        kwargs.setdefault("ra", 100.0)
        kwargs.setdefault("dec", -20.0)
        return Task.objects.create(user=self.user, **kwargs)

    def fp_command(self, task) -> str:
        return taskrunner_main.build_fp_command(task, remoteresultfile=Path("~/atlasserver/results/job00001.txt"))

    def test_coordinates_are_passed_as_floats(self) -> None:
        command = self.fp_command(self.make_task(mjd_min=None))
        assert "/atlas/bin/force.sh 100.0 -20.0" in command
        assert "dodb=1 parallel=4" in command

    def test_mpc_name_uses_the_solar_system_script(self) -> None:
        command = self.fp_command(self.make_task(ra=None, dec=None, mpc_name="Makemake", mjd_min=None))
        assert "/atlas/bin/ssforce.sh 'Makemake'" in command
        assert "force.sh" not in command.replace("ssforce.sh", "")

    def test_zero_mjd_min_is_still_passed(self) -> None:
        # a falsy-but-present bound used to be dropped, silently widening the request
        assert " m0=0.0" in self.fp_command(self.make_task(mjd_min=0))

    def test_absent_mjd_bounds_are_omitted(self) -> None:
        command = self.fp_command(self.make_task(mjd_min=None, mjd_max=None))
        assert " m0=" not in command
        assert " m1=" not in command

    def test_reduced_images_set_both_the_flag_and_the_image_mode(self) -> None:
        command = self.fp_command(self.make_task(use_reduced=True, mjd_min=None))
        assert " red=1" in command
        assert command.endswith(" red")

    def test_difference_images_are_the_default(self) -> None:
        command = self.fp_command(self.make_task(mjd_min=None))
        assert " red=1" not in command
        assert command.endswith(" diff")

    def test_proper_motion_is_only_sent_when_set(self) -> None:
        plain = self.fp_command(self.make_task(mjd_min=None))
        assert "pmra=" not in plain
        assert "epoch=" not in plain

        moving = self.fp_command(
            self.make_task(mjd_min=None, radec_epoch_year=2015.5, propermotion_ra=12.5, propermotion_dec=-3.25)
        )
        assert " epoch=2015.5" in moving
        assert " pmra=12.5" in moving
        assert " pmdec=-3.25" in moving

    def test_tdo_is_only_added_for_test_users(self) -> None:
        task = self.make_task(mjd_min=None)
        assert " tdo=1" not in self.fp_command(task)

        # the task runner imports the settings module directly rather than django.conf.settings,
        # so override_settings does not reach it (as in TaskRunnerResultFileTests)
        with mock.patch.object(taskrunner_main.settings, "TEST_USERS", [self.user.pk]):
            assert " tdo=1" in self.fp_command(task)

    def ssostack_command(self, task) -> str:
        return taskrunner_main.build_ssostack_command(
            task,
            remoteresultfile=Path("~/atlasserver/results/job00001.fits"),
            remotedatafile=Path("~/atlasserver/results/job00001.txt"),
            remotetaskdir=Path("~/atlasserver/results/task00001"),
        )

    def test_ssostack_mjds_are_integers(self) -> None:
        # stack_rock.sh does not accept floating point MJDs
        task = self.make_task(ra=None, dec=None, mpc_name="Makemake", mjd_min=59000.4, mjd_max=59100.6)
        command = self.ssostack_command(task)
        assert "stack_rock.sh 'Makemake' 59000 59101" in command, command

    def test_ssostack_site_list_is_a_single_shell_word(self) -> None:
        # the whole command is handed to one remote shell, so escaped quotes would leave literal
        # characters behind and split the site list into five separate arguments
        task = self.make_task(ra=None, dec=None, mpc_name="Makemake", mjd_min=59000.0, mjd_max=59100.0)
        with mock.patch.object(taskrunner_main.settings, "TEST_USERS", [self.user.pk]):
            command = self.ssostack_command(task)

        assert " sites='hko mlo chl sth tdo'" in command, command
        assert "\\'" not in command, command

    def test_ssostack_cleans_up_the_remote_directory(self) -> None:
        task = self.make_task(ra=None, dec=None, mpc_name="Makemake", mjd_min=59000.0, mjd_max=59100.0)
        command = self.ssostack_command(task)
        assert command.endswith(" rm -rf ~/atlasserver/results/task00001"), command

    def test_result_filename_depends_on_the_request_type(self) -> None:
        fptask = self.make_task()
        imgzip = Task.objects.create(user=self.user, ra=1.0, dec=2.0, request_type="IMGZIP", parent_task=fptask)
        ssostack = self.make_task(ra=None, dec=None, mpc_name="Makemake", request_type="SSOSTACK")

        assert taskrunner_main.remote_result_filename(fptask) == f"job{fptask.id:05d}.txt"
        # the zip belongs to the parent, because that is the task whose images these are
        assert taskrunner_main.remote_result_filename(imgzip) == f"job{fptask.id:05d}.zip"
        assert taskrunner_main.remote_result_filename(ssostack) == f"job{ssostack.id:05d}.fits"

    def test_unknown_request_type_has_no_result_file(self) -> None:
        task = self.make_task()
        task.request_type = "NONSENSE"
        assert taskrunner_main.remote_result_filename(task) is None


class RunRsyncTests(TestCase):
    def test_timeout_kills_the_process_and_reports_no_exit_code(self) -> None:
        # without a timeout an rsync against a wedged host holds the worker slot forever
        proc = mock.MagicMock()
        proc.communicate.side_effect = subprocess.TimeoutExpired(cmd="rsync", timeout=1)
        logged: list[str] = []

        with mock.patch("subprocess.Popen", return_value=proc):
            result = taskrunner_main.run_rsync(["rsync", "a", "b"], logged.append)

        assert result is None
        proc.kill.assert_called_once()
        proc.wait.assert_called_once()  # reaped, so no zombie is left behind
        assert any("timed out" in line for line in logged), logged

    def test_nonzero_exit_is_reported(self) -> None:
        proc = mock.MagicMock()
        proc.communicate.return_value = ("", "rsync: connection unexpectedly closed")
        proc.returncode = 12
        logged: list[str] = []

        with mock.patch("subprocess.Popen", return_value=proc):
            result = taskrunner_main.run_rsync(["rsync", "a", "b"], logged.append)

        assert result == 12
        assert any("exited with code 12" in line for line in logged), logged


class RemoveOldTasksTests(TestCase):
    """The hourly maintenance sweep, which now writes once per batch rather than once per task."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="sweeper", email="sw@example.com", password=None)

    def make_old_task(self, days: int, **kwargs) -> Task:
        return Task.objects.create(
            user=self.user,
            ra=1.0,
            dec=2.0,
            finishtimestamp=timezone.now() - datetime.timedelta(days=days),
            **kwargs,
        )

    def test_old_tasks_are_archived_and_recent_ones_are_left_alone(self) -> None:
        old = self.make_old_task(days=200)
        recent = self.make_old_task(days=1)

        taskrunner_main.remove_old_tasks(days_ago=183, request_type="FP", logfunc=lambda _msg: None)

        old.refresh_from_db()
        recent.refresh_from_db()
        assert old.is_archived
        assert not recent.is_archived

    def test_hard_delete_removes_the_rows(self) -> None:
        self.make_old_task(days=200, is_archived=True, from_api=True)
        keep = self.make_old_task(days=1, from_api=True)

        taskrunner_main.remove_old_tasks(
            days_ago=7, harddeleterecord=True, is_archived=True, from_api=True, logfunc=lambda _msg: None
        )

        assert list(Task.objects.values_list("id", flat=True)) == [keep.id]

    def test_unfinished_tasks_are_never_touched(self) -> None:
        queued = Task.objects.create(user=self.user, ra=1.0, dec=2.0)

        taskrunner_main.remove_old_tasks(days_ago=0, logfunc=lambda _msg: None)

        queued.refresh_from_db()
        assert not queued.is_archived

    def test_archiving_is_a_bounded_number_of_queries(self) -> None:
        # the old loop cost a query for the image request plus a full-row save() per task
        for _ in range(12):
            self.make_old_task(days=200)

        with CaptureQueriesContext(connection) as queries:
            taskrunner_main.remove_old_tasks(days_ago=183, request_type="FP", logfunc=lambda _msg: None)

        assert Task.objects.filter(is_archived=True).count() == 12
        # one batch: ids, load, prefetch, update. Well under one write per task either way.
        assert len(queries) <= 6, f"{len(queries)} queries:\n" + "\n".join(q["sql"] or "" for q in queries)

    def test_archiving_spans_several_batches(self) -> None:
        # the sweep takes one bounded batch at a time rather than reading every matching id into
        # memory, and relies on a processed task dropping out of the queryset so that the next
        # pass sees fresh rows. With the real batch size of 500 the loop only ever runs once.
        tasks = [self.make_old_task(days=200) for _ in range(10)]

        with mock.patch.object(taskrunner_main, "MAINTENANCE_BATCH_SIZE", 3):
            taskrunner_main.remove_old_tasks(days_ago=183, request_type="FP", logfunc=lambda _msg: None)

        for task in tasks:
            task.refresh_from_db()
            assert task.is_archived, f"task {task.id} was left behind by the batching"

    def test_hard_delete_spans_several_batches(self) -> None:
        for _ in range(10):
            self.make_old_task(days=200, is_archived=True, from_api=True)

        with mock.patch.object(taskrunner_main, "MAINTENANCE_BATCH_SIZE", 3):
            taskrunner_main.remove_old_tasks(
                days_ago=7, harddeleterecord=True, is_archived=True, from_api=True, logfunc=lambda _msg: None
            )

        assert not Task.objects.exists()

    def test_batching_does_not_loop_forever_if_a_task_stops_matching(self) -> None:
        # the pass limit is a safety net: if a write ever failed to take a row out of the queryset
        # the loop must give up and say so rather than spin
        for _ in range(6):
            self.make_old_task(days=200)
        logged: list[str] = []

        with (
            mock.patch.object(taskrunner_main, "MAINTENANCE_BATCH_SIZE", 2),
            # neutered, so no task ever leaves the queryset and every pass sees the same rows
            mock.patch.object(models.QuerySet, "update", return_value=0),
            mock.patch("atlasserver.forcephot.models.Task.delete_result_files"),
        ):
            taskrunner_main.remove_old_tasks(days_ago=183, request_type="FP", logfunc=logged.append)

        assert any("stopped after" in line for line in logged), logged

    def test_soft_delete_rejects_an_impossible_filter(self) -> None:
        try:
            taskrunner_main.remove_old_tasks(days_ago=1, harddeleterecord=False, is_archived=True)
        except ValueError:
            return
        msg = "archiving already-archived tasks is a no-op and must be rejected"
        raise AssertionError(msg)


class QueuePositionConcurrencyTests(TransactionTestCase):
    """calculate_queue_positions() holds a lock precisely to stop this from producing duplicates.

    TransactionTestCase rather than TestCase: the threads need to see each other's committed work,
    which the single shared transaction of a TestCase would prevent.
    """

    @skipUnless(connection.features.has_select_for_update, "select_for_update is not supported on this database")
    def test_concurrent_recalculations_do_not_produce_duplicate_positions(self) -> None:
        users = [
            User.objects.create_user(username=f"conc{i}", email=f"conc{i}@example.com", password=None) for i in range(4)
        ]
        for user in users:
            for _ in range(3):
                Task.objects.create(user=user, ra=1.0, dec=2.0)

        errors: list[BaseException] = []

        def recalculate() -> None:
            try:
                calculate_queue_positions()
            except BaseException as ex:
                errors.append(ex)
            finally:
                connection.close()  # each thread holds its own connection

        threads = [threading.Thread(target=recalculate) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert not errors, errors

        rawpositions = list(
            Task.objects.filter(finishtimestamp__isnull=True, is_archived=False).values_list(
                "queuepos_relative", flat=True
            )
        )
        assert None not in rawpositions, rawpositions

        positions = [pos for pos in rawpositions if pos is not None]
        assert len(set(positions)) == len(positions), f"duplicate queue positions: {sorted(positions)}"
        assert sorted(positions) == list(range(len(positions))), sorted(positions)


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
