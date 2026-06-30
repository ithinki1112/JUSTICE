import os
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta

# 배포 시 영구 디스크 경로를 DB_PATH 환경변수로 지정 (예: /data/justice.db)
DB_PATH = os.environ.get('DB_PATH', 'justice.db')

# DB 파일이 들어갈 디렉터리 보장 (마운트 볼륨 등)
_db_dir = os.path.dirname(DB_PATH)
if _db_dir:
    os.makedirs(_db_dir, exist_ok=True)


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS clients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                place_url TEXT NOT NULL UNIQUE,
                place_id TEXT NOT NULL,
                memo TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS keywords (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                keyword TEXT NOT NULL,
                exposure_count INTEGER DEFAULT 0,
                goal_days INTEGER DEFAULT 25,
                is_complete INTEGER DEFAULT 0,
                started_at DATE,
                completed_at DATE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
                UNIQUE(client_id, keyword)
            );

            -- PC와 모바일 순위를 한 행에 기록 (날짜당 1행)
            CREATE TABLE IF NOT EXISTS tracking_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword_id INTEGER NOT NULL,
                check_date DATE NOT NULL,
                pc_rank INTEGER,
                pc_exposed INTEGER DEFAULT 0,
                mobile_rank INTEGER,
                mobile_exposed INTEGER DEFAULT 0,
                is_exposed INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (keyword_id) REFERENCES keywords(id) ON DELETE CASCADE,
                UNIQUE(keyword_id, check_date)
            );

            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                keyword_id INTEGER,
                type TEXT DEFAULT 'payment_request',
                message TEXT NOT NULL,
                is_read INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (client_id) REFERENCES clients(id)
            );
        ''')
        _migrate(conn)


def _column_exists(conn, table: str, col: str) -> bool:
    return any(r['name'] == col for r in conn.execute(f"PRAGMA table_info({table})"))


def _migrate(conn):
    """기존 DB에 누락된 컬럼을 추가합니다 (idempotent)."""
    # 업체명: place_id로 1회 조회해 캐시 (검색 목록에서 이름으로 매칭하기 위함)
    if not _column_exists(conn, 'clients', 'place_name'):
        conn.execute("ALTER TABLE clients ADD COLUMN place_name TEXT")
    # 업체 좌표: 검색 시 업체 위치 기준으로 조회해 순위를 일관되게 (네이버 지역 순위 대응)
    if not _column_exists(conn, 'clients', 'place_x'):
        conn.execute("ALTER TABLE clients ADD COLUMN place_x TEXT")
    if not _column_exists(conn, 'clients', 'place_y'):
        conn.execute("ALTER TABLE clients ADD COLUMN place_y TEXT")
    # 수기 노출일: 구글시트 등에서 옮겨적는 시작 누적 일수
    if not _column_exists(conn, 'keywords', 'manual_days'):
        conn.execute("ALTER TABLE keywords ADD COLUMN manual_days INTEGER DEFAULT 0")
    # 결제 사이클: 25일 보장 달성 → 결제대기, 카운트는 자동 재시작(다음 사이클)
    if not _column_exists(conn, 'keywords', 'payment_pending'):
        conn.execute("ALTER TABLE keywords ADD COLUMN payment_pending INTEGER DEFAULT 0")
    if not _column_exists(conn, 'keywords', 'cycle_count'):
        conn.execute("ALTER TABLE keywords ADD COLUMN cycle_count INTEGER DEFAULT 0")
    if not _column_exists(conn, 'keywords', 'cycle_start'):
        conn.execute("ALTER TABLE keywords ADD COLUMN cycle_start DATE")
    if not _column_exists(conn, 'keywords', 'last_paid_at'):
        conn.execute("ALTER TABLE keywords ADD COLUMN last_paid_at DATE")


# ── Client CRUD ──────────────────────────────────────────────────────────────

def create_client(name: str, place_url: str, place_id: str, memo: str = ''):
    with get_db() as conn:
        cur = conn.execute(
            'INSERT INTO clients (name, place_url, place_id, memo) VALUES (?,?,?,?)',
            (name, place_url, place_id, memo)
        )
        return cur.lastrowid


def get_clients():
    with get_db() as conn:
        rows = conn.execute('''
            SELECT c.*, COUNT(k.id) as keyword_count
            FROM clients c
            LEFT JOIN keywords k ON k.client_id = c.id
            GROUP BY c.id
            ORDER BY c.created_at DESC
        ''').fetchall()
        return [dict(r) for r in rows]


def get_client(client_id: int):
    with get_db() as conn:
        row = conn.execute('SELECT * FROM clients WHERE id=?', (client_id,)).fetchone()
        return dict(row) if row else None


def delete_client(client_id: int):
    with get_db() as conn:
        conn.execute('DELETE FROM clients WHERE id=?', (client_id,))


# ── Keyword CRUD ──────────────────────────────────────────────────────────────

def create_keyword(client_id: int, keyword: str):
    with get_db() as conn:
        cur = conn.execute(
            'INSERT INTO keywords (client_id, keyword, started_at) VALUES (?,?,?)',
            (client_id, keyword, date.today().isoformat())
        )
        return cur.lastrowid


def get_keywords_by_client(client_id: int):
    with get_db() as conn:
        rows = conn.execute(
            'SELECT * FROM keywords WHERE client_id=? ORDER BY created_at DESC',
            (client_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_active_keywords():
    """추적 대상 키워드 전체. 보장 달성 후에도 다음 사이클을 위해 계속 추적합니다."""
    with get_db() as conn:
        rows = conn.execute('''
            SELECT k.*, c.name as client_name, c.place_id, c.place_name,
                   c.place_x, c.place_y, c.place_url
            FROM keywords k
            JOIN clients c ON c.id = k.client_id
            ORDER BY k.id
        ''').fetchall()
        return [dict(r) for r in rows]


def delete_keyword(keyword_id: int):
    with get_db() as conn:
        conn.execute('DELETE FROM keywords WHERE id=?', (keyword_id,))


# ── Tracking ──────────────────────────────────────────────────────────────────

def _recompute_exposure(conn, keyword_id: int, as_of: str = None) -> bool:
    """
    현재 사이클의 노출일을 재계산합니다.
      exposure_count = (첫 사이클이면 manual_days) + 현재 사이클 기간의 노출일 수

    25일(goal_days) 달성 시:
      - 결제 대기(payment_pending=1) 설정 + 결제요청 알림 생성 + 보장 완료일 기록
      - 카운트를 자동 재시작 (다음 사이클): cycle_count +1, 다음 날부터 새로 카운트
    이번 호출로 새 사이클을 달성하면 True 반환.

    as_of: 달성/사이클 기준일 (기본 오늘). record_tracking은 체크 날짜를 넘깁니다.
    """
    as_of = as_of or date.today().isoformat()
    row = conn.execute(
        '''SELECT manual_days, goal_days, cycle_count, cycle_start, started_at, client_id
           FROM keywords WHERE id=?''',
        (keyword_id,)
    ).fetchone()
    if not row:
        return False

    cycle_start = row['cycle_start'] or row['started_at'] or '0000-01-01'
    seed = (row['manual_days'] or 0) if (row['cycle_count'] or 0) == 0 else 0
    auto = conn.execute(
        '''SELECT COUNT(*) AS c FROM tracking_logs
           WHERE keyword_id=? AND is_exposed=1 AND check_date >= ?''',
        (keyword_id, cycle_start)
    ).fetchone()['c']
    total = seed + auto

    if total >= row['goal_days']:
        # 보장 달성 → 결제 대기 + 다음 사이클 자동 시작
        next_start = (date.fromisoformat(as_of) + timedelta(days=1)).isoformat()
        new_cycle = (row['cycle_count'] or 0) + 1
        conn.execute(
            '''UPDATE keywords
               SET cycle_count=?, completed_at=?, payment_pending=1,
                   cycle_start=?, exposure_count=0, is_complete=1
               WHERE id=?''',
            (new_cycle, as_of, next_start, keyword_id)
        )
        kw = conn.execute('SELECT keyword FROM keywords WHERE id=?', (keyword_id,)).fetchone()
        suffix = f'{new_cycle}회차 ' if new_cycle > 1 else ''
        conn.execute('''
            INSERT INTO notifications (client_id, keyword_id, type, message)
            VALUES (?,?,?,?)
        ''', (
            row['client_id'], keyword_id, 'payment_request',
            f'키워드 [{kw["keyword"]}] {suffix}누적 {row["goal_days"]}일 노출 달성! 결제 요청 시점입니다.'
        ))
        return True

    conn.execute('UPDATE keywords SET exposure_count=? WHERE id=?', (total, keyword_id))
    return False


def record_tracking(keyword_id: int, check_date: str,
                    pc_rank, pc_exposed: bool,
                    mobile_rank, mobile_exposed: bool) -> bool:
    """
    하루치 PC+모바일 순위를 저장합니다.
    노출 판정은 PC 우선: PC 순위를 읽었으면 PC 기준(1~5위),
    PC를 읽지 못한 경우에만 모바일로 판정합니다.
    목표(기본 25일) 달성 시 True 반환.
    """
    if pc_rank is not None:
        is_exposed = 1 if pc_exposed else 0
    else:
        is_exposed = 1 if mobile_exposed else 0

    with get_db() as conn:
        conn.execute('''
            INSERT INTO tracking_logs
                (keyword_id, check_date, pc_rank, pc_exposed, mobile_rank, mobile_exposed, is_exposed)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(keyword_id, check_date) DO UPDATE SET
                pc_rank=excluded.pc_rank,
                pc_exposed=excluded.pc_exposed,
                mobile_rank=excluded.mobile_rank,
                mobile_exposed=excluded.mobile_exposed,
                is_exposed=excluded.is_exposed
        ''', (keyword_id, check_date,
              pc_rank, 1 if pc_exposed else 0,
              mobile_rank, 1 if mobile_exposed else 0,
              is_exposed))

        return _recompute_exposure(conn, keyword_id, as_of=check_date)


def set_manual_days(keyword_id: int, days: int) -> bool:
    """
    수기 시작 노출일을 설정합니다(구글시트 등에서 옮겨적기).
    이후 자동 체크된 노출일이 이 값 위에 누적됩니다.
    이번 설정으로 목표를 새로 달성하면 True 반환.
    """
    days = max(0, int(days))
    with get_db() as conn:
        conn.execute('UPDATE keywords SET manual_days=? WHERE id=?', (days, keyword_id))
        return _recompute_exposure(conn, keyword_id)


def mark_payment_complete(keyword_id: int):
    """결제 완료 처리 — 깜빡이는 결제 대기 상태를 해제하고 결제일을 기록합니다."""
    with get_db() as conn:
        conn.execute(
            'UPDATE keywords SET payment_pending=0, last_paid_at=? WHERE id=?',
            (date.today().isoformat(), keyword_id)
        )


def update_client_place_info(client_id: int, place_name: str = None,
                             place_x: str = None, place_y: str = None):
    """크롤링 중 확인된 업체명/좌표를 캐시합니다 (값이 있는 항목만 갱신)."""
    sets, vals = [], []
    if place_name:
        sets.append('place_name=?'); vals.append(place_name)
    if place_x and place_y:
        sets += ['place_x=?', 'place_y=?']; vals += [place_x, place_y]
    if not sets:
        return
    vals.append(client_id)
    with get_db() as conn:
        conn.execute(f'UPDATE clients SET {", ".join(sets)} WHERE id=?', vals)


def get_tracking_logs(keyword_id: int, limit: int = 30):
    with get_db() as conn:
        rows = conn.execute('''
            SELECT * FROM tracking_logs
            WHERE keyword_id=?
            ORDER BY check_date DESC
            LIMIT ?
        ''', (keyword_id, limit)).fetchall()
        return [dict(r) for r in rows]


def already_checked_today(keyword_id: int) -> bool:
    today = date.today().isoformat()
    with get_db() as conn:
        row = conn.execute(
            'SELECT id FROM tracking_logs WHERE keyword_id=? AND check_date=?',
            (keyword_id, today)
        ).fetchone()
        return row is not None


# ── Notifications ─────────────────────────────────────────────────────────────

def get_notifications(unread_only: bool = False):
    with get_db() as conn:
        q = '''
            SELECT n.*, c.name as client_name, k.keyword
            FROM notifications n
            JOIN clients c ON c.id = n.client_id
            LEFT JOIN keywords k ON k.id = n.keyword_id
        '''
        if unread_only:
            q += ' WHERE n.is_read=0'
        q += ' ORDER BY n.created_at DESC LIMIT 100'
        rows = conn.execute(q).fetchall()
        return [dict(r) for r in rows]


def mark_notification_read(notification_id: int):
    with get_db() as conn:
        conn.execute('UPDATE notifications SET is_read=1 WHERE id=?', (notification_id,))


def mark_all_notifications_read():
    with get_db() as conn:
        conn.execute('UPDATE notifications SET is_read=1')


def get_unread_count() -> int:
    with get_db() as conn:
        row = conn.execute('SELECT COUNT(*) as cnt FROM notifications WHERE is_read=0').fetchone()
        return row['cnt'] if row else 0


# ── Dashboard ─────────────────────────────────────────────────────────────────

def get_dashboard_data():
    with get_db() as conn:
        total_clients = conn.execute('SELECT COUNT(*) as c FROM clients').fetchone()['c']
        total_keywords = conn.execute('SELECT COUNT(*) as c FROM keywords').fetchone()['c']
        # 모든 키워드는 계속 추적됨 = 진행 중
        active_keywords = total_keywords
        # 결제 대기(보장 달성 후 결제완료 미처리) 키워드 수
        payment_pending = conn.execute('SELECT COUNT(*) as c FROM keywords WHERE payment_pending=1').fetchone()['c']
        unread_noti = conn.execute('SELECT COUNT(*) as c FROM notifications WHERE is_read=0').fetchone()['c']

        # 오늘 노출 현황
        today = date.today().isoformat()
        today_exposed = conn.execute(
            'SELECT COUNT(*) as c FROM tracking_logs WHERE check_date=? AND is_exposed=1', (today,)
        ).fetchone()['c']

        clients_rows = conn.execute('''
            SELECT c.id, c.name, c.place_url, c.place_id, c.place_name, c.memo, c.created_at,
                   k.id as kw_id, k.keyword, k.exposure_count, k.goal_days, k.manual_days,
                   k.is_complete, k.started_at, k.completed_at,
                   k.payment_pending, k.cycle_count, k.last_paid_at
            FROM clients c
            LEFT JOIN keywords k ON k.client_id = c.id
            ORDER BY c.created_at DESC, k.created_at DESC
        ''').fetchall()

        # 오늘 순위 조회
        today_logs = {}
        for row in conn.execute(
            'SELECT keyword_id, pc_rank, pc_exposed, mobile_rank, mobile_exposed FROM tracking_logs WHERE check_date=?',
            (today,)
        ).fetchall():
            today_logs[row['keyword_id']] = dict(row)

        clients_map = {}
        for r in clients_rows:
            cid = r['id']
            if cid not in clients_map:
                clients_map[cid] = {
                    'id': cid, 'name': r['name'], 'place_url': r['place_url'],
                    'place_id': r['place_id'], 'place_name': r['place_name'], 'memo': r['memo'],
                    'created_at': r['created_at'], 'keywords': []
                }
            if r['kw_id']:
                log = today_logs.get(r['kw_id'], {})
                clients_map[cid]['keywords'].append({
                    'id': r['kw_id'],
                    'keyword': r['keyword'],
                    'exposure_count': r['exposure_count'],
                    'goal_days': r['goal_days'],
                    'manual_days': r['manual_days'] or 0,
                    'is_complete': bool(r['is_complete']),
                    'started_at': r['started_at'],
                    'completed_at': r['completed_at'],
                    'payment_pending': bool(r['payment_pending']),
                    'cycle_count': r['cycle_count'] or 0,
                    'last_paid_at': r['last_paid_at'],
                    'progress': round(r['exposure_count'] / r['goal_days'] * 100, 1) if r['goal_days'] else 0,
                    'today_pc_rank': log.get('pc_rank'),
                    'today_pc_exposed': bool(log.get('pc_exposed')),
                    'today_mobile_rank': log.get('mobile_rank'),
                    'today_mobile_exposed': bool(log.get('mobile_exposed')),
                })

        return {
            'stats': {
                'total_clients': total_clients,
                'total_keywords': total_keywords,
                'active_keywords': active_keywords,
                'payment_pending': payment_pending,
                'unread_notifications': unread_noti,
                'today_exposed': today_exposed,
            },
            'clients': list(clients_map.values())
        }
