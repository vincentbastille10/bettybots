 # api/index.py — Adaptateur Vercel (handler) pour une app Flask (WSGI)
 import io
+import sys
+from pathlib import Path
 from typing import Tuple, List
+
+
+# S'assure que le dossier racine (où se trouve app.py) est importable lorsque
+# Vercel exécute la fonction depuis /var/task/api.  Sans cela, "from app import"
+# échoue en production alors que ça marche en local.
+ROOT_DIR = Path(__file__).resolve().parent.parent
+if str(ROOT_DIR) not in sys.path:
+    sys.path.insert(0, str(ROOT_DIR))
+
+
 from app import app as flask_app
 
 
 def _build_environ(request) -> dict:
     """
     Construit l'environnement WSGI à partir de l'objet request fourni par Vercel.
     """
     # Vercel fournit request.method, request.path, request.headers, request.body, request.query
     qs = request.query or ""
     if isinstance(qs, dict):
         # suivant le runtime, request.query peut déjà être une chaîne
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
