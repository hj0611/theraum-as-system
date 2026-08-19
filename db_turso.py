"""
Turso(libSQL) HTTP API 어댑터 — 별도 pip 패키지 설치 없이 표준 라이브러리(urllib)만으로 동작.

Render 무료 플랜 등 '로컬 디스크가 슬립/재배포마다 초기화되는' 호스팅에서도
접수 데이터가 영구 보존되도록, SQLite 파일 대신 Turso(무료·카드불필요·5GB) DB를
HTTP로 사용한다. app.py 안에서 sqlite3.Connection 대신 쓸 수 있도록
execute()/commit()/close() 등 실제로 사용하는 최소 API만 흉내낸다.

참고(공식 스펙): https://docs.turso.tech/sdk/http/reference
"""
import os
import json
import base64
import urllib.request
import urllib.error


def _http_url():
    url = os.environ["TURSO_DATABASE_URL"].strip()
    if url.startswith("libsql://"):
        url = "https://" + url[len("libsql://"):]
    return url.rstrip("/") + "/v2/pipeline"


def _encode_arg(v):
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "integer", "value": str(int(v))}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    if isinstance(v, (bytes, bytearray)):
        return {"type": "blob", "base64": base64.b64encode(bytes(v)).decode("ascii")}
    return {"type": "text", "value": str(v)}


def _decode_cell(cell):
    t = cell.get("type")
    if t in (None, "null"):
        return None
    if t == "integer":
        return int(cell["value"])
    if t == "float":
        return float(cell["value"])
    if t == "blob":
        raw = cell.get("base64", cell.get("value", ""))
        return base64.b64decode(raw) if raw else b""
    return cell.get("value")


class TursoRow(dict):
    """dict(row) / row['col'] 둘 다 지원 (sqlite3.Row 호환용)."""
    pass


class TursoCursor:
    def __init__(self, cols, rows, affected=0, last_id=None):
        names = [c.get("name") for c in (cols or [])]
        self._rows = []
        for r in rows or []:
            d = TursoRow()
            for name, cell in zip(names, r):
                d[name] = _decode_cell(cell)
            self._rows.append(d)
        self.rowcount = affected
        self.lastrowid = last_id

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


class TursoError(RuntimeError):
    pass


def _post(payload):
    token = os.environ.get("TURSO_AUTH_TOKEN", "")
    req = urllib.request.Request(
        _http_url(),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        raise TursoError(f"Turso HTTP {e.code}: {body}")
    except urllib.error.URLError as e:
        raise TursoError(f"Turso 연결 실패: {e}")


def _stmt(sql, params):
    return {"sql": sql, "args": [_encode_arg(p) for p in (params or ())]}


def _cursor_from_result(result):
    if result.get("type") != "ok":
        err = result.get("error", {})
        raise TursoError(f"Turso SQL 오류: {err.get('message', result)}")
    r = result["response"]["result"]
    return TursoCursor(
        r.get("cols", []),
        r.get("rows", []),
        r.get("affected_row_count", 0),
        r.get("last_insert_rowid"),
    )


class TursoConn:
    """app.py에서 실제로 사용하는 execute/executescript/commit/close 만 지원하는
    sqlite3.Connection 호환 래퍼."""

    def execute(self, sql, params=()):
        payload = {"requests": [{"type": "execute", "stmt": _stmt(sql, params)}, {"type": "close"}]}
        data = _post(payload)
        return _cursor_from_result(data["results"][0])

    def execute_batch(self, stmts):
        """여러 SQL을 같은 커넥션(하나의 파이프라인 요청)에서 순서대로 실행.
        counters 증가 + 조회처럼 원자성이 필요한 두 문장을 묶을 때 사용한다."""
        requests_ = [{"type": "execute", "stmt": _stmt(sql, params)} for sql, params in stmts]
        requests_.append({"type": "close"})
        data = _post({"requests": requests_})
        return [_cursor_from_result(r) for r in data["results"][:-1]]

    def executescript(self, script):
        for s in [s.strip() for s in script.split(";") if s.strip()]:
            self.execute(s)

    def commit(self):
        pass  # 각 execute가 즉시 반영됨(자동 커밋)

    def close(self):
        pass


def is_configured():
    return bool(os.environ.get("TURSO_DATABASE_URL"))
