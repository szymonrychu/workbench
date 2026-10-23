import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
LIB_DIR = Path(__file__).resolve().parents[2] / "_lib"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(LIB_DIR))

import update  # noqa: E402

GH_SHIM = """#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as f:
    f.write(json.dumps(["gh", *argv]) + "\\n")
if argv[0] != "api":
    sys.exit(1)
method = argv[argv.index("-X") + 1] if "-X" in argv else "GET"
endpoint = [a for a in argv[1:] if a.startswith("repos/")][0]
if method == "GET" and "/pulls?" in endpoint:
    print("[]")
elif method == "GET" and endpoint.endswith("/issues/5/labels?per_page=100"):
    print("[[]]")
elif method == "GET":
    print(json.dumps({"default_branch": "main"}))
elif method == "POST" and endpoint.endswith("/pulls"):
    print(json.dumps({"html_url": "https://github.com/acme/gh-repo/pull/5"}))
else:
    print("{}")
"""

GLAB_SHIM = """#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as f:
    f.write(json.dumps(["glab", *argv]) + "\\n")
method = argv[argv.index("--method") + 1] if "--method" in argv else "GET"
endpoint = [a for a in argv[1:] if "projects/" in a][0]
if method == "GET" and endpoint.endswith("&state=opened"):
    print("[]")
elif method == "POST" and endpoint.endswith("merge_requests"):
    print(json.dumps({"web_url": "https://gitlab.com/acme/gl-repo/-/merge_requests/8", "iid": 8}))
elif method == "GET":
    print(json.dumps({"labels": []}))
else:
    print("{}")
"""


def _init_repo(path, origin_url):
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    if origin_url:
        subprocess.run(["git", "remote", "add", "origin", origin_url], cwd=path, check=True)


@pytest.fixture
def mixed_stubs(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, shim in (("gh", GH_SHIM), ("glab", GLAB_SHIM)):
        (bin_dir / name).write_text(shim)
        (bin_dir / name).chmod(0o755)
    log = tmp_path / "calls.log"
    monkeypatch.setenv("STUB_LOG", str(log))
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.delenv("WORKBENCH_SCM_HOSTS", raising=False)

    def calls():
        return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []

    return calls


@pytest.fixture
def mixed_workbench(tmp_path, monkeypatch):
    root = tmp_path / "workbench"
    root.mkdir()
    (root / ".gitmodules").write_text(
        "".join(
            f'[submodule "{n}"]\n\tpath = code/{n}\n\turl = x\n\tbranch = main\n'
            for n in ("gh-repo", "gl-repo", "plain-repo")
        )
    )
    (root / "code").mkdir()
    _init_repo(root / "code" / "gh-repo", "git@github.com:acme/gh-repo.git")
    _init_repo(root / "code" / "gl-repo", "https://gitlab.com/acme/gl-repo.git")
    _init_repo(root / "code" / "plain-repo", "git@git.internal.example:acme/plain-repo.git")
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def run_mixed():
    def run(*argv):
        with pytest.raises(SystemExit) as exc:
            update.main(list(argv))
        return exc.value.code

    return run
