# -*- coding: utf-8 -*-
"""把 使用说明书.md 转成单文件 HTML（图片内嵌，可直接发人）。

用法：python tools/build_manual_html.py
输出：使用说明书.html（图片 base64 内嵌，无外部依赖）
"""
from __future__ import annotations

import base64
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "使用说明书.md"
OUT = ROOT / "使用说明书.html"

CSS = """
:root {
  --bg: #F7F8FA; --surface: #FFFFFF; --border: #E3E6EB;
  --text: #1B1F24; --sub: #5A6472; --mute: #8A94A3;
  --primary: #1A3A5C; --primary-soft: #E8EEF5;
  --warn: #B45309; --warn-bg: #FDF3E3;
  --ok: #0F7B4F; --danger: #B42318; --danger-bg: #FDECEA;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans SC",
               -apple-system, "Segoe UI", sans-serif;
  font-size: 15px; line-height: 1.75;
}
.wrap { max-width: 960px; margin: 0 auto; padding: 48px 24px 96px; }
header.hero {
  background: linear-gradient(160deg, #1A3A5C 0%, #24496F 100%);
  color: #fff; padding: 44px 40px; border-radius: 12px; margin-bottom: 36px;
}
header.hero h1 { margin: 0 0 10px; font-size: 30px; letter-spacing: .5px; }
header.hero p { margin: 0; opacity: .85; font-size: 14px; }
header.hero .ver {
  display: inline-block; margin-top: 14px; padding: 3px 10px;
  background: rgba(255,255,255,.15); border-radius: 4px; font-size: 12px;
}
h2 {
  font-size: 21px; margin: 46px 0 14px; padding-bottom: 10px;
  border-bottom: 1px solid var(--border); color: var(--primary);
}
h3 { font-size: 16px; margin: 30px 0 10px; color: var(--text); }
p { margin: 10px 0; }
strong { color: var(--text); }
a { color: var(--primary); }
hr { border: none; border-top: 1px solid var(--border); margin: 36px 0; }

table {
  width: 100%; border-collapse: collapse; margin: 16px 0;
  background: var(--surface); font-size: 14px;
  box-shadow: 0 1px 2px rgba(27,31,36,.05); border-radius: 8px; overflow: hidden;
}
th, td { padding: 10px 14px; text-align: left; border-bottom: 1px solid var(--border); }
th {
  background: var(--primary-soft) !important; color: var(--primary) !important;
  font-weight: 600; white-space: nowrap;
}
td { color: var(--text); vertical-align: top; }
tr:last-child td { border-bottom: none; }
tbody tr:nth-child(even) { background: #FCFDFE; }
tbody tr:hover { background: #F5F8FC; }

blockquote {
  margin: 16px 0; padding: 14px 18px; background: var(--primary-soft);
  border-left: 3px solid var(--primary); border-radius: 0 6px 6px 0;
  color: var(--sub); font-size: 14px;
}
blockquote p { margin: 4px 0; }

code {
  background: #F0F2F5; padding: 1px 6px; border-radius: 4px;
  font-family: Consolas, "Cascadia Mono", monospace; font-size: 13px;
}
pre {
  background: #1B1F24; color: #E6EDF3; padding: 16px 18px; border-radius: 8px;
  overflow-x: auto; font-size: 13px; line-height: 1.6;
}
pre code { background: none; padding: 0; color: inherit; }

figure { margin: 22px 0; }
figure img {
  width: 100%; display: block; border: 1px solid var(--border);
  border-radius: 8px; box-shadow: 0 2px 8px rgba(27,31,36,.08);
}
figcaption { margin-top: 8px; font-size: 12.5px; color: var(--mute); text-align: center; }

ol, ul { padding-left: 22px; }
li { margin: 6px 0; }

/* 状态色小方块：说明里出现的六种校对色 */
.legend { display: flex; flex-wrap: wrap; gap: 10px; margin: 14px 0; }
.legend span {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 13px; color: var(--sub); background: var(--surface);
  border: 1px solid var(--border); border-radius: 20px; padding: 4px 12px;
}
.legend i { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }

footer {
  margin-top: 56px; padding-top: 20px; border-top: 1px solid var(--border);
  color: var(--mute); font-size: 12.5px;
}
@media print {
  body { background: #fff; }
  .wrap { max-width: none; padding: 0; }
  header.hero { background: #1A3A5C !important; -webkit-print-color-adjust: exact; }
  h2 { page-break-after: avoid; }
  figure, table { page-break-inside: avoid; }
}
@media (max-width: 720px) {
  .wrap { padding: 24px 14px 60px; }
  header.hero { padding: 28px 20px; }
  header.hero h1 { font-size: 23px; }
  table { font-size: 13px; }
  th, td { padding: 8px 10px; }
}
"""

LEGEND = """
<div class="legend">
  <span><i style="background:#FFFFFF;border:1px solid #CDD3DB"></i>一致</span>
  <span><i style="background:#FDF6DC"></i>采信本地</span>
  <span><i style="background:#E8F1FA"></i>采信模型</span>
  <span><i style="background:#FDF0E9"></i>精度修正</span>
  <span><i style="background:#FDEAEC"></i>需人工</span>
  <span><i style="background:#F1EEF6"></i>人工改</span>
</div>
"""


def embed_images(html: str) -> str:
    """把 <img src="shot_x.png"> 换成 base64 内嵌，并包成 figure。"""

    def repl(m: re.Match) -> str:
        alt, src = m.group(1), m.group(2)
        path = ROOT / src
        if not path.exists():
            return m.group(0)
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        return (f'<figure><img alt="{alt}" src="data:image/png;base64,{data}">'
                f'<figcaption>{alt}</figcaption></figure>')

    return re.sub(r'<img alt="([^"]*)" src="([^"]+)"\s*/?>', repl, html)


def add_anchor_ids(html: str) -> str:
    """给 h2 生成 id，便于做目录。"""
    def repl(m: re.Match) -> str:
        text = m.group(1)
        slug = re.sub(r"[^\w\u4e00-\u9fff]+", "-", text).strip("-")
        return f'<h2 id="{slug}">{text}</h2>'

    return re.sub(r"<h2>(.*?)</h2>", repl, html)


def build_toc(html: str) -> str:
    items = re.findall(r'<h2 id="([^"]+)">(.*?)</h2>', html)
    if not items:
        return ""
    links = "".join(f'<a href="#{i}">{t}</a>' for i, t in items)
    return f'<nav class="toc">{links}</nav>'


def main() -> None:
    import markdown

    md = SRC.read_text(encoding="utf-8")
    # 去掉 md 的一级标题（改用 hero 展示）
    md = re.sub(r"^# .*?\n", "", md, count=1)
    # 开头那行「版本…｜适用…｜…」已经放进 hero，正文里不再重复
    md = re.sub(r"^\s*版本\s*0\.3\.0.*?\n", "", md, count=1)
    md = md.lstrip()

    body = markdown.markdown(md, extensions=["tables", "fenced_code", "nl2br"])
    body = add_anchor_ids(body)
    body = embed_images(body)
    # 「校对」那张图下面补一个颜色图例，比纯文字表更直观
    body = body.replace("<h3>第 4 步：校对</h3>", "<h3>第 4 步：校对</h3>" + LEGEND, 1)

    toc = build_toc(body)
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>使用说明书 · PDF 取页 → Excel</title>
<style>{CSS}
.toc {{ display:flex; flex-wrap:wrap; gap:8px; margin:0 0 8px; }}
.toc a {{
  font-size:13px; color:var(--sub); text-decoration:none;
  background:var(--surface); border:1px solid var(--border);
  border-radius:20px; padding:5px 13px;
}}
.toc a:hover {{ color:var(--primary); border-color:var(--primary); }}
</style>
</head>
<body>
<div class="wrap">
<header class="hero">
  <h1>PDF 取页 → Excel 使用说明书</h1>
  <p>从一批 PDF 里取指定页、识别成表格、合成一份 Excel</p>
  <span class="ver">版本 0.3.0 ｜ Windows 10/11 ｜ 默认全程本机处理</span>
</header>
{toc}
{body}
<footer>
  本文由 使用说明书.md 自动生成，图片已内嵌，可单独分发。<br>
  技术细节见 README.md ｜ 当前测试 257 passed, 2 skipped
</footer>
</div>
</body>
</html>"""

    OUT.write_text(html, encoding="utf-8")
    size = OUT.stat().st_size / 1024
    print(f"-> {OUT.name}  {size:.0f} KB  （图片已内嵌）")


if __name__ == "__main__":
    main()
