"""문서 텍스트 추출: PDF(pypdfium2), HWPX(zip + section*.xml).

반환 형식은 페이지(또는 섹션) 단위 리스트 — 매칭 결과에 위치를
붙이기 위함이다. HWPX 는 물리 페이지 개념이 없어 섹션 단위로 묶는다.

한계(알려진 것):
- 이미지 스캔 PDF 는 텍스트가 비어 나온다 (OCR 미지원).
- 페이지 경계에서 잘린 법령명은 페이지 단위 매칭에서 누락될 수 있다.
"""
import re
import unicodedata
import zipfile
from pathlib import Path

_HP_T = re.compile(r"<hp:t[^>]*>(.*?)</hp:t>", re.DOTALL)
_TAG = re.compile(r"<[^>]+>")


def extract_pdf(path: str) -> list[str]:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(path)
    try:
        return [pdf[i].get_textpage().get_text_bounded() for i in range(len(pdf))]
    finally:
        pdf.close()


def extract_hwpx(path: str) -> list[str]:
    pages = []
    with zipfile.ZipFile(path) as z:
        sections = sorted(
            n for n in z.namelist()
            if re.fullmatch(r"Contents/section\d+\.xml", n)
        )
        for name in sections:
            xml = z.read(name).decode("utf-8", errors="replace")
            texts = [_TAG.sub("", t) for t in _HP_T.findall(xml)]
            pages.append("\n".join(texts))
    return pages


def extract_text(path: str) -> list[str]:
    """확장자로 분기. 반환: 페이지/섹션별 텍스트(NFC 정규화)."""
    suffix = Path(path).suffix.lower()
    if suffix == ".pdf":
        pages = extract_pdf(path)
    elif suffix == ".hwpx":
        pages = extract_hwpx(path)
    else:
        raise ValueError(f"지원하지 않는 형식: {suffix} (pdf/hwpx만 지원)")
    return [unicodedata.normalize("NFC", p) for p in pages]
