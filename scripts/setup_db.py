"""배포용 DB 구축 — 빈 PostgreSQL 서버에서 한 번 실행하면 끝나게 함.

하는 일 (순서대로)
    1) .env 를 읽어 접속 정보를 확인함
    2) PostgreSQL 서버에 붙음 (유지보수 DB: postgres)
    3) 대상 DB 가 없으면 UTF8 로 만듦
    4) database/schema.sql 을 적용함 (전부 IF NOT EXISTS 라 여러 번 돌려도 안전)
    5) 감시 대상 102건을 watchlist 에 적재함
    6) 만들어진 것을 세어 보여 줌

사용법
    python scripts/setup_db.py                 # 전체
    python scripts/setup_db.py --skip-watchlist # 스키마까지만
    python scripts/setup_db.py --check          # 아무것도 바꾸지 않고 현재 상태만 확인

CREATE DATABASE 를 schema.sql 이 하지 않는 이유: PostgreSQL 은
  CREATE DATABASE 에 IF NOT EXISTS 가 없고, 한 트랜잭션 안에서 DB 를 만든 뒤
  그 DB 로 접속을 옮길 수도 없음. 그래서 그 한 단계만 이 스크립트가 맡음.

이 스크립트는 기존 데이터를 지우지 않음. 처음부터 다시 만들려면 DB 를
  손으로 지운 뒤(dropdb) 다시 실행할 것 — 실수로 운영 데이터를 날리는 일이
  없도록 --drop 같은 옵션은 일부러 두지 않았음.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import psycopg2
from psycopg2 import sql as pgsql

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from lawtrack.config import load_settings  # noqa: E402

SCHEMA = ROOT / "database" / "schema.sql"

#: 스키마가 만들어야 하는 표. 검증 단계에서 이 목록대로 세어 봄.
TABLES = ("documents", "watchlist", "change_log", "article_diff", "law_summary")


def step(n: int, text: str) -> None:
    print(f"\n[{n}/6] {text}")


def connect(settings, dbname: str, *, autocommit: bool = False):
    kwargs = settings.db.as_connect_kwargs()
    kwargs["dbname"] = dbname
    kwargs["client_encoding"] = "UTF8"
    conn = psycopg2.connect(**kwargs)
    conn.autocommit = autocommit
    return conn


def ensure_database(settings) -> bool:
    """대상 DB 가 없으면 만듦. 반환값: 이번에 새로 만들었는가.

    커넥션을 `with` 로 감싸지 않음. psycopg2 에서 `with conn:` 은 그 블록을
      트랜잭션으로 묶는데, autocommit=True 로 열어 두었어도 블록 안에서 첫
      문장을 실행하는 순간 BEGIN 상태가 됨(실측: status 1 → 2). 그 상태에서
      CREATE DATABASE 를 부르면 "cannot run inside a transaction block" 으로
      막힘. 그래서 여기서만 try/finally 로 직접 닫음.
    """
    target = settings.db.database
    conn = connect(settings, "postgres", autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target,))
            if cur.fetchone():
                print(f"      이미 있음: {target}")
                return False
            cur.execute(
                pgsql.SQL("CREATE DATABASE {} ENCODING 'UTF8' TEMPLATE template0")
                .format(pgsql.Identifier(target))
            )
            print(f"      생성함: {target} (UTF8)")
            return True
    finally:
        conn.close()


def apply_schema(settings) -> None:
    if not SCHEMA.exists():
        raise SystemExit(f"스키마 파일이 없습니다: {SCHEMA}")
    ddl = SCHEMA.read_text(encoding="utf-8")
    with connect(settings, settings.db.database) as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)
        conn.commit()
    print(f"      {SCHEMA.name} 적용 완료 ({len(ddl):,}자)")


def load_watchlist_data() -> None:
    # scripts/load_watchlist.py 가 데이터와 적재 로직을 모두 갖고 있음.
    import load_watchlist

    rc = load_watchlist.main()
    if rc:
        raise SystemExit(f"watchlist 적재 실패 (코드 {rc})")


def report(settings) -> None:
    with connect(settings, settings.db.database) as conn:
        with conn.cursor() as cur:
            for table in TABLES:
                cur.execute(
                    "SELECT to_regclass(%s) IS NOT NULL", (f"public.{table}",))
                if not cur.fetchone()[0]:
                    print(f"      {table:<14} ✗ 없음")
                    continue
                cur.execute(
                    pgsql.SQL("SELECT count(*) FROM {}").format(pgsql.Identifier(table)))
                print(f"      {table:<14} {cur.fetchone()[0]:>6,}행")

            cur.execute("SELECT to_regclass('public.watchlist') IS NOT NULL")
            if cur.fetchone()[0]:
                cur.execute(
                    "SELECT law_type, count(*) FROM watchlist GROUP BY 1 ORDER BY 2 DESC")
                kinds = ", ".join(f"{t} {n}" for t, n in cur.fetchall())
                print(f"      감시 대상 구성 : {kinds}")
                # 한글이 깨지지 않고 들어갔는지 눈으로 확인할 표본 하나
                cur.execute(
                    "SELECT official_name FROM watchlist ORDER BY law_id LIMIT 1")
                row = cur.fetchone()
                if row:
                    print(f"      한글 표본      : {row[0]}")


def main() -> int:
    ap = argparse.ArgumentParser(description="법령 개정감지 시스템 DB 구축")
    ap.add_argument("--skip-watchlist", action="store_true",
                    help="스키마까지만 만들고 감시 대상 적재는 건너뛴다")
    ap.add_argument("--check", action="store_true",
                    help="아무것도 바꾸지 않고 현재 상태만 확인한다")
    args = ap.parse_args()

    step(1, ".env 확인")
    try:
        settings = load_settings()
    except Exception as exc:
        print(f"      실패: {exc}")
        print("      .env.example 을 .env 로 복사하고 값을 채웠는지 확인하세요.")
        return 1
    db = settings.db
    print(f"      {db.user}@{db.host}:{db.port} / {db.database}")

    step(2, "PostgreSQL 서버 접속")
    try:
        with connect(settings, "postgres", autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version()")
                print(f"      {cur.fetchone()[0].split(',')[0]}")
    except psycopg2.OperationalError as exc:
        print(f"      실패: {exc}")
        print("      PostgreSQL 이 켜져 있는지, 계정/비밀번호가 맞는지 확인하세요.")
        return 1

    if args.check:
        step(3, "확인만 하므로 DB 생성 건너뜀")
        step(4, "확인만 하므로 스키마 적용 건너뜀")
        step(5, "확인만 하므로 감시 대상 적재 건너뜀")
        step(6, "현재 상태")
        try:
            report(settings)
        except psycopg2.OperationalError:
            print(f"      DB({db.database})가 아직 없습니다 — --check 없이 실행하세요.")
        return 0

    step(3, f"데이터베이스 {db.database}")
    ensure_database(settings)

    step(4, "스키마 적용")
    apply_schema(settings)

    step(5, "감시 대상 적재")
    if args.skip_watchlist:
        print("      건너뜀 (--skip-watchlist)")
    else:
        load_watchlist_data()

    step(6, "결과 확인")
    report(settings)

    print("\n구축 완료. 다음 단계:")
    print("    python scripts/run_weekly.py --full     # 첫 배치 수집·요약")
    print("    python -m webapp.app                    # 웹 서버")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
