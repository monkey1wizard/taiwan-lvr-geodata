import json
from pathlib import Path
import shutil
from urllib.parse import unquote

import pytest

from lvr_pipeline.distribution import (
    fetch_output,
    publish_release,
    GitHubRelease,
    commit_pointer,
)
from lvr_pipeline.packaging import package_output
from lvr_pipeline.sources import sha256_file
from test_p2_offline import run


@pytest.fixture
def output(tmp_path):
    converted, *_, state = run(tmp_path)
    return package_output(
        converted,
        state,
        tmp_path / "output",
        notices={
            "publication_authorized": True,
            "legacy_coordinates_authorized": True,
            "sources": ["synthetic"],
        },
    )


def local_transport(output):
    files = {p.name: p for p in output.rglob("*") if p.is_file()}
    calls = []

    def retrieve(url, path):
        name = unquote(url.rsplit("/", 1)[1])
        calls.append(name)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(files[name], path)

    return retrieve, calls


def test_fetch_single_month_needs_no_raw_or_annual_package(output, tmp_path):
    transport, calls = local_transport(output)
    fetched = fetch_output(
        "https://example.invalid/manifest.json",
        sha256_file(output / "manifest.json"),
        tmp_path / "fresh",
        month=202601,
        category="sales",
        format="ndjson",
        transport=transport,
    )
    assert calls == ["manifest.json", "202601_sales.ndjson"]
    assert len(list(fetched.rglob("*.ndjson"))) == 1 and not list(
        fetched.rglob("*.zip")
    )


def test_fresh_verified_maintenance_download(output, tmp_path):
    transport, calls = local_transport(output)
    fetched = fetch_output(
        "https://example.invalid/manifest.json",
        sha256_file(output / "manifest.json"),
        tmp_path / "fresh",
        maintenance=True,
        transport=transport,
    )
    assert len(calls) == 2 and (fetched / "maintenance/handoff.json").exists()
    assert not list(fetched.rglob("*lvr_landcsv.zip"))


def test_manifest_wrong_hash_leaves_no_completed_target(output, tmp_path):
    transport, calls = local_transport(output)
    with pytest.raises(ValueError, match="manifest hash"):
        fetch_output(
            "https://example.invalid/manifest.json",
            "0" * 64,
            tmp_path / "fresh",
            transport=transport,
        )
    assert not (tmp_path / "fresh").exists() and calls == ["manifest.json"]


class FakeRelease:
    def __init__(self, parent, fail=None):
        self.parent = parent
        self.fail = fail
        self.calls = []

    def head(self):
        return self.parent

    def immutable_enabled(self):
        return True

    def create(self, *args):
        self.calls.append("create")
        if self.fail == "permission":
            raise PermissionError("No publication credentials")

    def upload(self, *args):
        self.calls.append("upload")
        if self.fail == "upload":
            raise OSError("interrupted upload")

    def verify_assets(self, *args):
        self.calls.append("verify")
        if self.fail == "hash":
            raise ValueError("Uploaded Release asset hash/size mismatch")
        if self.fail == "stale":
            self.parent = "0" * 40

    def publish(self, *args):
        self.calls.append("publish")


def release_candidate(output):
    path = output / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["producer_config"]["working_tree_source_dirty"] = False
    from lvr_pipeline.storage.runs import digest

    manifest["bindings"]["config_sha256"] = digest(manifest["producer_config"])
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest["bindings"]["code_commit"]


@pytest.mark.parametrize("failure", ["upload", "hash", "stale", "permission"])
def test_failed_release_never_publishes_or_updates_pointer(output, tmp_path, failure):
    parent = release_candidate(output)
    transport = FakeRelease(parent, failure)
    with pytest.raises((ValueError, OSError)):
        publish_release(output, parent, tmp_path, transport=transport)
    assert (
        "publish" not in transport.calls
        and not (tmp_path / "releases/latest.json").exists()
    )


def test_stale_writer_rejected_before_upload(output, tmp_path):
    parent = release_candidate(output)
    transport = FakeRelease("0" * 40)
    with pytest.raises(ValueError, match="Stale"):
        publish_release(output, parent, tmp_path, transport=transport)
    assert not transport.calls


def test_verified_release_returns_pinned_pointer_only_after_publish(output, tmp_path):
    parent = release_candidate(output)
    transport = FakeRelease(parent)
    pointer = publish_release(output, parent, tmp_path, transport=transport)
    assert transport.calls[-2:] == ["verify", "publish"]
    assert pointer["manifest_sha256"] == sha256_file(output / "manifest.json")
    assert pointer["expected_parent"] == parent and pointer["tgos_started"] is False


def test_resume_same_draft_skips_only_identical_assets(tmp_path, monkeypatch):
    old = tmp_path / "old.ndjson"
    old.write_bytes(b"\n")
    new = tmp_path / "new.ndjson"
    new.write_bytes(b"{}\n")
    transport = GitHubRelease("monkey1wizard/taiwan-lvr-geodata", tmp_path)
    release = {
        "tag_name": "data-test",
        "draft": True,
        "target_commitish": "a" * 40,
        "assets": [
            {"name": old.name, "size": 1, "digest": "sha256:" + sha256_file(old)}
        ],
    }
    calls = []

    def gh(*args):
        calls.append(args)
        if "--slurp" in args:
            return json.dumps([[release]])
        if args[:2] == ("release", "view"):
            return json.dumps(
                {
                    "apiUrl": "https://api.github.com/repos/monkey1wizard/taiwan-lvr-georeleases/1"
                }
            )
        if args[0] == "api":
            return json.dumps(release)
        return ""

    monkeypatch.setattr(transport, "_gh", gh)
    transport.create("data-test", "a" * 40, tmp_path / "notes")
    transport.upload_many("data-test", [old, new])
    uploads = [a for a in calls if a[:2] == ("release", "upload")]
    assert len(uploads) == 1 and str(new) in uploads[0] and str(old) not in uploads[0]
    release["assets"][0]["digest"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="hash differs"):
        transport.upload_many("data-test", [old, new])


def test_pointer_interleaved_local_commit_cannot_advance_remote(tmp_path, monkeypatch):
    import subprocess

    parent = "a" * 40
    other = "b" * 40
    new = "c" * 40
    calls = []
    committed = False
    pointer = {"snapshot_id": "synthetic", "expected_parent": parent}

    def command(args, **kwargs):
        nonlocal committed
        git_args = args[3:]
        calls.append(git_args)
        if git_args == ["branch", "--show-current"]:
            return "main\n"
        if git_args == ["rev-parse", "HEAD"]:
            return (new if committed else parent) + "\n"
        if git_args == ["status", "--porcelain"]:
            return ""
        if git_args[:1] == ["ls-remote"]:
            return parent + "\trefs/heads/main\n"
        if git_args[:1] == ["commit"]:
            committed = True
            return ""
        if git_args == ["rev-parse", new + "^"]:
            return other + "\n"
        return ""

    class Verified:
        def verify_receipt(self, pointer):
            pass

    monkeypatch.setattr(subprocess, "check_output", command)
    with pytest.raises(ValueError, match="Stale local parent"):
        commit_pointer(pointer, tmp_path, transport=Verified())
    assert not any(c[0] == "push" for c in calls)


def test_unrelated_producer_cannot_be_published_on_expected_main(output, tmp_path):
    release_candidate(output)
    parent = "f" * 40
    transport = FakeRelease(parent)
    with pytest.raises(ValueError, match="not an ancestor"):
        publish_release(output, parent, tmp_path, transport=transport)
    assert not transport.calls
