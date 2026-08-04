#!/usr/bin/env python3
"""Regenerate index.yaml when .tgz chart packages change on main.

This script is invoked by runnerlib after source checkout. It mirrors the
structure of reactorcide/jobs/scripts/deploy_k8s.py.
"""
import json
import os
import socket
import struct
import subprocess
import sys
from pathlib import Path

_SECRET_VALUES = set()


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

    register_secret(github_pat)
    remote_url = authenticated_url(charts_repo, github_pat)

    if not (Path(code_dir) / ".git").exists():
        log(f"Cloning {charts_repo} into {code_dir}")
        run_cmd(f"git clone {remote_url} {code_dir}")

    os.chdir(code_dir)
    log(f"Checking out {config['ref']}")
    run_cmd(f"git checkout {config['ref']}")

    log(f"Running helm repo index (url={charts_pages_url})")
    run_cmd(f"helm repo index . --url {charts_pages_url} --merge index.yaml")

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
