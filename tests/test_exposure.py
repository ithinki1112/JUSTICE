"""노출일 누적/수기 입력 로직 단위 테스트.

임시 DB에 대해 database 모듈을 직접 검증합니다 (네트워크 불필요).
실행: python -m pytest
"""

import importlib

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


def _exposure(kid):
    with database.get_db() as conn:
        r = conn.execute(
            "SELECT manual_days, exposure_count, goal_days, is_complete "
            "FROM keywords WHERE id=?", (kid,)
        ).fetchone()
        return dict(r)


def test_manual_days_sets_baseline(db):
    database.set_manual_days(db["kid"], 10)
    row = _exposure(db["kid"])
    assert row["manual_days"] == 10
    assert row["exposure_count"] == 10
    assert row["is_complete"] == 0


def test_auto_day_accumulates_on_top_of_manual(db):
    database.set_manual_days(db["kid"], 10)
    # 자동 체크 1일 (PC 노출)
    database.record_tracking(db["kid"], "2026-06-30", 3, True, None, False)
    assert _exposure(db["kid"])["exposure_count"] == 11


def test_goal_completion_triggers(db):
    database.set_manual_days(db["kid"], 24)
    newly = database.record_tracking(db["kid"], "2026-06-30", 1, True, 1, True)
    row = _exposure(db["kid"])
    assert newly is True          # 24 + 1 = 25 = goal
    assert row["exposure_count"] == 25
    assert row["is_complete"] == 1


def test_lowering_manual_days_releases_completion(db):
    database.set_manual_days(db["kid"], 25)        # 즉시 목표 달성
    assert _exposure(db["kid"])["is_complete"] == 1
    database.set_manual_days(db["kid"], 5)         # 정정
    row = _exposure(db["kid"])
    assert row["exposure_count"] == 5
    assert row["is_complete"] == 0


def test_non_exposed_day_does_not_count(db):
    database.record_tracking(db["kid"], "2026-06-30", 8, False, 9, False)
    assert _exposure(db["kid"])["exposure_count"] == 0


def test_same_day_recheck_is_idempotent(db):
    database.record_tracking(db["kid"], "2026-06-30", 3, True, None, False)
    database.record_tracking(db["kid"], "2026-06-30", 3, True, None, False)
    # 같은 날 재체크는 1일로만 카운트 (UNIQUE(keyword_id, check_date))
    assert _exposure(db["kid"])["exposure_count"] == 1
