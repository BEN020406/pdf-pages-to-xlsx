# -*- coding: utf-8 -*-
"""把「按表格线还原」的结果画成图，和原始页面并排看。

用法：python tools/render_layout_preview.py <pdf> <页码> [输出png]
说明：这是按同一个 block 数据结构渲染的预览，坐标与 PDF 页面一致，
      用来判断合并/居中/列宽对不对；不是 Excel 的真实渲染。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitz
from PIL import Image, ImageDraw, ImageFont

from core.layout import analyze_page
from core.ocr import run_ocr
from core.pipeline import render_page

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simsun.ttc",
]


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for p in ([r"C:\Windows\Fonts\msyhbd.ttc"] if bold else []) + FONT_CANDIDATES:
        try:
            return ImageFont.truetype(p, max(8, int(size)))
        except Exception:  # noqa: BLE001
            continue
    return ImageFont.load_default()


def render_blocks(blocks, size: tuple[int, int], dy: float = 0.0) -> Image.Image:
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)

    for blk in blocks:
        ys = [blk.y1 + dy]
        for h in blk.row_heights:
            ys.append(ys[-1] + h)
        xs = [blk.x1]
        for w in blk.col_widths:
            xs.append(xs[-1] + w)

        # 先画整张网格线，再由合并区覆盖 —— 和 Excel 里「整块加边框」一致
        for x in xs:
            d.line([(x, ys[0]), (x, ys[-1])], fill=(0, 0, 0), width=2)
        for y in ys:
            d.line([(xs[0], y), (xs[-1], y)], fill=(0, 0, 0), width=2)

        for cell in blk.cells:
            x0, y0 = xs[cell.col], ys[cell.row]
            x1 = xs[min(len(xs) - 1, cell.col + cell.colspan)]
            y1 = ys[min(len(ys) - 1, cell.row + cell.rowspan)]
            if cell.is_footnote:
                # 脚注：跨整行、灰字、不画框线
                px_size = cell.font_size * 200.0 / 72.0
                f = _font(px_size)
                lines = _wrap(d, cell.text, f, max(20, (x1 - x0) - 8))
                ty = y0 + 2
                for ln in lines:
                    d.text((x0 + 4, ty), ln, fill=(90, 90, 90), font=f)
                    ty += px_size * 1.2
                continue
            if cell.merged:
                d.rectangle([x0, y0, x1, y1], outline=(0, 0, 0), width=2)
            if not cell.text:
                continue

            px_size = cell.font_size * 200.0 / 72.0
            f = _font(px_size, cell.bold)
            avail = max(20, (x1 - x0) - 8)
            lines = _wrap(d, cell.text, f, avail) if cell.wrap else [cell.text]
            line_h = px_size * 1.15
            total = line_h * len(lines)
            ty = y0 + max(0, (y1 - y0 - total) / 2)
            for ln in lines:
                tw = d.textlength(ln, font=f)
                if cell.align == "center":
                    tx = x0 + max(2, (x1 - x0 - tw) / 2)
                elif cell.align == "right":
                    tx = x1 - tw - 6
                else:
                    tx = x0 + 4
                d.text((tx, ty), ln, fill=(0, 0, 0), font=f)
                ty += line_h
    return img


def _wrap(draw, text: str, font, avail: float) -> list[str]:
    """按可用宽度贪心折行，模拟 Excel 的 wrap_text。"""
    lines, cur = [], ""
    for ch in text:
        if draw.textlength(cur + ch, font=font) > avail and cur:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines or [""]
    return img


def _draw_loose(img: Image.Image, loose, top: float) -> None:
    """表格线之外的页眉文字：按 Excel 的做法逐行堆叠，把表格往下推。"""
    if not loose:
        return
    d = ImageDraw.Draw(img)
    px = max(l.font_size for l in loose) * 200.0 / 72.0
    total = sum(l.font_size * 200.0 / 72.0 * 1.35 for l in loose)
    y = max(10.0, top - total - 8)
    for line in sorted(loose, key=lambda l: l.y):
        f = _font(line.font_size * 200.0 / 72.0)
        d.text((line.x1, y), line.text, fill=(0, 0, 0), font=f)
        y += line.font_size * 200.0 / 72.0 * 1.35


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        return
    pdf = Path(sys.argv[1])
    page_no = int(sys.argv[2])
    out = Path(sys.argv[3]) if len(sys.argv) > 3 else Path("layout_preview.png")

    doc = fitz.open(str(pdf))
    png = render_page(doc, page_no - 1, 200)
    doc.close()

    tmp = Path.home() / "AppData/Local/Temp/_layout_page.png"
    tmp.write_bytes(png)
    boxes = run_ocr(png)
    layout = analyze_page(png, 200, ocr_boxes=boxes)
    blocks = layout.blocks

    original = Image.open(tmp).convert("RGB")
    hdr_h = sum(l.font_size * 200.0 / 72.0 * 1.35 for l in layout.loose)
    rebuilt = render_blocks(blocks, original.size, dy=hdr_h)
    if layout.loose:
        _draw_loose(rebuilt, layout.loose, min(b.y1 for b in blocks) + hdr_h)

    gap = 24
    canvas = Image.new("RGB", (original.width * 2 + gap, original.height), "white")
    canvas.paste(original, (0, 0))
    canvas.paste(rebuilt, (original.width + gap, 0))
    canvas.save(out)
    print(f"blocks={len(blocks)} size={canvas.size} -> {out}")


if __name__ == "__main__":
    main()
