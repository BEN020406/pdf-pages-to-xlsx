# -*- coding: utf-8 -*-
"""core.search —— 按标题在 PDF 全文里搜页。

用途：用户只知道「这张表叫 XX 明细表」，不知道在第几页。
扫描每页文本层；没有文本层的扫描件走 OCR（可选用大模型）。

匹配策略（按优先级）：
    1. 完全相等（去掉空白/标点后）
    2. 包含（标题出现在页内文本里）
    3. 模糊：标题的每个字符按顺序出现（容忍 OCR 少量错字）

一条命中记录页码 + 匹配到的原始行 + 匹配方式，供 UI 展示与人工确认。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import fitz

# 归一化时去掉的字符：空白、常见标点、括号
_STRIP = re.compile(r"[\s:：、,，.。·\-—–_()（）\[\]【】\"'“”‘’/\\|]+")


def _fold(s: str) -> str:
    """繁简折叠：把繁体统一成简体，用于比较（不改显示文本）。

    实测：澳门/香港的税表是繁体（「損益表」），用户习惯打简体（「损益表」），
    不折叠就永远 0 命中 —— 用户看到的就是「明明有却搜不到」。
    """
    if not s:
        return s
    try:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import zhconv

        return zhconv.convert(s, "zh-cn")
    except Exception:  # noqa: BLE001
        return s


def norm(s: str) -> str:
    return _fold(_STRIP.sub("", str(s or "")))


@dataclass
class PageMatch:
    page_no: int                     # 从 1 开始
    matched_text: str                # 命中的那一行原文
    how: str                         # "exact" | "contains" | "fuzzy"
    score: float = 1.0


@dataclass
class SearchResult:
    path: Path
    title: str
    matches: list[PageMatch] = field(default_factory=list)
    pages_scanned: int = 0
    used_ocr: bool = False
    cancelled: bool = False          # 中途被取消，结果不完整
    text_chars: int = 0              # 扫到的字符数，用来区分「没这个词」和「根本没识别出字」
    error: str = ""

    @property
    def page_numbers(self) -> list[int]:
        return [m.page_no for m in self.matches]


def _subsequence(needle: str, hay: str) -> bool:
    """needle 的字符是否按顺序出现在 hay 里（容忍中间插字）。"""
    it = iter(hay)
    return all(ch in it for ch in needle)


def match_in_text(title: str, text: str) -> tuple[str, str, float]:
    """在一页文本里找标题。返回 (命中行原文, 方式, 分数)；没命中返回 ("", "", 0)。

    短标题（归一化后 < 2 字）只允许精确相等 —— 否则「表」会命中
    「管理费用明细表」这类所有行，等于没筛。
    """
    nt = norm(title)
    if not nt:
        return "", "", 0.0
    allow_partial = len(nt) >= 2

    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    best: tuple[str, str, float] = ("", "", 0.0)

    for ln in lines:
        nl = norm(ln)
        if not nl:
            continue
        if nl == nt:
            return ln, "exact", 1.0
        if not allow_partial:
            continue
        if nt in nl:
            # 越短的行越可能是标题本身，而不是正文里顺带提到
            s = 0.9 * min(1.0, len(nt) / max(1, len(nl)) + 0.4)
            if s > best[2]:
                best = (ln, "contains", s)
        elif len(nt) >= 4 and _subsequence(nt, nl):
            s = 0.5 * len(nt) / max(1, len(nl))
            if s > best[2]:
                best = (ln, "fuzzy", s)

    # 整页兜底：扫描件里 OCR 常把一个标题拆成好几行
    # （实测「损\n益\n表」逐行比永远不命中），拼起来再找一次。
    if allow_partial and not best[0]:
        flat = norm(text)
        pos = flat.find(nt)
        if pos >= 0:
            snippet = flat[max(0, pos - 12): pos + len(nt) + 12]
            return snippet, "contains", 0.6

    return best


def count_pages(path: Path) -> int:
    """只读页数，不解析内容。用来算搜索的总进度。"""
    try:
        doc = fitz.open(str(path))
    except Exception:  # noqa: BLE001
        return 0
    try:
        return doc.page_count
    finally:
        doc.close()


def search_pdf(
    path: Path,
    title: str,
    *,
    use_ocr: bool = False,
    llm=None,
    max_pages: int = 0,
    should_stop=None,
    on_page=None,
) -> SearchResult:
    """在一个 PDF 里按标题搜页。

    use_ocr=True 时，没有文本层的页会渲染后跑本地 OCR（不调用大模型，
    因为搜标题只需要文字，本地 OCR 足够且免费）。

    should_stop: 每页开始前调用一次，返回 True 就停（返回已扫到的部分）。
                 扫描件的 OCR 一页就要一两秒，必须能按页中断。
    on_page:     每页开始前回调 on_page(当前页号, 总页数)，1 起算。
                 只按「第几个文件」报进度的话，单文件多页会一开始就 100%，
                 实测会被当成卡死。
    """
    res = SearchResult(path=Path(path), title=title)
    try:
        doc = fitz.open(str(res.path))
    except Exception as e:  # noqa: BLE001
        res.error = f"无法打开：{e}"
        return res

    try:
        n = doc.page_count if max_pages <= 0 else min(doc.page_count, max_pages)
        res.pages_scanned = n
        for i in range(n):
            if should_stop is not None and should_stop():
                res.cancelled = True
                break
            if on_page is not None:
                on_page(i + 1, n)
            page = doc[i]
            text = page.get_text("text").strip()

            if not text and use_ocr:
                try:
                    from .ocr import run_ocr
                    from .pipeline import render_page
                    boxes = run_ocr(render_page(doc, i, dpi=150))
                    text = "\n".join(b.text for b in boxes)
                    res.used_ocr = True
                except Exception:  # noqa: BLE001
                    text = ""

            line, how, score = match_in_text(title, text)
            if how:
                res.matches.append(PageMatch(i + 1, line, how, score))
            res.text_chars += len(text)
        return res
    finally:
        doc.close()


def search_many(
    paths: list[Path],
    title: str,
    *,
    use_ocr: bool = False,
    on_progress=None,
    should_stop=None,
) -> list[SearchResult]:
    """按页报进度：on_progress(已扫完页数, 总页数, 说明)。

    总页数先扫一遍页数（只读 page_count，不解析内容），
    这样单文件 15 页的扫描件也能看到 0/15 → 15/15，而不是一上来就 100%。
    """
    counts = [count_pages(p) for p in paths]
    total = sum(counts) or len(paths)
    out: list[SearchResult] = []
    done = 0
    for i, p in enumerate(paths, 1):
        if should_stop is not None and should_stop():
            break
        name = Path(p).name

        def _on_page(j: int, n: int, _done: int = done, _name: str = name):
            if on_progress:
                on_progress(_done + j - 1, total, f"{_name} 第 {j}/{n} 页")

        if on_progress:
            on_progress(done, total, f"{name} 第 1/{max(1, counts[i - 1])} 页")
        out.append(search_pdf(p, title, use_ocr=use_ocr,
                              should_stop=should_stop, on_page=_on_page))
        done += counts[i - 1] or out[-1].pages_scanned
    if on_progress and out:
        on_progress(done, total, "扫描结束")
    return out
