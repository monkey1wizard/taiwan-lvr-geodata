"""R08-4: verify a published release (Git data/output plus the GeoParquet release asset) from a fresh download.

    python scripts/verify_release.py --tag <tag> [--commit b21217e] [--mode full|stream]
        [--code-commit 8709337] [--code-python PATH]
        [--expected-catalog-sha256 H] [--candidate output.json] [--work data/tmp/r08-4] [--report PATH]

The script lists the release assets with ``gh``, downloads ``taiwanlvr.parquet``, makes a blobless clone of the
public repository and checks the committed ``data/output`` tree against ``catalog.json`` without a working tree
(``stream`` mode: Git blobs are hashed through one ``git cat-file --batch`` process). ``full`` mode also checks
out ``data/output`` and runs ``verify_output`` on it and on the downloaded GeoParquet. ``verify_output`` runs from
the code of ``--code-commit``, the commit that produced the release, not from this checkout: the catalog's rule
versions are compared with that code's constants. The code is taken from the clone with ``git archive`` and run
in its own ``uv`` environment, or with ``--code-python`` when that commit's ``uv.lock`` equals this checkout's.
Every step is recorded as
pass, fail or not-run. ``passed`` means no step failed; ``complete`` additionally needs every step to pass, so a
``stream`` run is never complete. Exit code: 0 passed, 1 failed, 2 usage error.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lvr_pipeline.output.catalog import CATALOG_NAME, PARQUET_NAME  # noqa: E402
from lvr_pipeline.storage.runs import sha256_file  # noqa: E402

DEFAULT_COMMIT = "b21217e"
DEFAULT_CODE_COMMIT = "8709337"  # code of baseline run r07-10-20261016b, which produced the first release
DEFAULT_CATALOG_SHA256 = "5a0067ccb056437ebd09ee8483b9c6ba34599d0e23803b4ab213c88482081ba2"
DEFAULT_REPO_URL = "https://github.com/monkey1wizard/taiwan-lvr-geodata.git"
DEFAULT_REPO_SLUG = "monkey1wizard/taiwan-lvr-geodata"
DEFAULT_WORK = "data/tmp/r08-4"
OUTPUT_DIR = "data/output"
CHUNK = 1 << 20
DUCKDB_DEFAULTS = {"LVR_DUCKDB_MEMORY_LIMIT": "8GiB", "LVR_DUCKDB_TEMP_LIMIT": "64GiB", "LVR_DUCKDB_THREADS": "4"}
CHECKS = ("release_assets", "download", "clone", "catalog", "file_set", "public_files", "release_file",
          "parquet_meta", "candidate", "verify_output")
MAX_LISTED = 20
LOCAL_ROOT = Path(__file__).resolve().parents[1]
CODE_PATHS = ("lvr_pipeline", "config", "pyproject.toml", "uv.lock", ".python-version")
# Runs inside the code copy: import that copy's lvr_pipeline, refuse any other, write the verify_output report.
CHILD = r"""
import json, sys
from pathlib import Path
code, public, release, spill, out = sys.argv[1:6]
sys.path.insert(0, code)
from lvr_pipeline.output import verify
if Path(verify.__file__).resolve().parents[2] != Path(code).resolve():
    raise SystemExit(f"lvr_pipeline imported from {verify.__file__}, not from {code}")
try:
    report = verify.verify_output(Path(public), Path(release), Path(spill))
except verify.OutputVerificationError as exc:
    report = getattr(exc, "report", exc.args[0])
Path(out).write_text(json.dumps(report, ensure_ascii=False, default=str), encoding="utf-8")
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_gh(args) -> bytes:
    """Run ``gh`` and return stdout. Tests replace this function."""
    done = subprocess.run(["gh", *args], capture_output=True)
    if done.returncode:
        raise RuntimeError(f"gh {' '.join(args[:2])} failed: {done.stderr.decode('utf-8', 'replace').strip()}")
    return done.stdout


def run_git(args, cwd=None) -> bytes:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True)
    if done.returncode:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {done.stderr.decode('utf-8', 'replace').strip()}")
    return done.stdout


def _rmtree(path: Path) -> None:
    def writable(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, onerror=writable)


def stream_blobs(repo: Path, commit: str, paths):
    """Yield (path, oid, size, sha256) for each ``data/output/<path>`` blob, using one cat-file process.

    oid, size and sha256 are None when the object is missing. Blobs are hashed in chunks.
    """
    proc = subprocess.Popen(["git", "-C", str(repo), "cat-file", "--batch"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        for path in paths:
            proc.stdin.write(f"{commit}:{OUTPUT_DIR}/{path}\n".encode("utf-8"))
            proc.stdin.flush()
            header = proc.stdout.readline().split()
            if len(header) != 3 or header[1] != b"blob":
                yield path, None, None, None
                continue
            size, remaining, digest = int(header[2]), int(header[2]), hashlib.sha256()
            while remaining:
                chunk = proc.stdout.read(min(CHUNK, remaining))
                if not chunk:
                    raise RuntimeError(f"cat-file ended inside {path}")
                digest.update(chunk)
                remaining -= len(chunk)
            proc.stdout.read(1)  # trailing LF
            yield path, header[0].decode(), size, digest.hexdigest()
    finally:
        proc.stdin.close()
        proc.stdout.close()
        proc.wait()


def prefetch_blobs(repo: Path, oids) -> None:
    """Best effort: fetch the needed blobs in batches instead of one lazy fetch per blob."""
    oids = list(oids)
    for start in range(0, len(oids), 200):
        try:
            run_git(["fetch", "--no-tags", "--no-write-fetch-head", "--filter=blob:none", "origin",
                     *oids[start:start + 200]], repo)
        except Exception:
            return


class Verifier:
    def __init__(self, args):
        self.args = args
        self.work = Path(args.work)
        self.repo = self.work / "download" / "repo"
        self.release_dir = self.work / "download" / "release"
        self.checks = {name: {"status": "not-run"} for name in CHECKS}
        self.disk = {}
        self.catalog = None
        self.commit = None
        self.public_hashes = {}
        self.release_hash = None

    def set(self, name, status, **detail):
        self.checks[name] = {"status": status, **detail}

    def note_disk(self, label):
        self.work.mkdir(parents=True, exist_ok=True)
        self.disk[label] = {"free_bytes": shutil.disk_usage(self.work).free}

    def entries(self, location):
        return [e for e in self.catalog["files"] if e["location"] == location]

    def release_entry(self):
        found = [e for e in self.entries("release") if e["path"] == PARQUET_NAME]
        return found[0] if len(found) == 1 else None

    def step_release_assets(self):
        a = self.args
        try:
            info = json.loads(run_gh(["release", "view", a.tag, "--repo", a.repo_slug, "--json",
                                      "assets,tagName,isDraft,isPrerelease"]))
            assets = info.get("assets") or []
        except Exception as exc:
            self.set("release_assets", "fail", error=str(exc))
            return False
        found = [x for x in assets if x.get("name") == PARQUET_NAME]
        detail = {"tag_name": info.get("tagName"), "is_draft": info.get("isDraft"),
                  "is_prerelease": info.get("isPrerelease"), "asset_names": [x.get("name") for x in assets],
                  "asset_size": found[0].get("size") if found else None,
                  "asset_digest": found[0].get("digest") if found else None}
        if not found:
            self.set("release_assets", "fail", error=f"release has no {PARQUET_NAME}", **detail)
            return False
        self.set("release_assets", "pass", **detail)
        return True

    def step_download(self):
        a = self.args
        self.release_dir.mkdir(parents=True, exist_ok=True)
        try:
            run_gh(["release", "download", a.tag, "--repo", a.repo_slug, "--pattern", PARQUET_NAME,
                    "--dir", str(self.release_dir), "--clobber"])
            if not (self.release_dir / PARQUET_NAME).is_file():
                raise RuntimeError(f"{PARQUET_NAME} not found after download")
        except Exception as exc:
            self.set("download", "fail", error=str(exc))
            return False
        self.set("download", "pass", size=(self.release_dir / PARQUET_NAME).stat().st_size)
        return True

    def step_clone(self):
        a = self.args
        self.note_disk("before_clone")
        try:
            if self.repo.exists():
                _rmtree(self.repo)
            self.repo.parent.mkdir(parents=True, exist_ok=True)
            run_git(["clone", "--filter=blob:none", "--no-checkout", a.repo_url, str(self.repo)])
            run_git(["config", "core.autocrlf", "false"], self.repo)
            try:
                resolved = run_git(["rev-parse", "--verify", f"{a.commit}^{{commit}}"], self.repo)
            except RuntimeError:
                run_git(["fetch", "origin", a.commit], self.repo)
                resolved = run_git(["rev-parse", "--verify", f"{a.commit}^{{commit}}"], self.repo)
            self.commit = resolved.decode().strip()
        except Exception as exc:
            self.set("clone", "fail", error=str(exc))
            return False
        self.set("clone", "pass", commit=self.commit, requested=a.commit)
        return True

    def step_catalog(self):
        try:
            raw = run_git(["show", f"{self.commit}:{OUTPUT_DIR}/{CATALOG_NAME}"], self.repo)
        except Exception as exc:
            self.set("catalog", "fail", error=str(exc))
            return
        actual = hashlib.sha256(raw).hexdigest()
        expected = self.args.expected_catalog_sha256
        detail = {"sha256": actual, "expected": expected}
        try:
            self.catalog = json.loads(raw)
            files = self.catalog["files"]
            assert isinstance(files, list)
        except Exception as exc:
            self.catalog = None
            self.set("catalog", "fail", error=f"catalog does not parse: {exc}", **detail)
            return
        if actual != expected:
            self.set("catalog", "fail", error="catalog sha256 differs from the expected value", **detail)
            return
        self.set("catalog", "pass", files=len(files), **detail)

    def step_file_set(self):
        try:
            listing = run_git(["ls-tree", "-r", "-l", "-z", self.commit, OUTPUT_DIR], self.repo)
        except Exception as exc:
            self.set("file_set", "fail", error=str(exc))
            return
        prefix = OUTPUT_DIR + "/"
        tree = {}
        for item in listing.decode("utf-8").split("\0"):
            if item:
                meta, path = item.split("\t", 1)
                tree[path[len(prefix):]] = meta.split()
        expected = {CATALOG_NAME} | {e["path"] for e in self.entries("public")}
        missing, extra = sorted(expected - set(tree)), sorted(set(tree) - expected)
        detail = {"tree_files": len(tree), "expected_files": len(expected), "missing": missing[:MAX_LISTED],
                  "extra": extra[:MAX_LISTED], "missing_count": len(missing), "extra_count": len(extra)}
        self.set("file_set", "fail" if missing or extra else "pass", **detail)
        self.tree_oids = [meta[2] for meta in tree.values()]

    def step_public_files(self):
        entries = {e["path"]: e for e in self.entries("public")}
        prefetch_blobs(self.repo, getattr(self, "tree_oids", []))
        bad = []
        paths = [CATALOG_NAME, *entries]
        for path, oid, size, sha in stream_blobs(self.repo, self.commit, paths):
            if oid is None:
                bad.append({"path": path, "problem": "blob missing from the commit"})
                continue
            self.public_hashes[path] = sha
            if path == CATALOG_NAME:
                continue
            entry = entries[path]
            if sha != entry["sha256"] or size != entry["size_bytes"]:
                bad.append({"path": path, "problem": "sha256 or size differs from the catalog",
                            "sha256": sha, "catalog_sha256": entry["sha256"], "size": size,
                            "catalog_size": entry["size_bytes"]})
        self.set("public_files", "fail" if bad else "pass", checked=len(entries), mismatches=len(bad),
                 examples=bad[:MAX_LISTED])

    def step_release_file(self):
        entry, path = self.release_entry(), self.release_dir / PARQUET_NAME
        if entry is None:
            self.set("release_file", "fail", error="catalog has no single release entry for " + PARQUET_NAME)
            return
        self.release_hash = sha256_file(path)
        size = path.stat().st_size
        detail = {"sha256": self.release_hash, "size": size, "catalog_sha256": entry["sha256"],
                  "catalog_size": entry["size_bytes"]}
        digest = (self.checks["release_assets"].get("asset_digest") or "")
        if digest.startswith("sha256:") and digest[7:] != entry["sha256"]:
            self.set("release_file", "fail", error="GitHub asset digest differs from the catalog", **detail)
            return
        same = self.release_hash == entry["sha256"] and size == entry["size_bytes"]
        self.set("release_file", "pass" if same else "fail", **detail)

    def step_parquet_meta(self):
        import pyarrow.parquet as pq

        entry = self.release_entry()
        try:
            parquet = pq.ParquetFile(self.release_dir / PARQUET_NAME)
            rows, columns = parquet.metadata.num_rows, parquet.metadata.num_columns
            has_geo = b"geo" in (parquet.schema_arrow.metadata or {})
        except Exception as exc:
            self.set("parquet_meta", "fail", error=str(exc))
            return
        ok = entry is not None and rows == entry["rows"]
        self.set("parquet_meta", "pass" if ok else "fail", rows=rows, catalog_rows=entry and entry["rows"],
                 columns=columns, has_geo_metadata=has_geo)

    def step_candidate(self):
        if not self.args.candidate:
            return
        try:
            record = json.loads(Path(self.args.candidate).read_text(encoding="utf-8"))
            baseline = record["files_sha256"]
        except Exception as exc:
            self.set("candidate", "fail", error=f"candidate unreadable: {exc}")
            return
        if not self.public_hashes:
            self.set("candidate", "not-run", detail="public files were not hashed")
            return
        # candidate keys look like <prefix>/public/<path> and <prefix>/release/<name>
        wanted = {}
        for key, sha in baseline.items():
            if "/public/" in key:
                wanted[("public", key.partition("/public/")[2])] = sha
            elif "/release/" in key:
                wanted[("release", key.partition("/release/")[2])] = sha
            else:
                wanted[("other", key)] = sha
        actual = {("public", p): h for p, h in self.public_hashes.items()}
        if self.release_hash:
            actual[("release", PARQUET_NAME)] = self.release_hash
        missing, extra = sorted(set(wanted) - set(actual)), sorted(set(actual) - set(wanted))
        different = sorted(k for k in set(wanted) & set(actual) if wanted[k] != actual[k])
        catalog_ok = record.get("catalog_sha256") == self.public_hashes.get(CATALOG_NAME)
        ok = not (missing or extra or different) and catalog_ok
        fmt = lambda keys: [f"{a}:{b}" for a, b in keys[:MAX_LISTED]]
        self.set("candidate", "pass" if ok else "fail", candidate_files=len(wanted), downloaded_files=len(actual),
                 catalog_matches=catalog_ok, missing=fmt(missing), extra=fmt(extra), different=fmt(different),
                 different_count=len(different))

    def resolve(self, ref):
        try:
            return run_git(["rev-parse", "--verify", f"{ref}^{{commit}}"], self.repo).decode().strip()
        except RuntimeError:
            run_git(["fetch", "origin", ref], self.repo)
            return run_git(["rev-parse", "--verify", f"{ref}^{{commit}}"], self.repo).decode().strip()

    def prepare_code(self, sha):
        """Extract the code of ``sha`` and return (code_dir, python command, environment note)."""
        code_dir = self.work / "code" / sha[:12]
        if code_dir.exists():
            _rmtree(code_dir)
        code_dir.mkdir(parents=True)
        archive = run_git(["archive", "--format=tar", sha, *CODE_PATHS], self.repo)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(code_dir, filter="data")
        if self.args.code_python:
            lock, local = sha256_file(code_dir / "uv.lock"), sha256_file(LOCAL_ROOT / "uv.lock")
            if lock != local:
                raise RuntimeError(f"uv.lock of {sha[:12]} ({lock[:12]}) differs from this checkout ({local[:12]});"
                                   " drop --code-python to build its own environment")
            return code_dir, [self.args.code_python], {"python": self.args.code_python, "uv_lock_sha256": lock}
        python = (code_dir / ".python-version").read_text(encoding="utf-8").strip()
        uv = ["--locked", "--project", str(code_dir), "--python", python]
        done = subprocess.run(["uv", "sync", *uv], capture_output=True)
        if done.returncode:
            raise RuntimeError(f"uv sync failed: {done.stderr.decode('utf-8', 'replace').strip()}")
        return code_dir, ["uv", "run", "--no-sync", *uv, "python"], {"uv_python": python,
                                                                  "uv_lock_sha256": sha256_file(code_dir / "uv.lock")}

    def step_verify_output(self):
        if self.args.mode != "full":
            self.set("verify_output", "not-run", detail="stream mode: no working tree")
            return
        self.note_disk("before_verify_output")
        try:
            run_git(["sparse-checkout", "set", "--no-cone", "/.gitattributes", f"/{OUTPUT_DIR}/"], self.repo)
            run_git(["checkout", self.commit], self.repo)
        except Exception as exc:
            self.set("verify_output", "fail", error=f"checkout failed: {exc}")
            return
        code = {"code_commit_requested": self.args.code_commit}
        try:
            code["code_commit"] = self.resolve(self.args.code_commit)
            code_dir, python, environment = self.prepare_code(code["code_commit"])
        except Exception as exc:
            self.set("verify_output", "fail", error=f"code not prepared: {exc}", **code)
            return
        code["environment"] = environment
        for name, value in DUCKDB_DEFAULTS.items():
            os.environ.setdefault(name, value)
        spill = self.work / "spill"
        spill.mkdir(parents=True, exist_ok=True)
        out = self.work / "verify_output.json"
        out.unlink(missing_ok=True)
        done = subprocess.run([*python, "-I", "-c", CHILD, str(code_dir), str(self.repo / OUTPUT_DIR),
                               str(self.release_dir), str(spill), str(out)], capture_output=True)
        if done.returncode or not out.is_file():
            error = done.stderr.decode("utf-8", "replace").strip()[-2000:]
            self.set("verify_output", "fail", error=f"verify_output exited {done.returncode}: {error}", **code)
            return
        report = json.loads(out.read_text(encoding="utf-8"))
        self.set("verify_output", "pass" if report.get("passed") else "fail", report=report, **code)

    def run(self):
        started = now()
        self.note_disk("start")
        assets_ok = self.step_release_assets()
        downloaded = assets_ok and self.step_download()
        cloned = self.step_clone()
        if cloned:
            self.step_catalog()
            if self.catalog is not None:
                self.step_file_set()
                self.step_public_files()
                if downloaded:
                    self.step_release_file()
                    self.step_parquet_meta()
                self.step_candidate()
                if downloaded or self.args.mode != "full":
                    self.step_verify_output()
                else:
                    self.set("verify_output", "not-run", detail="release file not downloaded")
        statuses = [c["status"] for n, c in self.checks.items() if n != "candidate" or self.args.candidate]
        passed = "fail" not in [c["status"] for c in self.checks.values()]
        return {"tag": self.args.tag, "commit": self.commit or self.args.commit, "mode": self.args.mode,
                "code_commit": self.checks["verify_output"].get("code_commit", self.args.code_commit),
                "repo_url": self.args.repo_url, "started_at": started, "finished_at": now(), "disk": self.disk,
                "checks": self.checks, "passed": passed, "complete": passed and all(s == "pass" for s in statuses)}


def parse_args(argv):
    p = argparse.ArgumentParser(description="Verify a published release from a fresh download.")
    p.add_argument("--tag", required=True)
    p.add_argument("--commit", default=DEFAULT_COMMIT)
    p.add_argument("--expected-catalog-sha256", default=DEFAULT_CATALOG_SHA256)
    p.add_argument("--mode", choices=("full", "stream"), default="full")
    p.add_argument("--code-commit", default=DEFAULT_CODE_COMMIT,
                   help="commit whose code produced the release; verify_output runs from it")
    p.add_argument("--code-python", help="interpreter to run that code with, allowed only when its uv.lock "
                                         "equals this checkout's; default builds an environment with uv")
    p.add_argument("--candidate", help="output.json of the baseline candidate to compare file hashes with")
    p.add_argument("--work", default=DEFAULT_WORK)
    p.add_argument("--repo-url", default=DEFAULT_REPO_URL)
    p.add_argument("--repo-slug", default=DEFAULT_REPO_SLUG)
    p.add_argument("--report")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.candidate and not Path(args.candidate).is_file():
        print(f"usage error: candidate file not found: {args.candidate}", file=sys.stderr)
        return 2
    report = Verifier(args).run()
    path = Path(args.report) if args.report else Path(args.work) / f"report-{args.tag}-{args.mode}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for name, check in report["checks"].items():
        print(f"{check['status']:8} {name}")
    print(f"passed={report['passed']} complete={report['complete']} report={path}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
