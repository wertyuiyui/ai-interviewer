from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pymupdf as fitz


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "testdata" / "fake_resumes.json"
PDF_DIR = ROOT / "public" / "sample-resumes"
TEXT_DIR = ROOT / "testdata" / "resumes"
PAGE = fitz.paper_rect("a4")
MARGIN_X = 48
TOP = 44
BOTTOM = 44
BODY_SIZE = 10.5
LINE_HEIGHT = 16
LATIN_FONT_PATH = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")


def split_font_runs(
    text: str, latin_font: fitz.Font, cjk_font: fitz.Font
) -> list[tuple[str, fitz.Font]]:
    runs: list[tuple[str, fitz.Font]] = []
    current = ""
    current_font: fitz.Font | None = None
    for char in text:
        font = latin_font if ord(char) < 128 else cjk_font
        if current_font is not None and font is not current_font:
            runs.append((current, current_font))
            current = ""
        current += char
        current_font = font
    if current and current_font is not None:
        runs.append((current, current_font))
    return runs


def text_width(
    text: str, latin_font: fitz.Font, cjk_font: fitz.Font, size: float
) -> float:
    return sum(
        font.text_length(run, fontsize=size)
        for run, font in split_font_runs(text, latin_font, cjk_font)
    )


def wrap(
    text: str,
    latin_font: fitz.Font,
    cjk_font: fitz.Font,
    size: float,
    width: float,
) -> list[str]:
    lines: list[str] = []
    current = ""
    for char in text:
        candidate = current + char
        if current and text_width(candidate, latin_font, cjk_font, size) > width:
            lines.append(current)
            current = char
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def insert_line(
    page: fitz.Page,
    text: str,
    x: float,
    y: float,
    *,
    size: float,
    color: tuple[float, float, float],
    latin_font: fitz.Font,
    cjk_font: fitz.Font,
) -> None:
    cursor = x
    for run, font in split_font_runs(text, latin_font, cjk_font):
        is_latin = font is latin_font
        page.insert_text(
            (cursor, y),
            run,
            fontname=(
                "resume-latin"
                if is_latin and LATIN_FONT_PATH.exists()
                else ("helv" if is_latin else "china-s")
            ),
            fontfile=str(LATIN_FONT_PATH) if is_latin and LATIN_FONT_PATH.exists() else None,
            fontsize=size,
            color=color,
        )
        cursor += font.text_length(run, fontsize=size)


def render_resume(profile: dict[str, Any], disclaimer: str) -> Path:
    document = fitz.open()
    cjk_font = fitz.Font(fontname="china-s")
    latin_font = (
        fitz.Font(fontfile=str(LATIN_FONT_PATH))
        if LATIN_FONT_PATH.exists()
        else fitz.Font(fontname="helv")
    )
    page = document.new_page(width=PAGE.width, height=PAGE.height)
    y = TOP

    def next_page() -> fitz.Page:
        nonlocal page, y
        page = document.new_page(width=PAGE.width, height=PAGE.height)
        y = TOP
        return page

    def add_wrapped(
        text: str,
        *,
        size: float = BODY_SIZE,
        color: tuple[float, float, float] = (0.12, 0.16, 0.22),
        indent: float = 0,
        gap: float = LINE_HEIGHT,
    ) -> None:
        nonlocal page, y
        available = PAGE.width - 2 * MARGIN_X - indent
        for line in wrap(text, latin_font, cjk_font, size, available):
            if y > PAGE.height - BOTTOM:
                page = next_page()
            insert_line(
                page,
                line,
                MARGIN_X + indent,
                y,
                size=size,
                color=color,
                latin_font=latin_font,
                cjk_font=cjk_font,
            )
            y += gap

    add_wrapped(disclaimer, size=8.2, color=(0.55, 0.18, 0.15), gap=12)
    y += 6
    add_wrapped(profile["name"], size=22, color=(0.06, 0.12, 0.22), gap=26)
    add_wrapped(profile["target"], size=12, color=(0.08, 0.35, 0.55), gap=18)
    add_wrapped(profile["contact"], size=9, color=(0.35, 0.39, 0.45), gap=14)
    y += 6

    text_parts = [disclaimer, profile["name"], profile["target"], profile["contact"]]
    for section in profile["sections"]:
        if y > PAGE.height - BOTTOM - 46:
            page = next_page()
        page.draw_line(
            (MARGIN_X, y - 7),
            (PAGE.width - MARGIN_X, y - 7),
            color=(0.77, 0.82, 0.88),
            width=0.8,
        )
        add_wrapped(section["title"], size=13, color=(0.04, 0.26, 0.43), gap=19)
        text_parts.append(section["title"])
        for line in section["lines"]:
            add_wrapped(line, indent=8 if line.startswith("•") else 0)
            text_parts.append(line)
        y += 5

    for index, pdf_page in enumerate(document, start=1):
        footer = f"AI 模拟面试官测试数据｜第 {index}/{document.page_count} 页"
        width = text_width(footer, latin_font, cjk_font, 8)
        insert_line(
            pdf_page,
            footer,
            PAGE.width - MARGIN_X - width,
            PAGE.height - 22,
            size=8,
            color=(0.45, 0.49, 0.55),
            latin_font=latin_font,
            cjk_font=cjk_font,
        )

    document.set_metadata(
        {
            "title": f"{profile['name']} - {profile['target']}（虚构测试简历）",
            "author": "AI 模拟面试官测试数据",
            "subject": disclaimer,
            "keywords": "synthetic,fake,resume,testing",
            "creator": "scripts/generate_fake_resumes.py",
            "producer": "PyMuPDF",
        }
    )
    output = PDF_DIR / f"{profile['slug']}.pdf"
    document.save(output, garbage=4, deflate=True)
    document.close()

    (TEXT_DIR / f"{profile['slug']}.txt").write_text(
        "\n".join(text_parts) + "\n", encoding="utf-8"
    )
    return output


def main() -> None:
    payload = json.loads(SOURCE.read_text(encoding="utf-8"))
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    TEXT_DIR.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, str]] = []
    for profile in payload["resumes"]:
        output = render_resume(profile, payload["disclaimer"])
        manifest.append(
            {
                "name": profile["name"],
                "target": profile["target"],
                "file": output.name,
                "url": f"/sample-resumes/{output.name}",
            }
        )
    (PDF_DIR / "manifest.json").write_text(
        json.dumps({"items": manifest}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"generated {len(manifest)} fake resumes in {PDF_DIR}")


if __name__ == "__main__":
    main()
