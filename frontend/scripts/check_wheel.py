"""Verify built wheel resources, then smoke-test the unpacked package without Node."""

import hashlib
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath


def main() -> None:
    wheel = Path(sys.argv[1]).resolve()
    with zipfile.ZipFile(wheel) as archive, tempfile.TemporaryDirectory() as temporary:
        manifest = json.loads(archive.read("eventide/workbench/manifest.json"))
        for path, expected in manifest["files"].items():
            content = archive.read("eventide/workbench/" + path)
            assert hashlib.sha256(content).hexdigest() == expected
        for name in archive.namelist():
            path = PurePosixPath(name)
            assert not path.is_absolute() and ".." not in path.parts
        archive.extractall(temporary)
        # -I excludes cwd and environment import paths; dependencies come from the
        # invoking test environment, while Eventide must come from this wheel.
        script = '''
import sys, tempfile
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import eventide
assert Path(eventide.__file__).is_relative_to(Path(sys.argv[1]))
from fastapi.testclient import TestClient
from eventide.api import create_app
from eventide.config import Settings
from eventide.host import RuntimeHost
from eventide.providers import ScriptedProvider
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    settings = Settings(workdir=root, state_dir=root / 'state', provider='openai',
                        api_key='', base_url=None, model='offline-fixture')
    host = RuntimeHost(settings, ScriptedProvider([]))
    with TestClient(create_app(host)) as client:
        assert client.get('/workbench/').status_code == 200
        manifest = client.get('/workbench/manifest.json').json()
        for asset in manifest['files']:
            assert client.get('/workbench/' + asset).status_code == 200
        assert client.get('/healthz').status_code == 200
'''
        subprocess.run([sys.executable, "-I", "-c", script, temporary], check=True, cwd=temporary)
    print("Wheel workbench resources and isolated package smoke passed")


if __name__ == "__main__":
    main()
