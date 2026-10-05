"""Hash-pinned public retrieval and publish-before-pointer GitHub handoff."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
from urllib.parse import urlparse, quote
from urllib.request import Request, urlopen

from .export import verify_month
from .packaging import (
    extract_handoff,
    safe_path,
    verify_output,
    write_json,
    MAX_ASSET_BYTES,
)
from .sources import sha256_file


def download(url, path, *, max_bytes=MAX_ASSET_BYTES):
    if urlparse(url).scheme != "https":
        raise ValueError("Public artifacts require HTTPS")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with (
        urlopen(
            Request(url, headers={"User-Agent": "taiwan-lvr-geodata/0.1"}), timeout=120
        ) as source,
        path.open("xb") as target,
    ):
        size = 0
        while chunk := source.read(2**20):
            size += len(chunk)
            if size > max_bytes:
                raise ValueError("Download exceeds resource budget")
            target.write(chunk)


def fetch_output(
    manifest_url,
    manifest_sha256,
    target: Path,
    *,
    month=None,
    category=None,
    format=None,
    maintenance=False,
    transport=download,
):
    target = Path(target)
    if target.exists():
        raise FileExistsError(target)
    if len(manifest_sha256) != 64:
        raise ValueError("Expected immutable manifest SHA-256 required")
    if category and category not in {"sales", "presale", "rent"}:
        raise ValueError("Unknown output category")
    if format and format not in {"geoparquet", "geojson", "ndjson"}:
        raise ValueError("Unknown output format")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fetch-", dir=target.parent) as folder:
        root = Path(folder)
        transport(manifest_url, root / "manifest.json")
        if sha256_file(root / "manifest.json") != manifest_sha256:
            raise ValueError("Public manifest hash mismatch")
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        if manifest["schema_version"] != "1.0" or manifest["key_version"] != "v2":
            raise ValueError("Incompatible offline snapshot version")
        selected = [
            a
            for a in manifest["assets"]
            if (
                a["kind"] == "maintenance"
                if maintenance
                else a["kind"] == "monthly"
                and (month is None or a["tx_yyyymm"] == month)
                and (category is None or a["category"] == category)
                and (format is None or a["format"] == format)
            )
        ]
        full = month is None and category is None and format is None and not maintenance
        if full:
            selected = manifest["assets"]
        if not selected:
            raise ValueError(
                "Requested month/category/format absent from declared scope"
            )
        base = manifest_url.rsplit("/", 1)[0]
        for item in selected:
            safe_path(item["path"])
            safe_path(item["asset_name"])
            path = root / item["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            transport(base + "/" + quote(item["asset_name"]), path)
            if (
                path.stat().st_size != item["bytes"]
                or sha256_file(path) != item["sha256"]
            ):
                raise ValueError("Downloaded artifact hash/size mismatch")
        if full:
            verify_output(root)
        elif maintenance:
            extract_handoff(root / selected[0]["path"], root / "maintenance")
        elif format is None:
            groups = {}
            for item in selected:
                groups.setdefault((item["tx_yyyymm"], item["category"]), {})[
                    item["format"]
                ] = root / item["path"]
            for key, paths in groups.items():
                stats = verify_month(paths)
                declared = next(
                    m for m in manifest["months"] if m["tx_yyyymm"] == key[0]
                )["categories"][key[1]]
                if any(declared[k] != v for k, v in stats.items()):
                    raise ValueError("Downloaded monthly statistics mismatch")
        write_json(
            root / "selection.json",
            {
                "snapshot_id": manifest["snapshot_id"],
                "manifest_sha256": manifest_sha256,
                "assets": [a["path"] for a in selected],
                "maintenance": maintenance,
            },
        )
        os.replace(root, target)
    return target


class GitHubRelease:
    def __init__(self, repository, checkout: Path):
        if repository != "monkey1wizard/taiwan-lvr-geodata":
            raise ValueError("Unexpected publication repository")
        self.repository, self.checkout = repository, Path(checkout)

    def _gh(self, *args):
        return subprocess.check_output(["gh", *args], cwd=self.checkout, text=True)

    def head(self):
        return json.loads(
            self._gh("api", f"repos/{self.repository}/git/ref/heads/main")
        )["object"]["sha"]

    def immutable_enabled(self):
        return json.loads(
            self._gh("api", f"repos/{self.repository}/immutable-releases")
        )["enabled"]

    def create(self, tag, parent, notes_path):
        self._gh(
            "release",
            "create",
            tag,
            "--repo",
            self.repository,
            "--target",
            parent,
            "--draft",
            "--title",
            tag,
            "--notes-file",
            str(notes_path),
        )

    def upload(self, tag, path):
        self._gh("release", "upload", tag, str(path), "--repo", self.repository)

    def upload_many(self, tag, paths):
        # Bound argv length on Windows while letting gh transfer each batch.
        for offset in range(0, len(paths), 40):
            self._gh(
                "release",
                "upload",
                tag,
                *[str(p) for p in paths[offset : offset + 40]],
                "--repo",
                self.repository,
            )

    def verify_assets(self, tag, expected):
        release = json.loads(
            self._gh("api", f"repos/{self.repository}/releases/tags/{tag}")
        )
        if sorted(a["name"] for a in release["assets"]) != sorted(
            name for name, _, _ in expected
        ):
            raise ValueError("Release assets differ from verified manifest")
        with tempfile.TemporaryDirectory(prefix="release-verify-") as folder:
            directory = Path(folder)
            self._gh(
                "release",
                "download",
                tag,
                "--repo",
                self.repository,
                "--dir",
                str(directory),
            )
            for name, sha, size in expected:
                path = directory / name
                if path.stat().st_size != size or sha256_file(path) != sha:
                    raise ValueError("Uploaded Release asset hash/size mismatch")

    def publish(self, tag):
        self._gh(
            "release",
            "edit",
            tag,
            "--repo",
            self.repository,
            "--draft=false",
            "--latest=false",
        )
        release = json.loads(
            self._gh("api", f"repos/{self.repository}/releases/tags/{tag}")
        )
        if not release.get("immutable"):
            raise ValueError(
                "Published release is not immutable; index was not advanced"
            )


def publish_release(
    output: Path, expected_parent: str, checkout: Path, *, transport=None
):
    output, checkout = Path(output), Path(checkout)
    manifest = verify_output(output)
    transport = transport or GitHubRelease("monkey1wizard/taiwan-lvr-geodata", checkout)
    tag = "data-" + manifest["snapshot_id"]
    if transport.head() != expected_parent:
        raise ValueError("Stale remote parent; release pointer unchanged")
    if not transport.immutable_enabled():
        raise ValueError(
            "Repository release immutability must be enabled before publication"
        )
    if manifest["producer_config"]["working_tree_source_dirty"]:
        raise ValueError("Commit producer code before public release")
    if manifest["bindings"]["code_commit"] != expected_parent:
        raise ValueError("Release parent differs from producer code commit")
    if len(manifest["assets"]) + 1 > 1000:
        raise ValueError("Release exceeds 1000 asset budget; scope partition required")
    expected = [(a["asset_name"], a["sha256"], a["bytes"]) for a in manifest["assets"]]
    expected.append(
        (
            "manifest.json",
            sha256_file(output / "manifest.json"),
            (output / "manifest.json").stat().st_size,
        )
    )
    with tempfile.TemporaryDirectory(prefix="release-notes-") as folder:
        notes = Path(folder) / "notes.md"
        notes.write_text(
            f"Offline source observations from {', '.join(manifest['source_batches'])}. Transaction-month downloads and byte-identical annual packages. Coordinate coverage is declared in NOTICE.json. Unlocated observations are retained. TGOS has not started.\n",
            encoding="utf-8",
        )
        transport.create(tag, expected_parent, notes)
    upload_paths = [output / item["path"] for item in manifest["assets"]] + [
        output / "manifest.json"
    ]
    if hasattr(transport, "upload_many"):
        transport.upload_many(tag, upload_paths)
    else:
        for path in upload_paths:
            transport.upload(tag, path)
    transport.verify_assets(tag, expected)
    if transport.head() != expected_parent:
        raise ValueError("Stale parent after asset upload; release remains a draft")
    transport.publish(tag)
    # Publishing a complete version is safe even when a competitor advances main.
    # Advance its pointer only through an exact-parent, non-force Git push.
    if transport.head() != expected_parent:
        raise ValueError("Stale parent after publication; previous pointer retained")
    base = (
        f"https://github.com/monkey1wizard/taiwan-lvr-geodata/releases/download/{tag}"
    )
    pointer = {
        "schema_version": "1.0",
        "snapshot_id": manifest["snapshot_id"],
        "release_tag": tag,
        "expected_parent": expected_parent,
        "manifest_url": base + "/manifest.json",
        "manifest_sha256": sha256_file(output / "manifest.json"),
        "months": [
            {
                "tx_yyyymm": m["tx_yyyymm"],
                "month_coverage_status": m["month_coverage_status"],
            }
            for m in manifest["months"]
        ],
        "scope_limited": True,
        "record_grain": manifest["record_grain"],
        "tgos_started": False,
    }
    return pointer


def commit_pointer(
    pointer: dict,
    checkout: Path,
    *,
    message="docs(release): index verified offline snapshot",
):
    """Local main is authoritative; no force update, new branch, clone or PR."""
    checkout = Path(checkout)
    expected = pointer["expected_parent"]

    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(checkout), *args], text=True
        ).strip()

    if (
        git("branch", "--show-current") != "main"
        or git("rev-parse", "HEAD") != expected
    ):
        raise ValueError("Local main differs from expected parent")
    if git("status", "--porcelain"):
        raise ValueError("Commit existing changes before writing the release pointer")
    if git("ls-remote", "origin", "refs/heads/main").split()[0] != expected:
        raise ValueError("Stale remote parent")
    path = checkout / "data/releases/latest.json"
    version = checkout / "data/releases" / f"{pointer['snapshot_id']}.json"
    write_json(path, pointer)
    write_json(version, pointer)
    git(
        "add",
        "--",
        "data/releases/latest.json",
        f"data/releases/{pointer['snapshot_id']}.json",
    )
    git("commit", "-m", message)
    # A plain Git push performs an atomic non-fast-forward rejection at the server.
    git("push", "origin", "HEAD:refs/heads/main")
    return git("rev-parse", "HEAD")
