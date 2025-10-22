# api/index.py — Adaptateur Vercel (WSGI -> Serverless)
import io
from typing import Tuple, List
from app import app as flask_app

def _build_environ(request) -> dict:
    qs = request.query or ""
    if isinstance(qs, dict):
        from urllib.parse import urlencode
        qs = urlencode(qs, doseq=True)

    body_bytes = request.body or b""
    if isinstance(body_bytes, str):
        body_bytes = body_bytes.encode("utf-8", errors="ignore")

    environ = {
        "REQUEST_METHOD": request.method or "GET",
        "PATH_INFO": request.path or "/",
        "QUERY_STRING": qs or "",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": "https",
        "wsgi.input": io.BytesIO(body_bytes),
        "wsgi.errors": io.StringIO(),
        "wsgi.multithread": False,
        "wsgi.multiprocess": False,
        "wsgi.run_once": True,
    }

    for k, v in (request.headers or {}).items():
        key = "HTTP_" + k.upper().replace("-", "_")
        if key in ("HTTP_CONTENT_TYPE", "HTTP_CONTENT_LENGTH"):
            key = key.replace("HTTP_", "")
        environ[key] = v

    return environ

def handler(request):
    status_holder: List[Tuple[int, List[Tuple[str, str]]]] = []

    def start_response(status, response_headers, exc_info=None):
        code = int(status.split(" ", 1)[0])
        status_holder.append((code, response_headers))
        return lambda x: None

    environ = _build_environ(request)
    result_iter = flask_app(environ, start_response)

    body = b"".join(result_iter)
    if hasattr(result_iter, "close"):
        result_iter.close()

    status_code, headers = status_holder[0] if status_holder else (200, [])
    return (body, status_code, dict(headers))
