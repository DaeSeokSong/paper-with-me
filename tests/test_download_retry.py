"""덤프 다운로드의 일시 오류 재시도 — HF 429/5xx 하나로 100분짜리
빌드가 통째로 죽던 문제(run 28730365778)의 회귀 테스트."""

import io
import urllib.error
import urllib.request

import pytest

from pwc import sources


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO())


def test_retries_transient_503_then_succeeds(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=0):
        calls.append(req.full_url)
        if len(calls) < 3:
            raise _http_error(503)
        return io.BytesIO(b"ok")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(sources.time, "sleep", lambda s: None)
    with sources.open_with_retry("http://x") as resp:
        assert resp.read() == b"ok"
    assert len(calls) == 3


def test_no_retry_on_permanent_404(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=0):
        calls.append(1)
        raise _http_error(404)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(sources.time, "sleep", lambda s: None)
    with pytest.raises(urllib.error.HTTPError):
        sources.open_with_retry("http://x")
    assert len(calls) == 1  # 영구 오류는 즉시 전파


def test_gives_up_after_max_retries(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=0):
        calls.append(1)
        raise _http_error(503)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(sources.time, "sleep", lambda s: None)
    with pytest.raises(urllib.error.HTTPError):
        sources.open_with_retry("http://x")
    assert len(calls) == sources._RETRIES
