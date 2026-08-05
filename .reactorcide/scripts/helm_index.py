#!/usr/bin/env python3
"""Regenerate index.yaml when .tgz chart packages change on main.

This script is invoked by runnerlib after source checkout. It mirrors the
structure of reactorcide/jobs/scripts/deploy_k8s.py.
"""
import hashlib
import json
import os
import platform
import shutil
import socket
import struct
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

_SECRET_VALUES = set()

HELM_VERSION = "v4.2.3"
HELM_BASE_URL = "https://get.helm.sh"

# sha256 checksums published at https://get.helm.sh/helm-{HELM_VERSION}-{platform}.{ext}.sha256sum
HELM_CHECKSUMS = {
    "darwin-amd64": "ff3ac86755a45f3422473bc1200776aac0fe04c5766abe6ca66699f7b564b23b",
    "darwin-arm64": "048ecf5ad3160f83d918f9fe945238d2132b079640f7b106175331c25f242c64",
    "linux-386": "31d57972d36e60388e173327fffcf9d58f272349dfa9ed3e1914f3cd88fe7283",
    "linux-amd64": "e9b88b4ee95b18c706839c28d3a0220e5bc470e9cd9262410c90793c45ff8b7c",
    "linux-arm": "ba00678361ca7a03ec42ca1ea459543e1d8eab2a7d5429a5eda71dc9741c8a9b",
    "linux-arm64": "21abd9354d39b2cd79a8d76be6912cd137a983cbf997193503fb8a6a6e2f2785",
    "linux-loong64": "232f82d787d530a621b2006965ed2b99644b4391bbc6261e9787f95700fc44f7",
    "linux-ppc64le": "43fc5a4b20839c3669a0748498bd2613b095e288425bf5678c6ba664eb4a0e70",
    "linux-riscv64": "09ff0772730678c652b9ac4a2b32cd20f4e62a2b040403bcacd4ad845d3d3e9c",
    "linux-s390x": "17932091e19d352585b540a482fca9b953d32a8ad7afec72bf9cbbcd96b094cb",
    "windows-amd64": "5ca7de684c92d48b93d5c34a029fdda57b38e1eac04bc8541bdf1eb249388679",
    "windows-arm64": "5f444ed097688ed3abaf1d8801e21110d9bddeb6ed13939afcac302888527ab5",
}

# Maps Python's platform.machine() values to helm's release arch names.
_ARCH_ALIASES = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
    "i386": "386",
    "i686": "386",
    "x86": "386",
    "armv7l": "arm",
    "armv6l": "arm",
    "ppc64le": "ppc64le",
    "riscv64": "riscv64",
    "s390x": "s390x",
    "loongarch64": "loong64",
}


def detect_platform() -> str:
    """Return the helm release platform key (e.g. 'linux-amd64') for the current host."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = _ARCH_ALIASES.get(machine)
    if arch is None:
        raise RuntimeError(f"Unsupported architecture for helm install: {machine}")

    key = f"{system}-{arch}"
    if key not in HELM_CHECKSUMS:
        raise RuntimeError(f"No known helm checksum for platform: {key}")
    return key

def log(msg: str) -> None:
    """Print log message."""
    print(f"[helm-index] {msg}", flush=True)


def redact(value: str) -> str:
    """Redact known secret values from log output."""
    redacted = value
    for secret in sorted(_SECRET_VALUES, key=len, reverse=True):
        if secret:
            redacted = redacted.replace(secret, "***")
    return redacted


def run_cmd(cmd: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    """Run a shell command."""
    log(f"Running: {redact(cmd)}")
    return subprocess.run(cmd, shell=True, check=check, capture_output=capture, text=True)


def run_cmd_output(cmd: str) -> str:
    """Run a command and return its output."""
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def register_secret(secret: str) -> None:
    """Register a secret value for masking via the runnerlib secrets socket."""
    if secret:
        _SECRET_VALUES.add(secret)

    socket_path = os.environ.get('REACTORCIDE_SECRETS_SOCKET')
    if not socket_path or not secret:
        return
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5.0)
        sock.connect(socket_path)
        msg = json.dumps({'action': 'register', 'secrets': [secret]}).encode('utf-8')
        sock.send(struct.pack('!I', len(msg)))
        sock.send(msg)
        sock.close()
    except Exception as e:
        log(f"ERROR: Failed to register secret for masking: {e}")


def install_helm() -> None:
    """Download, checksum-verify, and install the pinned helm version."""
    if shutil.which("helm"):
        log("helm already installed, skipping install")
        return

    plat = detect_platform()
    ext = "zip" if plat.startswith("windows-") else "tar.gz"
    tarball_name = f"helm-{HELM_VERSION}-{plat}.{ext}"
    tarball_url = f"{HELM_BASE_URL}/{tarball_name}"
    expected_sha256 = HELM_CHECKSUMS[plat]

    with tempfile.TemporaryDirectory() as tmpdir:
        tarball_path = Path(tmpdir) / tarball_name

        log(f"Downloading {tarball_url}")
        urllib.request.urlretrieve(tarball_url, tarball_path)

        actual_sha256 = hashlib.sha256(tarball_path.read_bytes()).hexdigest()
        if actual_sha256 != expected_sha256:
            raise RuntimeError(
                f"helm checksum mismatch: expected {expected_sha256}, got {actual_sha256}"
            )
        log("helm checksum verified")

        if ext == "zip":
            with zipfile.ZipFile(tarball_path) as zf:
                zf.extractall(tmpdir)
        else:
            with tarfile.open(tarball_path) as tar:
                tar.extractall(tmpdir)

        binary_name = "helm.exe" if plat.startswith("windows-") else "helm"
        install_dir = Path(tempfile.mkdtemp(prefix="helm-bin-"))
        extracted_binary = Path(tmpdir) / plat / binary_name
        shutil.copy2(extracted_binary, install_dir / binary_name)
        os.chmod(install_dir / binary_name, 0o755)
        os.environ["PATH"] = f"{install_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        log(f"Installed helm {HELM_VERSION} ({plat}) to {install_dir / binary_name}")


def read_config() -> dict:
    """Read job config from environment variables."""
    return {
        'code_dir': os.environ.get('REACTORCIDE_CODE_DIR', '/job/src'),
        'ref': os.environ.get('REACTORCIDE_SHA') or os.environ.get('REACTORCIDE_HEAD_REF', 'main'),
        'github_pat': os.environ.get('GITHUB_PAT', ''),
        'charts_repo': os.environ.get('CHARTS_REPO', ''),
        'charts_pages_url': os.environ.get('CHARTS_PAGES_URL', ''),
    }


def authenticated_url(charts_repo: str, github_pat: str) -> str:
    return f"https://x-access-token:{github_pat}@github.com/{charts_repo}.git"


def update_index(config: dict) -> int:
    code_dir = config['code_dir']
    github_pat = config['github_pat']
    charts_repo = config['charts_repo']
    charts_pages_url = config['charts_pages_url']

    install_helm()

    register_secret(github_pat)
    remote_url = authenticated_url(charts_repo, github_pat)

    if not (Path(code_dir) / ".git").exists():
        log(f"Cloning {charts_repo} into {code_dir}")
        run_cmd(f"git clone {remote_url} {code_dir}")

    os.chdir(code_dir)
    log(f"Checking out {config['ref']}")
    run_cmd(f"git checkout {config['ref']}")

    log(f"Running helm repo index (url={charts_pages_url})")
    run_cmd(f"helm repo index . --url {charts_pages_url} index.yaml")

    run_cmd('git config user.name "catalystcommunityci"')
    run_cmd('git config user.email "ci@catalystcommunity.org"')
    run_cmd("git add index.yaml")

    diff = run_cmd("git diff --cached --quiet", check=False)
    if diff.returncode == 0:
        log("index.yaml already up to date")
        return 0

    run_cmd('git commit -m "ci: Automation - update helm chart index"')
    run_cmd(f"git push {remote_url} HEAD:main")
    return 0


def main() -> int:
    try:
        config = read_config()
        return update_index(config)
    except subprocess.CalledProcessError as e:
        log(f"Command failed with exit code {e.returncode}")
        if e.stderr:
            log(f"stderr: {e.stderr}")
        return e.returncode
    except Exception as e:
        log(f"Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
