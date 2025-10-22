# api/index.py
from app import app as app  # expose le WSGI app attendu par Vercel
application = app           # alias optionnel (certains runtimes regardent aussi 'application')
