import importlib.util
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# PUBLIC_MODE is read from env at import; a nonexistent cache path keeps the LADD boot seed deterministic
PUBLIC_ENV = {"LIVEMAP_PUBLIC_MODE": "1", "LIVEMAP_LADD_CACHE_PATH": "/nonexistent/ladd_cache.json"}


def load_livemap_module(filename, name=None, env=None):
    # Spec-load preserves the flat image layout; livemap is not installed as a package. env is applied for the
    # import only and restored after — os.environ, not monkeypatch, so module-scoped callers can use it too.
    prev = {k: os.environ.get(k) for k in env or {}}
    os.environ.update(env or {})
    try:
        spec = importlib.util.spec_from_file_location(name or Path(filename).stem, REPO_ROOT / "livemap" / filename)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return mod
