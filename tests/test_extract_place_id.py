"""crawler.extract_place_id 단위 테스트.

순수 함수(네트워크 불필요)라 빠르고 안정적으로 검증할 수 있습니다.
실행: python -m pytest
"""

import pytest

from crawler import extract_place_id


@pytest.mark.parametrize(
    "url, expected",
    [
        # /entry/place/{id}
        (
            "https://m.place.naver.com/restaurant/1234567890/home",
            "1234567890",
        ),
        (
            "https://map.naver.com/p/entry/place/1234567890",
            "1234567890",
        ),
        # /place/{id}
        (
            "https://m.place.naver.com/place/987654321",
            "987654321",
        ),
        # place.naver.com/{category}/{id}
        (
            "https://pcmap.place.naver.com/restaurant/1122334455/home",
            "1122334455",
        ),
        # 쿼리스트링이 붙어도 추출
        (
            "https://map.naver.com/p/entry/place/555000?c=15.00,0,0,0,dh",
            "555000",
        ),
    ],
)
def test_extract_place_id_valid(url, expected):
    assert extract_place_id(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://www.naver.com",
        "https://map.naver.com/p/search/카페",
        "not a url",
        "",
    ],
)
def test_extract_place_id_returns_none_when_no_id(url):
    assert extract_place_id(url) is None
