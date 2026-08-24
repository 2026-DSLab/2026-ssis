"""배포용 꾸러미 만들기 — 넘길 것만 골라 zip 하나로 묶음.

왜 필요한가
    저장소에는 넘길 코드 말고도 보고서 원고, 발표자료, 예시 문서, 그동안 만든
    배치 산출물(out/)이 함께 있음. 그대로 압축해 보내면 받는 쪽이 무엇이
    프로그램이고 무엇이 참고자료인지 가릴 수 없고, .env 처럼 보내면 안 되는
    것이 섞여 나갈 위험도 있음.

사용법
    python scripts/make_release.py                 # dist/lawtrack-1.0.0.zip
    python scripts/make_release.py --out-dir C:/넘길폴더
    python scripts/make_release.py --dry-run       # 무엇이 들어가는지만 출력

.env 는 절대 넣지 않음(비밀번호·API 키). .env.example 만 넣고, 받는 쪽이
  값을 채우게 함 — README "환경변수" 절에 그렇게 적혀 있음.
"""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 꾸러미에 담을 것. 디렉터리는 통째로, 파일은 그 파일만.
INCLUDE = [
    "src",           # 감지·비교 (lawtrack, doc_match)
    "summarizer",    # 요약·검증·보고서
    "webapp",        # 웹 화면
    "scripts",       # 구축·배치·자동실행
    "database",      # 스키마·워치리스트
    "tests",         # 회귀 테스트 — 받는 쪽이 직접 돌려 확인할 수 있게 함께 넘긴다
    "README.md",         # 담당자용 — 설치·운영
    "DEVELOPMENT.md",    # 개발자용 — 내부 구조·설계 근거
    "pyproject.toml",
    "requirements.txt",
    ".env.example",
    ".gitignore",
]

#: 위 목록 안에 있어도 걸러낼 것.
EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
                ".git", ".claude", "node_modules"}
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".log", ".hwpx", ".pdf", ".pptx", ".xlsx"}
EXCLUDE_NAMES = {".env", "Thumbs.db", ".DS_Store"}


def wanted(path: Path) -> bool:
    if any(part in EXCLUDE_DIRS for part in path.parts):
        return False
    if path.name in EXCLUDE_NAMES:
        return False
    # 확장자 거르기는 '문서'를 빼려는 것이지 자료를 빼려는 게 아님.
    #   tests/fixtures 안에는 회귀 테스트가 실제로 읽는 PDF 가 있음
    #   (test_doc_match.py 의 표준가이드요약본.pdf). 여기만 예외를 둠 —
    #   빼고 넘기면 받는 쪽에서 pytest 가 그 파일이 없다고 실패함.
    in_fixtures = "fixtures" in path.parts
    if not in_fixtures and path.suffix.lower() in EXCLUDE_SUFFIX:
        return False
    return True


def collect() -> list[Path]:
    files: list[Path] = []
    for entry in INCLUDE:
        target = ROOT / entry
        if not target.exists():
            print(f"  ! 없음, 건너뜀: {entry}")
            continue
        if target.is_file():
            if wanted(target):
                files.append(target)
            continue
        for p in sorted(target.rglob("*")):
            if p.is_file() and wanted(p):
                files.append(p)
    return files


def version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.strip().startswith("version"):
            return line.split("=", 1)[1].strip().strip('"')
    return "0.0.0"


def main() -> int:
    ap = argparse.ArgumentParser(description="배포용 zip 만들기")
    ap.add_argument("--out-dir", default=str(ROOT / "dist"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    files = collect()
    total = sum(f.stat().st_size for f in files)
    print(f"담을 파일 {len(files)}개 / {total / 1024 / 1024:.1f}MB")

    by_top: dict[str, int] = {}
    for f in files:
        by_top[f.relative_to(ROOT).parts[0]] = by_top.get(f.relative_to(ROOT).parts[0], 0) + 1
    for name, n in sorted(by_top.items(), key=lambda kv: -kv[1]):
        print(f"    {name:<20} {n:>4}개")

    # 넣으면 안 되는 것이 섞이지 않았는지 마지막으로 확인함.
    leaked = [f for f in files if f.name == ".env"]
    if leaked:
        raise SystemExit(f"중단: .env 가 목록에 있습니다 — {leaked}")

    if args.dry_run:
        print("\n--dry-run 이라 파일을 만들지 않았습니다.")
        return 0

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"lawtrack-{version()}"
    zip_path = out_dir / f"{stem}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, Path(stem) / f.relative_to(ROOT))
    print(f"\n생성: {zip_path}  ({zip_path.stat().st_size / 1024 / 1024:.1f}MB)")
    print("받는 쪽 순서: 압축 해제 → .env.example 을 .env 로 복사해 값 채우기 →")
    print("             pip install -e \".[openai,web,dev]\" → python scripts/setup_db.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
