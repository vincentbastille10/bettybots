# api/index.py — Adaptateur Vercel (handler) pour une app Flask (WSGI)
import io
from typing import List, Tuple
from app import app as flask_app


def _build_environ(request) -> dict:
    """
    Construit l'environnement WSGI à partir de l'objet request fourni par Vercel.
    """
    # Vercel fournit request.method, request.path, request.headers, request.body, request.query
    qs = getattr(request, "query", "") or ""
    if isinstance(qs, dict):
        # suivant le runtime, request.query peut déjà être une chaîne
        from urllib.parse import urlencode
        qs = urlencode(qs, doseq=True)

    body_bytes = getattr(request, "body", b"") or b""
    if isinstance(body_bytes, str):
        body_bytes = body_bytes.encode("utf-8", errors="ignore")

    environ = {
        "REQUEST_METHOD": getattr(request, "method", "GET") or "GET",
        "PATH_INFO": getattr(request, "path", "/") or "/",
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

    # Transfert des en-têtes HTTP
    for k, v in (getattr(request, "headers", {}) or {}).items():
        key = "HTTP_" + k.upper().replace("-", "_")
        if key in ("HTTP_CONTENT_TYPE", "HTTP_CONTENT_LENGTH"):
            key = key.replace("HTTP_", "")
        environ[key] = v

    return environ


def handler(request):
    """
    Adaptateur principal pour Vercel : exécute l'application Flask
    et retourne (body, status, headers).
    """
    status_holder: List[Tuple[int, List[Tuple[str, str]]]] = []

    def start_response(status, response_headers, exc_info=None):
        code = int(status.split(" ", 1)[0])
        status_holder.append((code, response_headers))
        # WSGI "write" callable non utilisé
        return lambda x: None

    environ = _build_environ(request)
    result_iter = flask_app(environ, start_response)

    try:
        body = b"".join(result_iter)
    finally:
        if hasattr(result_iter, "close"):
            result_iter.close()

    status_code, headers = status_holder[0] if status_holder else (200, [])
    return (body, status_code, dict(headers))
