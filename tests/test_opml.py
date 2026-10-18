"""Tests for OPML import and export on the Feeds page."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

import pintxos.app as app_module
from pintxos.app import app
from pintxos.db import db

URLS = ["https://a.example/feed.xml", "https://b.example/rss", "https://c.example/atom"]
TITLES = ["Alpha & <Co>", "Beta", None]


@pytest.fixture
def polled(monkeypatch):
    ids: list[int] = []
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: ids.append(feed_id))
    return ids


def _seed(c):
    for u in URLS:
        c.post("/feeds", data={"url": u}, follow_redirects=False)
    with db() as conn:
        for i, t in enumerate(TITLES, start=1):
            if t:
                conn.execute("UPDATE feeds SET title=? WHERE id=?", (t, i))


def _outlines(text):
    return ET.fromstring(text).findall("./body/outline")


def _flash(resp, key):
    loc = unquote(resp.headers["location"])
    assert loc.startswith(f"/?{key}=")
    return loc.split("=", 1)[1]


def _import(c, data):
    return c.post("/feeds/import", files={"opml": ("x.opml", data)}, follow_redirects=False)


def _rows():
    with db() as conn:
        return [tuple(r) for r in conn.execute("SELECT id, url, title FROM feeds ORDER BY id")]


def test_export_source(polled):
    with TestClient(app) as c:
        _seed(c)
        resp = c.get("/feeds.opml")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/x-opml")
    assert "pintxos.opml" in resp.headers["content-disposition"]
    outs = _outlines(resp.text)
    assert len(outs) == 3
    assert all(o.get("type") == "rss" for o in outs)
    assert {o.get("xmlUrl") for o in outs} == set(URLS)
    assert [o.get("text") for o in outs] == [TITLES[0], TITLES[1], URLS[2]]
    assert [o.get("title") for o in outs] == [TITLES[0], TITLES[1], URLS[2]]


def test_export_output(polled):
    with TestClient(app) as c:
        _seed(c)
        resp = c.get("/feeds.opml?output=1")
    assert "pintxos-output.opml" in resp.headers["content-disposition"]
    urls = [o.get("xmlUrl") for o in _outlines(resp.text)]
    assert urls == [f"http://testserver/feeds/{i}.xml" for i in (1, 2, 3)]


def test_export_empty(polled):
    with TestClient(app) as c:
        resp = c.get("/feeds.opml")
    assert resp.status_code == 200
    root = ET.fromstring(resp.text)
    assert root.find("body") is not None
    assert _outlines(resp.text) == []


def test_roundtrip_and_reimport(polled):
    with TestClient(app) as c:
        _seed(c)
        data = c.get("/feeds.opml").content
        polled.clear()
        # Fresh DB per test is not available mid-test, so empty the table instead.
        with db() as conn:
            conn.execute("DELETE FROM feeds")
        resp = _import(c, data)
        assert resp.status_code == 303
        assert _flash(resp, "msg") == "Imported 3 feeds, 0 already subscribed, 0 skipped (not http/https)"
        assert _rows() == [(i + 1, URLS[i], TITLES[i] or URLS[i]) for i in range(3)]
        with db() as conn:
            assert polled == [r["id"] for r in conn.execute("SELECT id FROM feeds ORDER BY id")]
        resp = _import(c, data)
        assert _flash(resp, "msg") == "Imported 0 feeds, 3 already subscribed, 0 skipped (not http/https)"
        assert len(_rows()) == 3


def test_import_nested_and_skipped(polled):
    opml = b"""<opml version="2.0"><body>
    <outline text="Folder"><outline text="One" xmlUrl=" https://one.example/f "/>
    <outline title="Two" xmlUrl="http://two.example/f"/></outline>
    <outline text="Bad" xmlUrl="ftp://x"/><outline text="none"/></body></opml>"""
    with TestClient(app) as c:
        resp = _import(c, opml)
    assert _flash(resp, "msg") == "Imported 2 feeds, 0 already subscribed, 1 skipped (not http/https)"
    assert _rows() == [(1, "https://one.example/f", "One"), (2, "http://two.example/f", "Two")]


def test_import_duplicate_in_file_and_empty(polled):
    opml = b"""<opml><body><outline text="" xmlUrl="https://d.example/f"/>
    <outline text="x" xmlUrl="https://d.example/f"/></body></opml>"""
    with TestClient(app) as c:
        resp = _import(c, opml)
        assert _flash(resp, "msg") == "Imported 1 feeds, 1 already subscribed, 0 skipped (not http/https)"
        assert _rows()[0][2] is None
        resp = _import(c, b"<opml><body/></opml>")
        assert _flash(resp, "msg") == "Imported 0 feeds, 0 already subscribed, 0 skipped (not http/https)"


def test_import_invalid(polled):
    with TestClient(app) as c:
        resp = _import(c, b"not xml")
        assert _flash(resp, "err") == "Not an OPML file"
        resp = c.post("/feeds/import", follow_redirects=False)
        assert _flash(resp, "err") == "Not an OPML file"
        resp = _import(c, b"x" * (1024 * 1024 + 1))
        assert _flash(resp, "err") == "File too large"
    assert _rows() == []


def test_index_links(polled):
    with TestClient(app) as c:
        page = c.get("/").text
    assert 'action="/feeds/import"' in page
    assert 'href="/feeds.opml"' in page
    assert 'href="/feeds.opml?output=1"' in page


def test_import_bogus_encoding(polled):
    with TestClient(app) as c:
        for enc in ("bogus", "utf-7"):
            resp = _import(c, f'<?xml version="1.0" encoding="{enc}"?><opml/>'.encode())
            assert _flash(resp, "err") == "Not an OPML file"
    assert _rows() == []


def test_import_not_opml_root(polled):
    with TestClient(app) as c:
        resp = _import(c, b"<rss><channel/></rss>")
        assert _flash(resp, "err") == "Not an OPML file"
    assert _rows() == []
