"""주간 배치가 감지 실패를 성공 종료로 오판하지 않는지 검증."""

from lawtrack.detect import DetectStatus
from scripts.run_weekly import FAILURE_STATUSES, REPORTABLE_STATUSES


def test_lookup_failures_trigger_nonzero_batch_result():
    assert DetectStatus.ERROR in FAILURE_STATUSES
    assert DetectStatus.NOT_FOUND in FAILURE_STATUSES
    assert DetectStatus.AMBIGUOUS in FAILURE_STATUSES


def test_valid_business_outcomes_are_not_batch_failures():
    assert DetectStatus.UNCHANGED not in FAILURE_STATUSES
    assert DetectStatus.CHANGED not in FAILURE_STATUSES
    assert DetectStatus.NO_COMPARISON not in FAILURE_STATUSES


def test_changed_versions_are_reported_regardless_of_enforce_date():
    assert DetectStatus.CHANGED in REPORTABLE_STATUSES
    assert DetectStatus.NO_COMPARISON in REPORTABLE_STATUSES
    assert DetectStatus.UNCHANGED not in REPORTABLE_STATUSES
