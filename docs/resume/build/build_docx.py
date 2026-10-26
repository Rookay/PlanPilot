import re
import sys
from pathlib import Path

import markdown


CSS = """
  @page {
    @top-right { content: "__RUNNING__"; font-family: "SimSun", "宋体", serif; font-size: 9pt; color: #7a8794; }
    @bottom-center { content: counter(page); font-family: "SimSun", "宋体", serif; font-size: 9pt; color: #7a8794; }
  }
  @page cover {
    @top-right { content: none; }
    @bottom-center { content: none; }
  }
  section[role="cover"] { page: cover; }

  * { box-sizing: border-box; }
  body {
    font-family: "SimSun", "宋体", serif;
    font-size: 12pt;
    line-height: 1.55;
    color: #1a1a1a;
    margin: 0;
  }
  h1, h2, h3, h4 {
    font-family: "SimHei", "黑体", sans-serif;
    color: #16324f;
    line-height: 1.35;
    page-break-after: avoid;
  }
  h1 {
    font-size: 20pt;
    margin: 26pt 0 14pt 0;
    padding-bottom: 6pt;
    border-bottom: 2pt solid #16324f;
    page-break-before: auto;
  }
  h1:first-of-type { margin-top: 0; }
  h2 {
    font-size: 14pt;
    margin: 20pt 0 8pt 0;
    padding-left: 8pt;
    border-left: 4pt solid #c8862a;
  }
  h3 {
    font-size: 12pt;
    margin: 14pt 0 6pt 0;
  }
  p {
    margin: 0 0 6pt 0;
    text-indent: 2em;
    text-align: justify;
  }
  strong { font-weight: bold; color: #10263d; }
  ul, ol { margin: 4pt 0 9pt 0; padding-left: 2em; }
  li { margin-bottom: 3pt; text-indent: 0; }
  blockquote {
    margin: 8pt 0 10pt 0;
    padding: 8pt 12pt;
    background: #f4f7fa;
    border-left: 3pt solid #9fb3c8;
    text-indent: 0;
    text-align: left;
  }
  blockquote p { text-indent: 0; margin-bottom: 5pt; text-align: left; color: #1a1a1a; }
  blockquote p:last-child { margin-bottom: 0; }
  table {
    width: 100%;
    table-layout: fixed;
    border-collapse: collapse;
    margin: 6pt 0 12pt 0;
    font-size: 10.5pt;
    line-height: 1.45;
  }
  th, td {
    border: 0.6pt solid #b9c6d4;
    padding: 4pt 6pt;
    vertical-align: top;
    text-align: left;
  }
  th {
    background: #e9eff5;
    font-family: "SimHei", "黑体", sans-serif;
    font-weight: bold;
    color: #16324f;
  }
  tr { page-break-inside: avoid; }
  .tbl-gap {
    text-indent: 0;
    margin: 0;
    font-size: 2pt;
    line-height: 1;
  }
  code {
    font-family: "Consolas", "Courier New", monospace;
    font-size: 10pt;
    background: #f2f2f2;
    padding: 0 2pt;
  }
  pre {
    background: #f6f8fa;
    border: 0.6pt solid #dde3ea;
    padding: 8pt 10pt;
    font-family: "Consolas", "Courier New", monospace;
    font-size: 9.5pt;
    line-height: 1.45;
    page-break-inside: avoid;
    white-space: pre-wrap;
  }
  pre code { background: none; padding: 0; }
  hr { border: none; border-top: 0.6pt solid #d5dde6; margin: 14pt 0; }

  /* 封面 */
  .cover {
    padding-top: 150pt;
    text-align: left;
  }
  .cover .kicker {
    font-family: "SimHei", "黑体", sans-serif;
    font-size: 10.5pt;
    letter-spacing: 3pt;
    color: #c8862a;
    text-indent: 0;
    margin-bottom: 18pt;
  }
  .cover .title {
    font-family: "SimHei", "黑体", sans-serif;
    font-size: 32pt;
    font-weight: bold;
    color: #16324f;
    line-height: 1.3;
    text-indent: 0;
    margin: 0 0 2pt 0;
  }
  .cover .subtitle {
    font-size: 15pt;
    margin-top: 10pt;
    color: #3d566e;
    text-indent: 0;
    margin: 0 0 26pt 0;
  }
  .cover .rule {
    width: 100%;
    border-top: 2pt solid #16324f;
    margin-bottom: 14pt;
  }
  .cover .meta {
    font-size: 10.5pt;
    color: #6b7a89;
    text-indent: 0;
    line-height: 1.9;
  }
  .cover .foot {
    margin-top: 180pt;
    font-size: 9.5pt;
    color: #8a97a5;
    text-indent: 0;
  }

  /* 目录 */
  .toc-title {
    font-family: "SimHei", "黑体", sans-serif;
    font-size: 18pt;
    color: #16324f;
    text-indent: 0;
    margin: 12pt 0 18pt 0;
    padding-bottom: 6pt;
    border-bottom: 1pt solid #b9c6d4;
  }
  .toc-list, .toc-list ol, .toc-list ul { list-style: none; padding-left: 0; margin: 0; }
  .toc-list li {
    font-size: 12pt;
    margin: 0 0 9pt 0;
    text-indent: 0;
    line-height: 1.5;
  }
  .toc-list a { color: #1a1a1a; text-decoration: none; }
"""

HTML_SHELL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>{title}</title>
<style>{css}</style>
</head>
<body>
<section role="cover">
  <div class="cover">
    <p class="kicker">{kicker}</p>
    {title_html}
    <p class="subtitle">{subtitle}</p>
    <div class="rule"></div>
    <p class="meta">{meta}</p>
    <p class="foot">内部准备材料 · 请勿外传</p>
  </div>
</section>
<section role="cover">
<nav class="doc-toc" aria-label="文档目录">
  <p class="toc-title">目 录</p>
  <ol class="toc-list">
{toc}
  </ol>
</nav>
</section>
<section role="body" data-page-restart="1">
{body}
</section>
</body>
</html>
"""


def parse_front_matter(text: str):
    meta = {}
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                meta[key.strip()] = value.strip()
        text = text[m.end():]
    return meta, text


def build(src: Path, dst_html: Path, out_docx_name: str, running: str):
    meta, text = parse_front_matter(src.read_text(encoding="utf-8"))
    md = markdown.Markdown(
        extensions=["tables", "fenced_code", "sane_lists", "attr_list"],
        output_format="html5",
    )
    body = md.convert(text)

    headings = []

    def add_id(match):
        inner = match.group(1)
        plain = re.sub(r"<[^>]+>", "", inner).strip()
        headings.append(plain)
        cls = ' class="no-break"' if plain.startswith('附录') else ''
        return '<h1 id="section-%d"%s>%s</h1>' % (len(headings), cls, inner)

    body = re.sub(r"<h1>(.*?)</h1>", add_id, body, flags=re.S)
    body = body.replace("</table>", '</table><p class="tbl-gap">&nbsp;</p>')

    widths_map = {
        2: [28, 72],
        3: [16, 42, 42],
        4: [9, 28, 11, 52],
        5: [7, 20, 15, 18, 40],
        6: [6, 16, 14, 16, 16, 32],
    }

    def add_colgroup(match):
        table = match.group(0)
        first_row = re.search(r"<tr[^>]*>(.*?)</tr>", table, re.S)
        if not first_row:
            return table
        count = first_row.group(1).count("<th")
        widths = widths_map.get(count)
        if not widths:
            return table
        cols = "".join('<col style="width:%d%%">' % w for w in widths)
        if "<thead>" in table:
            return table.replace("<thead>", "<colgroup>%s</colgroup><thead>" % cols, 1)
        return table.replace("<tbody>", "<colgroup>%s</colgroup><tbody>" % cols, 1)

    body = re.sub(r"<table>.*?</table>", add_colgroup, body, flags=re.S)

    toc_lines = []
    for i, title in enumerate(headings, 1):
        toc_lines.append('    <li><a href="#section-%d">%s</a></li>' % (i, title))

    raw_title = meta.get("title", "文档")
    parts = [p.strip() for p in raw_title.split("|") if p.strip()]
    title_html = "\n    ".join(
        '<p class="title">%s</p>' % part for part in parts
    )
    css = CSS.replace("__RUNNING__", running)
    html_text = HTML_SHELL.format(
        title=raw_title.replace("|", " "),
        title_html=title_html,
        kicker=meta.get("kicker", "INTERVIEW PREPARATION"),
        subtitle=meta.get("subtitle", ""),
        meta=meta.get("meta", ""),
        css=css,
        toc="\n".join(toc_lines),
        body=body,
    )
    dst_html.write_text(html_text, encoding="utf-8")

    plan = {
        "source": str(src),
        "html": str(dst_html),
        "docx_name": out_docx_name,
        "title": meta.get("title", ""),
        "chapters": len(headings),
    }
    return plan, html_text


if __name__ == "__main__":
    base = Path(__file__).resolve().parent
    docs = base.parent
    jobs = [
        ("interview_final.md", "interview_final.html", "PlanPilot_面试定稿.docx", "PlanPilot Agent 实习面试定稿"),
        ("knowledge_outline.md", "knowledge_outline.html", "AI_Agent_必学知识库目录.docx", "AI Agent 应用开发 必学知识库目录"),
    ]
    for src_name, html_name, docx_name, running in jobs:
        plan, html_text = build(base / src_name, base / html_name, docx_name, running)
        print(plan)
        print("html_chars=%d" % len(html_text))
