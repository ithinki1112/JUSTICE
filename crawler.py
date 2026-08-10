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

from __future__ import annotations

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

# 작은 클라우드 컨테이너(Railway 등)에서 크롬이 죽지 않도록 하는 옵션.
#  - disable-dev-shm-usage: 컨테이너의 /dev/shm 이 작아 크래시하는 문제 방지 (핵심)
#  - no-sandbox: 컨테이너 root 실행 대응
#  (--single-process 는 Playwright 에서 크래시를 유발해 제외)
CHROMIUM_ARGS = [
    '--no-sandbox',
    '--disable-setuid-sandbox',
    '--disable-dev-shm-usage',
    '--disable-gpu',
    '--disable-extensions',
]

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

# 목록 스크롤 JS. top이면 맨 위로, 아니면 현재 로드된 끝까지 내려 다음 lazy-load를 유발.
# (매 라운드마다 읽어서 누적하므로, 끝까지 내려 상위 항목이 재활용돼도 데이터 손실 없음)
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
                         step: int = 600, sleep: float = 0.7,
                         stable_max: int = 12, max_rounds: int = 60):
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


# pcmap 페이지의 Apollo 캐시(window.__APOLLO_STATE__)에서 자연순위 목록을 읽는다.
# ROOT_QUERY.placeList(...).businesses.items 에 display 수만큼(최대 70) 순서대로 담겨 있어,
# 스크롤/가상화 없이 정확한 순위를 얻는다. (광고는 별도 adBusinesses 라 자동 제외됨)
_APOLLO_JS = """
(targetNorm) => {
  const norm = s => (s || '').replace(/\\s+/g, '').toLowerCase();
  const st = window.__APOLLO_STATE__ || {};
  const root = st.ROOT_QUERY || {};
  const plKey = Object.keys(root).find(k => k.indexOf('placeList(') === 0);
  if (!plKey || !root[plKey]) return { ok: false };
  const items = ((root[plKey].businesses || {}).items) || [];
  const names = [];
  for (const it of items) {
    const ref = it && it.__ref;
    const n = ref && st[ref] ? st[ref].name : (it && it.name);
    if (n) names.push(n);
  }
  let rank = null;
  if (targetNorm.length >= 2) {
    for (let i = 0; i < names.length; i++) {
      if (norm(names[i]) === targetNorm) { rank = i + 1; break; }
    }
  }
  return { ok: true, total: names.length, rank };
}
"""


async def _find_rank(page_or_frame, target_name: str) -> dict:
    """pcmap Apollo 캐시에서 target_name의 자연순위를 읽습니다 (스크롤 불필요)."""
    result = {'rank': None, 'is_exposed': False, 'checked': 0, 'error': None}
    target = _norm(target_name)

    # placeList 캐시가 채워질 때까지 대기
    try:
        await page_or_frame.wait_for_function(
            "() => { const r=(window.__APOLLO_STATE__||{}).ROOT_QUERY||{};"
            " return Object.keys(r).some(k => k.indexOf('placeList(')===0"
            " && r[k] && r[k].businesses && (r[k].businesses.items||[]).length); }",
            timeout=12000)
    except PlaywrightTimeout:
        result['error'] = '업체 목록 없음 (데이터 로드 실패)'
        return result

    try:
        data = await page_or_frame.evaluate(_APOLLO_JS, target)
    except Exception as e:
        result['error'] = f'목록 파싱 실패: {e}'
        return result

    if not data or not data.get('ok'):
        result['error'] = '업체 목록 파싱 실패'
        return result

    result['checked'] = data.get('total', 0)
    if data.get('rank'):
        result['rank'] = data['rank']
        result['is_exposed'] = data['rank'] <= 5
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
    PC(pcmap) 기준 현재 순위를 확인합니다. (모바일은 PC와 순위가 달라 혼란만 주므로 사용 안 함)

    place_name/place_x/place_y가 없으면 place_id로 1회 조회합니다.
    좌표가 있으면 업체 위치 기준으로 검색해 지역 순위를 일관되게 측정합니다.

    Returns:
        {
          'place_name': str|None, 'place_x': str|None, 'place_y': str|None,
          'pc':     {'rank': int|None, 'is_exposed': bool, 'error': str|None},
          'mobile': {'rank': None, ...},   # 하위 호환용(사용 안 함)
        }
    """
    none_mb = {'rank': None, 'is_exposed': False, 'error': None}
    async with async_playwright() as p:
        pc_browser = await p.chromium.launch(headless=headless, args=CHROMIUM_ARGS)
        pc_ctx = await pc_browser.new_context(
            user_agent=DESKTOP_UA,
            locale='ko-KR', timezone_id='Asia/Seoul',
            viewport={'width': 1280, 'height': 900},
        )
        pc_page = await pc_ctx.new_page()
        await pc_page.add_init_script(
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
                        'pc': dict(err), 'mobile': dict(none_mb)}

            try:
                pc_result = await _check_pc(pc_page, keyword, place_name, place_x, place_y)
            except Exception as e:
                pc_result = {'rank': None, 'is_exposed': False, 'error': f'PC: {e}'}

            return {'place_name': place_name, 'place_x': place_x, 'place_y': place_y,
                    'pc': pc_result, 'mobile': dict(none_mb)}

        finally:
            await pc_browser.close()


def check_place_rank_sync(keyword: str, place_id: str,
                          place_name: str | None = None,
                          place_x: str | None = None,
                          place_y: str | None = None,
                          headless: bool = True) -> dict:
    """동기 래퍼"""
    return asyncio.run(
        check_place_rank_both(keyword, place_id, place_name, place_x, place_y, headless)
    )
