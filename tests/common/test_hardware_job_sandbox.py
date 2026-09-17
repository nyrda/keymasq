"""Exercise the job's runtime write allowances in a private mount namespace."""

import configparser
import errno
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("allow_marker_cleanup", [False, True])
def test_readonly_runtime_allows_only_the_declared_cleanup_paths(tmp_path, allow_marker_cleanup):
    mount_executable = shutil.which("mount")
    if not shutil.which("unshare") or not mount_executable:
        pytest.skip("Mount namespace tools are unavailable")
    probe = subprocess.run(
        ["unshare", "--user", "--map-root-user", "--mount", "true"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if probe.returncode:
        pytest.skip(f"User mount namespaces unavailable: {probe.stderr.strip()}")
    unit = configparser.ConfigParser(interpolation=None)
    unit.read(Path(__file__).resolve().parents[2] / "systemd/keymasq-hardware@.service")
    writable = unit["Service"]["ReadWritePaths"].split()
    if not allow_marker_cleanup:
        writable = [path for path in writable if "/hidden" not in path]
    # /run is replaced only inside the child namespace. The host daemon's
    # markers, socket, and hardware jobs are never accessed by this probe.
    result = subprocess.run(
        [
            "unshare",
            "--user",
            "--map-root-user",
            "--mount",
            "--fork",
            sys.executable,
            "-c",
            SANDBOX_PROBE,
            str(tmp_path),
            str(Path(mount_executable).resolve()),
            *writable,
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    outcomes = json.loads(result.stdout)
    expected = 0 if allow_marker_cleanup else errno.EROFS
    assert outcomes == {
        "keymasq/hidden/event5": expected,
        "keymasq/hidden-hardware/abcd:1234": expected,
        "keymasq/unrelated": errno.EROFS,
        "unrelated": errno.EROFS,
    }


SANDBOX_PROBE = """
import json
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1])
for directory in (
    'keymasq/hardware-requests', 'udev/rules.d', 'keymasq/hidden', 'keymasq/hidden-hardware'
):
    (root / directory).mkdir(parents=True)
names = (
    'keymasq/hidden/event5', 'keymasq/hidden-hardware/abcd:1234', 'keymasq/unrelated', 'unrelated'
)
for name in names:
    (root / name).write_text('marker')
def mount(*args):
    subprocess.run([sys.argv[2], *args], check=True, capture_output=True)
mount('--make-rprivate', '/')
mount('--bind', str(root), '/run')
mount('-o', 'remount,bind,ro', '/run')
for path in sys.argv[3:]:
    path = path.lstrip('-')
    mount('--bind', path, path)
    mount('-o', 'remount,bind,rw', path)
outcomes = {}
for name in names:
    try:
        (Path('/run') / name).unlink(missing_ok=True)
        outcomes[name] = 0
    except OSError as exc:
        outcomes[name] = exc.errno
print(json.dumps(outcomes))
"""
