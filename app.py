import os
import json
import sqlite3
import base64
import uuid
import time
import requests
import shutil
from flask import Flask, request, jsonify, render_template, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, 'templates')
STATIC_DIR = os.path.join(BASE_DIR, 'static')

app = Flask(__name__, template_folder=TEMPLATES_DIR, static_folder=STATIC_DIR)

def get_db_path():
    if os.environ.get('VERCEL'):
        tmp_path = '/tmp/vtu_results.db'
        bundled_path = os.path.join(BASE_DIR, 'vtu_results.db')
        if not os.path.exists(tmp_path):
            try:
                if os.path.exists(bundled_path):
                    shutil.copyfile(bundled_path, tmp_path)
            except Exception as e:
                print("Error copying db to /tmp:", e)
        return tmp_path
    return os.path.join(BASE_DIR, 'vtu_results.db')

class VercelPathMiddleware:
    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        from urllib.parse import parse_qs
        qs = environ.get('QUERY_STRING', '')
        parsed = parse_qs(qs, keep_blank_values=True)
        if '__vercel_path' in parsed:
            raw_path = parsed['__vercel_path'][0]
            clean_path = '/' + raw_path.lstrip('/')
            environ['PATH_INFO'] = clean_path if clean_path else '/'
            # Clean up query string
            cleaned_params = [p for p in qs.split('&') if p and not p.startswith('__vercel_path=')]
            environ['QUERY_STRING'] = '&'.join(cleaned_params)
        elif environ.get('PATH_INFO') in ('/api/index.py', '/api/index', '/api', '/api/'):
            environ['PATH_INFO'] = '/'
        return self.wsgi_app(environ, start_response)

app.wsgi_app = VercelPathMiddleware(app.wsgi_app)

VTU_CODES = {
    'sem5_normal': 'D25J26Ecbcs',
    'sem5_reval': 'D25J26RVcbcs',
    'sem6_normal': 'MJ26cbcs',
    'sem6_reval': 'MJ26rvcbcs'
}

VTU_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Content-Type': 'application/json',
    'Origin': 'https://www.vtulife.in'
}

# In-memory storage for active captcha sessions
# session_id -> { "session": requests.Session, "usn": str, "result_code": str, "created_at": float }
ACTIVE_CAPTCHA_SESSIONS = {}

# Try optional ddddocr
DDDDOCR_AVAILABLE = False
ocr_instance = None
try:
    import ddddocr
    ocr_instance = ddddocr.DdddOcr(show_ad=False)
    DDDDOCR_AVAILABLE = True
except Exception:
    DDDDOCR_AVAILABLE = False

def init_db():
    try:
        conn = sqlite3.connect(get_db_path())
        cur = conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS students (
                usn TEXT PRIMARY KEY,
                name TEXT,
                branch TEXT,
                sem5_data TEXT,
                sem5_rv_data TEXT,
                sem6_data TEXT,
                sem6_rv_data TEXT,
                sem5_weighted_marks REAL,
                sem5_credits REAL,
                sem6_weighted_marks REAL,
                sem6_credits REAL,
                aggregate_percentage REAL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
        conn.close()
    except Exception as e:
        print("Warning: init_db error:", e)

try:
    init_db()
except Exception as e:
    pass

def parse_int(val, default=0):
    try:
        if val is None: return default
        return int(str(val).strip())
    except (ValueError, TypeError):
        return default

def compute_semester_stats(sem_normal_data, sem_reval_data):
    """
    Computes weighted total marks = sum(marks_obtained * credits),
    credits sum, raw marks sum, and subject list with revaluation updates.
    """
    if not sem_normal_data or not sem_normal_data.get('subjects'):
        return None
    
    normal_subjects = {s['code']: s for s in sem_normal_data.get('subjects', [])}
    reval_subjects = {s['code']: s for s in sem_reval_data.get('subjects', [])} if (sem_reval_data and sem_reval_data.get('subjects')) else {}

    combined_subjects = []
    total_weighted_marks = 0
    total_credits = 0
    total_raw_marks = 0
    has_reval_applied = False

    for code, s_norm in normal_subjects.items():
        s_rev = reval_subjects.get(code)
        
        credits = parse_int(s_norm.get('credits', 0))
        norm_total = parse_int(s_norm.get('total', 0))
        norm_internal = parse_int(s_norm.get('internal', 0))
        norm_external = parse_int(s_norm.get('external', 0))
        norm_result = s_norm.get('result', '-')

        final_total = norm_total
        final_internal = norm_internal
        final_external = norm_external
        final_result = norm_result
        reval_badge = None

        if s_rev:
            rev_total = parse_int(s_rev.get('total', 0))
            rev_internal = parse_int(s_rev.get('internal', 0))
            rev_external = parse_int(s_rev.get('external', 0))
            rev_result = s_rev.get('result', '-')
            
            # If revaluation score is higher
            if rev_total > norm_total:
                final_total = rev_total
                final_internal = rev_internal
                final_external = rev_external
                final_result = rev_result
                reval_badge = f"+{rev_total - norm_total} (RV)"
                has_reval_applied = True
            elif rev_total != norm_total:
                reval_badge = f"RV: {rev_total}"
            else:
                reval_badge = "RV: Same"

        # User's core formula:
        # Total marks = marks obtained in each subject * credits of each subject
        weighted_marks = final_total * credits

        total_weighted_marks += weighted_marks
        total_credits += credits
        total_raw_marks += final_total

        combined_subjects.append({
            'code': code,
            'name': s_norm.get('name', ''),
            'semester': s_norm.get('semester', ''),
            'credits': credits,
            'internal': final_internal,
            'external': final_external,
            'total': final_total,
            'result': final_result,
            'weighted_marks': weighted_marks,
            'reval_badge': reval_badge,
            'normal_total': norm_total
        })

    max_weighted_marks = total_credits * 100
    percentage = round(total_weighted_marks / total_credits, 2) if total_credits > 0 else 0.0

    return {
        'subjects': combined_subjects,
        'total_weighted_marks': total_weighted_marks,
        'total_credits': total_credits,
        'total_raw_marks': total_raw_marks,
        'max_weighted_marks': max_weighted_marks,
        'percentage': percentage,
        'has_reval_applied': has_reval_applied
    }

def compute_aggregate(sem5_stats, sem6_stats):
    if not sem5_stats and not sem6_stats:
        return None

    total_weighted = 0
    total_credits = 0
    total_raw = 0

    if sem5_stats:
        total_weighted += sem5_stats['total_weighted_marks']
        total_credits += sem5_stats['total_credits']
        total_raw += sem5_stats['total_raw_marks']

    if sem6_stats:
        total_weighted += sem6_stats['total_weighted_marks']
        total_credits += sem6_stats['total_credits']
        total_raw += sem6_stats['total_raw_marks']

    max_weighted = total_credits * 100
    aggregate_percentage = round(total_weighted / total_credits, 2) if total_credits > 0 else 0.0

    return {
        'total_weighted_marks': total_weighted,
        'total_credits': total_credits,
        'total_raw_marks': total_raw,
        'max_weighted_marks': max_weighted,
        'aggregate_percentage': aggregate_percentage
    }

def query_vtulife(session, usn, result_code, captcha=None):
    url = 'https://www.vtulife.in/api/vtu-results/result'
    payload = {
        'usn': usn,
        'result_code': result_code
    }
    if captcha:
        payload['captcha'] = captcha

    headers = dict(VTU_HEADERS)
    headers['Referer'] = f'https://www.vtulife.in/vtu-results/{result_code}'

    try:
        resp = session.post(url, json=payload, headers=headers, timeout=12)
        try:
            data = resp.json()
        except:
            data = {'error': resp.text}
        return resp.status_code, data
    except Exception as e:
        return 500, {'error': str(e)}

def auto_solve_captcha(img_b64):
    if not DDDDOCR_AVAILABLE or not ocr_instance:
        return None
    try:
        if ',' in img_b64:
            img_b64 = img_b64.split(',', 1)[1]
        img_bytes = base64.b64decode(img_b64)
        pred = ocr_instance.classification(img_bytes)
        clean_pred = ''.join(c for c in pred if c.isalnum()).strip()
        if len(clean_pred) == 6:
            return clean_pred
    except Exception as e:
        print("OCR error:", e)
    return None

def fetch_single_exam(usn, result_code, allow_captcha_prompt=True):
    """
    Fetches one result code for the USN.
    Returns:
    - ('OK', data)
    - ('CAPTCHA', {session_id, captcha_image, ...})
    - ('NOT_FOUND', msg)
    - ('ERROR', msg)
    """
    session = requests.Session()
    status_code, data = query_vtulife(session, usn, result_code)

    if status_code == 200:
        return 'OK', data

    if status_code == 428 and ('captcha_image' in data or data.get('requires_captcha')):
        img_b64 = data.get('captcha_image', '')

        # Attempt auto-solve if enabled
        solved = auto_solve_captcha(img_b64)
        if solved:
            st2, d2 = query_vtulife(session, usn, result_code, captcha=solved)
            if st2 == 200:
                return 'OK', d2

        if allow_captcha_prompt:
            sess_id = str(uuid.uuid4())
            ACTIVE_CAPTCHA_SESSIONS[sess_id] = {
                'session': session,
                'usn': usn,
                'result_code': result_code,
                'created_at': time.time()
            }
            return 'CAPTCHA', {
                'session_id': sess_id,
                'captcha_image': img_b64,
                'usn': usn,
                'result_code': result_code
            }

    if status_code == 404 or 'not found' in str(data.get('error', '')).lower():
        return 'NOT_FOUND', None

    return 'ERROR', data.get('error', f'HTTP {status_code}')

def get_student_from_db(usn):
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute('SELECT * FROM students WHERE usn = ?', (usn,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None

    sem5_data = json.loads(row['sem5_data']) if row['sem5_data'] else None
    sem5_rv_data = json.loads(row['sem5_rv_data']) if row['sem5_rv_data'] else None
    sem6_data = json.loads(row['sem6_data']) if row['sem6_data'] else None
    sem6_rv_data = json.loads(row['sem6_rv_data']) if row['sem6_rv_data'] else None

    sem5_stats = compute_semester_stats(sem5_data, sem5_rv_data)
    sem6_stats = compute_semester_stats(sem6_data, sem6_rv_data)
    agg_stats = compute_aggregate(sem5_stats, sem6_stats)

    return {
        'usn': row['usn'],
        'name': row['name'],
        'branch': row['branch'],
        'sem5_stats': sem5_stats,
        'sem6_stats': sem6_stats,
        'aggregate_stats': agg_stats,
        'updated_at': row['updated_at'],
        'from_db': True
    }

def save_student_to_db(usn, name, branch, sem5_data, sem5_rv_data, sem6_data, sem6_rv_data):
    sem5_stats = compute_semester_stats(sem5_data, sem5_rv_data)
    sem6_stats = compute_semester_stats(sem6_data, sem6_rv_data)
    agg_stats = compute_aggregate(sem5_stats, sem6_stats)

    sem5_w = sem5_stats['total_weighted_marks'] if sem5_stats else None
    sem5_c = sem5_stats['total_credits'] if sem5_stats else None
    sem6_w = sem6_stats['total_weighted_marks'] if sem6_stats else None
    sem6_c = sem6_stats['total_credits'] if sem6_stats else None
    agg_p = agg_stats['aggregate_percentage'] if agg_stats else None

    conn = sqlite3.connect(get_db_path())
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO students (
            usn, name, branch, sem5_data, sem5_rv_data, sem6_data, sem6_rv_data,
            sem5_weighted_marks, sem5_credits, sem6_weighted_marks, sem6_credits,
            aggregate_percentage, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(usn) DO UPDATE SET
            name = excluded.name,
            branch = excluded.branch,
            sem5_data = COALESCE(excluded.sem5_data, students.sem5_data),
            sem5_rv_data = COALESCE(excluded.sem5_rv_data, students.sem5_rv_data),
            sem6_data = COALESCE(excluded.sem6_data, students.sem6_data),
            sem6_rv_data = COALESCE(excluded.sem6_rv_data, students.sem6_rv_data),
            sem5_weighted_marks = excluded.sem5_weighted_marks,
            sem5_credits = excluded.sem5_credits,
            sem6_weighted_marks = excluded.sem6_weighted_marks,
            sem6_credits = excluded.sem6_credits,
            aggregate_percentage = excluded.aggregate_percentage,
            updated_at = CURRENT_TIMESTAMP
    ''', (
        usn, name, branch,
        json.dumps(sem5_data) if sem5_data else None,
        json.dumps(sem5_rv_data) if sem5_rv_data else None,
        json.dumps(sem6_data) if sem6_data else None,
        json.dumps(sem6_rv_data) if sem6_rv_data else None,
        sem5_w, sem5_c, sem6_w, sem6_c, agg_p
    ))
    conn.commit()
    conn.close()

# Routes
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/students-list')
def get_students_list():
    """Returns all 193 USNs with their cached status and basic summary."""
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute('SELECT usn, name, branch, sem5_weighted_marks, sem6_weighted_marks, aggregate_percentage FROM students')
    rows = {r['usn']: dict(r) for r in cur.fetchall()}
    conn.close()

    result = []
    for i in range(1, 194):
        usn = f"1JT23CS{i:03d}"
        if usn in rows:
            info = rows[usn]
            result.append({
                'usn': usn,
                'cached': True,
                'name': info.get('name') or 'N/A',
                'branch': info.get('branch') or 'CS',
                'sem5_marks': info.get('sem5_weighted_marks'),
                'sem6_marks': info.get('sem6_weighted_marks'),
                'aggregate_percentage': info.get('aggregate_percentage')
            })
        else:
            result.append({
                'usn': usn,
                'cached': False,
                'name': '',
                'branch': 'CS',
                'sem5_marks': None,
                'sem6_marks': None,
                'aggregate_percentage': None
            })
    return jsonify({'students': result})

@app.route('/api/result')
def get_result():
    raw_usn = request.args.get('usn', '').strip().upper()
    force_refresh = request.args.get('refresh', '0') == '1'

    if not raw_usn.startswith('1JT23CS'):
        if raw_usn.isdigit() and len(raw_usn) <= 3:
            raw_usn = f"1JT23CS{int(raw_usn):03d}"
        elif raw_usn.isalnum() and not raw_usn.startswith('1'):
            raw_usn = f"1JT23CS{raw_usn}"

    if not raw_usn.startswith('1JT23CS') or len(raw_usn) != 10:
        return jsonify({'error': 'Please provide a valid USN like 1JT23CS001 to 1JT23CS193'}), 400

    # Check local cache first if not forced refresh
    if not force_refresh:
        cached = get_student_from_db(raw_usn)
        if cached:
            return jsonify({'status': 'success', 'data': cached})

    # Fetch live from vtulife
    # 1. 5th Sem Normal
    res_sem5, data_sem5 = fetch_single_exam(raw_usn, VTU_CODES['sem5_normal'])
    if res_sem5 == 'CAPTCHA':
        return jsonify({
            'status': 'captcha_required',
            'session_id': data_sem5['session_id'],
            'captcha_image': data_sem5['captcha_image'],
            'exam_name': '5th Semester (Regular)',
            'result_code': VTU_CODES['sem5_normal'],
            'usn': raw_usn
        })

    # 2. 6th Sem Normal
    res_sem6, data_sem6 = fetch_single_exam(raw_usn, VTU_CODES['sem6_normal'])
    if res_sem6 == 'CAPTCHA':
        return jsonify({
            'status': 'captcha_required',
            'session_id': data_sem6['session_id'],
            'captcha_image': data_sem6['captcha_image'],
            'exam_name': '6th Semester (Regular)',
            'result_code': VTU_CODES['sem6_normal'],
            'usn': raw_usn
        })

    # 3. 5th Sem Revaluation (silent fetch, ignore captcha if optional)
    res_sem5_rv, data_sem5_rv = fetch_single_exam(raw_usn, VTU_CODES['sem5_reval'], allow_captcha_prompt=False)
    # 4. 6th Sem Revaluation (silent fetch)
    res_sem6_rv, data_sem6_rv = fetch_single_exam(raw_usn, VTU_CODES['sem6_reval'], allow_captcha_prompt=False)

    s5 = data_sem5 if res_sem5 == 'OK' else None
    s6 = data_sem6 if res_sem6 == 'OK' else None
    s5_rv = data_sem5_rv if res_sem5_rv == 'OK' else None
    s6_rv = data_sem6_rv if res_sem6_rv == 'OK' else None

    if not s5 and not s6:
        err_msg = "Result not found for this USN on VTU servers."
        if res_sem5 == 'ERROR': err_msg = f"VTU API error: {data_sem5}"
        return jsonify({'error': err_msg}), 404

    name = (s5 and s5.get('student', {}).get('name')) or (s6 and s6.get('student', {}).get('name')) or 'Unknown'
    branch = (s5 and s5.get('student', {}).get('branch')) or (s6 and s6.get('student', {}).get('branch')) or 'CS'

    save_student_to_db(raw_usn, name, branch, s5, s5_rv, s6, s6_rv)
    cached = get_student_from_db(raw_usn)
    return jsonify({'status': 'success', 'data': cached})

@app.route('/api/submit_captcha', methods=['POST'])
def submit_captcha():
    body = request.get_json() or {}
    session_id = body.get('session_id')
    captcha_text = body.get('captcha', '').strip()

    if not session_id or session_id not in ACTIVE_CAPTCHA_SESSIONS:
        return jsonify({'error': 'Captcha session expired. Please search again.'}), 400

    sess_entry = ACTIVE_CAPTCHA_SESSIONS[session_id]
    session = sess_entry['session']
    usn = sess_entry['usn']
    result_code = sess_entry['result_code']

    status_code, data = query_vtulife(session, usn, result_code, captcha=captcha_text)

    if status_code == 200:
        del ACTIVE_CAPTCHA_SESSIONS[session_id]

        # Save this result into DB
        name = data.get('student', {}).get('name', '')
        branch = data.get('student', {}).get('branch', 'CS')

        # Check existing data in DB to merge
        existing = get_student_from_db(usn)
        sem5_data = None
        sem6_data = None
        if result_code == VTU_CODES['sem5_normal']:
            sem5_data = data
            if existing and existing.get('sem6_stats'):
                # keep existing sem6
                pass
        elif result_code == VTU_CODES['sem6_normal']:
            sem6_data = data

        save_student_to_db(usn, name, branch, sem5_data, None, sem6_data, None)

        # Now attempt to fetch remaining semester if needed
        # Call get_result for this USN
        cached = get_student_from_db(usn)
        return jsonify({'status': 'success', 'data': cached})
    else:
        # Check if new captcha required
        if 'captcha_image' in data:
            return jsonify({
                'status': 'captcha_retry',
                'error': data.get('error', 'Incorrect captcha, please try again.'),
                'captcha_image': data['captcha_image'],
                'session_id': session_id
            })
        return jsonify({'error': data.get('error', 'Failed to submit captcha')}), 400

@app.route('/api/stats')
def get_stats():
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute('''
        SELECT usn, name, branch, sem5_weighted_marks, sem5_credits,
               sem6_weighted_marks, sem6_credits, aggregate_percentage
        FROM students
        WHERE aggregate_percentage IS NOT NULL
        ORDER BY aggregate_percentage DESC
    ''')
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    total_cached = len(rows)
    if total_cached == 0:
        return jsonify({
            'total_students': 193,
            'cached_count': 0,
            'class_average': 0,
            'top_scorer': None,
            'rankers': []
        })

    avg_agg = round(sum(r['aggregate_percentage'] for r in rows) / total_cached, 2)
    top_scorer = rows[0] if rows else None

    return jsonify({
        'total_students': 193,
        'cached_count': total_cached,
        'class_average': avg_agg,
        'top_scorer': top_scorer,
        'rankers': rows[:15]
    })

@app.errorhandler(404)
def not_found(e):
    return render_template('index.html'), 200

if __name__ == '__main__':
    print("Starting VTU Marks Calculator Server on http://localhost:5000 ...")
    app.run(host='0.0.0.0', port=5000, debug=True)
