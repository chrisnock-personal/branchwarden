import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BRANCHWARDEN_SECRET_KEY", "t" * 64)
os.environ.setdefault("BRANCHWARDEN_KEY_FILE", "/nonexistent/secret.key")
