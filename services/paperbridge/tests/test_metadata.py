from paperbridge.metadata import (
    Identifiers,
    _split,
    fallback_item,
    identifiers_in_text,
    identifiers_in_url,
    identifiers_of_item,
    item_doi,
    lookup,
)


def test_arxiv_ids_in_urls():
    assert identifiers_in_url("https://arxiv.org/abs/1706.03762").arxiv == "1706.03762"
    assert identifiers_in_url("https://arxiv.org/pdf/1706.03762v7").arxiv == "1706.03762"
    assert identifiers_in_url("http://arxiv.org/pdf/2401.12345v2.pdf").arxiv == "2401.12345"
    assert identifiers_in_url("https://arxiv.org/abs/hep-th/9901001v1").arxiv == "hep-th/9901001"
    assert identifiers_in_url("https://arxiv.org/abs/math.GT/0309136").arxiv == "math.GT/0309136"
    assert identifiers_in_url("https://example.org/1706.03762").arxiv is None


def test_dois_in_urls():
    assert identifiers_in_url("https://doi.org/10.1038/nature14539").doi == "10.1038/nature14539"
    assert identifiers_in_url("https://doi.org/10.1038%2Fnature14539").doi == "10.1038/nature14539"
    assert (
        identifiers_in_url("https://dl.acm.org/doi/pdf/10.1145/3313831.3376227").doi
        == "10.1145/3313831.3376227"
    )
    assert (
        identifiers_in_url("https://onlinelibrary.wiley.com/doi/full/10.1002/ece3.1234/full").doi
        == "10.1002/ece3.1234"
    )
    assert identifiers_in_url("https://example.org/paper").doi is None


def test_identifiers_in_text():
    text = "arXiv:1706.03762v7 [cs.CL] 2 Aug 2023\nAttention Is All You Need\n... doi:10.1000/xyz123."
    assert identifiers_in_text(text) == Identifiers(arxiv="1706.03762", doi="10.1000/xyz123")


def test_identifiers_of_items():
    preprint = {"archiveID": "arXiv:1706.03762", "DOI": "10.48550/arXiv.1706.03762"}
    assert identifiers_of_item(preprint) == Identifiers("1706.03762", "10.48550/arxiv.1706.03762")
    assert item_doi({"extra": "Some: thing\nDOI: 10.1/ABC"}) == "10.1/abc"
    assert item_doi({"DOI": "https://doi.org/10.1/X"}) == "10.1/x"


def test_split_cleans_translator_output():
    items = [
        {"key": "A", "version": 0, "itemType": "preprint", "title": "T", "attachments": []},
        {"itemType": "note", "parentItem": "A", "note": "Comment: 15 pages"},
    ]
    assert _split(items) == ({"itemType": "preprint", "title": "T"}, [{"itemType": "note", "note": "Comment: 15 pages"}])
    assert _split([]) is None


class FakeTS:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def web(self, url):
        self.calls.append(("web", url))
        return self.answers.get(("web", url))

    def search(self, q):
        self.calls.append(("search", q))
        return self.answers.get(("search", q))


def test_lookup_prefers_identifiers():
    ts = FakeTS({("search", "arXiv:1706.03762"): [{"itemType": "preprint", "title": "T"}]})
    assert lookup(ts, "https://arxiv.org/abs/1706.03762", None)[0]["title"] == "T"
    assert ts.calls == [("search", "arXiv:1706.03762")]


def test_lookup_from_pdf_text_checks_the_title():
    cited = [{"itemType": "journalArticle", "title": "Some Cited Paper"}]
    ts = FakeTS({("search", "10.1000/cited"): cited})
    text = "Our Own Paper\n... as shown in doi:10.1000/cited"
    assert lookup(ts, None, text) is None
    assert lookup(ts, None, "Some cited\npaper by X. doi:10.1000/cited")[0]["title"] == "Some Cited Paper"


def test_fallback_item():
    bookmark = {"title": None, "content": {"type": "asset", "assetType": "pdf", "fileName": "x.pdf"}}
    item = fallback_item(bookmark, None)
    assert item["itemType"] == "document" and item["title"] == "x.pdf"
    assert {"tag": "needs-metadata"} in item["tags"]
