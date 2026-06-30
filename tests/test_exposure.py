"""노출일 누적 + 결제 사이클 로직 단위 테스트.

임시 DB에 대해 database 모듈을 직접 검증합니다 (네트워크 불필요).
실행: python -m pytest
"""

from datetime import date, timedelta

import pytest

import database


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """매 테스트마다 임시 파일 DB를 새로 초기화."""
    path = tmp_path / "test.db"
    monkeypatch.setattr(database, "DB_PATH", str(path))
    database.init_db()
    cid = database.create_client("테스트업체", "https://map.naver.com/p/place/123", "123", "")
    kid = database.create_keyword(cid, "테스트키워드")
    return {"cid": cid, "kid": kid}


def _kw(kid):
    with database.get_db() as conn:
        r = conn.execute(
            "SELECT exposure_count, goal_days, manual_days, payment_pending, "
            "cycle_count, completed_at, last_paid_at FROM keywords WHERE id=?", (kid,)
        ).fetchone()
        return dict(r)


def test_manual_days_sets_baseline(db):
    database.set_manual_days(db["kid"], 10)
    row = _kw(db["kid"])
    assert row["manual_days"] == 10
    assert row["exposure_count"] == 10
    assert row["payment_pending"] == 0


def test_auto_day_accumulates_on_top_of_manual(db):
    database.set_manual_days(db["kid"], 10)
    database.record_tracking(db["kid"], date.today().isoformat(), 3, True, None, False)
    assert _kw(db["kid"])["exposure_count"] == 11


def test_goal_triggers_payment_and_restarts_count(db):
    database.set_manual_days(db["kid"], 24)
    newly = database.record_tracking(db["kid"], date.today().isoformat(), 1, True, 1, True)
    row = _kw(db["kid"])
    assert newly is True                  # 24 + 1 = 25 = goal
    assert row["payment_pending"] == 1     # 결제 대기(깜빡임)
    assert row["cycle_count"] == 1
    assert row["completed_at"] == date.today().isoformat()  # 보장 완료일 기록
    assert row["exposure_count"] == 0      # 다음 사이클로 카운트 자동 재시작


def test_recount_continues_after_completion(db):
    database.set_manual_days(db["kid"], 24)
    database.record_tracking(db["kid"], date.today().isoformat(), 1, True, None, False)  # 완료
    # 완료 다음 날부터 노출 → 새 사이클에 누적
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    database.record_tracking(db["kid"], tomorrow, 2, True, None, False)
    assert _kw(db["kid"])["exposure_count"] == 1


def test_payment_complete_clears_pending(db):
    database.set_manual_days(db["kid"], 25)             # 즉시 보장 달성 → 결제 대기
    assert _kw(db["kid"])["payment_pending"] == 1
    database.mark_payment_complete(db["kid"])
    row = _kw(db["kid"])
    assert row["payment_pending"] == 0
    assert row["last_paid_at"] == date.today().isoformat()


def test_payment_not_completed_keeps_blinking_but_count_restarts(db):
    # 결제완료를 누르지 않아도 카운트는 재시작 (payment_pending 유지)
    database.set_manual_days(db["kid"], 25)
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    database.record_tracking(db["kid"], tomorrow, 2, True, None, False)
    row = _kw(db["kid"])
    assert row["payment_pending"] == 1     # 여전히 결제 대기(깜빡임)
    assert row["exposure_count"] == 1      # 그래도 다음 사이클 카운트는 진행


def test_non_exposed_day_does_not_count(db):
    database.record_tracking(db["kid"], date.today().isoformat(), 8, False, 9, False)
    assert _kw(db["kid"])["exposure_count"] == 0


def test_exposure_is_pc_priority(db):
    # PC 13위(미노출) + 모바일 5위(노출)면, PC 우선이라 노출일로 카운트하지 않음
    database.record_tracking(db["kid"], date.today().isoformat(), 13, False, 5, True)
    assert _kw(db["kid"])["exposure_count"] == 0


def test_exposure_falls_back_to_mobile_when_pc_missing(db):
    # PC를 읽지 못한 경우(rank None)에는 모바일로 판정
    database.record_tracking(db["kid"], date.today().isoformat(), None, False, 3, True)
    assert _kw(db["kid"])["exposure_count"] == 1


def test_same_day_recheck_is_idempotent(db):
    today = date.today().isoformat()
    database.record_tracking(db["kid"], today, 3, True, None, False)
    database.record_tracking(db["kid"], today, 3, True, None, False)
    assert _kw(db["kid"])["exposure_count"] == 1
