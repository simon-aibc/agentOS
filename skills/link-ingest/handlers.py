"""Bounded, read-only URL ingestion for SimonOS interfaces."""

from __future__ import annotations

import html
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

FETCH_TIMEOUT_SECONDS = 12
GALLERY_DL_TIMEOUT_SECONDS = 45
MAX_FETCH_BYTES = 2 * 1024 * 1024
MAX_TEXT_CHARS = 3_800  # Telegram sendMessage allows at most 4,096 characters.
MAX_OCR_MEDIA = 8
MAX_OCR_BYTES = 6 * 1024 * 1024
MAX_OCR_CHARS = 6_000
DEFAULT_COOKIES_FILE = "~/.agent-os/cookies/chrome-cookies.txt"
SOCIAL_HOSTS = frozenset({"instagram.com", "x.com", "twitter.com", "tiktok.com", "youtube.com", "youtu.be", "threads.com", "threads.net", "facebook.com", "reddit.com", "pinterest.com"})


class _ReadableTextParser(HTMLParser):
    _SKIP = frozenset({"head", "iframe", "noscript", "script", "style", "svg"})
    _BLOCK = frozenset({"article", "br", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "p", "section", "tr"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif not self._skip_depth and tag in self._BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif not self._skip_depth and tag in self._BLOCK:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        return "\n".join(" ".join(line.split()) for line in "".join(self._parts).splitlines() if line.strip())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Force every redirect through the same public-URL validation."""

    def redirect_request(self, _req: object, _fp: object, _code: int, _msg: str, _headers: object, _newurl: str) -> None:
        return None


def _is_public_host(hostname: str) -> tuple[bool, str | None]:
    if hostname in {"localhost", "localhost.localdomain"}:
        return False, "URL nội bộ không được phép."
    try:
        addresses = {entry[4][0] for entry in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)}
    except socket.gaierror:
        return False, "Không tìm thấy máy chủ của URL này."
    for address in addresses:
        if not ipaddress.ip_address(address).is_global:
            return False, "URL nội bộ hoặc địa chỉ riêng không được phép."
    return True, None


def _validate_url(url: str) -> tuple[bool, str | None]:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False, "URL không hợp lệ."
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return False, "Chỉ hỗ trợ URL http(s) công khai."
    return _is_public_host(parsed.hostname.lower().rstrip("."))


def _is_social(url: str) -> bool:
    hostname = (urllib.parse.urlparse(url).hostname or "").lower().rstrip(".")
    return any(hostname == host or hostname.endswith(f".{host}") for host in SOCIAL_HOSTS)


def _gallery_argv(url: str) -> list[str]:
    cookies_path = Path(os.getenv("AGENT_OS_COOKIES_FILE", DEFAULT_COOKIES_FILE)).expanduser()
    argv = ["gallery-dl"]
    if cookies_path.is_file():
        argv.extend(["--cookies", str(cookies_path)])
    return [*argv, "-j", url]


def _format_social_payload(payload: object) -> str:
    records = payload if isinstance(payload, list) else [payload]
    metadata: dict[str, Any] = {}
    for record in records:
        if isinstance(record, list) and len(record) > 1 and isinstance(record[1], dict):
            metadata = record[1]
            break
        if isinstance(record, dict):
            metadata = record
            break
    if not metadata:
        return "Không thể trích xuất nội dung từ metadata của liên kết này."
    author = metadata.get("author") or metadata.get("username") or metadata.get("uploader")
    text = metadata.get("description") or metadata.get("caption") or metadata.get("title")
    caption = str(text).strip() if isinstance(text, str) else ""
    output = _user_facing_summary(caption, str(author).strip() if isinstance(author, str) else "")
    ocr_text = _ocr_gallery_media(payload)
    mentioned_urls = _extract_urls(f"{caption}\n{ocr_text}")
    if mentioned_urls:
        links_block = "\n\nLink phát hiện trong bài/ảnh:\n" + "\n".join(f"- {item}" for item in mentioned_urls[:12])
        output = f"{output[:max(0, MAX_TEXT_CHARS - len(links_block))]}{links_block}"
    return f"{output[:MAX_TEXT_CHARS]}\n\nNhắn “lưu” nếu bạn muốn lưu vào Second Brain."


def _user_facing_summary(caption: str, author: str) -> str:
    clean_caption = " ".join(caption.split())
    if not clean_caption:
        return "Mình đã đọc bài viết nhưng không tìm thấy caption để tóm tắt."
    summary = clean_caption[:700].rstrip()
    if len(clean_caption) > len(summary):
        summary += "…"
    prefix = f"Bài viết từ @{author}:\n" if author else "Tóm tắt bài viết:\n"
    return f"{prefix}{summary}"


def _extract_urls(text: str) -> list[str]:
    source = str(text or "")
    found = re.findall(r"https?://[^\s<>()\[\]{}\"']+", source, flags=re.IGNORECASE)
    found.extend(
        f"https://{item}" for item in re.findall(
            r"(?<![\w/@])(?:(?:www\.)?[a-z0-9-]+\.)+(?:com|org|net|io|ai|dev|co|app|me|vn)(?:/[^\s<>()\[\]{}\"']*)?",
            source,
            flags=re.IGNORECASE,
        )
    )
    cleaned = [item.rstrip(".,;:!?") for item in found]
    return list(dict.fromkeys(item for item in cleaned if item))


def _gallery_media_urls(payload: object) -> list[str]:
    if not isinstance(payload, list):
        return []
    urls: list[str] = []
    for row in payload:
        if not isinstance(row, list) or len(row) < 3 or not isinstance(row[1], str):
            continue
        candidate = row[1]
        if candidate.startswith(("http://", "https://")) and candidate not in urls:
            urls.append(candidate)
    return urls[:MAX_OCR_MEDIA]


def _ocr_gallery_media(payload: object) -> str:
    if not shutil.which("tesseract") or not shutil.which("sips"):
        return ""
    snippets: list[str] = []
    with tempfile.TemporaryDirectory(prefix="simonos-link-ocr-") as temp_dir:
        for index, media_url in enumerate(_gallery_media_urls(payload), start=1):
            allowed, _reason = _validate_url(media_url)
            if not allowed:
                continue
            source = Path(temp_dir) / f"media-{index}.img"
            converted = Path(temp_dir) / f"media-{index}.png"
            try:
                request = urllib.request.Request(media_url, headers={"User-Agent": "SimonOS-LinkIngest/1.0"})
                with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
                    raw = response.read(MAX_OCR_BYTES + 1)
                if len(raw) > MAX_OCR_BYTES:
                    continue
                source.write_bytes(raw)
                converted_result = subprocess.run(["sips", "-s", "format", "png", str(source), "--out", str(converted)], capture_output=True, text=True, timeout=15)
                target = converted if converted_result.returncode == 0 and converted.is_file() else source
                result = subprocess.run(["tesseract", str(target), "stdout", "-l", "eng"], capture_output=True, text=True, timeout=15)
                text = " ".join(result.stdout.split())
                if text:
                    snippets.append(text)
            except (TimeoutError, OSError, subprocess.TimeoutExpired, urllib.error.URLError):
                continue
    return "\n".join(snippets)[:MAX_OCR_CHARS]


def _fetch_social(url: str) -> str:
    try:
        result = subprocess.run(_gallery_argv(url), capture_output=True, check=False, text=True, timeout=GALLERY_DL_TIMEOUT_SECONDS)
    except FileNotFoundError:
        return "Không thể đọc liên kết mạng xã hội: gallery-dl chưa được cài trên máy chủ."
    except subprocess.TimeoutExpired:
        return "Không thể đọc liên kết mạng xã hội: quá thời gian chờ."
    except OSError:
        return "Không thể đọc liên kết mạng xã hội trên máy chủ."
    if result.returncode != 0:
        details = f"{result.stderr}\n{result.stdout}".lower()
        if any(marker in details for marker in ("unsupported url", "no extractor", "no suitable extractor")):
            return "Mình chưa hỗ trợ đọc trực tiếp nguồn này. Bạn dán nội dung hoặc caption vào đây nhé."
        return "Không thể đọc liên kết mạng xã hội: bài đăng có thể riêng tư hoặc cần đăng nhập/cookies hợp lệ."
    try:
        return _format_social_payload(json.loads(result.stdout))
    except (TypeError, ValueError, json.JSONDecodeError):
        return "Không thể đọc liên kết mạng xã hội: metadata trả về không hợp lệ."


def _fetch_web(url: str) -> str:
    current_url = url
    opener = urllib.request.build_opener(_NoRedirect())
    for _ in range(4):
        request = urllib.request.Request(current_url, headers={"User-Agent": "SimonOS-LinkIngest/1.0"})
        try:
            with opener.open(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
                content_type = str(response.headers.get("Content-Type", "")).lower()
                if "text/html" not in content_type and "text/plain" not in content_type:
                    return "URL không trả về nội dung văn bản hoặc HTML có thể đọc."
                raw = response.read(MAX_FETCH_BYTES + 1)
        except urllib.error.HTTPError as error:
            # HTTPError is file-like and owns a temporary file when urllib built
            # it without a response body; close it instead of leaving it to GC.
            with error:
                if error.code in {301, 302, 303, 307, 308}:
                    target = urllib.parse.urljoin(current_url, str(error.headers.get("Location") or ""))
                    allowed, reason = _validate_url(target)
                    if not allowed:
                        return reason or "URL chuyển hướng không hợp lệ."
                    current_url = target
                    continue
                return f"Không đọc được URL: lỗi HTTP {error.code}."
        except (urllib.error.URLError, TimeoutError):
            return "Không kết nối được URL trong thời gian chờ."
        if len(raw) > MAX_FETCH_BYTES:
            return "Nội dung URL vượt giới hạn đọc an toàn."
        decoded = raw.decode("utf-8", errors="replace")
        if "text/plain" in content_type:
            return " ".join(decoded.split())[:MAX_TEXT_CHARS] or "URL không có nội dung văn bản."
        parser = _ReadableTextParser()
        parser.feed(decoded)
        return html.unescape(parser.text())[:MAX_TEXT_CHARS] or "Không thể trích xuất nội dung văn bản dễ đọc từ URL này."
    return "URL chuyển hướng quá nhiều lần."


def fetch_url(url: str) -> str:
    clean_url = str(url or "").strip()
    allowed, reason = _validate_url(clean_url)
    if not allowed:
        return reason or "URL không hợp lệ."
    return _fetch_social(clean_url) if _is_social(clean_url) else _fetch_web(clean_url)


def link_ingest(task: str, **_kwargs: object) -> dict[str, object]:
    """Agent OS handler. Input is JSON {"url": "https://..."} or a raw URL."""
    try:
        request = json.loads(task)
    except (TypeError, ValueError, json.JSONDecodeError):
        request = task
    url = str(request.get("url") if isinstance(request, dict) else request or "").strip()
    if not url:
        return {"success": False, "content": "Hãy gửi một URL http(s) để mình đọc.", "url": ""}
    return {"success": True, "content": fetch_url(url), "url": url}
