"""Spec: the updater must actually pick up new code.

The live-host failure that motivated this was an update that reported success but
left the tree unchanged: the old updater only ever *overlaid* a tarball, so it could
never delete stale files, could not detect "nothing new", and said nothing when the
download was empty. These tests pin the git-aware path.
"""

from __future__ import annotations

import argparse

import pytest

from penny import cli


def _args(**over):
    base = dict(version=None, install_dir=None, private=False, no_backup=True)
    base.update(over)
    return argparse.Namespace(**base)


class _FakeGit:
    """Records git calls and returns scripted output keyed by the argv tuple."""

    def __init__(self, responses, heads):
        self.responses = responses
        self.heads = list(heads)
        self.calls = []

    def __call__(self, install_dir, *argv, timeout=180):
        self.calls.append(argv)
        key = " ".join(argv)
        for pat, result in self.responses.items():
            if key.startswith(pat):
                return result
        if key.startswith("rev-parse --short HEAD"):
            head = self.heads.pop(0) if self.heads else "0000000"
            return 0, head
        return 0, ""


def _ok_selftest(monkeypatch):
    class _Report:
        ok = True
        def text(self):
            return "SELF-TEST OK"
    monkeypatch.setattr("penny.selftest.run_selftest", lambda cfg: _Report())


def test_is_git_checkout_requires_dot_git(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_git_available", lambda: True)
    assert not cli._is_git_checkout(tmp_path)
    (tmp_path / ".git").mkdir()
    assert cli._is_git_checkout(tmp_path)


def test_is_git_checkout_false_without_git_binary(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(cli, "_git_available", lambda: False)
    assert not cli._is_git_checkout(tmp_path)


def test_update_git_reports_up_to_date_and_skips_reinstall(tmp_path, cfg, monkeypatch):
    (tmp_path / ".git").mkdir()
    head = "aaaaaaaa"
    fake = _FakeGit(
        {"fetch --prune origin main": (0, ""),
         "rev-parse --short origin/main": (0, head),
         "rev-parse --short HEAD": (0, head)},
        heads=[],
    )
    monkeypatch.setattr(cli, "_git", fake)
    called = {"reset": False}
    # drive _update_git directly so we can assert on the reset decision
    monkeypatch.setattr(cli, "_update_archive",
                        lambda *a, **k: pytest.fail("archive path must not run for a checkout"))

    # reset must NOT be issued when we are already at origin/main
    def _guard(install_dir, *argv, timeout=180):
        if argv[:1] == ("reset",):
            called["reset"] = True
        return fake(install_dir, *argv, timeout=timeout)
    monkeypatch.setattr(cli, "_git", _guard)
    _ok_selftest(monkeypatch)

    rc = cli._update_git(cfg, _args(), tmp_path, "main")
    assert rc == 0
    assert called["reset"] is False
    assert cfg.raw("PENNY_VERSION") == "main"


def test_update_git_advances_a_stale_checkout(tmp_path, cfg, monkeypatch):
    (tmp_path / ".git").mkdir()
    fake = _FakeGit(
        {"fetch --prune origin main": (0, ""),
         "rev-parse --short origin/main": (0, "bbbbbbb"),
         "rev-parse --short HEAD": (0, "aaaaaaa"),
         "reset --hard origin/main": (0, "HEAD is now at bbbbbbb")},
        heads=["aaaaaaa", "bbbbbbb"],
    )
    monkeypatch.setattr(cli, "_git", fake)
    _ok_selftest(monkeypatch)
    rc = cli._update_git(cfg, _args(), tmp_path, "main")
    assert rc == 0
    assert any(c and c[0] == "reset" for c in fake.calls)


def test_update_git_fails_loudly_when_fetch_fails(tmp_path, cfg, monkeypatch):
    (tmp_path / ".git").mkdir()
    fake = _FakeGit({"fetch --prune origin main": (128, "fatal: could not resolve host")}, heads=[])
    monkeypatch.setattr(cli, "_git", fake)
    monkeypatch.setattr(cli, "_update_archive",
                        lambda *a, **k: pytest.fail("must not fall back silently to the archive"))
    rc = cli._update_git(cfg, _args(), tmp_path, "main")
    assert rc == 1
    assert not any(c and c[0] == "reset" for c in fake.calls)


def test_update_archive_aborts_on_empty_download(tmp_path, cfg, monkeypatch):
    class _Resp:
        def read(self):
            return b""
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())
    cfg.set("PENNY_URL", "https://example.invalid/repo")
    rc = cli._update_archive(cfg, _args(install_dir=str(tmp_path)), tmp_path, "main")
    assert rc == 1  # never touches the install directory


def test_cmd_update_dispatches_to_git_when_checkout(tmp_path, cfg, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(cli, "_is_git_checkout", lambda d: True)
    seen = {}
    monkeypatch.setattr(cli, "_update_git",
                        lambda c, a, d, v: seen.update(version=v) or 0)
    monkeypatch.setattr(cli, "_update_archive",
                        lambda *a, **k: pytest.fail("must use git for a checkout"))
    args = _args(install_dir=str(tmp_path))
    # _config reads the config file at args.config; point it at the fixture's file
    # so cfg resolution is exercised end to end.
    args.config = str(cfg.path)
    rc = cli.cmd_update(args)
    assert rc == 0
    assert seen["version"] == "main"
