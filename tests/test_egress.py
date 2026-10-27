"""Egress rendering: a manifest's `egress.hosts` becomes a tinyproxy allowlist
file."""

from __future__ import annotations

from pathlib import Path

import pytest

from stx import egress
from stx.manifest import EgressManifest, Manifest

_WIDGETSHOP = Path(__file__).parent.parent / "examples" / "widgetshop"


def _manifest(**egress_kwargs) -> Manifest:
    return Manifest(
        schema=1,
        name="app",
        compose="compose.yaml",
        image="x",
        egress=EgressManifest(**egress_kwargs),
    )


class TestRender:
    def test_plain_hostname_becomes_escaped_exact_match(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        path = egress.render(_manifest(), "demo")
        lines = path.read_text().splitlines()
        assert lines == [egress._filter_pattern(h) for h in egress.HARNESS_HOSTS]

    def test_manifest_hostname_becomes_escaped_exact_match(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        path = egress.render(_manifest(hosts=("pypi.org",)), "demo")
        assert "^pypi\\.org$" in path.read_text().splitlines()

    def test_harness_hosts_always_added(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        path = egress.render(_manifest(hosts=("pypi.org",)), "demo")
        lines = path.read_text().splitlines()
        for host in egress.HARNESS_HOSTS:
            assert egress._filter_pattern(host) in lines

    def test_forge_host_added_when_given(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        path = egress.render(_manifest(), "demo", forge_host="gitlab.com")
        assert "^gitlab\\.com$" in path.read_text().splitlines()

    def test_no_forge_host_omits_it(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        path = egress.render(_manifest(), "demo")
        assert "^gitlab\\.com$" not in path.read_text().splitlines()

    def test_duplicate_host_rendered_once(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        path = egress.render(
            _manifest(hosts=(egress.HARNESS_HOSTS[0],)),
            "demo",
            forge_host=egress.HARNESS_HOSTS[0],
        )
        lines = path.read_text().splitlines()
        assert lines.count(egress._filter_pattern(egress.HARNESS_HOSTS[0])) == 1

    def test_regex_entry_used_verbatim(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        pattern = "~^foo-[a-z0-9]+\\.example\\.com$"
        path = egress.render(_manifest(hosts=(pattern,)), "demo")
        assert "^foo-[a-z0-9]+\\.example\\.com$" in path.read_text().splitlines()

    def test_multiple_hosts_one_per_line(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        path = egress.render(_manifest(hosts=("pypi.org", "gitlab.com")), "demo")
        lines = path.read_text().splitlines()
        assert lines[0] == "^pypi\\.org$"
        assert lines[1] == "^gitlab\\.com$"

    def test_empty_manifest_hosts_still_renders_harness_hosts(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        path = egress.render(_manifest(), "demo")
        assert path.read_text() != ""

    def test_path_matches_allowlist_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        from stx.core.paths import allowlist_path

        path = egress.render(_manifest(hosts=("pypi.org",)), "demo")
        assert path == allowlist_path("demo")

    def test_widgetshop_manifest_renders_without_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        manifest = Manifest.load(_WIDGETSHOP)
        path = egress.render(manifest, "demo", forge_host="gitlab.com")
        lines = path.read_text().splitlines()
        assert "^pypi\\.org$" in lines
        assert "^gitlab\\.com$" in lines

    def test_write_is_atomic_no_tmp_file_left_behind(self, tmp_path, monkeypatch):
        """A tinyproxy reload can race a re-render — the write must go
        through a temp file + rename, never leaving a stray temp sibling
        (or a torn file at the real path) behind."""
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        path = egress.render(_manifest(hosts=("pypi.org",)), "demo")
        assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize(
    "entry,expected",
    [
        ("pypi.org", "^pypi\\.org$"),
        ("api.anthropic.com", "^api\\.anthropic\\.com$"),
        ("~^foo$", "^foo$"),
    ],
)
def test_filter_pattern_matches_manifest_validations_own_pattern(entry, expected):
    assert egress._filter_pattern(entry) == expected
