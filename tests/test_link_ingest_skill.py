import importlib.util
import io
import json
import subprocess
import urllib.error
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "skills" / "link-ingest"


def load_handlers():
    spec = importlib.util.spec_from_file_location("link_ingest_handlers_test", PACKAGE / "handlers.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_handler_returns_readable_page_text(monkeypatch):
    handlers = load_handlers()
    monkeypatch.setattr(handlers, "_validate_url", lambda _url: (True, None))
    monkeypatch.setattr(handlers, "_is_social", lambda _url: False)
    monkeypatch.setattr(handlers, "_fetch_web", lambda _url: "Article body")
    assert handlers.link_ingest(json.dumps({"url": "https://example.com/article"})) == {"success": True, "content": "Article body", "url": "https://example.com/article"}


def test_private_network_urls_are_rejected_without_fetch(monkeypatch):
    handlers = load_handlers()
    monkeypatch.setattr(handlers, "_is_public_host", lambda _host: (False, "URL nội bộ hoặc địa chỉ riêng không được phép."))
    assert handlers.fetch_url("http://127.0.0.1/private") == "URL nội bộ hoặc địa chỉ riêng không được phép."


def test_redirect_to_private_network_is_rejected(monkeypatch):
    handlers = load_handlers()

    class RedirectingOpener:
        def open(self, _request, timeout):
            raise urllib.error.HTTPError(
                "https://example.com",
                302,
                "Found",
                {"Location": "http://127.0.0.1/private"},
                io.BytesIO(b""),
            )

    monkeypatch.setattr(handlers.urllib.request, "build_opener", lambda *_handlers: RedirectingOpener())
    monkeypatch.setattr(handlers, "_validate_url", lambda url: (False, "URL nội bộ hoặc địa chỉ riêng không được phép.") if "127.0.0.1" in url else (True, None))

    assert handlers._fetch_web("https://example.com") == "URL nội bộ hoặc địa chỉ riêng không được phép."


def test_threads_unsupported_url_is_clear_and_not_a_login_error(monkeypatch):
    handlers = load_handlers()
    monkeypatch.setattr(handlers.subprocess, "run", lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 1, "", "Unsupported URL 'https://www.threads.com/share/example'"))
    output = handlers._fetch_social("https://www.threads.com/share/example")
    assert "chưa hỗ trợ đọc trực tiếp nguồn này" in output
    assert "đăng nhập" not in output


def test_social_metadata_is_extracted(monkeypatch):
    handlers = load_handlers()
    payload = [[0, {"author": "bestapps", "description": "Useful caption"}]]
    monkeypatch.setattr(handlers.subprocess, "run", lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0, json.dumps(payload), ""))
    assert handlers._fetch_social("https://instagram.com/p/example/") == (
        "Bài viết từ @bestapps:\nUseful caption\n\nNhắn “lưu” nếu bạn muốn lưu vào Second Brain."
    )


def test_caption_urls_are_returned_as_mentioned_links(monkeypatch):
    handlers = load_handlers()
    monkeypatch.setattr(handlers, "_ocr_gallery_media", lambda _payload: "")
    payload = [[0, {"author": "person", "description": "Read https://example.com/a and https://github.com/org/repo."}]]
    output = handlers._format_social_payload(payload)
    assert "Link phát hiện trong bài/ảnh:" in output
    assert "https://example.com/a" in output
    assert "https://github.com/org/repo" in output
    assert "Nhắn “lưu”" in output


def test_ocr_text_is_not_exposed_but_its_urls_are_listed(monkeypatch):
    handlers = load_handlers()
    monkeypatch.setattr(
        handlers,
        "_ocr_gallery_media",
        lambda _payload: "Raw OCR slide text that must stay private: github.com/acme/project",
    )
    payload = [[0, {"author": "person", "description": "A concise caption."}]]

    output = handlers._format_social_payload(payload)

    assert "Raw OCR slide text" not in output
    assert "https://github.com/acme/project" in output


def test_bare_domains_from_carousel_ocr_are_normalized_to_links():
    handlers = load_handlers()
    assert handlers._extract_urls("Try github.com/chartdb/chartdb and www.example.org/docs.") == [
        "https://github.com/chartdb/chartdb",
        "https://www.example.org/docs",
    ]


def test_manifest_exposes_link_ingest_handler():
    manifest = (PACKAGE / "manifest.toml").read_text(encoding="utf-8")
    assert 'name = "link_ingest"' in manifest
    assert 'entrypoint = "handlers:link_ingest"' in manifest
