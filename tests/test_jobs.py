# -*- coding: utf-8 -*-
"""Smoke tests for web.jobs — JobStore and JobStatus, no FastAPI needed."""

import sys
import os
import threading
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from web.jobs import JobRecord, JobStatus, JobStore


class TestJobStatus:
    def test_string_equality(self):
        # StrEnum: enum member == its string value
        assert JobStatus.DONE == "done"
        assert JobStatus.FAILED == "failed"
        assert JobStatus.RUNNING == "running"
        assert JobStatus.PENDING == "pending"
        assert JobStatus.SKIPPED == "skipped"

    def test_values(self):
        assert JobStatus.PENDING.value == "pending"
        assert JobStatus.RUNNING.value == "running"
        assert JobStatus.DONE.value == "done"
        assert JobStatus.FAILED.value == "failed"
        assert JobStatus.SKIPPED.value == "skipped"

    def test_distinct_members(self):
        statuses = [JobStatus.PENDING, JobStatus.RUNNING, JobStatus.DONE,
                    JobStatus.FAILED, JobStatus.SKIPPED]
        assert len(set(statuses)) == 5


class TestJobStoreCreate:
    def test_create_returns_record(self):
        store = JobStore()
        rec = store.create()
        assert isinstance(rec, JobRecord)

    def test_created_record_is_pending(self):
        store = JobStore()
        rec = store.create()
        assert rec.status == JobStatus.PENDING

    def test_created_record_has_id(self):
        store = JobStore()
        rec = store.create()
        assert rec.id and len(rec.id) > 0

    def test_two_records_have_different_ids(self):
        store = JobStore()
        r1 = store.create()
        r2 = store.create()
        assert r1.id != r2.id

    def test_get_returns_same_record(self):
        store = JobStore()
        rec = store.create()
        fetched = store.get(rec.id)
        assert fetched is rec

    def test_get_unknown_id_returns_none(self):
        store = JobStore()
        assert store.get("nonexistent-id") is None


class TestJobStoreFail:
    def test_fail_sets_status(self):
        store = JobStore()
        rec = store.create()
        store.fail(rec.id, "something broke")
        assert rec.status == JobStatus.FAILED

    def test_fail_sets_error_message(self):
        store = JobStore()
        rec = store.create()
        store.fail(rec.id, "disk full")
        assert rec.error == "disk full"

    def test_fail_unknown_id_no_error(self):
        store = JobStore()
        store.fail("nonexistent", "msg")  # must not raise


class TestJobStoreComplete:
    def _fake_path(self, name: str) -> Path:
        return Path(f"/tmp/{name}")

    def test_complete_sets_status_done(self):
        store = JobStore()
        rec = store.create()
        store.complete(rec.id, self._fake_path("out.pdf"), None)
        assert rec.status == JobStatus.DONE

    def test_complete_sets_output_pdf(self):
        store = JobStore()
        rec = store.create()
        p = self._fake_path("out.pdf")
        store.complete(rec.id, p, None)
        assert rec.output_pdf == p

    def test_complete_sets_answers_pdf(self):
        store = JobStore()
        rec = store.create()
        a = self._fake_path("ans.pdf")
        store.complete(rec.id, self._fake_path("out.pdf"), a)
        assert rec.answers_pdf == a

    def test_complete_sets_optional_pdfs(self):
        store = JobStore()
        rec = store.create()
        four_up = self._fake_path("4up.pdf")
        two_up = self._fake_path("2up.pdf")
        store.complete(
            rec.id,
            self._fake_path("out.pdf"),
            None,
            exercise_4up_pdf=four_up,
            exercise_2up_pdf=two_up,
        )
        assert rec.exercise_4up_pdf == four_up
        assert rec.exercise_2up_pdf == two_up

    def test_complete_sets_overview(self):
        store = JobStore()
        rec = store.create()
        ov = {"papers": []}
        store.complete(rec.id, self._fake_path("out.pdf"), None, overview=ov)
        assert rec.overview == ov

    def test_complete_unknown_id_no_error(self):
        store = JobStore()
        store.complete("nonexistent", self._fake_path("out.pdf"), None)


class TestJobStoreRankingResult:
    def test_set_ranking_result_sets_pdf(self):
        store = JobStore()
        rec = store.create()
        p = Path("/tmp/ranking.pdf")
        store.set_ranking_result(rec.id, p)
        assert rec.ranking_pdf == p

    def test_set_ranking_result_sets_status_done(self):
        store = JobStore()
        rec = store.create()
        store.set_ranking_result(rec.id, Path("/tmp/ranking.pdf"))
        assert rec.ranking_status == JobStatus.DONE


class TestJobStoreLogLine:
    def test_set_log_line(self):
        store = JobStore()
        rec = store.create()
        store.set_log_line(rec.id, "processing page 3")
        assert rec.log_line == "processing page 3"

    def test_log_line_truncated_at_800(self):
        store = JobStore()
        rec = store.create()
        long_line = "x" * 1000
        store.set_log_line(rec.id, long_line)
        assert len(rec.log_line) == 800

    def test_set_ranking_log_line(self):
        store = JobStore()
        rec = store.create()
        store.set_ranking_log_line(rec.id, "ranking step 2")
        assert rec.ranking_log_line == "ranking step 2"


class TestJobStoreThreadSafety:
    def test_concurrent_creates_all_unique(self):
        store = JobStore()
        ids: list[str] = []
        lock = threading.Lock()

        def worker():
            rec = store.create()
            with lock:
                ids.append(rec.id)

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(ids) == 50
        assert len(set(ids)) == 50  # all unique

    def test_concurrent_fail_and_get(self):
        store = JobStore()
        rec = store.create()
        errors: list[Exception] = []

        def failer():
            try:
                store.fail(rec.id, "concurrent failure")
            except Exception as e:
                errors.append(e)

        def getter():
            try:
                store.get(rec.id)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=failer if i % 2 == 0 else getter)
                   for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
