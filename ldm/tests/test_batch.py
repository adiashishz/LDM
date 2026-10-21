import pytest

from ldm.batch import expand, parse_link_list


def test_numbers_with_padding_and_reverse():
    assert expand("https://s/img*.jpg", numbers=(8, 10, 2)) == ["https://s/img08.jpg", "https://s/img09.jpg",
                                                                "https://s/img10.jpg"]
    assert expand("https://s/p*", numbers=(3, 1, 0)) == ["https://s/p3", "https://s/p2", "https://s/p1"]
    assert len(expand("https://s/*", numbers=(1, 10_000, 0))) == 10_000


def test_letters():
    assert expand("https://s/part-*.zip", letters=("a", "c")) == [
        "https://s/part-a.zip", "https://s/part-b.zip", "https://s/part-c.zip"]
    assert expand("https://s/*", letters=("C", "A")) == ["https://s/C", "https://s/B", "https://s/A"]
    assert expand("https://s/*", letters=("z", "z")) == ["https://s/z"]


@pytest.mark.parametrize("url, kwargs", [
    ("https://s/a.jpg", {"numbers": (1, 2, 0)}),            # no *
    ("https://s/*/*.jpg", {"numbers": (1, 2, 0)}),          # two *
    ("https://s/*", {}),                                    # neither
    ("https://s/*", {"numbers": (1, 2, 0), "letters": ("a", "b")}),
    ("https://s/*", {"numbers": (1, 10_001, 0)}),
    ("https://s/*", {"numbers": (1, 2, 11)}),
    ("https://s/*", {"numbers": (-1, 2, 0)}),
    ("https://s/*", {"letters": ("a", "B")}),
    ("https://s/*", {"letters": ("aa", "b")}),
    ("https://s/*", {"letters": ("1", "2")}),
])
def test_errors(url, kwargs):
    with pytest.raises(ValueError):
        expand(url, **kwargs)


def test_parse_link_list():
    text = "# my links\nhttps://a/1.zip\n\n  https://a/2.zip  \nhttps://a/1.zip\nnot a link\nmagnet:?xt=urn:btih:x\n"
    assert parse_link_list(text) == ["https://a/1.zip", "https://a/2.zip", "magnet:?xt=urn:btih:x"]
