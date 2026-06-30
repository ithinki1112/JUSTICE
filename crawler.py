"""
네이버 플레이스 순위 크롤러 (PC + 모바일)

매칭 방식 (2026 기준):
  네이버는 더 이상 검색 목록 HTML에 place_id를 넣지 않습니다(링크가 href="#" 형태).
  따라서 place_id로 업체명을 1회 조회한 뒤, 검색 목록에서 **업체명**으로 순위를 찾습니다.

  - PC:     pcmap.place.naver.com/place/list?query={keyword}&display=70
  - 모바일: m.place.naver.com/place/list?query={keyword}&entry=pll

광고(플레이스 광고)는 제외하고 자연 순위만 카운트합니다.

※ 네이버는 CSS 클래스명을 자주 변경합니다.
  크롤링이 안 되면 ITEM_SELECTORS / NAME_SELECTORS 를 업데이트하세요.
"""

import asyncio
import random
import re
from urllib.parse import quote
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ── 셀렉터 (네이버 업데이트 시 이 부분만 수정) ────────────────────────────────

# 검색 목록의 업체 항목(li) 후보 셀렉터 — 먼저 매칭되는 것을 사용
ITEM_SELECTORS = [
    '.Ryr1F li',
    'li[data-laim-exp-id]',
    'li.VLTHu',
    'ul[class] > li[data-id]',
    '#_pcmap_list_scroll_container li',
    '#_list_scroll_container li',
]

# 항목 내부의 업체명 셀렉터 후보 (JS에서 순서대로 시도)
NAME_SELECTORS = ['.YwYLL', '.place_bluelink', '.TYaxT', 'span.O_Uah', '.CMy2_', '.tit']

DESKTOP_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
MOBILE_UA  = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1'

# 항목별 {업체명, 광고여부}를 한 번에 추출하는 JS
_EXTRACT_JS = """
(els, nameSels) => els.map(el => {
  const txt = (el.innerText || '').trim();
  const isAd = /광고/.test(txt.slice(0, 30));
  let name = '';
  for (const s of nameSels) {
    const n = el.querySelector(s);
    if (n && n.textContent.trim()) { name = n.textContent.trim(); break; }
  }
  if (!name) {
    const cand = el.querySelector('a span, strong, span');
    if (cand) name = cand.textContent.trim();
  }
  return { name, isAd };
})
"""


def extract_place_id(url: str) -> str | None:
    patterns = [
        r'/entry/place/(\d+)',
        r'/place/(\d+)',
        r'place\.naver\.com/[^/]+/(\d+)',
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def _norm(s: str) -> str:
    """업체명 비교용 정규화: 공백 제거 + 소문자."""
    return re.sub(r'\s+', '', (s or '')).lower()


# ── 업체명 조회 (place_id → 이름) ─────────────────────────────────────────────

async def _resolve_place_name(page, place_id: str) -> str | None:
    """place_id로 플레이스 상세 페이지를 열어 업체명을 얻습니다."""
    url = f'https://m.place.naver.com/place/{place_id}/home'
    try:
        await page.goto(url, wait_until='domcontentloaded', timeout=20000)
        await asyncio.sleep(1.5)
    except PlaywrightTimeout:
        return None

    # 1) og:title 메타가 가장 안정적
    try:
        og = await page.get_attribute('meta[property="og:title"]', 'content')
        if og and og.strip():
            return re.split(r'\s*[:|]\s*', og.strip())[0].strip()
    except Exception:
        pass

    # 2) 상세 페이지 제목 후보 셀렉터
    for sel in ['.GHAhO', '#_title span', '.Fc1rA', 'h2 span', '.YwYLL']:
        try:
            el = await page.query_selector(sel)
            if el:
                t = (await el.text_content() or '').strip()
                if t:
                    return t
        except Exception:
            continue
    return None


# ── 목록에서 순위 찾기 ─────────────────────────────────────────────────────────

async def _find_rank(page_or_frame, target_name: str) -> dict:
    """현재 페이지/프레임의 검색 목록에서 target_name의 자연 순위를 찾습니다."""
    result = {'rank': None, 'is_exposed': False, 'checked': 0, 'error': None}
    target = _norm(target_name)

    # 목록 항목이 하나라도 뜰 때까지 한 번만 대기 (후보 셀렉터 union)
    try:
        await page_or_frame.wait_for_selector(', '.join(ITEM_SELECTORS), timeout=8000)
    except PlaywrightTimeout:
        result['error'] = '업체 목록 없음 (셀렉터 업데이트 필요)'
        return result

    items = []
    for sel in ITEM_SELECTORS:
        try:
            data = await page_or_frame.eval_on_selector_all(sel, _EXTRACT_JS, NAME_SELECTORS)
        except Exception:
            continue
        # 이름이 채워진 항목이 있으면 이 셀렉터 채택
        if data and sum(1 for d in data if d.get('name')) >= 1:
            items = data
            break

    if not items:
        result['error'] = '업체 목록 없음 (셀렉터 업데이트 필요)'
        return result

    organic = 0
    for d in items[:50]:
        name = d.get('name') or ''
        if not name:
            continue
        result['checked'] += 1
        if d.get('isAd'):
            continue
        organic += 1
        n = _norm(name)
        if n == target or (len(target) >= 2 and (target in n or n in target)):
            result['rank'] = organic
            result['is_exposed'] = organic <= 5
            return result

    return result


# ── 로드 + 순위 (목록 미로딩 시 1회 재시도) ──────────────────────────────────

async def _load_and_rank(page, url: str, target_name: str, base_wait: float) -> dict:
    last = {'rank': None, 'is_exposed': False, 'checked': 0, 'error': '페이지 로드 실패'}
    for attempt in range(2):
        try:
            await page.goto(url, wait_until='domcontentloaded', timeout=30000)
        except PlaywrightTimeout:
            last = {'rank': None, 'is_exposed': False, 'checked': 0, 'error': '페이지 로드 실패'}
            continue
        await asyncio.sleep(base_wait + attempt * 1.5 + random.uniform(0.5, 1.5))
        res = await _find_rank(page, target_name)
        # 순위를 찾았거나, 목록은 정상인데 해당 업체가 없을 뿐이면 즉시 반환
        if res['rank'] is not None or not res['error']:
            return res
        last = res
    return last


# ── PC 크롤러 ──────────────────────────────────────────────────────────────────

async def _check_pc(page, keyword: str, target_name: str) -> dict:
    url = f'https://pcmap.place.naver.com/place/list?query={quote(keyword)}&display=70'
    res = await _load_and_rank(page, url, target_name, base_wait=2.5)
    if res['error']:
        res['error'] = 'PC: ' + res['error']
    return res


# ── 모바일 크롤러 ─────────────────────────────────────────────────────────────

async def _check_mobile(page, keyword: str, target_name: str) -> dict:
    url = f'https://m.place.naver.com/place/list?query={quote(keyword)}&entry=pll'
    res = await _load_and_rank(page, url, target_name, base_wait=3.0)
    if res['error']:
        res['error'] = '모바일: ' + res['error']
    return res


# ── 메인 함수 ──────────────────────────────────────────────────────────────────

async def check_place_rank_both(keyword: str, place_id: str,
                                 place_name: str | None = None,
                                 headless: bool = True) -> dict:
    """
    PC와 모바일 순위를 확인합니다.

    place_name이 없으면 place_id로 1회 조회합니다.

    Returns:
        {
          'place_name': str|None,
          'pc':     {'rank': int|None, 'is_exposed': bool, 'error': str|None},
          'mobile': {'rank': int|None, 'is_exposed': bool, 'error': str|None},
        }
    """
    async with async_playwright() as p:
        # PC 브라우저
        pc_browser = await p.chromium.launch(headless=headless)
        pc_ctx = await pc_browser.new_context(
            user_agent=DESKTOP_UA,
            locale='ko-KR', timezone_id='Asia/Seoul',
            viewport={'width': 1280, 'height': 900},
        )
        pc_page = await pc_ctx.new_page()
        await pc_page.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )

        # 모바일 브라우저
        mb_browser = await p.chromium.launch(headless=headless)
        mb_ctx = await mb_browser.new_context(
            user_agent=MOBILE_UA,
            locale='ko-KR', timezone_id='Asia/Seoul',
            viewport={'width': 390, 'height': 844},
            is_mobile=True,
        )
        mb_page = await mb_ctx.new_page()
        await mb_page.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )

        try:
            # 업체명 확보 (없으면 PC 페이지로 조회)
            if not place_name:
                place_name = await _resolve_place_name(pc_page, place_id)

            if not place_name:
                err = {'rank': None, 'is_exposed': False, 'error': '업체명을 확인할 수 없음 (place_id 확인 필요)'}
                return {'place_name': None, 'pc': dict(err), 'mobile': dict(err)}

            pc_result, mb_result = await asyncio.gather(
                _check_pc(pc_page, keyword, place_name),
                _check_mobile(mb_page, keyword, place_name),
                return_exceptions=True
            )

            if isinstance(pc_result, Exception):
                pc_result = {'rank': None, 'is_exposed': False, 'error': f'PC: {pc_result}'}
            if isinstance(mb_result, Exception):
                mb_result = {'rank': None, 'is_exposed': False, 'error': f'모바일: {mb_result}'}

            return {'place_name': place_name, 'pc': pc_result, 'mobile': mb_result}

        finally:
            await pc_browser.close()
            await mb_browser.close()


def check_place_rank_sync(keyword: str, place_id: str,
                          place_name: str | None = None,
                          headless: bool = True) -> dict:
    """동기 래퍼"""
    return asyncio.run(check_place_rank_both(keyword, place_id, place_name, headless))
