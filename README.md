# THE RAUM AS 처리절차 시스템

고객이 직접 접수하고, 담당자가 캘린더형 대시보드로 처리하며, 전 건이 자동으로 DB화되어 월간 반복이슈까지 집계되는 AS(사후관리) 처리 시스템입니다. Flask + SQLite 기반이며 클라우드에 실배포하여 실제 운영할 수 있도록 구성되어 있습니다.

## 이번 작업에서 수정한 오류

이전 데모(단일 HTML 버전)에서 보고되었던 **모바일(390px) 표 가독성 문제**를 아래와 같이 해결했습니다.

- 문제: DB요약 · 월간보고 화면의 표가 모바일 폭에서 열이 강제로 압축되어 한글이 한 글자씩 줄바꿈됨
- 해결: 640px 이하 화면에서는 표 대신 **카드형 레이아웃**으로 자동 전환 (`static/css/style.css`의 `@media (max-width: 640px)` 규칙 + `templates/summary.html`의 `.card-list` 블록). 데스크톱에서는 기존처럼 표 형태를 유지합니다.

또한 실제로 서버를 구현하는 과정에서 아래 오류를 추가로 발견하여 수정했습니다.

- 월간보고의 "반복이슈" 데이터를 Jinja2 템플릿에서 `r.items`로 접근하면 dict의 내장 메서드 `items()`와 이름이 충돌하여 `TypeError: 'builtin_function_or_method' object is not iterable` 로 500 오류가 발생하는 문제 → 키 이름을 `reqs`로 변경하여 해결 (`app.py`, `templates/summary.html`)
- (배포 검토 중 발견) Render 등 무료 클라우드 플랜은 로컬 디스크가 슬립/재배포마다 초기화되어, 기존 SQLite 파일 저장 방식 그대로는 실제 운영 데이터가 유실되는 구조적 문제가 있었음 → `db_turso.py`를 추가해 무료 영구 DB(Turso)를 선택적으로 사용하도록 전환(자세한 내용은 아래 "클라우드 실배포" 참고)

## 화면 구성 (5개)

1. **고객 접수** (`/`) — 단지명(주소검색, 필수)/동/호수/연락처/카테고리/상세내용/사진(최대 10장) 입력 → 제출 즉시 6자리 접수번호 발급
2. **접수완료 팝업 → 자동 이동** — 접수 직후 팝업으로 접수번호 안내, 약 2초 후 진행상태 확인 화면으로 자동 이동
3. **진행상태 확인** (`/status`) — 접수번호 + 연락처 뒷 4자리로 조회, 4단계 트래커(접수완료→담당자확인→일정편성완료→처리완료) 표시
4. **담당자 대시보드** (`/dashboard`) — 캘린더 뷰가 기본 화면(목록 뷰 보조 제공)
   - 담당자 미확인 건: 주황색 표시
   - 처리완료 제외 + 접수 후 **2일 이상 경과** 시 빨간색 "긴급" 표시 (화면 표시까지만 구현, 외부 알림 발송은 이번 범위에서 제외)
5. **DB 자동요약 / 월간 자동보고** (`/summary`) — 전 건 리스트 + 접수일 기준 단계별 소요일(day) 자동 계산, 월별 카테고리 집계, 동일 위치·카테고리 3회 이상 반복 이슈 자동 감지

## 로컬 실행 (개발용)

```bash
pip install -r requirements.txt
python app.py
# http://127.0.0.1:5050 접속
```

담당자 접속코드는 환경변수 `MANAGER_PASSCODE`로 지정합니다 (미지정 시 기본값 `theraum2026` 사용 — 운영 배포 시 반드시 변경).

```bash
export MANAGER_PASSCODE="원하는코드"
python app.py
```

## 클라우드 실배포 — 비용 0원 구성 (Render + Turso + 기존 도메인)

카페24·가비아 같은 일반 웹호스팅(PHP 기반 공유호스팅)은 Flask(Python WSGI) 앱을 상시 구동할 수 없습니다. 대신 앱은 무료 클라우드에 올리고, 이미 보유한 도메인은 서브도메인으로 연결하는 방식을 사용하면 **추가 비용 없이** 실제 서비스가 가능합니다.

### 왜 SQLite 그대로는 안 되는가

Render 무료(Free) 플랜은 로컬 디스크가 완전히 휘발성입니다. 15분간 요청이 없으면 서비스가 슬립되고, 다음 요청에서 재기동되는데 이때 **로컬 파일시스템 변경 사항(= SQLite DB 파일, 첨부사진)이 전부 초기화**됩니다. 즉 지금 구조 그대로 무료 플랜에 올리면 접수 데이터가 계속 사라집니다. 이 문제를 해결하기 위해 이번 작업에서 **Turso**(무료·카드등록 불필요·5GB, https://turso.tech) 연동을 추가했습니다. `TURSO_DATABASE_URL` / `TURSO_AUTH_TOKEN` 환경변수가 설정되면 앱이 자동으로 Turso를 사용하며(접수 데이터 + 첨부사진 모두 DB에 저장), 설정하지 않으면 기존처럼 로컬 SQLite로 동작합니다(로컬 테스트용).

### 배포 절차

1. **Turso 무료 계정 생성** (카드 불필요, 약 2분)
   - https://turso.tech 에서 가입 → 데이터베이스 1개 생성
   - `Database URL`과 `Auth Token`을 발급받습니다 (Turso 대시보드 또는 CLI `turso db create`, `turso db tokens create`)
2. **GitHub에 이 폴더를 저장소로 업로드**
3. **Render 가입** (https://render.com, 카드 불필요) 후 **New + → Blueprint**로 저장소를 연결하면 `render.yaml`을 자동 인식합니다.
4. Environment 탭에서 아래 값을 입력합니다.
   - `MANAGER_PASSCODE`: 담당자 접속코드 (필수)
   - `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`: 1번에서 발급받은 값 (데이터 영구보존을 위해 강력 권장)
5. 배포가 끝나면 `https://xxxx.onrender.com` 주소가 발급됩니다.
6. **기존 도메인 연결(선택)**: 카페24/가비아 등 도메인 관리 페이지의 DNS 설정에서 서브도메인(예: `as.귀사도메인.com`)에 대해 `CNAME → xxxx.onrender.com` 레코드를 추가합니다. Render의 Settings → Custom Domain에서 같은 주소를 등록하면 HTTPS 인증서가 자동 발급됩니다. (도메인 자체를 옮기거나 기존 홈페이지에 영향을 주지 않고, 서브도메인 하나만 추가로 연결하는 방식입니다.)

### 무료 구성의 한계 (알아두어야 할 점)

- Render 무료 플랜은 15분 미접속 시 슬립되며, 슬립 후 첫 접속은 재기동에 수십 초가 걸립니다. 접수/조회가 뜸한 새벽 시간대 등에는 첫 화면 로딩이 느릴 수 있습니다. 상시 즉시 응답이 필요하면 Render 유료 플랜(월 7달러 내외)으로 전환하면 슬립이 없어집니다.
- Turso 무료 플랜은 5GB 저장공간, 월 5억 회 읽기/1천만 회 쓰기까지 무료입니다 — AS 접수 시스템 사용량으로는 사실상 넉넉합니다.
- Railway 등 다른 PaaS를 사용할 경우에도 `requirements.txt` + `Procfile` 구조를 그대로 사용할 수 있습니다 (단, Railway는 2026년 기준 완전 무료 플랜을 제공하지 않습니다).

## 실제 서비스로 전환 시 추가로 고려할 사항

- **로그인/권한 분리**: 현재는 담당자 전원이 하나의 공용 접속코드(`MANAGER_PASSCODE`)를 공유합니다. 담당자별 계정, 접수 이력 감사(audit log)가 필요하면 로그인 체계를 확장해야 합니다.
- **DB 확장성**: Turso도 libSQL(=SQLite 호환) 기반이라 소규모~중간 규모 운영에 적합합니다. 동시접속/데이터량이 크게 늘어나면 PostgreSQL 전환을 고려할 수 있습니다.
- **담당자 알림 연동**: 이번 배포에는 포함하지 않았습니다(화면상 긴급 표시만 구현). Slack Webhook 또는 카카오워크 API로 확장하려면 `app.py`의 `manager_update`, `submit` 함수에 알림 발송 호출을 추가하면 됩니다.
- **HTTPS/도메인**: Render는 기본적으로 HTTPS를 제공합니다. 자체 도메인을 연결하는 방법은 위 "배포 절차" 6번을 참고하세요.

## 폴더 구조

```
as_system/
  app.py              # Flask 앱 본체 (라우트, DB 로직)
  db_turso.py          # Turso(HTTP API) 어댑터 — TURSO_DATABASE_URL 설정 시 자동 사용
  requirements.txt
  Procfile            # gunicorn 실행 명령 (프로덕션 WSGI)
  render.yaml          # Render 블루프린트 (자동 배포 설정)
  data/                # SQLite DB 저장 위치 (Turso 미사용 시, 로컬 실행용)
  static/
    css/style.css
    js/main.js
    uploads/           # 첨부 사진 저장 위치 (Turso 미사용 시, 로컬 실행용)
  templates/           # 5개 화면 + 공통 레이아웃
```
