import threading
from datetime import date
from flask import Flask, render_template, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

from database import (
    init_db, create_client, get_clients, get_client, delete_client,
    create_keyword, get_keywords_by_client, delete_keyword,
    get_all_active_keywords, record_tracking, get_tracking_logs,
    already_checked_today, get_notifications, mark_notification_read,
    mark_all_notifications_read, get_unread_count, get_dashboard_data,
    set_manual_days, update_client_place_name
)
from crawler import check_place_rank_sync, extract_place_id

app = Flask(__name__)

# 동시에 여러 크롤링이 실행되지 않도록 락 사용
crawl_lock = threading.Lock()
crawl_status = {'running': False, 'last_run': None, 'last_result': ''}


# ── 크롤링 공통 ────────────────────────────────────────────────────────────────

def crawl_and_record(kw):
    """키워드 1개를 크롤링하고 결과를 기록합니다. (result, completed) 반환."""
    result = check_place_rank_sync(kw['keyword'], kw['place_id'], kw.get('place_name'))
    # 처음 확인된 업체명은 캐시 (이후 검색 목록에서 이름으로 매칭)
    if result.get('place_name') and not kw.get('place_name'):
        update_client_place_name(kw['client_id'], result['place_name'])
    today = date.today().isoformat()
    pc = result.get('pc', {})
    mb = result.get('mobile', {})
    completed = record_tracking(
        kw['id'], today,
        pc.get('rank'), pc.get('is_exposed', False),
        mb.get('rank'), mb.get('is_exposed', False),
    )
    return result, completed


# ── 스케줄러 ──────────────────────────────────────────────────────────────────

def run_daily_check():
    """매일 오전 9시 자동 순위 체크"""
    if crawl_lock.locked():
        return
    with crawl_lock:
        crawl_status['running'] = True
        crawl_status['last_result'] = ''
        keywords = get_all_active_keywords()
        results = []
        for kw in keywords:
            if already_checked_today(kw['id']):
                results.append(f"[SKIP] {kw['client_name']} / {kw['keyword']} - 오늘 이미 체크됨")
                continue
            result, completed = crawl_and_record(kw)
            pc = result.get('pc', {})
            mb = result.get('mobile', {})
            pc_str = f"PC {pc.get('rank')}위" if pc.get('rank') else 'PC 미노출'
            mb_str = f"모바일 {mb.get('rank')}위" if mb.get('rank') else '모바일 미노출'
            flag = ' ★결제요청!' if completed else ''
            results.append(f"[OK] {kw['client_name']} / {kw['keyword']} → {pc_str} / {mb_str}{flag}")
        crawl_status['last_run'] = date.today().isoformat()
        crawl_status['last_result'] = '\n'.join(results) if results else '대상 없음'
        crawl_status['running'] = False


scheduler = BackgroundScheduler(timezone='Asia/Seoul')
scheduler.add_job(run_daily_check, 'cron', hour=9, minute=0, id='daily_check')
scheduler.start()


# ── HTML ──────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


# ── API: 대시보드 ──────────────────────────────────────────────────────────────

@app.route('/api/dashboard')
def api_dashboard():
    return jsonify(get_dashboard_data())


# ── API: 업체 ──────────────────────────────────────────────────────────────────

@app.route('/api/clients', methods=['GET'])
def api_get_clients():
    return jsonify(get_clients())


@app.route('/api/clients', methods=['POST'])
def api_create_client():
    data = request.json
    name = (data.get('name') or '').strip()
    place_url = (data.get('place_url') or '').strip()
    memo = (data.get('memo') or '').strip()

    if not name or not place_url:
        return jsonify({'error': '업체명과 네이버 플레이스 URL을 입력하세요'}), 400

    place_id = extract_place_id(place_url)
    if not place_id:
        return jsonify({'error': '네이버 플레이스 URL에서 업체 ID를 추출할 수 없습니다'}), 400

    try:
        client_id = create_client(name, place_url, place_id, memo)
        return jsonify({'id': client_id, 'place_id': place_id}), 201
    except Exception as e:
        if 'UNIQUE' in str(e):
            return jsonify({'error': '이미 등록된 URL입니다'}), 409
        return jsonify({'error': str(e)}), 500


@app.route('/api/clients/<int:client_id>', methods=['DELETE'])
def api_delete_client(client_id):
    delete_client(client_id)
    return jsonify({'ok': True})


# ── API: 키워드 ────────────────────────────────────────────────────────────────

@app.route('/api/clients/<int:client_id>/keywords', methods=['GET'])
def api_get_keywords(client_id):
    return jsonify(get_keywords_by_client(client_id))


@app.route('/api/clients/<int:client_id>/keywords', methods=['POST'])
def api_create_keyword(client_id):
    data = request.json
    keyword = (data.get('keyword') or '').strip()
    if not keyword:
        return jsonify({'error': '검색어를 입력하세요'}), 400
    if not get_client(client_id):
        return jsonify({'error': '업체를 찾을 수 없습니다'}), 404
    try:
        kw_id = create_keyword(client_id, keyword)
        return jsonify({'id': kw_id}), 201
    except Exception as e:
        if 'UNIQUE' in str(e):
            return jsonify({'error': '이미 등록된 검색어입니다'}), 409
        return jsonify({'error': str(e)}), 500


@app.route('/api/keywords/<int:keyword_id>', methods=['DELETE'])
def api_delete_keyword(keyword_id):
    delete_keyword(keyword_id)
    return jsonify({'ok': True})


@app.route('/api/keywords/<int:keyword_id>/logs')
def api_keyword_logs(keyword_id):
    logs = get_tracking_logs(keyword_id)
    return jsonify(logs)


@app.route('/api/keywords/<int:keyword_id>/manual-days', methods=['POST'])
def api_set_manual_days(keyword_id):
    """수기 시작 노출일 설정 (구글시트 등에서 옮겨적기). 이후 자동 체크가 누적됨."""
    data = request.json or {}
    try:
        days = int(data.get('days'))
    except (TypeError, ValueError):
        return jsonify({'error': '노출일은 숫자로 입력하세요'}), 400
    if days < 0:
        return jsonify({'error': '0 이상의 값을 입력하세요'}), 400
    completed = set_manual_days(keyword_id, days)
    return jsonify({'ok': True, 'completed': completed})


# ── API: 크롤링 ────────────────────────────────────────────────────────────────

@app.route('/api/check', methods=['POST'])
def api_manual_check():
    """수동 즉시 체크"""
    if crawl_status['running']:
        return jsonify({'error': '이미 체크 중입니다. 잠시 후 다시 시도하세요'}), 429
    thread = threading.Thread(target=run_daily_check, daemon=True)
    thread.start()
    return jsonify({'ok': True, 'message': '순위 체크를 시작했습니다'})


@app.route('/api/check/status')
def api_check_status():
    return jsonify(crawl_status)


@app.route('/api/check/single', methods=['POST'])
def api_check_single():
    """특정 키워드 1개만 즉시 체크"""
    data = request.json
    keyword_id = data.get('keyword_id')
    if not keyword_id:
        return jsonify({'error': 'keyword_id 필요'}), 400

    keywords = get_all_active_keywords()
    kw = next((k for k in keywords if k['id'] == keyword_id), None)
    if not kw:
        return jsonify({'error': '키워드를 찾을 수 없습니다'}), 404

    result, completed = crawl_and_record(kw)
    pc = result.get('pc', {})
    # 단일 체크 응답: 더 잘 노출된 쪽(PC/모바일)을 대표값으로
    mb = result.get('mobile', {})
    best = pc if (pc.get('rank') or 999) <= (mb.get('rank') or 999) else mb
    result['rank'] = best.get('rank')
    result['is_exposed'] = bool(pc.get('is_exposed') or mb.get('is_exposed'))
    # 양쪽 다 순위를 못 찾았고 오류가 있으면 대표 오류 노출
    if result['rank'] is None:
        result['error'] = pc.get('error') or mb.get('error')
    result['completed'] = completed
    result['keyword'] = kw['keyword']
    return jsonify(result)


# ── API: 알림 ──────────────────────────────────────────────────────────────────

@app.route('/api/notifications')
def api_notifications():
    unread_only = request.args.get('unread_only', 'false').lower() == 'true'
    return jsonify(get_notifications(unread_only))


@app.route('/api/notifications/count')
def api_notification_count():
    return jsonify({'count': get_unread_count()})


@app.route('/api/notifications/<int:noti_id>/read', methods=['POST'])
def api_mark_read(noti_id):
    mark_notification_read(noti_id)
    return jsonify({'ok': True})


@app.route('/api/notifications/read-all', methods=['POST'])
def api_mark_all_read():
    mark_all_notifications_read()
    return jsonify({'ok': True})


# ── 테스트: URL에서 place_id 추출 ──────────────────────────────────────────────

@app.route('/api/parse-url', methods=['POST'])
def api_parse_url():
    url = (request.json.get('url') or '').strip()
    place_id = extract_place_id(url)
    if place_id:
        return jsonify({'place_id': place_id})
    return jsonify({'error': 'place_id를 추출할 수 없습니다'}), 400


if __name__ == '__main__':
    init_db()
    app.run(debug=True, port=5000, use_reloader=False)
