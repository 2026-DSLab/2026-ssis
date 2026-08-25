# 법령·행정규칙 개정 알림 시스템

감시 대상 법령·행정규칙 102건의 개정을 주 1회 자동으로 확인하고, 몇 조 몇 항이
바뀌었는지 정리해 웹 화면과 한글(HWPX) 보고서로 제공함.

- 개발: 국민대학교 법인사이트 / 협업: 한국사회보장정보원 정보분석부
- 자료 출처: 국가법령정보센터 Open API

## 기능

브라우저로 `http://서버주소:5000` 에 접속해 씀. 별도 설치가 필요 없음.

| 화면 | 하는 일 |
|---|---|
| 개정 요약 | 이번 주 개정을 법령별·조문별로 정리. 최근 5일·2주·1개월 기간 조회 |
| 전문 비교 | 102건의 전문을 조문 단위로 열람. 개정 전·후를 나란히 놓고 변경 부분 표시 |
| 문서 인용 확인 | PDF·한글 문서를 올리면 인용한 감시 대상 법령과 쪽수를 표시. 문서는 저장하지 않음 |
| 보고서 내려받기 | 기관 양식 한글(HWPX) 문서. 조문·위치·구분·변경 내용 4칸 표 포함 |

## 설치

필요한 것: Python 3.11+, PostgreSQL 14+, 국가법령정보 Open API 인증키
(<https://open.law.go.kr>), LLM API 키(요약을 쓸 경우).

### 1. 패키지 설치

```bash
pip install -e ".[openai,web,dev]"
python -m pytest -q          # 460 passed 나오면 정상
```

### 2. 환경 설정

`.env.example` 을 `.env` 로 복사하고 값을 채움.

```bash
copy .env.example .env
```

| 항목 | 값 |
|---|---|
| `LAW_API_OC` | 국가법령정보 Open API 인증키 |
| `POSTGRES_HOST` / `POSTGRES_PORT` | 기본 `127.0.0.1` / `5432` |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` | PostgreSQL 계정 |
| `POSTGRES_DATABASE` | 기본 `law_tracking_db` |
| `OPENAI_API_KEY` | 요약에 쓰는 LLM 키 |
| `SUMMARY_MODEL` | 모델 이름 |

`.env` 에는 비밀번호와 인증키가 평문으로 들어감. 파일 권한을 제한하고 복사·전송하지 말 것.

### 3. 데이터베이스 구축

```bash
python scripts/setup_db.py
```

DB 생성, 표 생성, 감시 대상 102건 등록을 한 번에 함. 여러 번 실행해도 기존
자료를 지우지 않음.

- `--check` : 상태만 확인 (변경 없음)
- `--skip-watchlist` : 표만 생성

### 4. 첫 자료 수집

```bash
python scripts/run_weekly.py            # 감지·비교 (LLM 비용 없음)
python scripts/run_weekly.py --full     # + 요약·보고서 (LLM 비용 발생)
```

102건을 순회하며 몇 분 걸림. 빈 DB에서 첫 실행 시 `documents` 101행,
`change_log` 101행, `article_diff` 1,108행이 채워짐.

배치는 항목마다 상태를 출력함.

| 상태 | 뜻 |
|---|---|
| `개정발생` | 새 버전을 받아 조문까지 비교함 |
| `신구법없음` | 법제처가 대비표를 제공하지 않음. 전문만 저장 |
| `조회결과없음` | 검색되지 않음. 제명 변경·폐지이므로 목록 확인 필요 |
| `매칭모호` | 같은 이름이 여러 건. 소관부처 지정 필요 |

한 건이 실패해도 나머지는 계속 처리함. 오류가 있으면 종료 코드 `1` 로 끝남.

### 5. 웹 서버

```bash
python -m webapp.app
```

<http://127.0.0.1:5000>

| 환경변수 | 기본값 | 설명 |
|---|---|---|
| `WEB_HOST` | `127.0.0.1` | 다른 PC에서 접속하려면 `0.0.0.0` |
| `WEB_PORT` | `5000` | |
| `WEB_DEBUG` | 꺼짐 | 켜면 브라우저에서 서버 코드를 실행할 수 있음. 원내 운영 중에는 켜지 말 것 |

## 원내 모델(Qwen)로 바꾸기

요약 단계만 바뀜. 개정 감지·조문 비교·전문 비교·문서 인용 확인은 LLM을 쓰지 않음.

### OpenAI 호환 엔드포인트인 경우

vLLM·Ollama·TGI 등은 대부분 해당함. 코드 수정 없이 `.env` 만 고침.

```bash
SUMMARY_PROVIDER=openai                        # 그대로 둠
SUMMARY_BASE_URL=http://원내주소:8000/v1
SUMMARY_MODEL=Qwen/Qwen3.5-122B-FP8
OPENAI_API_KEY=원내키                           # 인증이 없어도 빈 값은 안 됨
```

`SUMMARY_BASE_URL` 이 있으면 외부 OpenAI로 나가지 않음. 바꾼 뒤 확인:

```bash
python -m summarizer out/weekly_contract_<날짜>.json --dry-run --echo   # 호출 없이 프롬프트만
python scripts/run_weekly.py --full
```

### 자체 API인 경우

`summarizer/llm.py` 만 고침.

1. `OpenAIClient` 를 본떠 `QwenClient` 작성 — `complete_text()`, `complete_json()` 두 개 구현
2. `build_client()` 에 분기 추가
3. `summarizer/config.py` 의 `API_KEY_ENV` 에 `"qwen": "QWEN_API_KEY"` 추가

파이프라인·에이전트·프롬프트는 고치지 않음.

### 바꾼 뒤

- 요약 몇 건을 읽어 문체·길이를 확인
- 어느 모델로 만든 요약인지는 `law_summary.llm_model` 에 남음
- 응답이 잘리면 `SUMMARY_ARTICLE_MAX_TOKENS`, `SUMMARY_LAW_MAX_TOKENS` 를 올림
- 원내 서버 부하가 크면 `SUMMARY_MAX_WORKERS` 를 낮춤

이미 만든 요약은 다시 만들어지지 않음. 새로 감지된 개정분부터 새 모델을 씀.

## 자동 실행

Windows 작업 스케줄러에 등록함.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Verify   # 사전 점검
powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1           # 매주 월 06:00
powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 -DayOfWeek Friday -Time 18:30
```

실행 기록은 `out/logs/` 에 쌓임.

## 데이터베이스

표 5개를 씀. `documents` 의 전문이 출발점이고 나머지는 거기서 파생됨.

| 표 | 담는 것 | 첫 배치 직후 |
|---|---|---|
| `documents` | 법령·행정규칙 전문(법제처 원본) | 101행 |
| `watchlist` | 감시 대상 목록, 마지막으로 처리한 일련번호 | 102행 |
| `change_log` | 개정 이벤트 | 101행 |
| `article_diff` | 조문 단위 변경 | 1,108행 |
| `law_summary` | 요약 결과. 웹 화면이 읽는 표 | `--full` 로 채워짐 |

개정이 확인되면 새 전문을 추가하고 이전 버전은 지우지 않음.

개정 여부는 일련번호로 판단함. 법제처의 현행 일련번호와
`watchlist.last_serial_no` 를 비교해, 다르면 개정으로 보고 전문을 받음.
같으면 전문을 갖고 있는지 확인해 없으면 받음.

표에 값을 직접 넣지 말 것. `watchlist.last_serial_no` 를 임의로 바꾸면 그 법의
개정이 잡히지 않음.

감시 대상을 늘리려면 `scripts/load_watchlist.py` 의 목록에 추가하고 다시 실행함.

## 문제 해결

| 증상 | 확인 |
|---|---|
| `환경변수 ... 가 설정되지 않았습니다` | `.env` 파일과 값 |
| `PostgreSQL 연결 실패` | 서버 기동 여부, 계정·비밀번호. `python scripts/setup_db.py --check` |
| 웹 화면이 비어 있음 | 아직 수집 전. `python scripts/run_weekly.py --full` |
| 첫 화면 날짜가 오래됨 | 마지막 배치 날짜. 자동 실행 등록 확인 |
| 법령 조회 실패 | API 일시 장애일 수 있음. `out/logs/` 확인 후 재실행 |

```bash
python scripts/run_single_check.py 009199   # 법령 1건만 상세 실행
python scripts/check_document.py 문서.pdf    # 문서 1개 인용 확인
```

## 그 밖

내부 구조, 조문 위치 확정 방식, 표별 컬럼 정의, 산출물 JSON 형식은
[DEVELOPMENT.md](DEVELOPMENT.md) 에 있음.
