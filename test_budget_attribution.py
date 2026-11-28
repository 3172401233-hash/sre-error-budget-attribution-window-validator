import unittest

from budget_attribution import (
    Failure,
    Request,
    ServiceOwnership,
    SharedRootCause,
    SLOWindow,
    attribute_budget,
)


def _result(report, service, start):
    for r in report.results:
        if r.service == service and r.window_start == start:
            return r
    raise AssertionError("no result for %s @ %s" % (service, start))


class SharedFailureTest(unittest.TestCase):
    """Shared dependency outage: one root cause, many services."""

    def setUp(self):
        self.requests = [
            Request("req-a1", "svc-a", 100.0, "db"),
            Request("req-b1", "svc-b", 101.0, "db"),
            Request("req-c1", "svc-c", 102.0, "db"),
        ]
        self.failures = [
            Failure("f-a1", "req-a1", "svc-a", 110.0, "db", "rc-db"),
            Failure("f-b1", "req-b1", "svc-b", 111.0, "db", "rc-db"),
            Failure("f-c1", "req-c1", "svc-c", 112.0, "db", "rc-db"),
        ]
        self.windows = [
            SLOWindow("svc-a", 0.0, 1000.0, 0.99),
            SLOWindow("svc-b", 0.0, 1000.0, 0.99),
            SLOWindow("svc-c", 0.0, 1000.0, 0.99),
        ]
        self.ownership = [
            ServiceOwnership("svc-a", "team-a"),
            ServiceOwnership("svc-b", "team-b"),
            ServiceOwnership("svc-c", "team-c"),
        ]
        self.shared = [SharedRootCause("rc-db", "db", ("svc-a", "svc-b", "svc-c"))]

    def test_each_service_consumes_once(self):
        report = attribute_budget(
            self.requests, self.failures, self.windows, self.ownership, self.shared)
        for svc in ("svc-a", "svc-b", "svc-c"):
            res = _result(report, svc, 0.0)
            self.assertEqual(res.unique_failures, 1)
            self.assertEqual(res.budget_consumed, 1)

    def test_shared_duplicate_counted(self):
        report = attribute_budget(
            self.requests, self.failures, self.windows, self.ownership, self.shared)
        # 3 services hit by 1 physical root cause -> 2 extra counts
        self.assertEqual(report.shared_duplicates, 2)
        self.assertEqual(report.retry_duplicates, 0)
        self.assertEqual(report.duplicate_count, 2)
        self.assertEqual(report.undetermined, [])

    def test_shared_root_cause_detail(self):
        report = attribute_budget(
            self.requests, self.failures, self.windows, self.ownership, self.shared)
        self.assertEqual(len(report.shared_root_causes), 1)
        detail = report.shared_root_causes[0]
        self.assertEqual(detail["root_cause_id"], "rc-db")
        self.assertEqual(detail["dependency"], "db")
        self.assertEqual(detail["services"], ["svc-a", "svc-b", "svc-c"])
        self.assertEqual(detail["unique_failures"], 3)

    def test_owner_attribution(self):
        report = attribute_budget(
            self.requests, self.failures, self.windows, self.ownership, self.shared)
        self.assertEqual(_result(report, "svc-a", 0.0).owner, "team-a")
        self.assertEqual(_result(report, "svc-b", 0.0).owner, "team-b")


class WindowBoundaryTest(unittest.TestCase):
    """Window edges are [start, end); boundary failures land in the
    window containing their timestamp; retries crossing a boundary are
    attributed to the root failure's window, not double counted."""

    def test_failure_at_window_end_belongs_to_next_window(self):
        requests = [Request("r1", "svc", 10.0), Request("r2", "svc", 1010.0)]
        failures = [Failure("f1", "r2", "svc", 1000.0)]  # exactly at boundary
        windows = [
            SLOWindow("svc", 0.0, 1000.0, 0.5),
            SLOWindow("svc", 1000.0, 2000.0, 0.5),
        ]
        report = attribute_budget(requests, failures, windows, [], [])
        self.assertEqual(_result(report, "svc", 0.0).unique_failures, 0)
        self.assertEqual(_result(report, "svc", 1000.0).unique_failures, 1)
        self.assertEqual(report.undetermined, [])

    def test_failure_at_window_start_belongs_to_that_window(self):
        requests = [Request("r1", "svc", 0.0)]
        failures = [Failure("f1", "r1", "svc", 0.0)]
        windows = [SLOWindow("svc", 0.0, 1000.0, 0.5)]
        report = attribute_budget(requests, failures, windows, [], [])
        self.assertEqual(_result(report, "svc", 0.0).unique_failures, 1)

    def test_retry_crossing_boundary_counted_once_in_root_window(self):
        requests = [Request("r1", "svc", 900.0)]
        failures = [
            Failure("f1", "r1", "svc", 990.0),              # root, window 1
            Failure("f2", "r1", "svc", 1010.0, retry_of="f1"),  # retry, window 2
        ]
        windows = [
            SLOWindow("svc", 0.0, 1000.0, 0.5),
            SLOWindow("svc", 1000.0, 2000.0, 0.5),
        ]
        report = attribute_budget(requests, failures, windows, [], [])
        self.assertEqual(_result(report, "svc", 0.0).unique_failures, 1)
        self.assertEqual(_result(report, "svc", 1000.0).unique_failures, 0)
        self.assertEqual(report.retry_duplicates, 1)
        self.assertEqual(report.duplicate_count, 1)

    def test_failure_outside_all_windows_is_undetermined(self):
        requests = [Request("r1", "svc", 5000.0)]
        failures = [Failure("f1", "r1", "svc", 5000.0)]
        windows = [SLOWindow("svc", 0.0, 1000.0, 0.5)]
        report = attribute_budget(requests, failures, windows, [], [])
        self.assertEqual(_result(report, "svc", 0.0).unique_failures, 0)
        self.assertEqual(len(report.undetermined), 1)
        self.assertEqual(report.undetermined[0]["reason"], "outside_all_windows")


class RetryAmplificationTest(unittest.TestCase):
    """Upstream retries must not amplify budget consumption."""

    def test_retry_chain_counts_once(self):
        requests = [Request("r1", "svc", 10.0)]
        failures = [
            Failure("f1", "r1", "svc", 20.0),
            Failure("f2", "r1", "svc", 21.0, retry_of="f1"),
            Failure("f3", "r1", "svc", 22.0, retry_of="f2"),
            Failure("f4", "r1", "svc", 23.0, retry_of="f3"),
        ]
        windows = [SLOWindow("svc", 0.0, 1000.0, 0.5)]
        report = attribute_budget(requests, failures, windows, [], [])
        res = _result(report, "svc", 0.0)
        self.assertEqual(res.unique_failures, 1)
        self.assertEqual(res.budget_consumed, 1)
        self.assertEqual(report.retry_duplicates, 3)
        self.assertEqual(report.duplicate_count, 3)

    def test_independent_failures_not_deduplicated(self):
        requests = [Request("r1", "svc", 10.0), Request("r2", "svc", 11.0)]
        failures = [
            Failure("f1", "r1", "svc", 20.0),
            Failure("f2", "r2", "svc", 21.0),
        ]
        windows = [SLOWindow("svc", 0.0, 1000.0, 0.5)]
        report = attribute_budget(requests, failures, windows, [], [])
        self.assertEqual(_result(report, "svc", 0.0).unique_failures, 2)
        self.assertEqual(report.retry_duplicates, 0)

    def test_broken_retry_chain_is_undetermined(self):
        requests = [Request("r1", "svc", 10.0)]
        failures = [Failure("f1", "r1", "svc", 20.0, retry_of="ghost")]
        windows = [SLOWindow("svc", 0.0, 1000.0, 0.5)]
        report = attribute_budget(requests, failures, windows, [], [])
        self.assertEqual(_result(report, "svc", 0.0).unique_failures, 0)
        self.assertEqual(len(report.undetermined), 1)
        self.assertEqual(
            report.undetermined[0]["reason"], "retry_chain_broken_or_cyclic")

    def test_cyclic_retry_chain_is_undetermined(self):
        requests = [Request("r1", "svc", 10.0)]
        failures = [
            Failure("f1", "r1", "svc", 20.0, retry_of="f2"),
            Failure("f2", "r1", "svc", 21.0, retry_of="f1"),
        ]
        windows = [SLOWindow("svc", 0.0, 1000.0, 0.5)]
        report = attribute_budget(requests, failures, windows, [], [])
        self.assertEqual(_result(report, "svc", 0.0).unique_failures, 0)
        self.assertEqual(len(report.undetermined), 2)


class MissingRequestTest(unittest.TestCase):
    def test_missing_request_is_undetermined(self):
        requests = [Request("r1", "svc", 10.0)]
        failures = [
            Failure("f1", "r1", "svc", 20.0),
            Failure("f2", "r-missing", "svc", 21.0),
        ]
        windows = [SLOWindow("svc", 0.0, 1000.0, 0.5)]
        report = attribute_budget(requests, failures, windows, [], [])
        self.assertEqual(_result(report, "svc", 0.0).unique_failures, 1)
        self.assertEqual(len(report.undetermined), 1)
        self.assertEqual(report.undetermined[0]["failure_id"], "f2")
        self.assertEqual(report.undetermined[0]["reason"], "missing_request")

    def test_service_mismatch_is_undetermined(self):
        requests = [Request("r1", "svc-a", 10.0)]
        failures = [Failure("f1", "r1", "svc-b", 20.0)]
        windows = [SLOWindow("svc-b", 0.0, 1000.0, 0.5)]
        report = attribute_budget(requests, failures, windows, [], [])
        self.assertEqual(_result(report, "svc-b", 0.0).unique_failures, 0)
        self.assertEqual(report.undetermined[0]["reason"], "request_service_mismatch")


class BudgetConsumptionTest(unittest.TestCase):
    def test_budget_allowed_remaining_exhausted(self):
        requests = [Request("r%d" % i, "svc", float(i)) for i in range(100)]
        failures = [
            Failure("f%d" % i, "r%d" % i, "svc", float(i) + 0.5)
            for i in range(3)
        ]
        windows = [SLOWindow("svc", 0.0, 1000.0, 0.99)]  # 1% of 100 -> 1 allowed
        report = attribute_budget(requests, failures, windows, [], [])
        res = _result(report, "svc", 0.0)
        self.assertEqual(res.total_requests, 100)
        self.assertEqual(res.budget_allowed, 1)
        self.assertEqual(res.budget_consumed, 3)
        self.assertEqual(res.budget_remaining, -2)
        self.assertTrue(res.budget_exhausted)


if __name__ == "__main__":
    unittest.main()
