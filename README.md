# JUSTICE

네이버 플레이스 순위 추적 도구. 업체별 키워드를 등록해두면 매일 자동으로
**PC + 모바일** 자연 순위(광고 제외)를 크롤링하고, 누적 노출 일수가 목표
(기본 25일)에 도달하면 결제 요청 알림을 띄웁니다.

## 주요 기능

- **업체/키워드 관리** — 네이버 플레이스 URL로 업체를 등록하고 추적할 검색어 추가
- **자동 순위 체크** — 매일 오전 9시(Asia/Seoul) PC·모바일 순위를 동시에 크롤링
- **수동 체크** — 전체 또는 특정 키워드 1개만 즉시 체크
- **노출 누적 추적** — PC 또는 모바일 중 하나라도 1~5위면 "노출일"로 카운트
- **목표 달성 알림** — 누적 노출 25일 도달 시 결제 요청 알림 생성
- **대시보드** — 업체·키워드·노출 현황을 한눈에

## 요구 사항

- Python 3.11 이상
- [Playwright](https://playwright.dev/python/) (Chromium)

## 설치

Windows에서는 `setup.bat`을 실행하면 됩니다(pip 패키지 → Playwright 브라우저
→ DB 초기화를 순서대로 진행):

```bat
setup.bat
```

수동 설치:

```bash
pip install -r requirements.txt
python -m playwright install chromium
python -c "from database import init_db; init_db()"
```

## 실행

Windows에서는 `start.bat`을 실행하면 서버가 뜨고 브라우저가 열립니다.
수동 실행:

```bash
python app.py
```

브라우저에서 <http://localhost:5000> 으로 접속하세요.

## 프로젝트 구조

| 파일 | 설명 |
|------|------|
| `app.py` | Flask 웹 서버, REST API, 일일 스케줄러(APScheduler) |
| `crawler.py` | Playwright 기반 네이버 플레이스 순위 크롤러 (PC + 모바일) |
| `database.py` | SQLite 스키마 및 데이터 접근 함수 |
| `templates/` | HTML 템플릿 |
| `static/` | 정적 자산 (CSS/JS) |
| `tests/` | 단위 테스트 |
| `justice.db` | SQLite 데이터베이스 (로컬 생성, 버전 관리 제외) |

## 참고

네이버는 페이지의 CSS 클래스명을 자주 변경합니다. 크롤링이 동작하지 않으면
`crawler.py` 상단의 `PC_SELECTORS` / `MOBILE_SELECTORS`를 업데이트하세요.

## 테스트

```bash
python -m pytest
```
