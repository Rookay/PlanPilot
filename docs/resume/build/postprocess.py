import re
import shutil
import sys
import zipfile
from pathlib import Path


def fix_document_xml(xml: str) -> str:
    # 固定表格布局
    xml = xml.replace(
        '<w:tblLayout w:type="autofit"/>', '<w:tblLayout w:type="fixed"/>'
    )

    def fix_row(match):
        row = match.group(0)
        if "<w:cantSplit" in row:
            return row
        if "<w:trPr>" in row:
            return row.replace("<w:trPr>", "<w:trPr><w:cantSplit/>", 1)
        opening = re.match(r"<w:tr(?:\s[^>]*)?>", row).group(0)
        return row.replace(opening, opening + "<w:trPr><w:cantSplit/></w:trPr>", 1)

    return re.sub(r"<w:tr(?:\s[^>]*)?>.*?</w:tr>", fix_row, xml, flags=re.S)


def process(path: Path):
    tmp = path.with_suffix(".tmp.docx")
    with zipfile.ZipFile(path, "r") as src, zipfile.ZipFile(
        tmp, "w", zipfile.ZIP_DEFLATED
    ) as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "word/document.xml":
                xml = data.decode("utf-8")
                xml = fix_document_xml(xml)
                data = xml.encode("utf-8")
            dst.writestr(item, data)
    shutil.move(tmp, path)


if __name__ == "__main__":
    for p in sys.argv[1:]:
        process(Path(p))
        print("fixed", p)
