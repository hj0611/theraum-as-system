"""
THE RAUM AS 처리절차 시스템
Flask + SQLite 기반 실배포용 애플리케이션

화면 구성
  1. 고객 접수            GET/POST /
  2. 접수완료 팝업 → 자동이동   (intake.html 내 JS 모달, /status 로 자동 리다이렉트)
  3. 진행상태 확인         GET/POST /status
  4. 담당자 대시보드(캘린더) GET /dashboard  (+ /dashboard?view=list)
  5. DB요약 / 월간보고     GET /summary
"""
import os
import json
import mimetypes
import shutil
import sqlite3
import calendar
import secrets
from datetime import datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

from flask import (
    Flask, request, session, redirect, url_for, render_template,
    jsonify, flash, send_from_directory, abort, Response
)
from werkzeug.utils import secure_filename

import db_turso

# ---------------------------------------------------------------------------
# 기본 설정
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")
DB_PATH = os.path.join(DATA_DIR, "as_system.db")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

ALLOWED_EXT = {"jpg", "jpeg", "png", "webp", "gif"}
MAX_PHOTOS = 10

CATEGORIES = ["마감재", "가구·붙박이장", "전기·조명", "설비", "누수·방수", "에어컨", "기타"]
STAGES = ["접수완료", "담당자확인", "일정편성완료", "처리완료"]
URGENT_DAYS = 2          # 처리완료 제외, 접수 후 경과일이 이 값 이상이면 긴급
START_ID = 100001        # 6자리 접수번호 시작값

# 서버 구동 위치(리전)와 무관하게 모든 시각을 한국 표준시(KST, UTC+9) 기준으로 기록·표시한다.
KST = ZoneInfo("Asia/Seoul")


def kst_now():
    """현재 시각을 한국 표준시(KST) 기준의 naive datetime으로 반환한다.
    DB에는 타임존 정보 없이 'KST 벽시계 시각' 문자열로 저장하므로,
    기존에 저장된 값과 동일한 방식(naive datetime 비교)으로 계속 다룰 수 있다."""
    return datetime.now(KST).replace(tzinfo=None)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", secrets.token_hex(16))
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 요청 전체 20MB 제한

MANAGER_PASSCODE = os.environ.get("MANAGER_PASSCODE", "theraum2026")
if os.environ.get("MANAGER_PASSCODE") is None:
    print("[경고] MANAGER_PASSCODE 환경변수가 설정되지 않아 기본값을 사용합니다. "
          "실제 운영 배포 시 반드시 별도 값으로 설정하세요.")


# ---------------------------------------------------------------------------
# DB 유틸리티
# ---------------------------------------------------------------------------
USE_TURSO = db_turso.is_configured()
if USE_TURSO:
    print("[정보] TURSO_DATABASE_URL이 설정되어 Turso(원격 영구 DB)를 사용합니다. "
          "접수 데이터와 첨부사진이 서버 재배포/슬립과 무관하게 보존됩니다.")
else:
    print("[정보] TURSO_DATABASE_URL이 없어 로컬 SQLite 파일을 사용합니다. "
          "Render 무료 플랜 등 디스크가 휘발성인 호스팅에서는 데이터가 유실될 수 있으니 "
          "실제 운영 시 Turso 연동을 권장합니다 (README.md 참고).")


def get_db():
    if USE_TURSO:
        return db_turso.TursoConn()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS requests (
        id INTEGER PRIMARY KEY,
        complex_name TEXT,
        dong TEXT NOT NULL,
        ho TEXT NOT NULL,
        customer_name TEXT NOT NULL,
        phone TEXT NOT NULL,
        category TEXT NOT NULL,
        description TEXT NOT NULL,
        photos TEXT DEFAULT '[]',
        status TEXT NOT NULL DEFAULT '접수완료',
        manager_checked INTEGER NOT NULL DEFAULT 0,
        checked_by TEXT,
        scheduled_date TEXT,
        memo TEXT,
        created_at TEXT NOT NULL,
        confirmed_at TEXT,
        scheduled_at TEXT,
        completed_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS counters (
        name TEXT PRIMARY KEY,
        value INTEGER NOT NULL
    )
    """,
    # 첨부사진 원본 데이터. 로컬 디스크가 휘발성인 호스팅(Render 무료 등)에서도
    # 사진이 보존되도록 Turso 사용 시에는 파일 대신 이 테이블에 저장한다.
    """
    CREATE TABLE IF NOT EXISTS photos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id INTEGER NOT NULL,
        filename TEXT NOT NULL,
        mimetype TEXT,
        data BLOB NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
]

# 기존에 이미 만들어진 DB(운영 중인 Turso 등)에는 CREATE TABLE IF NOT EXISTS가 적용되지
# 않으므로, 새로 추가된 컬럼은 아래처럼 ALTER TABLE로 별도 반영한다. 컬럼이 이미 있으면
# 오류가 발생하므로 무시한다(멱등 처리).
MIGRATIONS = [
    "ALTER TABLE requests ADD COLUMN checked_by TEXT",
]


def init_db():
    conn = get_db()
    for stmt in SCHEMA_STATEMENTS:
        conn.execute(stmt)
    for stmt in MIGRATIONS:
        try:
            conn.execute(stmt)
        except Exception:
            pass  # 이미 컬럼이 존재하는 등 정상적으로 무시 가능한 오류
    cur = conn.execute("SELECT value FROM counters WHERE name = 'request_id'")
    if cur.fetchone() is None:
        conn.execute(
            "INSERT INTO counters (name, value) VALUES ('request_id', ?)",
            (START_ID - 1,),
        )
    conn.commit()
    conn.close()


def next_request_id(conn):
    """접수번호 채번. Turso 사용 시에는 두 문장을 한 파이프라인 요청(동일 커넥션)으로
    묶어 처리하여 증가값 조회의 원자성을 최대한 보장한다."""
    if hasattr(conn, "execute_batch"):
        cursors = conn.execute_batch(
            [
                ("UPDATE counters SET value = value + 1 WHERE name = 'request_id'", ()),
                ("SELECT value FROM counters WHERE name = 'request_id'", ()),
            ]
        )
        return cursors[1].fetchone()["value"]
    conn.execute(
        "UPDATE counters SET value = value + 1 WHERE name = 'request_id'"
    )
    row = conn.execute(
        "SELECT value FROM counters WHERE name = 'request_id'"
    ).fetchone()
    return row["value"]


def now_iso():
    return kst_now().isoformat(timespec="seconds")


def parse_dt(s):
    if not s:
        return None
    return datetime.fromisoformat(s)


def days_between(start, end):
    """경과 일수(day 단위, 소수점 첫째자리까지)."""
    if not start or not end:
        return None
    delta = end - start
    return round(delta.total_seconds() / 86400, 1)


def is_urgent(row):
    if row["status"] == "처리완료":
        return False
    created = parse_dt(row["created_at"])
    if not created:
        return False
    return (kst_now() - created) >= timedelta(days=URGENT_DAYS)


def row_to_dict(row):
    d = dict(row)
    d["photos"] = json.loads(d.get("photos") or "[]")
    d["urgent"] = is_urgent(row)
    return d


# ---------------------------------------------------------------------------
# 인증 (담당자)
# ---------------------------------------------------------------------------
def manager_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_manager"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        code = request.form.get("passcode", "")
        if secrets.compare_digest(code, MANAGER_PASSCODE):
            session["is_manager"] = True
            nxt = request.args.get("next") or url_for("dashboard")
            return redirect(nxt)
        error = "접속코드가 일치하지 않습니다."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.pop("is_manager", None)
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# 1) 고객 접수
# ---------------------------------------------------------------------------
@app.route("/", methods=["GET"])
def intake():
    return render_template("intake.html", categories=CATEGORIES)


@app.route("/submit", methods=["POST"])
def submit():
    form = request.form
    required = ["complex_name", "dong", "ho", "customer_name", "phone", "category", "description"]
    missing = [f for f in required if not form.get(f, "").strip()]
    if missing:
        return jsonify({"ok": False, "error": "필수 항목이 누락되었습니다."}), 400

    if form.get("privacy_consent") != "y":
        return jsonify({"ok": False, "error": "개인정보 수집 및 이용에 동의해주세요."}), 400

    phone_digits = "".join(ch for ch in form.get("phone", "") if ch.isdigit())
    if len(phone_digits) < 4:
        return jsonify({"ok": False, "error": "연락처를 정확히 입력해주세요."}), 400

    if form.get("category") not in CATEGORIES:
        return jsonify({"ok": False, "error": "AS 카테고리를 선택해주세요."}), 400

    conn = get_db()
    req_id = next_request_id(conn)

    # 사진 저장 — Turso 사용 시에는 로컬 디스크가 휘발성일 수 있으므로 DB(photos 테이블)에
    # 직접 저장하고, 로컬 SQLite 개발 모드에서는 기존처럼 디스크에 저장한다.
    saved_photos = []
    files = request.files.getlist("photos")[:MAX_PHOTOS]
    if files and not USE_TURSO:
        req_dir = os.path.join(UPLOAD_DIR, str(req_id))
        os.makedirs(req_dir, exist_ok=True)
    for f in files:
        if not f or not f.filename:
            continue
        ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
        if ext not in ALLOWED_EXT:
            continue
        safe_name = secure_filename(f.filename)
        fname = f"{secrets.token_hex(4)}_{safe_name}"
        if USE_TURSO:
            data = f.read()
            mimetype = f.mimetype or mimetypes.guess_type(fname)[0] or "application/octet-stream"
            conn.execute(
                "INSERT INTO photos (request_id, filename, mimetype, data, created_at) VALUES (?,?,?,?,?)",
                (req_id, fname, mimetype, data, now_iso()),
            )
        else:
            f.save(os.path.join(UPLOAD_DIR, str(req_id), fname))
        saved_photos.append(fname)

    created = now_iso()
    conn.execute(
        """INSERT INTO requests
           (id, complex_name, dong, ho, customer_name, phone, category,
            description, photos, status, manager_checked, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,0,?)""",
        (
            req_id,
            form.get("complex_name", "").strip(),
            form.get("dong", "").strip(),
            form.get("ho", "").strip(),
            form.get("customer_name", "").strip(),
            phone_digits,
            form.get("category"),
            form.get("description", "").strip(),
            json.dumps(saved_photos, ensure_ascii=False),
            "접수완료",
            created,
        ),
    )
    conn.commit()
    conn.close()

    return jsonify({"ok": True, "request_id": req_id})


# ---------------------------------------------------------------------------
# 3) 진행상태 확인
# ---------------------------------------------------------------------------
@app.route("/status", methods=["GET", "POST"])
def status_check():
    result = None
    error = None
    prefill_id = request.args.get("id", "")

    if request.method == "POST":
        req_id = request.form.get("request_id", "").strip()
        phone_last4 = request.form.get("phone_last4", "").strip()
        conn = get_db()
        row = None
        if req_id.isdigit():
            row = conn.execute(
                "SELECT * FROM requests WHERE id = ?", (int(req_id),)
            ).fetchone()
        conn.close()
        if not row or not row["phone"].endswith(phone_last4) or len(phone_last4) != 4:
            error = "접수번호 또는 연락처 뒷 4자리가 일치하지 않습니다."
        else:
            result = row_to_dict(row)
            result["stage_index"] = STAGES.index(result["status"])

    return render_template(
        "status_check.html",
        result=result,
        error=error,
        stages=STAGES,
        prefill_id=prefill_id,
    )


# ---------------------------------------------------------------------------
# 4) 담당자 대시보드 (캘린더 기본, 목록 보조)
# ---------------------------------------------------------------------------
@app.route("/dashboard")
@manager_required
def dashboard():
    view = request.args.get("view", "calendar")
    today = kst_now()
    year = int(request.args.get("year", today.year))
    month = int(request.args.get("month", today.month))
    selected_date = request.args.get("date")  # YYYY-MM-DD

    conn = get_db()
    rows = conn.execute("SELECT * FROM requests ORDER BY id DESC").fetchall()
    conn.close()
    items = [row_to_dict(r) for r in rows]

    # 캘린더에 표시할 날짜별 집계
    by_date = {}
    for it in items:
        d = it["created_at"][:10]
        by_date.setdefault(d, {"count": 0, "unchecked": False, "urgent": False})
        by_date[d]["count"] += 1
        if not it["manager_checked"]:
            by_date[d]["unchecked"] = True
        if it["urgent"]:
            by_date[d]["urgent"] = True

    cal = calendar.Calendar(firstweekday=6)  # 일요일 시작
    month_days = list(cal.itermonthdates(year, month))

    prev_month = (month - 1) or 12
    prev_year = year - 1 if month == 1 else year
    next_month = (month % 12) + 1
    next_year = year + 1 if month == 12 else year

    day_items = []
    if selected_date:
        day_items = [it for it in items if it["created_at"][:10] == selected_date]

    total_unchecked = sum(1 for it in items if not it["manager_checked"])
    total_urgent = sum(1 for it in items if it["urgent"])

    return render_template(
        "dashboard.html",
        view=view,
        items=items,
        by_date=by_date,
        month_days=month_days,
        year=year,
        month=month,
        prev_year=prev_year,
        prev_month=prev_month,
        next_year=next_year,
        next_month=next_month,
        today_str=today.strftime("%Y-%m-%d"),
        selected_date=selected_date,
        day_items=day_items,
        stages=STAGES,
        categories=CATEGORIES,
        total_unchecked=total_unchecked,
        total_urgent=total_urgent,
    )


@app.route("/manager/update/<int:req_id>", methods=["POST"])
@manager_required
def manager_update(req_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM requests WHERE id = ?", (req_id,)).fetchone()
    if not row:
        conn.close()
        abort(404)

    action = request.form.get("action")
    ts = now_iso()

    if action == "check":
        checked_by = request.form.get("checked_by", "").strip()
        if not checked_by:
            conn.close()
            if request.headers.get("X-Requested-With") == "fetch":
                return jsonify({"ok": False, "error": "담당자 성함을 입력해주세요."}), 400
            return redirect(request.referrer or url_for("dashboard"))
        conn.execute(
            "UPDATE requests SET manager_checked = 1, checked_by = ? WHERE id = ?",
            (checked_by, req_id),
        )
        if row["status"] == "접수완료":
            conn.execute(
                "UPDATE requests SET status = ?, confirmed_at = ? WHERE id = ?",
                ("담당자확인", ts, req_id),
            )
    elif action == "schedule":
        sched_date = request.form.get("scheduled_date", "")
        conn.execute(
            """UPDATE requests
               SET status = ?, scheduled_date = ?, scheduled_at = ?, manager_checked = 1
               WHERE id = ?""",
            ("일정편성완료", sched_date, ts, req_id),
        )
        if not row["confirmed_at"]:
            conn.execute("UPDATE requests SET confirmed_at = ? WHERE id = ?", (ts, req_id))
    elif action == "complete":
        conn.execute(
            "UPDATE requests SET status = ?, completed_at = ? WHERE id = ?",
            ("처리완료", ts, req_id),
        )
    elif action == "memo":
        memo = request.form.get("memo", "")
        conn.execute("UPDATE requests SET memo = ? WHERE id = ?", (memo, req_id))
    elif action == "revert":
        # 잘못 넘어간 단계를 한 단계 이전으로 되돌린다. 되돌아가는 단계에서
        # 기록됐던 시각/값은 함께 초기화하여 소요일 계산이 어긋나지 않게 한다.
        cur_idx = STAGES.index(row["status"])
        if cur_idx > 0:
            prev_status = STAGES[cur_idx - 1]
            if row["status"] == "담당자확인":
                conn.execute(
                    """UPDATE requests
                       SET status = ?, manager_checked = 0, checked_by = NULL, confirmed_at = NULL
                       WHERE id = ?""",
                    (prev_status, req_id),
                )
            elif row["status"] == "일정편성완료":
                conn.execute(
                    """UPDATE requests
                       SET status = ?, scheduled_date = NULL, scheduled_at = NULL
                       WHERE id = ?""",
                    (prev_status, req_id),
                )
            elif row["status"] == "처리완료":
                conn.execute(
                    "UPDATE requests SET status = ?, completed_at = NULL WHERE id = ?",
                    (prev_status, req_id),
                )

    conn.commit()
    conn.close()

    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"ok": True})
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/manager/delete", methods=["POST"])
@manager_required
def manager_delete():
    """대시보드에서 체크박스로 선택한 접수 건을 일괄 삭제한다.
    (건 1개만 선택해도 동작 — '건별 선택 삭제'는 선택 개수 1건 이상을 지원하는 것으로 구현)"""
    raw_ids = request.form.getlist("req_ids")
    ids = []
    for v in raw_ids:
        try:
            ids.append(int(v))
        except (TypeError, ValueError):
            continue
    ids = sorted(set(ids))

    deleted = 0
    if ids:
        conn = get_db()
        placeholders = ",".join("?" for _ in ids)
        if USE_TURSO:
            # Turso 사용 시 사진 원본이 photos 테이블에 저장되므로 함께 삭제한다.
            conn.execute(f"DELETE FROM photos WHERE request_id IN ({placeholders})", ids)
        conn.execute(f"DELETE FROM requests WHERE id IN ({placeholders})", ids)
        conn.commit()
        conn.close()
        deleted = len(ids)

        if not USE_TURSO:
            # 로컬 디스크 모드에서는 접수 건별 업로드 사진 폴더도 함께 정리한다.
            for rid in ids:
                shutil.rmtree(os.path.join(UPLOAD_DIR, str(rid)), ignore_errors=True)

    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"ok": True, "deleted": deleted})
    return redirect(request.referrer or url_for("dashboard"))


# ---------------------------------------------------------------------------
# 5) DB 자동요약 / 월간 자동보고
# ---------------------------------------------------------------------------
@app.route("/summary")
@manager_required
def summary():
    conn = get_db()
    rows = conn.execute("SELECT * FROM requests ORDER BY id DESC").fetchall()
    conn.close()
    items = []
    for r in rows:
        d = row_to_dict(r)
        created = parse_dt(d["created_at"])
        confirmed = parse_dt(d["confirmed_at"])
        scheduled = parse_dt(d["scheduled_at"])
        completed = parse_dt(d["completed_at"])
        d["d_receive_to_confirm"] = days_between(created, confirmed)
        d["d_confirm_to_schedule"] = days_between(confirmed, scheduled)
        d["d_schedule_to_complete"] = days_between(scheduled, completed)
        d["d_total"] = days_between(created, completed or kst_now())
        d["d_total_open"] = completed is None
        items.append(d)

    # 월간 보고 대상 월 (기본: 전월)
    today = kst_now()
    default_month = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    target_month = request.args.get("month", default_month)

    month_items = [it for it in items if it["created_at"][:7] == target_month]

    # 카테고리별 집계
    cat_counts = {c: 0 for c in CATEGORIES}
    for it in month_items:
        cat_counts[it["category"]] = cat_counts.get(it["category"], 0) + 1

    # 반복이슈: 동일 카테고리가 해당 월에 3회 이상 접수되면 시공 품질 이슈로 감지
    # (세대가 서로 달라도 같은 하자 유형이 반복되면 잡히도록 카테고리 단위로 집계한다)
    repeat_map = {}
    for it in month_items:
        repeat_map.setdefault(it["category"], []).append(it)
    # 주의: dict 키를 'items'로 두면 Jinja2에서 dict.items() 내장 메서드와
    # 충돌하여 TypeError가 발생하므로 'reqs'로 명명한다.
    repeat_issues = [
        {"category": category, "count": len(v), "reqs": v}
        for category, v in repeat_map.items()
        if len(v) >= 3
    ]
    repeat_issues.sort(key=lambda x: -x["count"])

    available_months = sorted({it["created_at"][:7] for it in items}, reverse=True)

    return render_template(
        "summary.html",
        items=items,
        target_month=target_month,
        month_items=month_items,
        cat_counts=cat_counts,
        repeat_issues=repeat_issues,
        available_months=available_months,
        categories=CATEGORIES,
    )


# ---------------------------------------------------------------------------
# 업로드 사진 서빙 — Turso 사용 시 DB(photos 테이블)에서, 아니면 로컬 디스크에서 서빙
# ---------------------------------------------------------------------------
@app.route("/uploads/<int:req_id>/<path:filename>")
def uploaded_file(req_id, filename):
    if USE_TURSO:
        conn = get_db()
        row = conn.execute(
            "SELECT mimetype, data FROM photos WHERE request_id = ? AND filename = ?",
            (req_id, filename),
        ).fetchone()
        conn.close()
        if not row:
            abort(404)
        return Response(row["data"], mimetype=row["mimetype"] or "application/octet-stream")
    return send_from_directory(os.path.join(UPLOAD_DIR, str(req_id)), filename)


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "time": now_iso()})


# ---------------------------------------------------------------------------
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=True)
