"""Tests for OPML import and export on the Feeds page."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import timedelta
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

import pintxos.app as app_module
from pintxos.app import app
from pintxos.db import db, now

URLS = ["https://a.example/feed.xml", "https://b.example/rss", "https://c.example/atom"]
TITLES = ["Alpha & <Co>", "Beta", None]


@pytest.fixture
def polled(monkeypatch):
    class Ids(list):
        delays: list[timedelta]

    ids = Ids()
    delays: list[timedelta] = []

    def fake(feed_id, delay=timedelta(0)):
        ids.append(feed_id)
        delays.append(delay)

    monkeypatch.setattr(app_module, "poll_one", fake)
    monkeypatch.setattr("pintxos.poll.poll_one", fake)  # poll_spread calls it from poll.py
    ids.delays = delays
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


def _opml(n):
    outlines = "".join(f'<outline xmlUrl="https://f{i}.example/x"/>' for i in range(n))
    return f"<opml><body>{outlines}</body></opml>".encode()


def test_import_over_five_waits_for_go_ahead(polled):
    with TestClient(app) as c:
        resp = _import(c, _opml(7))
        assert polled == []
        assert "wait for your go-ahead" in _flash(resp, "msg")
        page = c.get("/").text
        assert "7 feeds have not been polled yet" in page and "Start first polls" in page
        resp = c.post("/feeds/poll-new", follow_redirects=False)
        assert polled.delays == [timedelta(minutes=m) for m in (0, 0, 0, 0, 0, 2, 4)]
        assert _flash(resp, "msg").endswith("spread over the next 4 minutes")


def test_import_five_feeds_polled_now(polled):
    with TestClient(app) as c:
        resp = _import(c, _opml(5))
        assert polled.delays == [timedelta(0)] * 5
        assert "go-ahead" not in _flash(resp, "msg")
        # mocked poll_one fills no status, so mark them polled to mimic real polls
        with db() as conn:
            conn.execute("UPDATE feeds SET last_polled_at=?", (now(),))
        assert "have not been polled" not in c.get("/").text


def test_poll_new_nothing_waiting(polled):
    with TestClient(app) as c:
        resp = c.post("/feeds/poll-new", follow_redirects=False)
    assert _flash(resp, "msg") == "No feeds are waiting for a first poll"


def test_delete_drops_waiting_dated_job():
    from pintxos import poll

    with TestClient(app) as c:
        try:
            poll.poll_one(777, delay=timedelta(minutes=2))
            assert poll.scheduler.get_job("feed-777") is not None
            c.post("/feeds/777/delete", follow_redirects=False)
            assert poll.scheduler.get_job("feed-777") is None
            assert 777 not in poll._status
        finally:
            if poll.scheduler.get_job("feed-777"):
                poll.scheduler.remove_job("feed-777")
            poll._status.pop(777, None)
