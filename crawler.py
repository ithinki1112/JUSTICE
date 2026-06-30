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

DESKTOP_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
MOBILE_UA  = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1'

# 항목별 {광고여부, 업체명일치여부, 표시이름}을 한 번에 계산하는 JS.
#
# 네이버는 검색 페이지마다 업체명 CSS 클래스를 다르게 준다(.YwYLL, .q2LdB ...).
# 따라서 클래스에 의존하지 않고, 항목 내부에 "업체명과 정확히 일치하는 텍스트(leaf)"가
# 있는지로 매칭한다(부분일치로 동명 다른 지점을 잘못 잡는 문제도 방지).
#
# 항목별 {업체명, 광고여부, 일치여부}를 계산하는 JS.
# 광고 마커: 항목 안에 정확히 "광고" 텍스트만 가진 leaf 요소(span.place_blind 등).
# name: 가상 스크롤 누적 수집 시 중복 제거 식별자 (클래스 후보 → 없으면 첫 줄).
_NAME_SELS = ['.YwYLL', '.q2LdB', '.TYaxT', '.place_bluelink', '.CMy2_', '.tit']
_COLLECT_JS = """
(els, args) => {
  const targetNorm = args.t;
  const NSEL = args.nsel;
  const norm = s => (s || '').replace(/\\s+/g, '').toLowerCase();
  return els.map(el => {
    const leaves = Array.from(el.querySelectorAll('*')).filter(e => e.children.length === 0);
    const isAd = leaves.some(e => e.textContent.trim() === '광고');
    let isMatch = false;
    if (targetNorm.length >= 2) {
      for (const e of leaves) {
        if (norm(e.textContent) === targetNorm) { isMatch = true; break; }
      }
    }
    let name = '';
    for (const s of NSEL) { const n = el.querySelector(s); if (n && n.textContent.trim()) { name = n.textContent.trim(); break; } }
    if (!name) name = (el.innerText || '').split(String.fromCharCode(10))[0].trim();
    return { name, isAd, isMatch };
  });
}
"""

# 목록 스크롤 컨테이너를 이동시키는 JS (위로 / 아래로 step px)
_SCROLL_JS = """
(arg) => {
  const c = document.querySelector('#_pcmap_list_scroll_container')
         || document.querySelector('#_list_scroll_container')
         || (document.querySelector('.Ryr1F') ? document.querySelector('.Ryr1F').parentElement : null);
  if (arg.top) { if (c) c.scrollTop = 0; else window.scrollTo(0, 0); return; }
  if (c) c.scrollTop += arg.step; else window.scrollBy(0, arg.step);
}
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

async def _resolve_place_info(page, place_id: str):
    """place_id로 플레이스 상세 페이지를 열어 (업체명, x, y)를 얻습니다."""
    name, x, y = None, None, None
    url = f'https://m.place.naver.com/place/{place_id}/home'
    try:
        await page.goto(url, wait_until='domcontentloaded', timeout=20000)
        await asyncio.sleep(1.5)
    except PlaywrightTimeout:
        return name, x, y

    # 업체명: og:title 메타가 가장 안정적
    try:
        og = await page.get_attribute('meta[property="og:title"]', 'content')
        if og and og.strip():
            name = re.split(r'\s*[:|]\s*', og.strip())[0].strip()
    except Exception:
        pass
    if not name:
        for sel in ['.GHAhO', '#_title span', '.Fc1rA', 'h2 span', '.YwYLL']:
            try:
                el = await page.query_selector(sel)
                if el:
                    t = (await el.text_content() or '').strip()
                    if t:
                        name = t
                        break
            except Exception:
                continue

    # 좌표: 페이지에 박힌 경위도 추출 (검색을 업체 위치 기준으로 하기 위함)
    try:
        html = await page.content()
        mx = re.search(r'"(?:x|lng|longitude)"\s*:\s*"?(1[0-9]{2}\.[0-9]{4,})', html)
        my = re.search(r'"(?:y|lat|latitude)"\s*:\s*"?(3[0-9]\.[0-9]{4,})', html)
        if mx and my:
            x, y = mx.group(1), my.group(1)
    except Exception:
        pass

    return name, x, y


# ── 목록에서 순위 찾기 ─────────────────────────────────────────────────────────

async def _collect_items(page_or_frame, item_sel: str, target: str,
                         step: int = 350, sleep: float = 0.7,
                         stable_max: int = 5, max_rounds: int = 40):
    """가상 스크롤 목록을 위에서부터 조금씩 내리며 항목을 순서대로 누적합니다.
       (끝까지 한 번에 내리면 상위 항목이 DOM에서 제거되므로, 조금씩 내리며 모아야 함)
       업체명+광고여부로 중복 제거. 우리 업체를 찾으면 즉시 중단."""
    try:
        await page_or_frame.evaluate(_SCROLL_JS, {'top': True, 'step': 0})
        await asyncio.sleep(0.4)
    except Exception:
        pass

    seen, keys, stable = [], set(), 0
    arg = {'t': target, 'nsel': _NAME_SELS}
    for _ in range(max_rounds):
        try:
            batch = await page_or_frame.eval_on_selector_all(item_sel, _COLLECT_JS, arg)
        except Exception:
            batch = []
        grew = False
        for d in batch:
            name = d.get('name') or ''
            k = name + ('|A' if d.get('isAd') else '|O')
            if name and k not in keys:
                keys.add(k)
                seen.append(d)
                grew = True
        if any(d.get('isMatch') for d in seen):
            break
        stable = 0 if grew else stable + 1
        if stable >= stable_max:
            break
        try:
            await page_or_frame.evaluate(_SCROLL_JS, {'top': False, 'step': step})
        except Exception:
            break
        await asyncio.sleep(sleep)
    return seen


async def _find_rank(page_or_frame, target_name: str) -> dict:
    """현재 페이지/프레임의 검색 목록에서 target_name의 자연 순위를 찾습니다."""
    result = {'rank': None, 'is_exposed': False, 'checked': 0, 'error': None}
    target = _norm(target_name)

    # 목록 항목이 하나라도 뜰 때까지 대기
    try:
        await page_or_frame.wait_for_selector(', '.join(ITEM_SELECTORS), timeout=8000)
    except PlaywrightTimeout:
        result['error'] = '업체 목록 없음 (셀렉터 업데이트 필요)'
        return result

    # 항목이 잡히는 셀렉터 선택
    item_sel = None
    for sel in ITEM_SELECTORS:
        try:
            if await page_or_frame.eval_on_selector_all(sel, '(els) => els.length'):
                item_sel = sel
                break
        except Exception:
            continue
    if not item_sel:
        result['error'] = '업체 목록 없음 (셀렉터 업데이트 필요)'
        return result

    # 스크롤하며 항목을 순서대로 누적 (하위 순위까지 정확히)
    items = await _collect_items(page_or_frame, item_sel, target)

    # 광고 슬롯만 제외하고, 네이버가 표시하는 자연순위 위치 그대로 카운트
    organic = 0
    for d in items:
        result['checked'] += 1
        if d.get('isAd'):
            continue
        organic += 1
        if d.get('isMatch'):
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


# 좌표를 쿼리스트링으로 (업체 위치 기준 지역 순위)
def _xy(x, y):
    return f'&x={x}&y={y}' if (x and y) else ''


# ── PC 크롤러 ──────────────────────────────────────────────────────────────────

async def _check_pc(page, keyword: str, target_name: str, x=None, y=None) -> dict:
    url = f'https://pcmap.place.naver.com/place/list?query={quote(keyword)}{_xy(x, y)}&display=70'
    res = await _load_and_rank(page, url, target_name, base_wait=2.5)
    if res['error']:
        res['error'] = 'PC: ' + res['error']
    return res


# ── 모바일 크롤러 ─────────────────────────────────────────────────────────────

async def _check_mobile(page, keyword: str, target_name: str, x=None, y=None) -> dict:
    url = f'https://m.place.naver.com/place/list?query={quote(keyword)}{_xy(x, y)}&entry=pll'
    res = await _load_and_rank(page, url, target_name, base_wait=3.0)
    if res['error']:
        res['error'] = '모바일: ' + res['error']
    return res


# ── 메인 함수 ──────────────────────────────────────────────────────────────────

async def check_place_rank_both(keyword: str, place_id: str,
                                 place_name: str | None = None,
                                 place_x: str | None = None,
                                 place_y: str | None = None,
                                 headless: bool = True) -> dict:
    """
    PC와 모바일 순위를 확인합니다.

    place_name/place_x/place_y가 없으면 place_id로 1회 조회합니다.
    좌표가 있으면 업체 위치 기준으로 검색해 지역 순위를 일관되게 측정합니다.

    Returns:
        {
          'place_name': str|None, 'place_x': str|None, 'place_y': str|None,
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
            # 업체명/좌표 확보 (없으면 place_id로 조회)
            if not place_name or not (place_x and place_y):
                r_name, r_x, r_y = await _resolve_place_info(pc_page, place_id)
                place_name = place_name or r_name
                if not (place_x and place_y):
                    place_x, place_y = r_x, r_y

            if not place_name:
                err = {'rank': None, 'is_exposed': False, 'error': '업체명을 확인할 수 없음 (place_id 확인 필요)'}
                return {'place_name': None, 'place_x': None, 'place_y': None,
                        'pc': dict(err), 'mobile': dict(err)}

            pc_result, mb_result = await asyncio.gather(
                _check_pc(pc_page, keyword, place_name, place_x, place_y),
                _check_mobile(mb_page, keyword, place_name, place_x, place_y),
                return_exceptions=True
            )

            if isinstance(pc_result, Exception):
                pc_result = {'rank': None, 'is_exposed': False, 'error': f'PC: {pc_result}'}
            if isinstance(mb_result, Exception):
                mb_result = {'rank': None, 'is_exposed': False, 'error': f'모바일: {mb_result}'}

            return {'place_name': place_name, 'place_x': place_x, 'place_y': place_y,
                    'pc': pc_result, 'mobile': mb_result}

        finally:
            await pc_browser.close()
            await mb_browser.close()


def check_place_rank_sync(keyword: str, place_id: str,
                          place_name: str | None = None,
                          place_x: str | None = None,
                          place_y: str | None = None,
                          headless: bool = True) -> dict:
    """동기 래퍼"""
    return asyncio.run(
        check_place_rank_both(keyword, place_id, place_name, place_x, place_y, headless)
    )
