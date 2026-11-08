"""Give each test file its own private copy of server.py.

server.py reads its settings from environment variables and builds the Kiosk (K) when it is imported, so a plain
`import server` is shared by every test file in one run (`python3 -m unittest discover`): the first file's database and
settings would win and the files would trample each other's patches. fresh_server() loads a separate module object with
the given environment, then restores os.environ so nothing leaks into the next test file."""
import importlib.util, itertools, os, sys

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_n = itertools.count(1)


def fresh_server(**env):
    saved = dict(os.environ)
    os.environ.update(env)
    try:
        name = "server_under_test_%d" % next(_n)
        spec = importlib.util.spec_from_file_location(name, os.path.join(APP_DIR, "server.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        os.environ.clear(); os.environ.update(saved)
