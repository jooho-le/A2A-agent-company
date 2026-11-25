#!/usr/bin/env python3
"""Combine editable slide folders into the existing standalone presentation.

Requires Python 3 only. No web server, packages, API calls, or paid services.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import mimetypes
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT.parent / 'capstone-presentation-draft.html'
START = '<!-- AIO:SLIDE:START -->'
END = '<!-- AIO:SLIDE:END -->'
NOTES_START = '<!-- AIO:NOTES:START -->'
NOTES_END = '<!-- AIO:NOTES:END -->'
CSS_URL = re.compile(r'url\(\s*([\'\"]?)(.*?)\1\s*\)', re.S)
IMAGE_SRC = re.compile(r'(<img\b[^>]*?\bsrc=)([\'\"])(.*?)\2', re.S | re.I)


def read_manifest() -> dict:
    manifest = json.loads((ROOT / 'manifest.json').read_text(encoding='utf-8'))
    pages = manifest['pages']
    if not pages:
        raise ValueError('manifest.json에 페이지가 없습니다.')
    folders = [p['folder'] for p in pages]
    if len(set(folders)) != len(folders):
        raise ValueError('페이지 폴더가 중복되어 있습니다.')
    for number, page in enumerate(pages, 1):
        if not re.fullmatch(r'\d+', page['folder']):
            raise ValueError('페이지 폴더명은 숫자여야 합니다.')
        if page['pdf_page'] != number:
            raise ValueError('pdf_page는 출력 순서대로 1부터 연속이어야 합니다.')
    return manifest


def read_slide(page: dict) -> tuple[str, str]:
    folder = ROOT / page['folder']
    document = (folder / 'index.html').read_text(encoding='utf-8')
    if document.count(START) != 1 or document.count(END) != 1:
        raise ValueError(f'{folder.name}/index.html: SLIDE 시작·끝 표시를 유지해 주세요.')
    section = document.split(START, 1)[1].split(END, 1)[0].strip()
    if not re.match(r'<section\b', section) or not section.endswith('</section>'):
        raise ValueError(f'{folder.name}: 장표는 section 하나로 감싸야 합니다.')
    marker = f'data-editable-page="{page["folder"]}"'
    if marker not in section.split('>', 1)[0]:
        raise ValueError(f'{folder.name}: {marker} 속성을 유지해 주세요.')
    match = re.search(r'\bdata-title="([^"]*)"', section.split('>', 1)[0])
    if not match:
        raise ValueError(f'{folder.name}: data-title 제목이 필요합니다.')
    notes = (folder / 'notes.html').read_text(encoding='utf-8')
    if notes.count(NOTES_START) != 1 or notes.count(NOTES_END) != 1:
        raise ValueError(f'{folder.name}/notes.html: NOTES 시작·끝 표시를 유지해 주세요.')
    notes = notes.split(NOTES_START, 1)[1].split(NOTES_END, 1)[0].strip()
    section = section[:-len('</section>')] + '\n<aside class="speaker-note" hidden="">' + notes + '</aside>\n</section>'
    # Only initial visibility changes; the existing deck runtime handles navigation.
    opening, rest = section.split('>', 1)
    opening = re.sub(r'\s+hidden(?:="[^"]*")?(?=[\s>])', '', opening)
    if page['pdf_page'] != 1:
        opening += ' hidden=""'
    section = opening + '>' + rest
    return section, html.unescape(match[1])


def inline_assets(text: str, base: Path, cache: dict[Path, str]) -> str:
    def resource(value: str) -> str:
        if value.startswith(('data:', 'http:', 'https:', '//', '#')):
            return value
        file = (base / value).resolve()
        if not file.is_relative_to(ROOT):
            raise ValueError(f'편집 폴더 바깥의 이미지·폰트는 삽입하지 않습니다: {value}')
        mime = mimetypes.guess_type(file.name)[0]
        if not mime or not mime.startswith(('image/', 'font/')):
            raise ValueError(f'이미지·폰트 파일만 자동 삽입합니다: {value}')
        if file not in cache:
            cache[file] = f'data:{mime};base64,' + base64.b64encode(file.read_bytes()).decode('ascii')
        return cache[file]

    text = CSS_URL.sub(lambda m: 'url("' + resource(m[2]) + '")', text)
    return IMAGE_SRC.sub(lambda m: m[1] + m[2] + resource(m[3]) + m[2], text)


def gallery(pages: list[dict], titles: list[str]) -> str:
    cards = '\n'.join(
        f'<a href="{p["folder"]}/index.html"><b>{p["pdf_page"]:02d}</b><span>{html.escape(t)}</span></a>'
        for p, t in zip(pages, titles)
    )
    return f'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AIO · 페이지별 장표 편집</title>
<style>
:root{{color-scheme:dark;font-family:-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo",sans-serif;background:#030407;color:#f0f2ec}}
body{{max-width:1200px;margin:0 auto;padding:60px 30px}}h1{{font-size:40px;margin:12px 0 20px}}p{{color:#aaaeb8;line-height:1.8}}
.label{{color:#d4ff24}}.links{{display:flex;gap:20px;margin:25px 0 40px}}.links a{{color:#d4ff24}}
nav{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}}
nav a{{display:flex;align-items:center;gap:20px;padding:22px;color:inherit;text-decoration:none;border:1px solid #30353e;border-radius:14px;background:#111318}}
nav a:hover,nav a:focus-visible{{border-color:#d4ff24;background:#181b21}}nav b{{color:#d4ff24;font-size:30px}}nav span{{line-height:1.5}}
code{{color:#d5e5ff}}a:focus-visible{{outline:2px solid #d4ff24;outline-offset:4px}}
</style></head><body><div class="label">AIO / EDITABLE PRESENTATION</div><h1>페이지별 장표 편집</h1>
<p>폴더 번호는 현재 PDF 출력 순서입니다. 각 <code>index.html</code>의 <b>[화면 글자]</b> 표시 아래 문구를 수정하세요.<br>
색상·크기·위치는 <code>style.css</code>, 화면에 안 나오는 발표 메모는 <code>notes.html</code>에 있습니다.<br>
개별 페이지는 새로고침하면 반영됩니다. <b>통합본</b>을 보고 있다면 <code>python3 docs/Presentation/editable-slides/build.py</code>로 다시 합쳐야 합니다.</p>
<div class="links"><a href="../capstone-presentation-draft.html">전체 발표자료 열기 ↗</a><a href="README.md">수정·출력 방법</a></div>
<nav aria-label="PDF 페이지 목록">{cards}</nav></body></html>'''


def build(output: Path | None) -> int:
    manifest = read_manifest()
    template = (ROOT / 'shared/template.html').read_text(encoding='utf-8')
    required = ['<!-- AIO:COMMON_CSS -->', '<!-- AIO:PAGE_CSS -->', '<!-- AIO:SLIDES -->']
    for token in required:
        if template.count(token) != 1:
            raise ValueError(f'공통 template.html의 {token} 표시가 필요합니다.')
    cache: dict[Path, str] = {}
    common = inline_assets((ROOT / 'shared/global.css').read_text(encoding='utf-8'), ROOT / 'shared', cache)
    sections, styles, titles = [], [], []
    for page in manifest['pages']:
        folder = ROOT / page['folder']
        section, title = read_slide(page)
        sections.append(inline_assets(section, folder, cache))
        css = inline_assets((folder / 'style.css').read_text(encoding='utf-8'), folder, cache)
        if '</style' in css.lower():
            raise ValueError(f'{page["folder"]}/style.css: HTML 태그를 넣을 수 없습니다.')
        styles.append(f'/* PDF {page["pdf_page"]:02d} */\n{css}')
        titles.append(title)
    result = inline_assets(template, ROOT / 'shared', cache)
    result = result.replace(required[0], '<style>\n' + common + '\n</style>')
    result = result.replace(required[1], '<style id="editable-page-styles">\n' + '\n'.join(styles) + '\n</style>')
    result = result.replace(required[2], '\n'.join(sections))
    result = re.sub(r'data-slide-count="\d+"', f'data-slide-count="{len(sections)}"', result, count=1)
    if output:
        output = output.resolve()
        if output.is_relative_to(ROOT / 'shared') or any(output.is_relative_to(ROOT / p['folder']) for p in manifest['pages']):
            raise ValueError('출력 파일로 편집 원본을 덮어쓸 수 없습니다.')
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(result, encoding='utf-8')
        (ROOT / 'index.html').write_text(gallery(manifest['pages'], titles), encoding='utf-8')
        print(f'{len(sections)}장 통합 완료: {output}')
        print('PDF: 통합 HTML을 Chrome에서 열고 상단 “PDF 인쇄” → “PDF로 저장”')
    else:
        print(f'{len(sections)}개 페이지·스타일·이미지 확인 완료. 파일 변경 없음.')
    return len(sections)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT, help='통합 HTML 경로')
    parser.add_argument('--check', action='store_true', help='필수 파일·표시·이미지만 확인; 출력하지 않음')
    args = parser.parse_args()
    try:
        build(None if args.check else args.output)
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f'장표 통합 실패: {error}\n')


if __name__ == '__main__':
    main()
