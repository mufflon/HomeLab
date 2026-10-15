import hashlib
import io
import zipfile

from paperbridge.files import attachment_filename, md5, prop_for_webdav, zip_for_webdav


def item(*last_names, date="2017-06-12", title="Attention Is All You Need"):
    creators = [{"firstName": "A", "lastName": n, "creatorType": "author"} for n in last_names]
    return {"creators": creators, "date": date, "title": title}


def test_filename_like_zotero():
    assert attachment_filename(item("Vaswani")) == "Vaswani - 2017 - Attention Is All You Need.pdf"
    assert attachment_filename(item("A", "B")) == "A and B - 2017 - Attention Is All You Need.pdf"
    assert attachment_filename(item("A", "B", "C")) == "A et al. - 2017 - Attention Is All You Need.pdf"
    assert attachment_filename(item(date="", title="")) == "paper.pdf"
    assert attachment_filename(item("Gödel", title='What/is: "this"?')) == "Gödel - 2017 - Whatis this.pdf"


def test_editors_are_not_authors():
    i = item("Author")
    i["creators"].append({"lastName": "Editor", "creatorType": "editor"})
    assert attachment_filename(i).startswith("Author - ")


def test_webdav_files():
    pdf = b"%PDF-1.7 hello"
    zipped = zipfile.ZipFile(io.BytesIO(zip_for_webdav("a.pdf", pdf)))
    assert zipped.namelist() == ["a.pdf"] and zipped.read("a.pdf") == pdf
    assert md5(pdf) == hashlib.md5(pdf).hexdigest()
    assert prop_for_webdav(1792105737901, "84c1") == (
        b'<properties version="1"><mtime>1792105737901</mtime><hash>84c1</hash></properties>'
    )
