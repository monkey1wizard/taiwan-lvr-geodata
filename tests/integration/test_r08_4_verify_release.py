"""R08-4: scripts/verify_release.py against a synthetic release and a local bare Git repository."""
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO / "tests" / "acceptance"))

from test_r07_9_catalog_verify import MONTH_CSV, base, built  # noqa: E402,F401  (module fixtures: synthetic output)

SCRIPT = REPO / "scripts" / "verify_release.py"
_spec = importlib.util.spec_from_file_location("r08_4_verify_release_script", SCRIPT)
vr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vr)

CODE = [*vr.CODE_PATHS]
IDENT = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "core.hooksPath=.git/hooks"]  # no host hooks
TAG = "v-test"


def git(cwd, *args):
    done = subprocess.run(["git", *IDENT, *args], cwd=cwd, capture_output=True, check=True)
    return done.stdout.decode().strip()


class Fixture:
    def __init__(self, tmp, baseline):
        self.tmp, self.baseline = tmp, baseline
        self.src = tmp / "src"
        shutil.copytree(baseline["root"] / "public", self.src / "data" / "output")
        (self.src / ".gitattributes").write_text("data/output/** -text\n", encoding="utf-8")
        for name in CODE:  # the code that built the synthetic output, so verify_output can run from this commit
            source = REPO / name
            if source.is_dir():
                shutil.copytree(source, self.src / name, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copyfile(source, self.src / name)
        git(self.src, "init", "-q", "-b", "main")
        git(self.src, "add", "-A")
        git(self.src, "commit", "-q", "-m", "data")
        self.first = git(self.src, "rev-parse", "HEAD")
        self.bare = tmp / "bare.git"
        git(tmp, "clone", "-q", "--bare", str(self.src), str(self.bare))
        git(self.bare, "config", "uploadpack.allowFilter", "true")
        git(self.bare, "config", "uploadpack.allowAnySHA1InWant", "true")
        self.parquet = baseline["root"] / "release" / "taiwanlvr.parquet"
        self.catalog_sha = hashlib.sha256((self.src / "data" / "output" / "catalog.json").read_bytes()).hexdigest()
        self.assets = [{"name": "taiwanlvr.parquet", "size": self.parquet.stat().st_size}]
        self.truncate = False

    def commit_more(self, change, root=False):
        change(self.src if root else self.src / "data" / "output")
        git(self.src, "add", "-A")
        git(self.src, "commit", "-q", "-m", "more")
        git(self.src, "push", "-q", str(self.bare), "main")
        return git(self.src, "rev-parse", "HEAD")

    def fake_gh(self, args):
        if args[:2] == ["release", "view"]:
            return json.dumps({"tagName": TAG, "isDraft": False, "isPrerelease": False, "assets": self.assets}).encode()
        assert args[:2] == ["release", "download"]
        target = Path(args[args.index("--dir") + 1]) / "taiwanlvr.parquet"
        data = self.parquet.read_bytes()
        target.write_bytes(data[:-10] if self.truncate else data)
        return b""

    def run(self, monkeypatch, *extra, mode="full", commit=None, sha=None, code=None):
        monkeypatch.setattr(vr, "run_gh", self.fake_gh)
        for name, value in (("LVR_DUCKDB_MEMORY_LIMIT", "256MB"), ("LVR_DUCKDB_TEMP_LIMIT", "1GiB"),
                            ("LVR_DUCKDB_THREADS", "2")):
            monkeypatch.setenv(name, value)
        report_path = self.tmp / "report.json"
        code = vr.main(["--tag", TAG, "--commit", commit or self.first, "--mode", mode,
                        "--expected-catalog-sha256", sha or self.catalog_sha, "--repo-url", self.bare.as_uri(),
                        "--work", str(self.tmp / "work"), "--report", str(report_path),
                        "--code-commit", code or self.first, "--code-python", sys.executable, *extra])
        return code, json.loads(report_path.read_text(encoding="utf-8"))


@pytest.fixture
def fx(base, tmp_path):
    return Fixture(tmp_path, base)


def statuses(report):
    return {name: check["status"] for name, check in report["checks"].items()}


def test_full_mode_passes_and_is_complete(fx, monkeypatch):
    code, report = fx.run(monkeypatch)
    assert code == 0 and report["passed"] is True and report["complete"] is True, statuses(report)
    assert all(s == "pass" for n, s in statuses(report).items() if n != "candidate")
    assert statuses(report)["candidate"] == "not-run"  # optional, so it does not block completeness
    assert report["checks"]["parquet_meta"]["has_geo_metadata"] is True
    assert report["checks"]["public_files"]["checked"] > 0 and report["disk"]["before_verify_output"]["free_bytes"] > 0
    check = report["checks"]["verify_output"]
    assert check["code_commit"] == fx.first and report["code_commit"] == fx.first
    assert check["environment"]["python"] == sys.executable


def test_verify_output_runs_from_the_code_commit(fx, monkeypatch):
    """A later commit with another rule version must not change the verdict on data built by the first commit."""
    def bump(root):
        path = root / "lvr_pipeline" / "addresses" / "identity.py"
        text = path.read_text(encoding="utf-8")
        assert text.count('NORMALIZATION_VERSION = "') == 1
        path.write_text(text.replace('NORMALIZATION_VERSION = "', 'NORMALIZATION_VERSION = "x', 1), encoding="utf-8")

    later = fx.commit_more(bump, root=True)
    code, report = fx.run(monkeypatch, commit=later, code=later)
    check = report["checks"]["verify_output"]
    assert code == 1 and check["status"] == "fail" and check["code_commit"] == later
    assert any("door_rule_version" in f["detail"] for f in check["report"]["failures"])

    code, report = fx.run(monkeypatch, commit=later, code=fx.first)
    assert code == 0 and report["checks"]["verify_output"]["status"] == "pass", report["checks"]["verify_output"]


def test_code_python_refused_when_uv_lock_differs(fx, monkeypatch):
    later = fx.commit_more(lambda root: (root / "uv.lock").write_text("other\n", encoding="utf-8"), root=True)
    code, report = fx.run(monkeypatch, code=later)
    check = report["checks"]["verify_output"]
    assert code == 1 and check["status"] == "fail" and "uv.lock" in check["error"]


def test_stream_mode_passes_but_is_never_complete(fx, monkeypatch):
    code, report = fx.run(monkeypatch, mode="stream")
    assert code == 0 and report["passed"] is True and report["complete"] is False
    assert report["checks"]["verify_output"]["status"] == "not-run"
    assert "no working tree" in report["checks"]["verify_output"]["detail"]
    assert not (fx.tmp / "work" / "download" / "repo" / "data").exists()


def test_wrong_expected_catalog_hash_fails(fx, monkeypatch):
    code, report = fx.run(monkeypatch, sha="0" * 64)
    assert code == 1 and report["passed"] is False and report["complete"] is False
    assert report["checks"]["catalog"]["status"] == "fail"


def test_altered_csv_names_the_path(fx, monkeypatch):
    def alter(output):
        path = output / MONTH_CSV
        path.write_bytes(path.read_bytes().replace(b"3250000", b"3250001", 1))

    second = fx.commit_more(alter)
    code, report = fx.run(monkeypatch, mode="stream", commit=second)
    check = report["checks"]["public_files"]
    assert code == 1 and check["status"] == "fail"
    assert [m["path"] for m in check["examples"]] == [MONTH_CSV]
    assert report["checks"]["file_set"]["status"] == "pass"


def test_extra_file_in_commit_fails_file_set(fx, monkeypatch):
    second = fx.commit_more(lambda output: (output / "stray.txt").write_text("x", encoding="utf-8"))
    code, report = fx.run(monkeypatch, mode="stream", commit=second)
    assert code == 1 and report["checks"]["file_set"]["status"] == "fail"
    assert report["checks"]["file_set"]["extra"] == ["stray.txt"]


def test_missing_release_asset(fx, monkeypatch):
    fx.assets = [{"name": "other.bin", "size": 1}]
    code, report = fx.run(monkeypatch, mode="stream")
    found = statuses(report)
    assert code == 1 and found["release_assets"] == "fail" and found["download"] == "not-run"
    assert found["release_file"] == "not-run" and found["parquet_meta"] == "not-run"
    assert found["public_files"] == "pass"


def candidate_file(fx, tmp_path, change=None):
    prefix = "output/output-x"
    files = {f"{prefix}/public/{p.relative_to(fx.src / 'data' / 'output').as_posix()}": hashlib.sha256(
        p.read_bytes()).hexdigest() for p in (fx.src / "data" / "output").rglob("*") if p.is_file()}
    files[f"{prefix}/release/taiwanlvr.parquet"] = hashlib.sha256(fx.parquet.read_bytes()).hexdigest()
    if change:
        change(files)
    path = tmp_path / "output.json"
    path.write_text(json.dumps({"catalog_sha256": fx.catalog_sha, "files_sha256": files}), encoding="utf-8")
    return path


def test_candidate_matching_and_changed(fx, monkeypatch, tmp_path):
    code, report = fx.run(monkeypatch, "--candidate", str(candidate_file(fx, tmp_path)), mode="stream")
    assert code == 0 and report["checks"]["candidate"]["status"] == "pass"

    def change(files):
        key = next(k for k in files if k.endswith(MONTH_CSV))
        files[key] = "0" * 64

    code, report = fx.run(monkeypatch, "--candidate", str(candidate_file(fx, tmp_path, change)), mode="stream")
    check = report["checks"]["candidate"]
    assert code == 1 and check["status"] == "fail" and check["different_count"] == 1
    assert check["different"] == [f"public:{MONTH_CSV}"]


def test_truncated_download_fails_release_file(fx, monkeypatch):
    fx.truncate = True
    code, report = fx.run(monkeypatch, mode="stream")
    assert code == 1 and report["checks"]["release_file"]["status"] == "fail"
    assert report["checks"]["release_file"]["size"] == fx.parquet.stat().st_size - 10
