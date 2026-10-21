"""Atualização do MicruxSip a partir do repositório no GitHub."""
import io
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request


def _get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "MicruxSip-updater", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def parse_version(v):
    nums = [int(x) for x in re.findall(r"\d+", v)[:3]]
    return tuple(nums + [0] * (3 - len(nums)))


def latest_version(repo, branch):
    return _get(f"https://raw.githubusercontent.com/{repo}/{branch}/VERSION", timeout=8).decode().strip()


def is_newer(remote, local):
    return parse_version(remote) > parse_version(local)


def writable(path):
    return os.access(path, os.W_OK)


def update(repo, branch, dest):
    """Baixa a versão mais nova e grava por cima da instalação em `dest`."""
    if os.path.isdir(os.path.join(dest, ".git")) and shutil.which("git"):
        r = subprocess.run(["git", "-C", dest, "pull", "--ff-only"], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or "git pull falhou")
        return
    data = _get(f"https://codeload.github.com/{repo}/tar.gz/refs/heads/{branch}", timeout=60)
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            try:
                tar.extractall(tmp, filter="data")
            except TypeError:       # Python sem o parâmetro filter
                tar.extractall(tmp)
        dirs = [d for d in os.listdir(tmp) if os.path.isdir(os.path.join(tmp, d))]
        if len(dirs) != 1 or not os.path.isfile(os.path.join(tmp, dirs[0], "micruxsip.py")):
            raise RuntimeError("pacote baixado não reconhecido")
        top = os.path.join(tmp, dirs[0])
        for root, _, files in os.walk(top):
            rel = os.path.relpath(root, top)
            target_dir = os.path.normpath(os.path.join(dest, rel))
            os.makedirs(target_dir, exist_ok=True)
            for name in files:
                src = os.path.join(root, name)
                dst = os.path.join(target_dir, name)
                shutil.copy2(src, dst + ".new")
                os.replace(dst + ".new", dst)
