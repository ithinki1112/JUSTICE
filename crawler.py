"""
네이버 플레이스 순위 크롤러 (PC + 모바일)
- PC:     map.naver.com/p/search/{keyword}  (searchIframe)
- 모바일: m.place.naver.com/place/list?query={keyword}

광고(플레이스 광고) 제외한 자연 순위만 카운트합니다.
place_id로 업체를 식별합니다.

※ 네이버는 CSS 클래스명을 자주 변경합니다.
  크롤링이 안 되면 SELECTORS를 업데이트하세요.
"""

import asyncio
import random
import re
from urllib.parse import quote
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ── 셀렉터 (네이버 업데이트 시 이 부분만 수정) ────────────────────────────────

PC_SELECTORS = {
    'search_iframe': 'iframe#searchIframe',
    'list_candidates': [
        'ul.F9vbC > li',
        'ul._listMenu > li',
        'ul[class*="list"] > li',
        '.Ryr1F li',
    ],
    'ad_badge_candidates': [
        '.ojuvJ', 'span[class*="ad"]', 'em[class*="ad"]',
        'span:has-text("광고")', 'em:has-text("광고")',
    ],
    'place_link': 'a[href*="/place/"], a[href*="entry/place"]',
}

MOBILE_SELECTORS = {
    'list_candidates': [
        'ul.place_list > li',
        'ul[class*="list"] > li',
        '.list_place > li',
        'li[class*="place"]',
        'li[data-id]',
    ],
    'ad_badge_candidates': [
        '.ad_icon', 'span[class*="ad"]', 'em[class*="ad"]',
        'span:has-text("광고")', 'em:has-text("광고")',
        '.AdBadge', '[class*="AdBadge"]',
    ],
    'place_link': 'a[href*="/place/"]',
}

DESKTOP_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
MOBILE_UA  = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1'


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


# ── 공통 유틸 ─────────────────────────────────────────────────────────────────

async def _is_ad(item, ad_selectors: list) -> bool:
    for sel in ad_selectors:
        try:
            badge = await item.query_selector(sel)
            if badge:
                return True
        except Exception:
            pass
    try:
        text = await item.inner_text()
        if '광고' in text[:80]:
            return True
    except Exception:
        pass
    return False


async def _match_place(item, place_id: str, link_sel: str) -> bool:
    try:
        link = await item.query_selector(link_sel)
        if link:
            href = await link.get_attribute('href') or ''
            if place_id in href:
                return True
    except Exception:
        pass
    try:
        item_id = await item.get_attribute('data-id') or ''
        if place_id in item_id:
            return True
    except Exception:
        pass
    return False


# ── PC 크롤러 ──────────────────────────────────────────────────────────────────

async def _check_pc(page, keyword: str, place_id: str) -> dict:
    result = {'rank': None, 'is_exposed': False, 'checked': 0, 'error': None}
    url = f'https://map.naver.com/p/search/{quote(keyword)}'
    await page.goto(url, wait_until='domcontentloaded', timeout=30000)
    await asyncio.sleep(random.uniform(2.0, 3.5))

    try:
        await page.wait_for_selector(PC_SELECTORS['search_iframe'], timeout=15000)
    except PlaywrightTimeout:
        result['error'] = 'PC: searchIframe 로드 실패'
        return result

    frame = page.frame(name='searchIframe')
    if not frame:
        for f in page.frames:
            if 'pcmap.place.naver.com' in f.url:
                frame = f
                break

    if not frame:
        result['error'] = 'PC: iframe을 찾을 수 없음'
        return result

    await asyncio.sleep(random.uniform(1.0, 2.0))

    items = []
    for sel in PC_SELECTORS['list_candidates']:
        try:
            await frame.wait_for_selector(sel, timeout=5000)
            items = await frame.query_selector_all(sel)
            if items:
                break
        except PlaywrightTimeout:
            continue

    if not items:
        result['error'] = 'PC: 업체 목록 없음 (셀렉터 업데이트 필요)'
        return result

    organic = 0
    for item in items[:30]:
        result['checked'] += 1
        if await _is_ad(item, PC_SELECTORS['ad_badge_candidates']):
            continue
        organic += 1
        if await _match_place(item, place_id, PC_SELECTORS['place_link']):
            result['rank'] = organic
            result['is_exposed'] = organic <= 5
            return result

    return result


# ── 모바일 크롤러 ─────────────────────────────────────────────────────────────

async def _check_mobile(page, keyword: str, place_id: str) -> dict:
    result = {'rank': None, 'is_exposed': False, 'checked': 0, 'error': None}
    url = f'https://m.place.naver.com/place/list?query={quote(keyword)}'
    await page.goto(url, wait_until='domcontentloaded', timeout=30000)
    await asyncio.sleep(random.uniform(2.0, 3.5))

    items = []
    for sel in MOBILE_SELECTORS['list_candidates']:
        try:
            await page.wait_for_selector(sel, timeout=8000)
            items = await page.query_selector_all(sel)
            if items:
                break
        except PlaywrightTimeout:
            continue

    if not items:
        result['error'] = '모바일: 업체 목록 없음 (셀렉터 업데이트 필요)'
        return result

    organic = 0
    for item in items[:30]:
        result['checked'] += 1
        if await _is_ad(item, MOBILE_SELECTORS['ad_badge_candidates']):
            continue
        organic += 1
        if await _match_place(item, place_id, MOBILE_SELECTORS['place_link']):
            result['rank'] = organic
            result['is_exposed'] = organic <= 5
            return result

    return result


# ── 메인 함수 ──────────────────────────────────────────────────────────────────

async def check_place_rank_both(keyword: str, place_id: str, headless: bool = True) -> dict:
    """
    PC와 모바일 순위를 동시에 확인합니다.

    Returns:
        {
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
            viewport={'width': 1280, 'height': 800},
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
            pc_result, mb_result = await asyncio.gather(
                _check_pc(pc_page, keyword, place_id),
                _check_mobile(mb_page, keyword, place_id),
                return_exceptions=True
            )

            if isinstance(pc_result, Exception):
                pc_result = {'rank': None, 'is_exposed': False, 'error': str(pc_result)}
            if isinstance(mb_result, Exception):
                mb_result = {'rank': None, 'is_exposed': False, 'error': str(mb_result)}

            return {'pc': pc_result, 'mobile': mb_result}

        finally:
            await pc_browser.close()
            await mb_browser.close()


def check_place_rank_sync(keyword: str, place_id: str, headless: bool = True) -> dict:
    """동기 래퍼"""
    return asyncio.run(check_place_rank_both(keyword, place_id, headless))
