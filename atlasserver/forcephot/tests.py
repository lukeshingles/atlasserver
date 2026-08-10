import contextlib
import datetime
import ipaddress
import itertools
import json
import re
import socket
import subprocess
import tempfile
import threading
import time
import typing as t
import urllib.error
import urllib.parse
from multiprocessing import Process
from pathlib import Path
from unittest import mock
from unittest import skipUnless

from django.conf import settings

# the project uses the default user model, and the concrete class is needed for typing
from django.contrib.auth.models import User  # pylint: disable=imported-auth-user
from django.core import mail as django_mail
from django.core.cache import caches
from django.db import connection
from django.db import IntegrityError
from django.db import models
from django.test import override_settings
from django.test import TestCase
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.serializers import ValidationError

from atlasserver.forcephot import misc
from atlasserver.forcephot import queue as taskqueue
from atlasserver.forcephot import verification
from atlasserver.forcephot import views
from atlasserver.forcephot.misc import splitradeclist
from atlasserver.forcephot.models import Task
from atlasserver.forcephot.queue import calculate_queue_positions
from atlasserver.forcephot.serializers import ForcePhotTaskSerializer
from atlasserver.forcephot.serializers import is_finite_float
from atlasserver.forcephot.throttles import ForcedPhotRateThrottle
from atlasserver.forcephot.views import geoip_reader
from atlasserver.forcephot.views import geoip_reader_forget
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
        # the confirmation send is rate limited per account, in a locmem cache that outlives the
        # database rollback between tests -- and ids get reused, so one test's send would otherwise
        # suppress the next one's
        caches["throttle"].clear()

    def test_requires_login(self) -> None:
        response = self.client.get(reverse("email_change"))
        assert response.status_code == 302
        assert reverse("login") in response["Location"]

    def request_email_change(self, new_email: str = "new@example.com") -> t.Any:
        return self.client.post(reverse("email_change"), {"password": "testpassword123", "new_email": new_email})

    def confirmation_link(self, index: int = 0) -> str:
        """Return the confirmation URL from a sent email, like RegistrationVerificationTests.verification_link."""
        match = re.search(r"https?://\S+/emailchange/confirm/\S+", str(django_mail.outbox[index].body))
        assert match is not None, django_mail.outbox[index].body
        return match.group(0)

    def test_change_email_requires_confirmation_at_the_new_address(self) -> None:
        # verifying at registration would be pointless if the address could then be changed to an
        # unproved one, so nothing is written until the emailed link is followed
        self.client.force_login(self.user)

        response = self.request_email_change()

        assert response.status_code == 200
        self.user.refresh_from_db()
        assert self.user.email == "old@example.com", "the address changed before it was confirmed"
        assert len(django_mail.outbox) == 1
        assert django_mail.outbox[0].to == ["new@example.com"], "the link must go to the new address"

    def test_the_confirmation_send_is_rate_limited(self) -> None:
        """Nothing is stored until the link is followed, so nothing else makes a repeat a no-op.

        Without a limit one logged-in account can post this form in a loop and have the server mail
        an arbitrary address as fast as it will go.
        """
        self.client.force_login(self.user)

        for _ in range(3):
            self.request_email_change("victim@example.com")

        assert len(django_mail.outbox) == 1, len(django_mail.outbox)

    def test_the_limit_is_per_account_not_per_target_address(self) -> None:
        # otherwise varying the address each time buys another send, which is the flooding case
        self.client.force_login(self.user)

        self.request_email_change("first@example.com")
        response = self.request_email_change("second@example.com")

        assert len(django_mail.outbox) == 1, len(django_mail.outbox)
        assert "wait a minute" in response.content.decode().lower()

    def test_following_the_confirmation_link_applies_the_change(self) -> None:
        self.client.force_login(self.user)
        self.request_email_change()

        response = self.client.get(self.confirmation_link())

        assert response.status_code == 200, response.status_code
        self.user.refresh_from_db()
        assert self.user.email == "new@example.com"

    def test_a_tampered_confirmation_token_is_rejected(self) -> None:
        self.client.force_login(self.user)
        self.request_email_change()

        response = self.client.get(reverse("email_change_confirm", kwargs={"token": "not-a-real-token"}))

        assert response.status_code == 400, response.status_code
        self.user.refresh_from_db()
        assert self.user.email == "old@example.com"

    def test_a_confirmation_link_cannot_be_replayed(self) -> None:
        """A link is good for one change, or an old one can undo a later one.

        It is delivered to a mailbox and scanners and forwarding rules keep copies, so anyone
        holding it could otherwise point result mail and password resets back at that address for
        as long as the signature stayed fresh.
        """
        self.client.force_login(self.user)
        self.request_email_change("first@example.com")
        firstlink = self.confirmation_link()
        assert self.client.get(firstlink).status_code == 200
        django_mail.outbox.clear()

        # move on to a third address, then replay the first link. The limiter is cleared because
        # this test is about token replay, not about the send rate -- two sends is the whole setup.
        caches["throttle"].clear()
        self.request_email_change("second@example.com")
        self.client.get(self.confirmation_link())

        replayed = self.client.get(firstlink)

        assert replayed.status_code == 400, replayed.status_code
        self.user.refresh_from_db()
        assert self.user.email == "second@example.com", self.user.email

    def test_an_address_taken_between_request_and_confirmation_is_refused(self) -> None:
        # the database index would otherwise turn this into a 500 rather than something actionable
        self.client.force_login(self.user)
        self.request_email_change()
        link = self.confirmation_link()
        User.objects.create_user(username="sniper", email="new@example.com", password=None)

        response = self.client.get(link)

        assert response.status_code == 400, response.status_code
        self.user.refresh_from_db()
        assert self.user.email == "old@example.com"

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
        # formatting None with a float format spec used to raise TypeError. Such a row can no
        # longer be saved -- task_target_is_mpcname_or_radec rejects it -- so this builds the
        # instance in memory, with an id because __str__ formats that too. The branch is kept
        # rather than deleted along with the possibility: it costs nothing, and it is what stops a
        # targetless row from being unprintable (and so unfixable in the admin) if the constraint
        # is ever dropped, or if one predates it.
        user = User.objects.create_user(username="nocoords", email="n@example.com", password=None)
        assert "RA Dec" in str(Task(id=1, user=user))

    def test_a_targetless_task_cannot_be_saved(self) -> None:
        # the serializer rejected these, but it was the only thing that did, so the admin and the
        # shell could both create a task the runner would dispatch a job for with nothing to point at
        user = User.objects.create_user(username="notarget", email="nt@example.com", password=None)

        try:
            Task.objects.create(user=user)
        except IntegrityError:
            return
        msg = "a task with neither an mpc_name nor coordinates was accepted"
        raise AssertionError(msg)

    def test_a_whitespace_only_mpc_name_is_not_a_target(self) -> None:
        # it is truthy, so it satisfied a bare != "" and then reached the runner, which would have
        # asked ssforce.sh for an object named nothing
        user = User.objects.create_user(username="blanktarget", email="bt@example.com", password=None)

        try:
            Task.objects.create(user=user, mpc_name="   ")
        except IntegrityError:
            return
        msg = "a task whose only target was whitespace was accepted"
        raise AssertionError(msg)


class TaskDeleteFileTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="deleter", email="d@example.com", password=None)

    def make_finished_task(self, staticroot: Path, **kwargs: t.Any) -> Task:
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

    def test_a_refused_browser_action_gets_a_real_page_not_a_bare_403_string(self) -> None:
        # without a 403.html template, DRF's TemplateHTMLRenderer falls back to
        # Template('403 Forbidden'): one line of text, no navigation, no way back to the queue
        self.client.force_login(self.other)

        response = self.client.delete(reverse("task-detail", args=[self.task.id]), HTTP_ACCEPT="text/html")

        assert response.status_code == 403, response.status_code
        content = response.content.decode()
        assert "<html" in content.lower(), content[:500]
        assert reverse("task-list") in content, content[:500]

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
        # matched on the name rather than on the exact declaration, which this has now pinned twice:
        # once on the parameter list (a second parameter broke it) and once on it being a class
        # method (converting the component to hooks broke it). Both failed with a message about the
        # match rather than about session handling.
        decl = re.search(r"^\s*(?:const\s+)?fetchData\b", frontendpath, re.MULTILINE)
        assert decl is not None, "could not find the fetchData declaration"
        pollbody = frontendpath[decl.end() :]

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


# the header line of a forced photometry result file. Module level because two unrelated test
# classes build result files from it, and reaching across for another class's attribute made the
# coupling invisible to a reader of either one.
RESULTFILE_HEADER = "###MJD m dm uJy duJy F err chi/N RA Dec x y maj min phi apfit mag5sig Sky Obs"


class ClientLocationTests(TestCase):
    """X-Forwarded-For is written by whoever sends the request unless a proxy in front maintains it.

    Nothing in this deployment does (httpconf.txt sets X-Forwarded-Proto and not this), so reading
    the leftmost entry of it meant the client chose its own country_code — and, because the string
    was handed to GeoIP2 unparsed, a *name* there was passed to socket.gethostbyname(): an
    arbitrary DNS lookup made by the server, with no timeout, on a name the caller picked. A value
    that resolved to nothing raised socket.gaierror out of task creation as a 500 and an admin
    email.
    """

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="located", email="l@example.com", password=None)
        self.client.force_login(self.user)

    @staticmethod
    def fake_reader():
        reader = mock.Mock()
        reader.city.return_value = {"country_code": "ZZ", "region_code": "QQ"}
        return reader

    def submit(self, **extra):
        return self.client.post(
            reverse("task-list"),
            data=json.dumps({"ra": 1.0, "dec": 2.0}),
            content_type="application/json",
            HTTP_ACCEPT="application/json",
            **extra,
        )

    def created_task(self, response: t.Any) -> Task:
        assert response.status_code == 201, response.content
        return Task.objects.get(id=response.json()["id"])

    def test_x_forwarded_for_is_ignored_without_a_trusted_proxy(self) -> None:
        # pinned rather than left to the environment: TRUSTED_PROXY_COUNT follows
        # ATLASSERVER_TRUSTED_PROXY_COUNT, which load_dotenv applies from a developer's .env, and
        # this is the test of the secure default
        with (
            override_settings(TRUSTED_PROXY_COUNT=0),
            mock.patch("atlasserver.forcephot.views.geoip_reader") as reader,
        ):
            task = self.created_task(self.submit(HTTP_X_FORWARDED_FOR="8.8.8.8"))

        # REMOTE_ADDR is 127.0.0.1 for the test client, so there is nothing to look up
        assert reader.call_count == 0
        assert task.country_code is None, task.country_code

    def test_a_hostname_in_x_forwarded_for_is_never_resolved(self) -> None:
        # asserted with the header trusted, which is the configuration that would reach a lookup
        # at all: the name must be rejected by parsing, not merely by being ignored
        with override_settings(TRUSTED_PROXY_COUNT=1), mock.patch("socket.gethostbyname") as resolve:
            response = self.submit(HTTP_X_FORWARDED_FOR="attacker-controlled.example.com")

        assert response.status_code == 201, response.content
        assert resolve.call_count == 0, resolve.call_args_list

    def test_an_unparseable_address_does_not_fail_the_submission(self) -> None:
        # this used to escape as socket.gaierror, i.e. an HTTP 500 for a valid task request
        with override_settings(TRUSTED_PROXY_COUNT=1), mock.patch("socket.gethostbyname") as resolve:
            task = self.created_task(self.submit(HTTP_X_FORWARDED_FOR="not an ip"))

        assert resolve.call_count == 0, resolve.call_args_list
        assert task.country_code is None, task.country_code

    def test_a_trusted_proxy_hop_is_used_when_one_is_configured(self) -> None:
        reader = self.fake_reader()
        with (
            override_settings(TRUSTED_PROXY_COUNT=1),
            mock.patch("atlasserver.forcephot.views.geoip_reader", return_value=reader),
        ):
            task = self.created_task(self.submit(HTTP_X_FORWARDED_FOR="8.8.8.8"))

        # the parsed address, not a string: GeoIP2.city() accepts one, and passing it keeps the
        # gethostbyname fallback inside GeoIP2._query structurally unreachable
        assert reader.city.call_args.args == (ipaddress.ip_address("8.8.8.8"),), reader.city.call_args
        assert task.country_code == "ZZ"
        assert task.region == "QQ"

    def test_only_the_hop_the_trusted_proxy_saw_is_read(self) -> None:
        # the client prepended its own entry; with one trusted proxy, only the last one counts
        reader = self.fake_reader()
        with (
            override_settings(TRUSTED_PROXY_COUNT=1),
            mock.patch("atlasserver.forcephot.views.geoip_reader", return_value=reader),
        ):
            self.created_task(self.submit(HTTP_X_FORWARDED_FOR="1.1.1.1, 8.8.8.8"))

        assert reader.city.call_args.args == (ipaddress.ip_address("8.8.8.8"),), reader.city.call_args

    def test_a_bracketed_ipv6_hop_is_understood(self) -> None:
        # nginx and several load balancers write an IPv6 address with a port this way, and
        # ipaddress.ip_address() rejects the brackets
        reader = self.fake_reader()
        with (
            override_settings(TRUSTED_PROXY_COUNT=1),
            mock.patch("atlasserver.forcephot.views.geoip_reader", return_value=reader),
        ):
            task = self.created_task(self.submit(HTTP_X_FORWARDED_FOR="[2001:4860:4860::8888]:443"))

        assert reader.city.call_args.args == (ipaddress.ip_address("2001:4860:4860::8888"),), reader.city.call_args
        assert task.country_code == "ZZ"

    def test_a_chain_shorter_than_the_proxy_count_is_not_trusted(self) -> None:
        # two proxies are supposed to have appended, so a single entry is a forged header
        with override_settings(TRUSTED_PROXY_COUNT=2), mock.patch("atlasserver.forcephot.views.geoip_reader") as reader:
            task = self.created_task(self.submit(HTTP_X_FORWARDED_FOR="8.8.8.8"))

        assert reader.call_count == 0
        assert task.country_code is None, task.country_code

    def test_private_and_loopback_addresses_are_not_looked_up(self) -> None:
        for address in ("127.0.0.1", "10.1.2.3", "192.168.0.7", "172.16.0.1", "::1", "fd00::1"):
            with (
                self.subTest(address=address),
                override_settings(TRUSTED_PROXY_COUNT=1),
                mock.patch("atlasserver.forcephot.views.geoip_reader") as reader,
            ):
                self.created_task(self.submit(HTTP_X_FORWARDED_FOR=address))
                assert reader.call_count == 0, address

    def test_an_address_the_database_does_not_list_is_handled_quietly(self) -> None:
        # a routine outcome, not a fault: logging it would put a line in the log for a share of
        # perfectly ordinary submissions
        from geoip2.errors import AddressNotFoundError

        reader = mock.Mock()
        reader.city.side_effect = AddressNotFoundError("no such address")
        with (
            override_settings(TRUSTED_PROXY_COUNT=1),
            mock.patch("atlasserver.forcephot.views.geoip_reader", return_value=reader),
            self.assertNoLogs("atlasserver.forcephot.views"),
        ):
            task = self.created_task(self.submit(HTTP_X_FORWARDED_FOR="8.8.8.8"))

        assert task.country_code is None, task.country_code

    def test_an_unusable_database_is_reported_rather_than_swallowed(self) -> None:
        from maxminddb import InvalidDatabaseError

        reader = mock.Mock()
        reader.city.side_effect = InvalidDatabaseError("corrupt")
        with (
            override_settings(TRUSTED_PROXY_COUNT=1),
            mock.patch("atlasserver.forcephot.views.geoip_reader", return_value=reader),
            self.assertLogs("atlasserver.forcephot.views", level="ERROR"),
        ):
            task = self.created_task(self.submit(HTTP_X_FORWARDED_FOR="8.8.8.8"))

        assert task.country_code is None, task.country_code

    def test_a_missing_database_does_not_fail_the_submission(self) -> None:
        # geoip_reader() stats the file, so a deleted database surfaces as FileNotFoundError
        geoip_reader_forget()
        self.addCleanup(geoip_reader_forget)
        with (
            override_settings(TRUSTED_PROXY_COUNT=1, GEOIP_PATH=tempfile.gettempdir()),
            self.assertLogs("atlasserver.forcephot.views", level="ERROR"),
        ):
            task = self.created_task(self.submit(HTTP_X_FORWARDED_FOR="8.8.8.8"))

        assert task.country_code is None, task.country_code

    def test_the_reader_is_kept_but_rebuilt_when_the_database_changes(self) -> None:
        # constructing one opens and memory-maps the database, which was being paid again on every
        # request that created a task — but keeping it unconditionally means the monthly
        # update_geoipdatabase.sh replacement is never picked up by a running worker
        geoip_reader_forget()
        self.addCleanup(geoip_reader_forget)
        with tempfile.TemporaryDirectory() as tmpdir:
            dbpath = Path(tmpdir, "GeoLite2-City.mmdb")
            dbpath.write_bytes(b"first database")
            with (
                override_settings(GEOIP_PATH=tmpdir),
                mock.patch("atlasserver.forcephot.views.GeoIP2") as geoip2class,
            ):
                geoip_reader()
                geoip_reader()
                assert geoip2class.call_count == 1, geoip2class.call_count

                dbpath.write_bytes(b"a replacement database of a different size")
                geoip_reader()
                assert geoip2class.call_count == 2, geoip2class.call_count


class TaskDetailIsPublicTests(TestCase):
    """A single task is readable by anyone, including anonymously, and is meant to be.

    Why is explained on ForcePhotPermission.has_object_permission. Pinned by a test because the
    task *list* is scoped to the requesting user and cross-user writes are refused, which together
    make the public detail view look like an oversight when it is not.
    """

    def setUp(self) -> None:
        self.owner = User.objects.create_user(username="detailowner", email="do@example.com", password=None)
        self.other = User.objects.create_user(username="detailother", email="dt@example.com", password=None)
        self.staff = User.objects.create_user(
            username="detailstaff", email="ds@example.com", password=None, is_staff=True
        )
        self.task = Task.objects.create(user=self.owner, ra=1.0, dec=2.0, comment="shared")

    def get_detail(self, accept="application/json"):
        return self.client.get(reverse("task-detail", args=[self.task.id]), HTTP_ACCEPT=accept)

    def test_the_owner_can_read_it(self) -> None:
        self.client.force_login(self.owner)
        response = self.get_detail()
        assert response.status_code == 200, response.status_code
        assert response.json()["comment"] == "shared"

    def test_staff_can_read_it(self) -> None:
        self.client.force_login(self.staff)
        assert self.get_detail().status_code == 200

    def test_another_user_can_read_it(self) -> None:
        self.client.force_login(self.other)
        response = self.get_detail()
        assert response.status_code == 200, response.status_code
        assert response.json()["comment"] == "shared"

    def test_an_anonymous_caller_can_read_it(self) -> None:
        response = self.get_detail()
        assert response.status_code == 200, response.status_code
        assert response.json()["comment"] == "shared"

    def test_an_anonymous_browser_gets_the_task_page(self) -> None:
        response = self.get_detail(accept="text/html")
        assert response.status_code == 200, response.status_code

    def test_the_same_caller_may_read_but_not_change(self) -> None:
        # the boundary this class exists to describe, asserted on one caller in one test: the
        # refusal on its own is PermissionResponseTests' subject, the contrast is this one's
        self.client.force_login(self.other)

        assert self.get_detail().status_code == 200

        response = self.client.patch(
            reverse("task-detail", args=[self.task.id]),
            data=json.dumps({"comment": "hijacked"}),
            content_type="application/json",
            HTTP_ACCEPT="application/json",
        )
        assert response.status_code == 403, response.status_code


class TaskResultsArePublicTests(TestCase):
    """The result files a task produced are public, like the task detail itself.

    Why is explained on ForcePhotPermission.has_object_permission; these views take a task id and
    no credentials, and this pins that a hardening pass does not quietly lock the data.
    """

    def setUp(self) -> None:
        self.owner = User.objects.create_user(username="fileowner", email="fo@example.com", password=None)
        self.task = Task.objects.create(
            user=self.owner, ra=1.0, dec=2.0, finishtimestamp=timezone.now(), request_type="IMGZIP"
        )
        caches["taskderived"].clear()

    def urls(self) -> list[str]:
        return [
            reverse("taskresultdata", args=[self.task.id]),
            reverse("taskpreviewimage", args=[self.task.id]),
            reverse("taskimagezip", args=[self.task.id]),
            reverse("taskpdfplot", args=[self.task.id]),
            reverse("resultplotdatajs", args=[self.task.id]),
        ]

    def write_result_files(self, tmpdir: str) -> None:
        prefix = Path(tmpdir, self.task.localresultfileprefix())
        prefix.parent.mkdir(parents=True, exist_ok=True)
        # a header with no data rows: enough for the plot view to parse and answer, without
        # needing the plotting script it appends only when there is something to plot
        prefix.with_suffix(".txt").write_text(RESULTFILE_HEADER + "\n")
        for suffix in (".jpg", ".zip", ".pdf"):
            prefix.with_suffix(suffix).write_text("results")

    def test_an_anonymous_caller_can_read_every_result_view(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, override_settings(STATIC_ROOT=tmpdir):
            self.write_result_files(tmpdir)
            for url in self.urls():
                with self.subTest(url=url):
                    assert self.client.get(url).status_code == 200, url

    def test_another_user_can_read_them_too(self) -> None:
        other = User.objects.create_user(username="fileother", email="ft@example.com", password=None)
        with tempfile.TemporaryDirectory() as tmpdir, override_settings(STATIC_ROOT=tmpdir):
            self.write_result_files(tmpdir)
            self.client.force_login(other)
            for url in self.urls():
                with self.subTest(url=url):
                    assert self.client.get(url).status_code == 200, url


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

    def test_the_parents_completion_callback_is_not_inherited(self) -> None:
        # model_to_dict copies every editable field of the parent, so the image request used to
        # come out carrying the parent's callback_url: the API client that submitted the parent
        # was then POSTed a completion notification for a task it never created and had no way to
        # know about
        task = Task.objects.create(
            user=self.user,
            ra=1.0,
            dec=2.0,
            finishtimestamp=timezone.now(),
            from_api=True,
            callback_url="https://example.com/hook",
        )
        with tempfile.TemporaryDirectory() as tmpdir, override_settings(STATIC_ROOT=tmpdir):
            resultfile = Path(tmpdir, f"{task.localresultfileprefix()}.txt")
            resultfile.parent.mkdir(parents=True, exist_ok=True)
            resultfile.touch()

            self.client.force_login(self.user)
            response = self.client.post(reverse("requestimages", args=[task.id]), HTTP_ACCEPT="application/json")

        assert response.status_code == 302, response.status_code
        imagerequest = Task.objects.get(parent_task_id=task.id)
        assert imagerequest.callback_url is None, imagerequest.callback_url
        # the parent keeps its own
        task.refresh_from_db()
        assert task.callback_url == "https://example.com/hook"

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
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="plotter", email="pl@example.com", password=None)
        # deliberately not logged in: the plot data, like every other result view, is public

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
            resultfile.write_text(RESULTFILE_HEADER + "\nWARNING: bad line\n" + self.datarow(0) + "\n")

            response = self.client.get(reverse("resultplotdatajs", args=[task.id]))
            assert response.status_code == 200
            assert response.content == b""

            # the view appends the plotting script from STATIC_ROOT: the minified bundle
            # normally, the source file when DEBUG is on (as it is in the CI settings)
            Path(tmpdir, "js", "queuepage", "src").mkdir(parents=True)
            Path(tmpdir, "js", "lightcurveplotly.min.js").write_text("// plot script\n")
            Path(tmpdir, "js", "queuepage", "src", "lightcurveplotly.js").write_text("// plot script\n")
            resultfile.write_text(RESULTFILE_HEADER + "\n" + self.datarow(0) + "\n" + self.datarow(1) + "\n")

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
class BrokenLinkEmailTests(TestCase):
    """A 404 must never mail the managers, whatever the referrer.

    DEBUG is forced off because that is the only mode in which BrokenLinkEmailsMiddleware would
    send anything, so with DEBUG on these would pass even if it were installed again.
    """

    def test_internal_referer_is_not_reported(self) -> None:
        # a link to a result the retention sweep has since deleted, which is not an admin's problem
        response = self.client.get("/queue/999999/data.txt", HTTP_REFERER="http://testserver/queue/")

        assert response.status_code == 404, response.status_code
        assert not django_mail.outbox, django_mail.outbox[0].subject

    def test_external_referer_is_not_reported(self) -> None:
        response = self.client.get("/no-such-page/", HTTP_REFERER="http://example.com/links")

        assert response.status_code == 404, response.status_code
        assert not django_mail.outbox, django_mail.outbox[0].subject


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

    def capture_callback_payload(self, task: Task) -> dict:
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

    def make_task(self, **kwargs: t.Any) -> Task:
        kwargs.setdefault("ra", 100.0)
        kwargs.setdefault("dec", -20.0)
        return Task.objects.create(user=self.user, **kwargs)

    def fp_command(self, task: Task) -> str:
        return taskrunner_main.build_fp_command(task, remoteresultfile=Path("~/atlasserver/results/job00001.txt"))

    def test_coordinates_are_passed_as_floats(self) -> None:
        command = self.fp_command(self.make_task(mjd_min=None))
        assert "/atlas/bin/force.sh 100.0 -20.0" in command
        assert "dodb=1 parallel=4" in command

    def test_mpc_name_uses_the_solar_system_script(self) -> None:
        command = self.fp_command(self.make_task(ra=None, dec=None, mpc_name="Makemake", mjd_min=None))
        assert "/atlas/bin/ssforce.sh 'Makemake'" in command
        assert "force.sh" not in command.replace("ssforce.sh", "")

    def test_a_padded_mpc_name_is_stripped_before_the_shell_sees_it(self) -> None:
        # the constraint permits a padded name; what it must not become is ssforce.sh '  Makemake '
        command = self.fp_command(self.make_task(ra=None, dec=None, mpc_name="  Makemake  ", mjd_min=None))
        assert "/atlas/bin/ssforce.sh 'Makemake'" in command

    def test_a_blank_mpc_name_takes_the_coordinate_path(self) -> None:
        # such a row is a coordinate request: the constraint reads a whitespace-only name as absent
        # and therefore required ra and dec, so dispatching it to ssforce.sh would drop the target
        command = self.fp_command(self.make_task(mpc_name="   ", mjd_min=None))
        assert "/atlas/bin/force.sh 100.0 -20.0" in command
        assert "ssforce.sh" not in command

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

    def ssostack_command(self, task: Task) -> str:
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

    def make_old_task(self, days: int, **kwargs: t.Any) -> Task:
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


class ProcessTimeoutTests(TestCase):
    # time.sleep as the target rather than a helper defined here: the default start method on this
    # platform is spawn, and a child that re-imports this module dies on AppRegistryNotReady before
    # it can overrun, which would make the timeout test pass for the wrong reason

    def test_a_process_that_finishes_reports_success(self) -> None:
        assert misc.run_process_with_timeout(Process(target=time.sleep, args=(0,)), timeout=30.0) is True

    def test_an_overrunning_process_is_killed_and_reports_failure(self) -> None:
        """The whole point: taskpdfplot forks matplotlib and used to join() it without a deadline.

        A result file that made plot_atlas_fp hang therefore held a mod_wsgi worker thread for as
        long as the process lived, and enough of them exhausted the pool.
        """
        proc = Process(target=time.sleep, args=(300,))

        started = time.monotonic()
        completed = misc.run_process_with_timeout(proc, timeout=0.5)
        elapsed = time.monotonic() - started

        assert completed is False
        assert elapsed < 30.0, f"took {elapsed:.1f}s, so it waited for the child rather than killing it"

        # run_process_with_timeout closed the handle, which Process.close() only permits once the
        # process has actually exited -- so a closed handle is the proof that the child was reaped
        try:
            proc.is_alive()
        except ValueError:
            return
        msg = "the process handle is still open, so the child was never reaped"
        raise AssertionError(msg)


class PdfPlotViewTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="pdfuser", email="pdf@example.com", password=None)
        self.task = Task.objects.create(user=self.user, ra=1.0, dec=2.0, finishtimestamp=timezone.now())

    @contextlib.contextmanager
    def result_file(self):
        """Give the task a result file but no PDF, so the view has to generate one."""
        with tempfile.TemporaryDirectory() as tmpdir, override_settings(STATIC_ROOT=tmpdir):
            resultfile = Path(tmpdir, f"{self.task.localresultfileprefix()}.txt")
            resultfile.parent.mkdir(parents=True, exist_ok=True)
            resultfile.touch()
            yield

    def test_a_timed_out_render_returns_503_rather_than_404(self) -> None:
        # 404 would tell the client the plot does not exist, when in fact it is only slow, and
        # nothing retries a 404
        with self.result_file(), mock.patch.object(views, "make_pdf_plot", return_value=False):
            response = self.client.get(reverse("taskpdfplot", args=[self.task.id]))

        assert response.status_code == 503, response.status_code
        assert response["Retry-After"] == "30"

    def test_only_one_render_runs_at_a_time_for_a_task(self) -> None:
        """The queue page links a PDF for every finished task, so concurrent hits are routine.

        Without the lock each one forks its own matplotlib for the same missing file.
        """
        concurrent: list[int] = []

        def render_while_reentering(*_args: t.Any, **_kwargs: t.Any) -> bool:
            # a second request arriving while this one is rendering must not start its own
            concurrent.append(self.client.get(reverse("taskpdfplot", args=[self.task.id])).status_code)
            return False

        with self.result_file(), mock.patch.object(views, "make_pdf_plot", side_effect=render_while_reentering):
            response = self.client.get(reverse("taskpdfplot", args=[self.task.id]))

        assert response.status_code == 503, response.status_code
        assert concurrent == [503], concurrent

    def test_the_lock_is_released_when_a_render_finishes(self) -> None:
        # a lock left behind would make the task's plot unavailable until the cache entry expired
        with self.result_file(), mock.patch.object(views, "make_pdf_plot", return_value=False):
            self.client.get(reverse("taskpdfplot", args=[self.task.id]))

        assert caches["default"].get(f"pdfplot-lock-{self.task.id}") is None


class RegistrationVerificationTests(TestCase):
    """Registration used to log the new account straight in without proving the address."""

    credentials = {"username": "newcomer", "password1": "a-long-test-password", "password2": "a-long-test-password"}

    def setUp(self) -> None:
        # the resend endpoint rate-limits per address through the throttle cache, and locmem is not
        # reset between tests, so without this one test's resend silently suppresses the next one's
        caches["throttle"].clear()

    def register(self, email: str = "newcomer@example.com") -> t.Any:
        return self.client.post(reverse("register"), {**self.credentials, "email": email})

    def verification_link(self) -> str:
        assert len(django_mail.outbox) == 1, django_mail.outbox
        match = re.search(r"https?://\S+/verify/\S+", str(django_mail.outbox[0].body))
        assert match is not None, django_mail.outbox[0].body
        return match.group(0)

    def test_registering_creates_an_inactive_account_and_sends_a_link(self) -> None:
        response = self.register()

        assert response.status_code == 200, response.status_code
        user = User.objects.get(username="newcomer")
        assert user.is_active is False
        assert django_mail.outbox[0].to == ["newcomer@example.com"]

    def test_registering_no_longer_logs_you_straight_in(self) -> None:
        self.register()
        assert "_auth_user_id" not in self.client.session

    def test_an_unverified_account_cannot_log_in(self) -> None:
        self.register()

        loggedin = self.client.login(username="newcomer", password=self.credentials["password1"])

        assert loggedin is False

    def test_following_the_link_activates_and_logs_in(self) -> None:
        self.register()

        response = self.client.get(self.verification_link())

        assert response.status_code == 302, response.status_code
        assert User.objects.get(username="newcomer").is_active is True
        assert "_auth_user_id" in self.client.session

    def test_a_link_cannot_be_used_twice(self) -> None:
        # is_active is part of the token hash, so activation invalidates the link with no state kept
        self.register()
        link = self.verification_link()
        self.client.get(link)
        self.client.logout()

        response = self.client.get(link)

        assert response.status_code == 400, response.status_code

    def test_a_tampered_token_is_rejected(self) -> None:
        self.register()
        link = self.verification_link()

        response = self.client.get(link[:-4] + "beef/")

        assert response.status_code == 400, response.status_code
        assert User.objects.get(username="newcomer").is_active is False

    def test_a_verification_link_is_not_a_password_reset_link(self) -> None:
        # separate key salts, so a token issued for one purpose cannot be replayed for the other
        from django.contrib.auth.tokens import default_token_generator

        self.register()
        user = User.objects.get(username="newcomer")
        token = verification.token_generator.make_token(user)

        assert not default_token_generator.check_token(user, token)

    def test_resend_issues_a_fresh_link(self) -> None:
        self.register()
        django_mail.outbox.clear()

        response = self.client.post(reverse("resend_verification"), {"email": "newcomer@example.com"})

        assert response.status_code == 200
        assert len(django_mail.outbox) == 1
        assert self.client.get(self.verification_link()).status_code == 302

    def test_resend_says_the_same_thing_whether_or_not_the_account_exists(self) -> None:
        # whether an address has an account here is not something a stranger should be able to probe
        self.register()
        django_mail.outbox.clear()

        withaccount = self.client.post(reverse("resend_verification"), {"email": "newcomer@example.com"})
        sentforknown = len(django_mail.outbox)
        django_mail.outbox.clear()

        noaccount = self.client.post(reverse("resend_verification"), {"email": "nobody@example.com"})

        assert withaccount.status_code == noaccount.status_code == 200
        assert "on its way" in withaccount.content.decode()
        assert "on its way" in noaccount.content.decode(), "the two responses must be indistinguishable"
        assert sentforknown == 1, "no link was sent to the address that does have an unverified account"
        assert not django_mail.outbox, "mail was sent for an address with no unverified account"

    def test_resend_is_rate_limited_per_address(self) -> None:
        # otherwise this endpoint mails any address with an unverified account as fast as it is
        # asked to, which makes the server an amplifier for flooding that inbox
        self.register()
        django_mail.outbox.clear()

        for _ in range(3):
            self.client.post(reverse("resend_verification"), {"email": "newcomer@example.com"})

        assert len(django_mail.outbox) == 1, len(django_mail.outbox)

    def test_a_rate_limited_resend_still_looks_the_same(self) -> None:
        self.register()
        self.client.post(reverse("resend_verification"), {"email": "newcomer@example.com"})

        response = self.client.post(reverse("resend_verification"), {"email": "newcomer@example.com"})

        assert response.status_code == 200
        assert "on its way" in response.content.decode()

    def test_a_failed_verification_email_creates_no_account(self) -> None:
        # the address and username would otherwise be taken by a row nobody can log into, verify
        # or recover, leaving the person unable even to register again
        # patched on views, not on verification: views imports the name directly, so patching it
        # at the definition site leaves the already-bound reference in place and the test passes
        # for the wrong reason
        with mock.patch.object(views, "send_verification_email", side_effect=OSError("smtp down")):
            response = self.client.post(reverse("register"), {**self.credentials, "email": "doomed@example.com"})

        assert response.status_code == 200, response.status_code
        assert not User.objects.filter(username="newcomer").exists(), "the account survived a failed send"
        assert "could not send the verification email" in response.content.decode().lower()

    def test_resend_does_nothing_for_an_already_active_account(self) -> None:
        User.objects.create_user(username="active", email="active@example.com", password=None)
        django_mail.outbox.clear()

        self.client.post(reverse("resend_verification"), {"email": "active@example.com"})

        assert not django_mail.outbox

    def test_resend_will_not_reactivate_an_account_an_admin_disabled(self) -> None:
        """Unchecking is_active is how an account is disabled, not only how one awaits verification.

        Treating the two as one state would make verification a way back in for a disabled account.
        Having logged in before is what tells them apart.
        """
        disabled = User.objects.create_user(username="banned", email="banned@example.com", password=None)
        disabled.last_login = timezone.now()
        disabled.is_active = False
        disabled.save()
        django_mail.outbox.clear()

        self.client.post(reverse("resend_verification"), {"email": "banned@example.com"})

        assert not django_mail.outbox, "a disabled account was sent a link that would reactivate it"

    def test_a_disabled_account_cannot_be_reactivated_by_an_old_link(self) -> None:
        # belt and braces for the check above: the activation view applies the same rule, so a link
        # obtained by any other route is refused too
        self.register()
        link = self.verification_link()
        user = User.objects.get(username="newcomer")
        user.last_login = timezone.now()
        user.save()

        response = self.client.get(link)

        assert response.status_code == 400, response.status_code
        assert User.objects.get(pk=user.pk).is_active is False


class EmailUniquenessConstraintTests(TransactionTestCase):
    """New accounts may not share an address; ones that already did when the index was added may.

    The form check is not enough on its own: it is bypassed by the admin, the shell and a race.

    TransactionTestCase because an IntegrityError aborts the surrounding transaction, which a
    TestCase would not be able to roll back cleanly.
    """

    @staticmethod
    def grandfathered_pair(address: str = "shared@example.com") -> tuple[User, User]:
        """Build the state migration 0005 leaves behind: two accounts on one address, the later exempt.

        The index already exists by the time these tests run, so the pair cannot simply be created
        sharing the address. The second account is made on a placeholder, licensed for the shared
        address, and only then moved onto it -- which is also why the licence is passed in rather
        than read off the row.
        """
        first = User.objects.create_user(username="old1", email=address, password=None)
        second = User.objects.create_user(username="old2", email="old2@example.com", password=None)

        with connection.cursor() as cursor:
            cursor.execute("UPDATE auth_user SET email_unique_exempt = %s WHERE id = %s", [address.lower(), second.pk])

        second.email = address
        second.save()

        return first, second

    def test_the_database_rejects_a_duplicate_created_outside_the_form(self) -> None:
        User.objects.create_user(username="first", email="dupe@example.com", password=None)

        try:
            User.objects.create_user(username="second", email="dupe@example.com", password=None)
        except IntegrityError:
            return
        msg = "a second account with the same email address was accepted"
        raise AssertionError(msg)

    def test_the_check_ignores_case(self) -> None:
        # email_is_taken() has always compared case-insensitively; the index has to agree, or the
        # form and the database disagree about what counts as a duplicate
        User.objects.create_user(username="lower", email="Mixed@Example.com", password=None)

        try:
            User.objects.create_user(username="upper", email="mixed@example.com", password=None)
        except IntegrityError:
            return
        msg = "an address differing only in case was accepted"
        raise AssertionError(msg)

    def test_blank_addresses_are_still_allowed_to_repeat(self) -> None:
        # User.email is blank=True, and accounts without one are not ambiguous for password reset
        User.objects.create_user(username="blank1", email="", password=None)
        User.objects.create_user(username="blank2", email="", password=None)

        assert User.objects.filter(email="").count() == 2

    def test_a_user_can_still_be_updated_without_changing_their_email(self) -> None:
        # a unique index on an expression can trip on an UPDATE that rewrites the same value
        user = User.objects.create_user(username="stable", email="stable@example.com", password=None)
        user.first_name = "Changed"
        user.save()

        assert User.objects.get(pk=user.pk).first_name == "Changed"

    def test_an_account_grandfathered_by_the_migration_keeps_its_shared_address(self) -> None:
        # the pairs that already existed when 0005 ran are kept, not merged or renamed
        first, _second = self.grandfathered_pair()

        assert User.objects.filter(email="shared@example.com").count() == 2
        assert User.objects.get(pk=first.pk).email == "shared@example.com"

    def test_a_new_account_still_cannot_take_a_grandfathered_address(self) -> None:
        """The oldest row of each set is left unexempt precisely so the address stays claimed.

        Exempting every row would have handed the address back to the next person to register it.
        """
        self.grandfathered_pair()

        try:
            User.objects.create_user(username="newcomer", email="shared@example.com", password=None)
        except IntegrityError:
            return
        msg = "a new account took an address that two grandfathered accounts already share"
        raise AssertionError(msg)

    def test_a_grandfathered_account_is_constrained_again_once_it_changes_address(self) -> None:
        """The licence covers one address, not the row for ever.

        Were it a flag, a grandfathered account that later moved -- through the confirmed
        email-change flow, or from the shell -- would stay outside the index and claim nothing, so
        its new address would still be free for a stranger to register.
        """
        _first, second = self.grandfathered_pair()

        # moving off the address it was pardoned for puts it back under the ordinary rule
        third = User.objects.create_user(username="other", email="elsewhere@example.com", password=None)
        second.email = "elsewhere@example.com"

        try:
            second.save()
        except IntegrityError:
            assert User.objects.get(pk=third.pk).email == "elsewhere@example.com"
            return
        msg = "a grandfathered account took another account's address after moving off its own"
        raise AssertionError(msg)

    def test_new_accounts_cannot_collide_with_each_other(self) -> None:
        # nothing created after the migration is ever exempt, so the ordinary rule applies to both
        User.objects.create_user(username="new1", email="fresh@example.com", password=None)

        try:
            User.objects.create_user(username="new2", email="FRESH@example.com", password=None)
        except IntegrityError:
            return
        msg = "two accounts created after the migration were allowed to share an address"
        raise AssertionError(msg)


class ApiTokenPageTests(TestCase):
    """Tokens never expire, and before this page there was no way for a user to rotate one."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="tokenuser", email="token@example.com", password=None)
        self.other = User.objects.create_user(username="tokenother", email="other@example.com", password=None)
        self.client.force_login(self.user)

    def test_requires_login(self) -> None:
        self.client.logout()
        response = self.client.get(reverse("apitoken"))
        assert response.status_code == 302, response.status_code
        assert "/login" in response["Location"], response["Location"]

    def test_a_user_with_no_token_is_offered_one(self) -> None:
        content = self.client.get(reverse("apitoken")).content.decode()
        assert "do not currently have an API token" in content

    def test_create_then_view(self) -> None:
        response = self.client.post(reverse("apitoken"), data={"action": "create"})

        token = Token.objects.get(user=self.user)
        # the key is only recoverable from this response, so the create must render rather than
        # redirect, and it must show the key in full
        assert response.status_code == 200, response.status_code
        assert token.key in response.content.decode()

    def test_regenerate_replaces_the_key_and_invalidates_the_old_one(self) -> None:
        oldkey = Token.objects.create(user=self.user).key

        response = self.client.post(reverse("apitoken"), data={"action": "regenerate"})

        newkey = Token.objects.get(user=self.user).key
        assert newkey != oldkey
        assert newkey in response.content.decode()
        assert not Token.objects.filter(key=oldkey).exists(), "the old key still authenticates"

    def test_the_old_key_stops_authenticating_after_a_regenerate(self) -> None:
        oldkey = Token.objects.create(user=self.user).key
        self.client.post(reverse("apitoken"), data={"action": "regenerate"})

        self.client.logout()
        response = self.client.get(
            reverse("task-list"), HTTP_AUTHORIZATION=f"Token {oldkey}", HTTP_ACCEPT="application/json"
        )

        assert response.status_code == 401, response.status_code

    def test_delete_removes_the_token(self) -> None:
        Token.objects.create(user=self.user)

        response = self.client.post(reverse("apitoken"), data={"action": "delete"})

        # post/redirect/get, so that a reload does not repeat a destructive action
        assert response.status_code == 302, response.status_code
        assert not Token.objects.filter(user=self.user).exists()

    def test_an_unknown_action_is_rejected(self) -> None:
        response = self.client.post(reverse("apitoken"), data={"action": "elevate"})
        assert response.status_code == 400, response.status_code

    def test_a_user_only_ever_sees_and_touches_their_own_token(self) -> None:
        otherkey = Token.objects.create(user=self.other).key

        content = self.client.get(reverse("apitoken")).content.decode()
        assert otherkey not in content

        self.client.post(reverse("apitoken"), data={"action": "regenerate"})
        assert Token.objects.get(user=self.other).key == otherkey, "another user's token was replaced"


class ReadThrottleTests(TestCase):
    """GET used to return True unconditionally, so reads were entirely unlimited.

    That is the traffic most likely to be hammered: the queue page polls, and task detail reads are
    public, so an anonymous caller could poll as fast as the server would answer.
    """

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="throttled", email="throttled@example.com", password=None)
        # the counters live in their own cache, not "default" -- see the "throttle" alias in
        # settings. locmem is not reset between tests, so one test's requests would otherwise
        # spend the next one's budget.
        caches["throttle"].clear()

    def tearDown(self) -> None:
        caches["throttle"].clear()

    # SimpleRateThrottle binds THROTTLE_RATES to the settings dict at class-definition time, so
    # override_settings(REST_FRAMEWORK=...) does not reach it and the tests silently see the real
    # rates. Patch the class attribute the throttle actually reads.
    @staticmethod
    def rates(**overrides: str) -> t.Any:
        return mock.patch.object(ForcedPhotRateThrottle, "THROTTLE_RATES", {"forcephottasks": "60/min", **overrides})

    def test_the_read_scope_is_configured(self) -> None:
        # a scope with no rate is not throttled at all, so a typo here would silently restore the
        # old "GET is exempt" behaviour
        assert "forcephotread" in ForcedPhotRateThrottle.THROTTLE_RATES

    def test_reads_are_throttled(self) -> None:
        self.client.force_login(self.user)

        with self.rates(forcephotread="3/min"):
            statuses = [
                self.client.get(reverse("task-list"), HTTP_ACCEPT="application/json").status_code for _ in range(5)
            ]

        assert statuses[:3] == [200, 200, 200], statuses
        assert 429 in statuses, statuses

    def test_reads_and_writes_are_counted_separately(self) -> None:
        # a burst of polling must not use up the user's ability to submit
        self.client.force_login(self.user)

        with self.rates(forcephotread="1/min"):
            assert self.client.get(reverse("task-list"), HTTP_ACCEPT="application/json").status_code == 200
            assert self.client.get(reverse("task-list"), HTTP_ACCEPT="application/json").status_code == 429

            response = self.client.post(
                reverse("task-list"),
                data=json.dumps({"ra": 1.0, "dec": 2.0}),
                content_type="application/json",
                HTTP_ACCEPT="application/json",
            )

        assert response.status_code == 201, response.content


class ThirdPartyScriptTests(TestCase):
    """Every script the site loads from a CDN must carry an integrity hash.

    The queue page's react/react-dom are no longer among them: they are served from
    static/js/vendor, because an import map cannot express integrity and esm.sh was therefore
    unverifiable script running in every signed-in session.
    """

    def test_the_stats_page_pins_bokeh_with_a_matching_hash(self) -> None:
        from bokeh import __version__ as bokeh_version
        from bokeh.resources import get_sri_hashes_for_version

        content = self.client.get(reverse("stats")).content.decode()
        hashes = get_sri_hashes_for_version(bokeh_version)

        for component in ("bokeh", "bokeh-widgets"):
            filename = f"{component}-{bokeh_version}.min.js"
            assert f"https://cdn.pydata.org/bokeh/release/{filename}" in content, filename
            assert f'integrity="sha384-{hashes[filename]}"' in content, filename

    def test_the_bokeh_url_tracks_the_installed_version(self) -> None:
        # the version used to be hardcoded in the template, so an upgrade of the pin in
        # pyproject.toml left the page loading a BokehJS that did not match the server-rendered plots
        from bokeh import __version__ as bokeh_version

        for script in views.bokeh_cdn_scripts():
            assert bokeh_version in script["src"], script
            assert script["integrity"].startswith("sha384-")

    def test_no_page_loads_jquery_or_bootstrap_3(self) -> None:
        """The site's own layout no longer copies DRF's, so it carries neither.

        jQuery came in through that copy (DRF's browsable-API scripts are written against it) and
        kept Bootstrap 3, which has been end of life since 2019, with it.
        """
        user = User.objects.create_user(username="chrome", email="chrome@example.com", password=None)
        self.client.force_login(user)

        # a <script src=...jquery...>, not the word: the templates explain in comments what the
        # jQuery they replaced used to do, and those comments are served to the browser
        loads_jquery = re.compile(r'<script[^>]*\bsrc="[^"]*jquery[^"]*"', re.IGNORECASE)
        # $(...) or $.ajax(...), but not a bare "$" in prose or a jQuery-free template literal
        uses_dollar = re.compile(r"[^\w$]\$[.(]")

        pages = ["index", "faq", "apiguide", "resultdesc", "stats", "apitoken", "login", "register"]
        for name in pages:
            content = self.client.get(reverse(name), HTTP_ACCEPT="text/html").content.decode()
            assert not loads_jquery.search(content), f"{name} loads jQuery"
            assert not uses_dollar.search(content), f"{name} calls jQuery"
            assert "bootstrap@3" not in content, f"{name} loads Bootstrap 3"
            assert "bootstrap@5" in content, f"{name} does not load Bootstrap 5"

    def test_every_cdn_script_carries_an_integrity_hash(self) -> None:
        user = User.objects.create_user(username="sri", email="sri@example.com", password=None)
        self.client.force_login(user)

        # <script src="https://..."> with no integrity= before the tag closes
        unpinned = re.compile(r'<script\b(?![^>]*\bintegrity=)[^>]*\bsrc="https://[^"]+"[^>]*>')

        for name in ("index", "stats", "task-list"):
            content = self.client.get(reverse(name), HTTP_ACCEPT="text/html").content.decode()
            found = [
                tag
                for tag in unpinned.findall(content)
                # Google Analytics is loaded from a URL whose contents google changes at will, so
                # a hash would break the page rather than protect it
                if "googletagmanager.com" not in tag
            ]
            assert not found, f"{name} has CDN scripts without integrity: {found}"

    def test_the_queue_page_loads_react_from_this_site(self) -> None:
        user = User.objects.create_user(username="importmap", email="importmap@example.com", password=None)
        self.client.force_login(user)

        # the viewset content-negotiates, and the default Accept gets the JSON representation
        response = self.client.get(reverse("task-list"), HTTP_ACCEPT="text/html")
        content = response.content.decode()

        assert "esm.sh" not in content, "react is being fetched from a third-party CDN again"
        assert "js/vendor/react.min.js" in content, content[:400]
        assert "js/vendor/react-dom-client.min.js" in content


class QueueRecalcHandoffTests(TestCase):
    """Renumbering moved out of the request and into the task runner loop.

    It used to run inline on every submit and delete, holding a lock on every queued row while the
    user waited, which also serialised concurrent submitters behind one another.
    """

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="recalcuser", email="recalc@example.com", password=None)
        caches["default"].delete(taskqueue.RECALC_FLAG_CACHEKEY)

    def test_a_request_is_consumed_exactly_once(self) -> None:
        assert taskqueue.consume_recalc_request() is False

        taskqueue.request_recalc()

        assert taskqueue.consume_recalc_request() is True
        assert taskqueue.consume_recalc_request() is False, "the flag was not cleared"

    def test_submitting_asks_for_a_renumbering_without_doing_one(self) -> None:
        self.client.force_login(self.user)

        with mock.patch.object(taskqueue, "calculate_queue_positions") as recalc:
            response = self.client.post(
                reverse("task-list"),
                data=json.dumps({"ra": 1.0, "dec": 2.0}),
                content_type="application/json",
                HTTP_ACCEPT="application/json",
            )

        assert response.status_code == 201, response.content
        assert not recalc.called, "the renumbering must be left to the task runner"
        assert taskqueue.consume_recalc_request() is True

    def test_a_submitted_task_gets_a_provisional_position(self) -> None:
        # NULL would render as no queue position at all until the runner's next pass
        self.client.force_login(self.user)
        existing = Task.objects.create(user=self.user, ra=1.0, dec=2.0, queuepos_relative=4)

        response = self.client.post(
            reverse("task-list"),
            data=json.dumps({"ra": 3.0, "dec": 4.0}),
            content_type="application/json",
            HTTP_ACCEPT="application/json",
        )

        assert response.status_code == 201, response.content
        newtask = Task.objects.exclude(id=existing.id).get()
        assert newtask.queuepos_relative == 5, newtask.queuepos_relative

    def test_deleting_a_queued_task_asks_for_a_renumbering(self) -> None:
        task = Task.objects.create(user=self.user, ra=1.0, dec=2.0)
        self.client.force_login(self.user)
        taskqueue.consume_recalc_request()

        response = self.client.delete(reverse("task-detail", args=[task.id]), HTTP_ACCEPT="application/json")

        assert response.status_code == 204, response.status_code
        assert taskqueue.consume_recalc_request() is True

    def test_deleting_a_finished_task_does_not(self) -> None:
        # a finished task holds no queue position, so nothing downstream of it moves
        task = Task.objects.create(user=self.user, ra=1.0, dec=2.0, finishtimestamp=timezone.now())
        self.client.force_login(self.user)
        taskqueue.consume_recalc_request()

        response = self.client.delete(reverse("task-detail", args=[task.id]), HTTP_ACCEPT="application/json")

        assert response.status_code == 204, response.status_code
        assert taskqueue.consume_recalc_request() is False


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
