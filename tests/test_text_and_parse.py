import zipfile

from youju.agent.citations import normalize, used_numbers, verify
from youju.agent.sources import SourceBook
from youju.rag.chunk import chunk_blocks
from youju.rag.parse import Block, parse_file, parse_markdown
from youju.rag.retrieve import Hit
from youju.textproc import informative, sentences, tokens

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def test_tokens_mix_chinese_and_english():
    toks = tokens("CPU 使用率高于90%")
    assert {"cpu", "使用", "用率", "率高", "高于", "90"} <= set(toks)
    assert "什么" not in informative(tokens("什么是安全带"))
    assert sentences("第一句。第二句！\n第三句") == ["第一句。", "第二句！", "第三句"]


def test_markdown_sections_skip_single_title():
    blocks = parse_markdown("# 标题\n\n## 第一节\n\n内容一。\n\n## 第二节\n\n### 小节\n\n内容二。")
    assert [(b.section, b.text) for b in blocks] == [("第一节", "内容一。"), ("第二节 / 小节", "内容二。")]


def test_gbk_text_and_heading(tmp_path):
    path = tmp_path / "制度.txt"
    path.write_bytes("第一章 总则\n员工应当遵守制度。".encode("gbk"))
    blocks = parse_file(path, "制度.txt")
    assert blocks[0].section == "第一章 总则"
    assert "遵守" in blocks[0].text


def test_docx_headings_and_table(tmp_path):
    styles = f'<w:styles xmlns:w="{W}"><w:style w:styleId="Heading1"><w:name w:val="heading 1"/></w:style></w:styles>'
    doc = (
        f'<w:document xmlns:w="{W}"><w:body>'
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>报销</w:t></w:r></w:p>'
        "<w:p><w:r><w:t>发票抬头必须是公司全称。</w:t></w:r></w:p>"
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>城市</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>标准</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
        "</w:body></w:document>"
    )
    path = tmp_path / "制度.docx"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", doc)
        zf.writestr("word/styles.xml", styles)
    blocks = parse_file(path, "制度.docx")
    assert [(b.section, b.text) for b in blocks] == [("报销", "发票抬头必须是公司全称。"), ("报销", "城市 | 标准")]


def test_csv_and_html(tmp_path):
    csv_path = tmp_path / "faq.csv"
    csv_path.write_text("问题,答案\n退货几天,7 天\n", encoding="utf-8")
    assert parse_file(csv_path, "faq.csv")[0].text == "问题：退货几天；答案：7 天"
    html_path = tmp_path / "page.html"
    html_path.write_text("<html><head><script>x()</script></head><body><h2>门禁</h2><p>门禁卡丢失当天补办。</p></body></html>", encoding="utf-8")
    blocks = parse_file(html_path, "page.html")
    assert blocks[0].section == "门禁" and "补办" in blocks[0].text


def test_chunks_keep_size_overlap_and_sections():
    lines = [f"第{i}条，规定内容比较长，需要切开处理。" for i in range(40)]
    drafts = chunk_blocks([Block("\n".join(lines), "章节A"), Block("另一节内容。", "章节B")], size=200, overlap=40)
    a = [d for d in drafts if d.section == "章节A"]
    assert len(a) > 1 and all(len(d.text) <= 200 for d in a)
    assert a[0].text.splitlines()[-1] in a[1].text.splitlines()[:3]
    assert drafts[-1].section == "章节B" and drafts[-1].text == "另一节内容。"


def _book() -> SourceBook:
    book = SourceBook()
    book.add(Hit(1, 1, "规程", 1, "高空.md", "安全带", None, "安全带未扣好时，必须立即停止作业。", {}))
    book.add(Hit(2, 1, "规程", 2, "动火.md", "监护人", None, "没有监护人时禁止开始动火。", {}))
    return book


def test_citation_normalize_and_verify():
    book = _book()
    text = "安全带未扣好必须立即停止作业【1】。没有监护人禁止开始动火[2, 9]。"
    assert normalize(text, book.numbers()) == "安全带未扣好必须立即停止作业[1]。没有监护人禁止开始动火[2]。"
    assert used_numbers("a[2] b[1][2]") == [2, 1]
    check = verify(normalize(text, book.numbers()), book)
    assert check["verdict"] == "pass" and check["supported"] == 2


def test_verify_flags_weak_uncited_and_invalid():
    book = _book()
    weak = verify("员工每年可以享受十五天带薪年假和额外福利[1]。", book)
    assert weak["claims"][0]["status"] == "weak" and weak["verdict"] == "fail"
    uncited = verify("必须立即停止作业[1]。\n另外还要通知所有相关部门负责人到场处理。", book)
    assert uncited["verdict"] == "warn" and uncited["uncited"] == 1
    paragraph = verify("安全带未扣好时必须立即停止作业。没有监护人时禁止开始动火[1][2]。", book)
    assert paragraph["verdict"] == "pass" and paragraph["supported"] == 2
    invalid = verify("必须立即停止作业[7]。", book)
    assert invalid["verdict"] == "fail" and invalid["invalid"] == 1
    refusal = verify("资料中没有相关依据，暂不作答。", book)
    assert refusal["verdict"] == "none"
