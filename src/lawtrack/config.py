"""설정.

모든 설정은 환경변수(.env)에서 읽는다.

getpass 대화형 입력을 쓰지 않는 이유:
    주 1회 자동 실행(cron)이 요구사항인데, getpass 는 입력 대기로 멈춘다.
    스케줄러에서는 절대 동작하지 않는다.
"""

from __future__ import annotations

import logging
import os
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
    마이페이지 > API인증키관리에 등록되어 있어야 한다.
    미등록 시 HTTP 200 + {"result": "사용자 정보 검증에 실패하였습니다."} 가 온다."""

    search_url: str = "https://www.law.go.kr/DRF/lawSearch.do"
    service_url: str = "https://www.law.go.kr/DRF/lawService.do"

    response_type: str = "JSON"

    display: int = 100
    """목록조회 1페이지 건수. 반드시 최대치(100)로 고정한다.

    실측: 검색어 '에너지법' → totalCnt=32 인데 기본 display 로는 20건만 수신되고,
    정답인 '에너지법' 은 가나다순 17번째라 아슬아슬하게 걸렸다.
    display=10 이었다면 정답을 아예 수신하지 못했다.
    검색어가 짧을수록 위험도가 올라간다."""

    timeout: float = 30.0
    max_retries: int = 3
    """네트워크 순간 끊김 대비. 111건 전수 적재에서는 반드시 발생한다.
    단, 인증 오류는 재시도해도 의미가 없으므로 client 에서 즉시 중단한다."""

    backoff_base: float = 1.0
    """지수 백오프 기준(초). 1s → 2s → 4s"""

    rate_limit_sleep: float = 0.2
    """호출 간 최소 간격(초). 약 5 req/s."""

    user_agent: str = "law-tracking/1.0"


@dataclass(frozen=True)
class DbSettings:
    """MySQL 접속 설정."""

    host: str
    port: int
    user: str
    password: str
    database: str
    charset: str = "utf8mb4"

    def as_connect_kwargs(self) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": self.password,
            "database": self.database,
            "charset": self.charset,
        }


@dataclass(frozen=True)
class ExportSettings:
    """LLM 팀 전달용 산출물 설정."""

    output_dir: Path = field(default=PROJECT_ROOT / "out")
    contract_version: str = "1.0"


@dataclass(frozen=True)
class Settings:
    api: ApiSettings
    db: DbSettings
    export: ExportSettings
    log_level: str = "INFO"


def load_settings(env_file: str | Path | None = None) -> Settings:
    """환경변수에서 설정을 읽는다.

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

    db = DbSettings(
        host=os.environ.get("MYSQL_HOST", "127.0.0.1").strip(),
        port=_int("MYSQL_PORT", 3306),
        user=os.environ.get("MYSQL_USER", "root").strip(),
        password=_require("MYSQL_PASSWORD"),
        database=os.environ.get("MYSQL_DATABASE", "law_tracking_db").strip(),
    )

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
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )