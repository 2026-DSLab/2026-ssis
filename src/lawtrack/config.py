"""설정.

모든 설정은 환경변수(.env)에서 읽음.

getpass 대화형 입력을 쓰지 않는 이유:
    주 1회 자동 실행(cron)이 요구사항인데, getpass 는 입력 대기로 멈춤.
    스케줄러에서는 절대 동작하지 않음.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # dotenv 미설치 시에도 OS 환경변수로 동작
    def load_dotenv(*_args, **_kwargs):  # type: ignore[misc]
        return False


PROJECT_ROOT = Path(__file__).resolve().parents[2]

log = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    """필수 설정 누락."""


def _require(key: str) -> str:
    value = os.environ.get(key, "").strip()
    if not value:
        raise ConfigError(
            f"환경변수 {key} 가 설정되지 않았습니다. .env 파일을 확인하세요."
        )
    return value


def _int(key: str, default: int) -> int:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"환경변수 {key} 는 정수여야 합니다: {raw!r}") from exc


def _float(key: str, default: float) -> float:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"환경변수 {key} 는 숫자여야 합니다: {raw!r}") from exc


@dataclass(frozen=True)
class ApiSettings:
    """국가법령정보 Open API 설정."""

    oc: str
    """인증키(OC). 이 키만으로는 부족하며, 호출 서버의 공인 IP/도메인이
    마이페이지 > API인증키관리에 등록되어 있어야 함.
    미등록 시 HTTP 200 + {"result": "사용자 정보 검증에 실패하였습니다."} 가 옴."""

    search_url: str = "https://www.law.go.kr/DRF/lawSearch.do"
    service_url: str = "https://www.law.go.kr/DRF/lawService.do"

    response_type: str = "JSON"

    display: int = 100
    """목록조회 1페이지 건수. 반드시 최대치(100)로 고정함.

    실측: 검색어 '에너지법' → totalCnt=32 인데 기본 display 로는 20건만 수신되고,
    정답인 '에너지법' 은 가나다순 17번째라 아슬아슬하게 걸렸음.
    display=10 이었다면 정답을 아예 수신하지 못했음.
    검색어가 짧을수록 위험도가 올라감."""

    timeout: float = 30.0
    max_retries: int = 3
    """네트워크 순간 끊김 대비. 111건 전수 적재에서는 반드시 발생함.
    단, 인증 오류는 재시도해도 의미가 없으므로 client 에서 즉시 중단함."""

    backoff_base: float = 1.0
    """지수 백오프 기준(초). 1s → 2s → 4s"""

    rate_limit_sleep: float = 0.2
    """호출 간 최소 간격(초). 약 5 req/s."""

    user_agent: str = "law-tracking/1.0"


@dataclass(frozen=True)
class DbSettings:
    """PostgreSQL 접속 설정."""

    host: str
    port: int
    user: str
    password: str
    database: str

    def as_connect_kwargs(self) -> dict:
        # psycopg2.connect() 는 database 가 아니라 dbname 을 받음.
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": self.password,
            "dbname": self.database,
        }


@dataclass(frozen=True)
class ExportSettings:
    """요약 단계 전달용 산출물 설정."""

    output_dir: Path = field(default=PROJECT_ROOT / "out")
    contract_version: str = "1.0"


@dataclass(frozen=True)
class Settings:
    api: ApiSettings
    db: DbSettings
    export: ExportSettings
    log_level: str = "INFO"


def load_db_settings(env_file: str | Path | None = None) -> DbSettings:
    """DB 설정만 읽음.

    load_settings() 와 따로 두는 이유: 요약 파이프라인(summarizer)은 DB 에
    요약을 적재하지만 국가법령정보 API 는 부르지 않음. load_settings() 를
    쓰면 쓰지도 않는 LAW_API_OC 가 없다는 이유로 실패함.
    """
    path = Path(env_file) if env_file else PROJECT_ROOT / ".env"
    if path.exists():
        load_dotenv(path, override=False)
        log.debug(".env 로드: %s", path)

    return DbSettings(
        host=os.environ.get("POSTGRES_HOST", "127.0.0.1").strip(),
        port=_int("POSTGRES_PORT", 5432),
        user=os.environ.get("POSTGRES_USER", "postgres").strip(),
        password=_require("POSTGRES_PASSWORD"),
        database=os.environ.get("POSTGRES_DATABASE", "law_tracking_db").strip(),
    )


def load_settings(env_file: str | Path | None = None) -> Settings:
    """환경변수에서 설정을 읽음.

    우선순위: 이미 설정된 OS 환경변수 > .env 파일
    """
    path = Path(env_file) if env_file else PROJECT_ROOT / ".env"
    if path.exists():
        load_dotenv(path, override=False)
        log.debug(".env 로드: %s", path)

    api = ApiSettings(
        oc=_require("LAW_API_OC"),
        response_type=os.environ.get("LAW_API_TYPE", "JSON").strip() or "JSON",
        display=_int("LAW_API_DISPLAY", 100),
        timeout=_float("LAW_API_TIMEOUT", 30.0),
        max_retries=_int("LAW_API_MAX_RETRIES", 3),
        backoff_base=_float("LAW_API_BACKOFF_BASE", 1.0),
        rate_limit_sleep=_float("LAW_API_RATE_LIMIT_SLEEP", 0.2),
    )

    db = load_db_settings(path)

    export = ExportSettings(
        output_dir=Path(os.environ.get("EXPORT_DIR", str(PROJECT_ROOT / "out"))),
        contract_version=os.environ.get("CONTRACT_VERSION", "1.0").strip(),
    )

    return Settings(
        api=api,
        db=db,
        export=export,
        log_level=os.environ.get("LOG_LEVEL", "INFO").strip().upper(),
    )


def setup_logging(level: str = "INFO") -> None:
    setup_console()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def setup_console() -> None:
    """콘솔에 못 쓰는 글자가 나와도 배치가 죽지 않게 함.

    실측: 한국어 Windows 콘솔의 기본 코드페이지는 cp949 인데,
      요약의 caveat 문장에는 em dash(—)가 들어감. cp949 에 그 글자가
      없어서 print 하는 순간 UnicodeEncodeError 로 프로세스가 죽었음 —
      요약을 다 만들어 놓고 화면에 뿌리다가 죽는 것이라, 그때까지의
      LLM 호출 비용을 그대로 날림.

      encoding 을 바꾸지 않고 errors 만 바꾸는 이유: utf-8 로 강제하면
      cp949 콘솔에서는 전부 깨져 보임. 못 쓰는 글자 하나를 '?'로
      바꾸는 편이 나음. 스케줄 실행(weekly.cmd)은 PYTHONUTF8=1 로
      아예 UTF-8 모드라 이 대체 자체가 일어나지 않음.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            # 파이프·리다이렉트로 교체된 스트림은 reconfigure 가 없을 수 있음.
            pass