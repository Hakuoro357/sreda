import argparse
import http.server
import json
import os
import socketserver
import subprocess
import time
from urllib.parse import urlsplit


UPSTREAM_BASE = "https://api.cerebras.ai"
ALLOWED_CLIENTS = {"127.0.0.1", "::1", "192.168.3.39"}
CURL_BIN = "curl.exe" if os.name == "nt" else "curl"
UPSTREAM_PROXY = os.environ.get("UPSTREAM_PROXY")
LOG_PATH = os.environ.get("PROXY_LOG_PATH", "/tmp/cerebras-proxy.trace.log")
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self._forward()

    def do_POST(self) -> None:
        self._forward()

    def do_OPTIONS(self) -> None:
        self._forward()

    def _forward(self) -> None:
        started_at = time.time()
        client_ip = self.client_address[0]
        if client_ip not in ALLOWED_CLIENTS:
            _log(f"deny client={client_ip} method={self.command} path={self.path}")
            self.send_error(403, "Client is not allowed")
            return

        body = b""
        content_length = self.headers.get("Content-Length")
        if content_length:
            body = self.rfile.read(int(content_length))

        path = self.path
        parsed = urlsplit(path)
        if parsed.scheme and parsed.netloc:
            upstream_url = path
        else:
            upstream_url = f"{UPSTREAM_BASE}{path}"

        body = _normalize_body(self.headers, upstream_url, body)

        cmd = [
            CURL_BIN,
            "-sS",
            "-i",
            "-X",
            self.command,
            upstream_url,
        ]

        if UPSTREAM_PROXY:
            cmd.extend(["--proxy", UPSTREAM_PROXY])

        for key, value in self.headers.items():
            if key.lower() in HOP_BY_HOP:
                continue
            cmd.extend(["-H", f"{key}: {value}"])

        if body:
            cmd.extend(["--data-binary", "@-"])

        result = subprocess.run(
            cmd,
            input=body,
            capture_output=True,
            check=False,
        )

        if result.returncode != 0:
            _log(
                f"curl_error method={self.command} path={self.path} code={result.returncode} "
                f"elapsed_ms={int((time.time() - started_at) * 1000)} stderr={result.stderr.decode('utf-8', errors='replace')[:500]}"
            )
            self.send_error(502, f"curl failed with code {result.returncode}")
            return

        raw = result.stdout
        header_blob, sep, response_body = raw.partition(b"\r\n\r\n")
        if not sep:
            header_blob, sep, response_body = raw.partition(b"\n\n")
        header_lines = header_blob.splitlines()
        if not header_lines:
            self.send_error(502, "Upstream returned no headers")
            return

        status_line = header_lines[0].decode("iso-8859-1", errors="replace")
        parts = status_line.split(" ", 2)
        if len(parts) < 2 or not parts[1].isdigit():
            self.send_error(502, "Invalid upstream status line")
            return

        status_code = int(parts[1])
        if status_code >= 400:
            dump_dir = os.environ.get("PROXY_DUMP_DIR", "/tmp/cerebras-proxy-dumps")
            os.makedirs(dump_dir, exist_ok=True)
            stamp = f"{int(time.time() * 1000)}-{status_code}"
            Path = __import__("pathlib").Path
            Path(dump_dir, f"{stamp}.request.bin").write_bytes(body)
            Path(dump_dir, f"{stamp}.response.bin").write_bytes(response_body)
        _log(
            f"ok method={self.command} path={self.path} upstream={upstream_url} "
            f"status={status_code} req_bytes={len(body)} resp_bytes={len(response_body)} "
            f"elapsed_ms={int((time.time() - started_at) * 1000)}"
        )
        self.send_response(status_code)

        for raw_line in header_lines[1:]:
            line = raw_line.decode("iso-8859-1", errors="replace")
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            if key.lower() in HOP_BY_HOP:
                continue
            self.send_header(key.strip(), value.strip())

        self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        if response_body:
            self.wfile.write(response_body)

    def log_message(self, fmt: str, *args) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8788)
    args = parser.parse_args()

    with ThreadingHTTPServer((args.host, args.port), ProxyHandler) as server:
        _log(f"server_start host={args.host} port={args.port} upstream_proxy={UPSTREAM_PROXY or '-'}")
        server.serve_forever()


def _normalize_body(headers, upstream_url: str, body: bytes) -> bytes:
    if not body:
        return body

    content_type = headers.get("Content-Type", "")
    if "application/json" not in content_type.lower():
        return body

    parsed = urlsplit(upstream_url)
    if parsed.path != "/v1/chat/completions":
        return body

    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return body

    if "store" in payload:
        payload.pop("store", None)
        _log("normalize removed_field=store path=/v1/chat/completions")
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    return body


def _log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n"
    with open(LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(line)


if __name__ == "__main__":
    main()
