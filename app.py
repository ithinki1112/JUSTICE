import os
import threading
from datetime import date, timedelta
from flask import (
    Flask, render_template, render_template_string, request, jsonify,
    session, redirect, url_for
)
from apscheduler.schedulers.background import BackgroundScheduler

from database import (
    init_db, create_client, get_clients, get_client, delete_client,
    create_keyword, get_keywords_by_client, delete_keyword,
    get_all_active_keywords, record_tracking, get_tracking_logs,
    already_checked_today, get_notifications, mark_notification_read,
    mark_all_notifications_read, get_unread_count, get_dashboard_data,
    set_manual_days, update_client_place_info, mark_payment_complete
)
from crawler import check_place_rank_sync, extract_place_id

app = Flask(__name__)

# ── 인증/세션 설정 ──────────────────────────────────────────────────────────────
# 운영 시 환경변수로 반드시 설정: SECRET_KEY(무작위 문자열), APP_PASSWORD(공용 비밀번호)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-change-me')
app.permanent_session_lifetime = timedelta(days=30)
APP_PASSWORD = os.environ.get('APP_PASSWORD', 'justice1234')

# 동시에 여러 크롤링이 실행되지 않도록 락 사용
crawl_lock = threading.Lock()
crawl_status = {'running': False, 'last_run': None, 'last_result': ''}


# ── 크롤링 공통 ────────────────────────────────────────────────────────────────

def crawl_and_record(kw):
    """키워드 1개를 크롤링하고 결과를 기록합니다. (result, completed) 반환."""
    result = check_place_rank_sync(
        kw['keyword'], kw['place_id'],
        kw.get('place_name'), kw.get('place_x'), kw.get('place_y')
    )
    # 처음 확인된 업체명/좌표는 캐시 (이후 검색 목록 이름 매칭 + 업체 위치 기준 조회)
    if result.get('place_name') and (not kw.get('place_name') or not kw.get('place_x')):
        update_client_place_info(
            kw['client_id'], result.get('place_name'),
            result.get('place_x'), result.get('place_y')
        )
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
            rank = pc.get('rank')
            rank_str = f"{rank}위" if rank else '미노출'
            flag = ' ★결제요청!' if completed else ''
            results.append(f"[OK] {kw['client_name']} / {kw['keyword']} → 현재 {rank_str}{flag}")
        crawl_status['last_run'] = date.today().isoformat()
        crawl_status['last_result'] = '\n'.join(results) if results else '대상 없음'
        crawl_status['running'] = False


# DB 초기화/마이그레이션 — gunicorn 등 import 시점에도 실행되도록 모듈 레벨에서 호출
init_db()

scheduler = BackgroundScheduler(timezone='Asia/Seoul')
scheduler.add_job(run_daily_check, 'cron', hour=9, minute=0, id='daily_check')
scheduler.start()


# ── 인증 (공용 비밀번호 로그인) ─────────────────────────────────────────────────

LOGIN_HTML = """
<!DOCTYPE html><html lang="ko"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>JUSTICE 로그인</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
 body{background:#f4f6f9;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;font-family:'Segoe UI',sans-serif}
 .login-card{background:#fff;border-radius:14px;box-shadow:0 4px 20px rgba(0,0,0,.08);padding:2.5rem;width:340px;border-top:4px solid #03C75A}
 .brand{color:#03C75A;font-weight:800;font-size:1.5rem;text-align:center;margin-bottom:.25rem}
 .sub{color:#999;font-size:.85rem;text-align:center;margin-bottom:1.5rem}
 .btn-naver{background:#03C75A;color:#fff;border:none;width:100%}
 .btn-naver:hover{background:#028a40;color:#fff}
</style></head><body>
 <div class="login-card">
   <div class="brand"><i class="bi"></i>JUSTICE</div>
   <div class="sub">네이버 플레이스 순위 관리</div>
   {% if error %}<div class="alert alert-danger py-2 small">{{ error }}</div>{% endif %}
   <form method="post">
     <input type="password" name="password" class="form-control mb-3" placeholder="비밀번호" autofocus required>
     <button class="btn btn-naver py-2" type="submit">로그인</button>
   </form>
 </div>
</body></html>
"""


@app.before_request
def require_login():
    # 로그인 화면과 정적 파일은 인증 예외
    if request.endpoint in ('login', 'static', 'healthz'):
        return
    if not session.get('authed'):
        if request.path.startswith('/api/'):
            return jsonify({'error': '로그인이 필요합니다', 'auth': False}), 401
        return redirect(url_for('login'))


@app.route('/healthz')
def healthz():
    """배포 플랫폼 헬스체크용 (로그인 불필요, 항상 200)."""
    return 'ok', 200


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if (request.form.get('password') or '') == APP_PASSWORD:
            session.permanent = True
            session['authed'] = True
            return redirect(url_for('index'))
        return render_template_string(LOGIN_HTML, error='비밀번호가 올바르지 않습니다'), 401
    if session.get('authed'):
        return redirect(url_for('index'))
    return render_template_string(LOGIN_HTML, error=None)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


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


@app.route('/api/keywords/<int:keyword_id>/payment-complete', methods=['POST'])
def api_payment_complete(keyword_id):
    """결제 완료 처리 — 깜빡이는 결제 대기 상태 해제."""
    mark_payment_complete(keyword_id)
    return jsonify({'ok': True})


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
    mb = result.get('mobile', {})
    # 대표 순위/노출 판정 모두 PC 우선 (PC 미확인 시에만 모바일)
    if pc.get('rank') is not None:
        result['rank'] = pc.get('rank')
        result['is_exposed'] = bool(pc.get('is_exposed'))
    else:
        result['rank'] = mb.get('rank')
        result['is_exposed'] = bool(mb.get('is_exposed'))
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
    # 로컬 실행용. 운영(클라우드)에서는 gunicorn으로 app:app 을 구동합니다.
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', '1') == '1'
    app.run(host='0.0.0.0', debug=debug, port=port, use_reloader=False)
