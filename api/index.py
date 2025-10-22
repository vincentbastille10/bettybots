# api/index.py
from pathlib import Path
import sys

# Ajouter la racine du projet au PYTHONPATH pour importer app.py
sys.path.append(str(Path(__file__).resolve().parent.parent))

# Expose l'objet Flask "app" de ton app.py à Vercel
from app import app  # noqa: F401
