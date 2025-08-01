from urllib.parse import urlparse

from scrapy.http import Request
from scrapy.utils.httpobj import urlparse_cached


def test_urlparse_cached():
    url = "http://www.example.com/index.html"
    request1 = Request(url)
    request2 = Request(url)
    req1a = urlparse_cached(request1)
    req1b = urlparse_cached(request1)
    req2 = urlparse_cached(request2)
    urlp = urlparse(url)

    assert req1a == req2
    assert req1a == urlp
    assert req1a is req1b
    assert req1a is not req2
    assert req1a is not req2
