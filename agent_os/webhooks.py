"""Webhook reference event sink with HMAC-SHA256 and DNS-rebinding-safe SSRF protection (Phase 4)."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import logging
import queue
import socket
import ssl
import threading
import time
import urllib.parse
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_WEBHOOK_TIMEOUT_SECONDS = 2.0
DEFAULT_WEBHOOK_MAX_RETRIES = 3
DEFAULT_WEBHOOK_QUEUE_SIZE = 1000
DEFAULT_WEBHOOK_WORKERS = 1
_SHUTDOWN_SENTINEL = object()


def _redact_url_for_log(url: str) -> str:
    """Redact path, query, and credentials from webhook URL to prevent secret leakage in logs."""
    try:
        parsed = urllib.parse.urlsplit(url)
        netloc = parsed.netloc
        if "@" in netloc:
            netloc = netloc.split("@", 1)[1]
        return f"{parsed.scheme}://{netloc}/*"
    except Exception:
        return "http(s)://<redacted>"


def is_safe_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Check whether an IP address is a safe, non-internal, global public destination."""
    return not (
        ip_obj.is_loopback
        or ip_obj.is_private
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_unspecified
        or ip_obj.is_reserved
        or not ip_obj.is_global
    )


def validate_webhook_url_syntax(
    url: str,
    allow_insecure_http: bool = False,
    allowed_internal_hosts: list[str] | set[str] | None = None,
) -> tuple[bool, str, urllib.parse.ParseResult | None]:
    """Validate URL scheme and structure, rejecting non-HTTP(S) and embedded credentials."""
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception as exc:
        return False, f"Invalid URL structure: {exc}", None

    if parsed.scheme not in {"http", "https"}:
        return False, f"Unsupported scheme '{parsed.scheme}'. Only 'http' and 'https' are allowed.", None

    if parsed.username or parsed.password:
        return False, "URLs with embedded credentials (userinfo) are not allowed.", None

    if not parsed.hostname:
        return False, "URL is missing a valid hostname.", None

    if parsed.scheme == "http" and not allow_insecure_http:
        allowed = {h.strip().lower() for h in (allowed_internal_hosts or []) if h.strip()}
        if (parsed.hostname or "").lower() not in allowed:
            return (
                False,
                "Insecure HTTP webhook URLs are disallowed by default. Use HTTPS or set 'allow_insecure_http = true' (or add host to 'allowed_internal_hosts').",
                None,
            )

    return True, "", parsed


def is_safe_webhook_url(
    url: str,
    allowed_internal_hosts: list[str] | set[str] | None = None,
    allow_insecure_http: bool = False,
) -> tuple[bool, str]:
    """Static validation of webhook URL and its current DNS resolution against SSRF.

    Note: At connection time, WebhookEventSink resolves and pins IP addresses
    directly to prevent DNS rebinding / TOCTOU vulnerabilities.
    """
    valid, reason, parsed = validate_webhook_url_syntax(
        url,
        allow_insecure_http=allow_insecure_http,
        allowed_internal_hosts=allowed_internal_hosts,
    )
    if not valid or parsed is None:
        return False, reason

    hostname = parsed.hostname or ""
    allowed = {h.strip().lower() for h in (allowed_internal_hosts or []) if h.strip()}
    if hostname.lower() in allowed:
        return True, ""

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addr_info = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except Exception as exc:
        return False, f"Could not resolve hostname '{hostname}': {exc}"

    if not addr_info:
        return False, f"No IP addresses resolved for hostname '{hostname}'."

    for item in addr_info:
        ip_str = item[4][0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError:
            return False, f"Invalid resolved IP address: {ip_str}"

        if not is_safe_ip(ip_obj):
            return False, f"Blocked unsafe/internal IP destination '{ip_str}' for host '{hostname}'."

    return True, ""


class PinnedIPHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection connecting directly to a validated pinned IP address."""

    def __init__(
        self,
        pinned_ip: str,
        original_host: str,
        port: int,
        timeout: float,
    ) -> None:
        super().__init__(host=pinned_ip, port=port, timeout=timeout)
        self.original_host = original_host


class PinnedIPHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection connecting directly to a validated pinned IP while preserving TLS SNI and cert validation."""

    def __init__(
        self,
        pinned_ip: str,
        original_host: str,
        port: int,
        timeout: float,
        context: ssl.SSLContext | None = None,
    ) -> None:
        super().__init__(host=pinned_ip, port=port, timeout=timeout, context=context)
        self.original_host = original_host

    def connect(self) -> None:
        super(http.client.HTTPSConnection, self).connect()
        # Ensure TLS handshake verifies certificate against the original domain hostname and sends proper SNI
        self.sock = self._context.wrap_socket(
            self.sock,
            server_hostname=self.original_host,
        )


def _resolve_and_validate_target(
    url: str,
    allowed_internal_hosts: list[str] | set[str] | None = None,
    allow_insecure_http: bool = False,
) -> tuple[bool, str, str, str, int, str, str]:
    """Resolve destination hostname immediately and validate all returned IPs against SSRF.

    Returns: (is_safe, error_reason, scheme, hostname, port, pinned_ip, path)
    """
    valid, reason, parsed = validate_webhook_url_syntax(
        url,
        allow_insecure_http=allow_insecure_http,
        allowed_internal_hosts=allowed_internal_hosts,
    )
    if not valid or parsed is None:
        return False, reason, "", "", 0, "", ""

    scheme = parsed.scheme
    hostname = parsed.hostname or ""
    port = parsed.port or (443 if scheme == "https" else 80)
    path = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")

    allowed = {h.strip().lower() for h in (allowed_internal_hosts or []) if h.strip()}
    is_allowed = hostname.lower() in allowed

    try:
        addr_info = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except Exception as exc:
        return False, f"DNS resolution failed for '{hostname}': {exc}", scheme, hostname, port, "", path

    if not addr_info:
        return False, f"No IP addresses found for '{hostname}'.", scheme, hostname, port, "", path

    valid_ips: list[str] = []
    for item in addr_info:
        ip_str = item[4][0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError:
            return False, f"Invalid IP address '{ip_str}'.", scheme, hostname, port, "", path

        if not is_allowed and not is_safe_ip(ip_obj):
            return (
                False,
                f"Blocked unsafe/internal IP destination '{ip_str}' for host '{hostname}'.",
                scheme,
                hostname,
                port,
                "",
                path,
            )
        valid_ips.append(ip_str)

    if not valid_ips:
        return False, f"No valid IP addresses for host '{hostname}'.", scheme, hostname, port, "", path

    # Pin the first validated IP address for the connection
    pinned_ip = valid_ips[0]
    return True, "", scheme, hostname, port, pinned_ip, path


class WebhookEventSink:
    """Reference webhook event sink delivering signed lifecycle events with anti-SSRF IP pinning."""

    name = "webhook"

    def __init__(
        self,
        url: str,
        secret: str,
        *,
        allowed_internal_hosts: list[str] | set[str] | None = None,
        allow_insecure_http: bool = False,
        max_retries: int = DEFAULT_WEBHOOK_MAX_RETRIES,
        timeout_seconds: float = DEFAULT_WEBHOOK_TIMEOUT_SECONDS,
        ssl_context: ssl.SSLContext | None = None,
        max_queue_size: int = DEFAULT_WEBHOOK_QUEUE_SIZE,
        num_workers: int = DEFAULT_WEBHOOK_WORKERS,
    ) -> None:
        self.url = url
        self.secret = secret
        self.allowed_internal_hosts = list(allowed_internal_hosts or [])
        self.allow_insecure_http = allow_insecure_http
        self.max_retries = max(1, max_retries)
        self.timeout_seconds = max(0.1, timeout_seconds)
        self.ssl_context = ssl_context
        self.max_queue_size = max(1, max_queue_size)
        self.num_workers = max(1, num_workers)

        self._queue: queue.Queue[Any] = queue.Queue(maxsize=self.max_queue_size)
        self._workers: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._closed = False

    def _ensure_workers_started(self) -> None:
        if self._closed:
            return
        if not self._workers:
            with self._lock:
                if not self._workers and not self._closed:
                    for i in range(self.num_workers):
                        worker = threading.Thread(
                            target=self._worker_loop,
                            name=f"agent-os-webhook-worker-{i}",
                            daemon=True,
                        )
                        worker.start()
                        self._workers.append(worker)

    def _worker_loop(self) -> None:
        while True:
            try:
                item = self._queue.get()
            except Exception:
                break
            if item is _SHUTDOWN_SENTINEL:
                self._queue.task_done()
                break
            try:
                self._send_with_retry(item)
            except Exception as exc:
                redacted = _redact_url_for_log(self.url)
                logger.error(
                    f"Unexpected error in webhook delivery to '{redacted}': {exc}"
                )
            finally:
                self._queue.task_done()

    def emit(self, event: dict[str, Any]) -> None:
        """Deliver lifecycle event in the background off the execution path."""
        if self._closed:
            redacted = _redact_url_for_log(self.url)
            logger.warning(
                f"WebhookEventSink is closed; dropping event '{event.get('event')}' to '{redacted}'."
            )
            return

        self._ensure_workers_started()

        try:
            self._queue.put_nowait(event)
        except queue.Full:
            redacted = _redact_url_for_log(self.url)
            logger.warning(
                f"Webhook queue is full (maxsize={self.max_queue_size}); "
                f"dropping event '{event.get('event')}' to '{redacted}'."
            )

    def close(self, timeout: float = 5.0) -> None:
        """Drain queued webhook events and stop background workers."""
        with self._lock:
            if self._closed:
                return
            self._closed = True

        if not self._workers:
            return

        for _ in self._workers:
            try:
                self._queue.put_nowait(_SHUTDOWN_SENTINEL)
            except queue.Full:
                pass

        deadline = time.time() + max(0.0, timeout)
        for worker in self._workers:
            remaining = max(0.0, deadline - time.time())
            worker.join(timeout=remaining)

    def _send_single_request(
        self,
        target_url: str,
        body: bytes,
        headers: dict[str, str],
    ) -> tuple[bool, int, str]:
        """Perform a single HTTP request connecting only to a validated pinned IP address."""
        is_safe, reason, scheme, hostname, port, pinned_ip, path = _resolve_and_validate_target(
            target_url, self.allowed_internal_hosts, self.allow_insecure_http
        )
        if not is_safe:
            logger.warning(f"Webhook delivery aborted for destination: {reason}")
            return False, 0, reason

        host_header = hostname if (port in (80, 443)) else f"{hostname}:{port}"
        req_headers = dict(headers)
        req_headers["Host"] = host_header

        conn: http.client.HTTPConnection
        if scheme == "https":
            ctx = self.ssl_context or ssl.create_default_context()
            conn = PinnedIPHTTPSConnection(
                pinned_ip=pinned_ip,
                original_host=hostname,
                port=port,
                timeout=self.timeout_seconds,
                context=ctx,
            )
        else:
            conn = PinnedIPHTTPConnection(
                pinned_ip=pinned_ip,
                original_host=hostname,
                port=port,
                timeout=self.timeout_seconds,
            )

        try:
            conn.request("POST", path, body=body, headers=req_headers)
            resp = conn.getresponse()
            status = resp.status

            if 200 <= status < 300:
                resp.read()
                return True, status, ""

            # Webhooks do not follow redirects to prevent signature forwarding & cross-origin leakage
            if 300 <= status < 400:
                resp.read()
                return False, status, f"Redirects not permitted (HTTP {status})"

            resp.read()
            return False, status, f"HTTP {status}"
        except Exception as exc:
            return False, 0, str(exc)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _send_with_retry(self, event: dict[str, Any]) -> bool:
        """Synchronously execute delivery with bounded retry and backoff."""
        body = json.dumps(event, separators=(",", ":"), sort_keys=True).encode("utf-8")
        timestamp = str(int(time.time()))
        sig_payload = f"{timestamp}.".encode() + body
        signature = hmac.new(
            self.secret.encode("utf-8"),
            sig_payload,
            hashlib.sha256,
        ).hexdigest()

        headers = {
            "Content-Type": "application/json",
            "X-AgentOS-Timestamp": timestamp,
            "X-AgentOS-Signature": f"sha256={signature}",
            "User-Agent": "AgentOS-Webhook/1.0",
        }

        redacted_url = _redact_url_for_log(self.url)
        for attempt in range(self.max_retries):
            success, status, err_msg = self._send_single_request(self.url, body, headers)
            if success:
                return True

            logger.warning(
                f"Webhook delivery attempt {attempt + 1}/{self.max_retries} "
                f"to '{redacted_url}' failed: {err_msg or f'status={status}'}"
            )

            if attempt < self.max_retries - 1:
                backoff = 0.05 * (2**attempt)
                time.sleep(backoff)

        logger.warning(
            f"Webhook delivery failed for event '{event.get('event')}' "
            f"to '{redacted_url}' after {self.max_retries} attempts."
        )
        return False
