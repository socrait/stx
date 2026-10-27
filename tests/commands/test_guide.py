import argparse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from stx.commands.guide import SandboxGuideCommand
from stx.core.registry import (
    BoxRecord,
    ForgeBoxRecord,
    ForgePushRecord,
    SandboxRecord,
    SandboxState,
)


def _utc_today() -> date:
    # Expiry dates are computed in UTC; a local-date comparison flips near midnight.
    return datetime.now(timezone.utc).date()


_SKILL_FILE = Path(__file__).parent.parent.parent / "skills" / "stx" / "SKILL.md"
_MOD = "stx.commands.guide"


def _args(**kwargs) -> argparse.Namespace:
    defaults = {
        "raw": False,
        "force": False,
        "yes": False,
        "verbose": False,
        "dry_run": False,
        "debug": False,
    }
    return argparse.Namespace(**{**defaults, **kwargs})


def test_raw_output_equals_the_skill_file_verbatim(capsys):
    with patch(f"{_MOD}.load_registry", return_value={}):
        result = SandboxGuideCommand().execute(_args(raw=True))

    assert result is None
    assert capsys.readouterr().out == _SKILL_FILE.read_text()


class TestBody:
    def test_strips_the_frontmatter_block(self, capsys):
        with patch(f"{_MOD}.load_registry", return_value={}):
            SandboxGuideCommand().execute(_args())

        out = capsys.readouterr().out
        assert not out.startswith("---\n")
        assert "name: stx" not in out
        assert "# Driving stx" in out

    def test_body_is_a_strict_suffix_of_the_raw_file(self, capsys):
        with patch(f"{_MOD}.load_registry", return_value={}):
            SandboxGuideCommand().execute(_args())
            body = capsys.readouterr().out

            SandboxGuideCommand().execute(_args(raw=True))
            raw = capsys.readouterr().out

        assert raw.endswith(body)
        assert len(raw) > len(body)


class TestExpiringTokenWarning:
    def _record_with_expiry(self, expires_at: str) -> SandboxRecord:
        return SandboxRecord(
            slug="foo",
            created="2026-07-07T00:00:00+00:00",
            state=SandboxState.READY,
            redis_index=None,
            tailnet_port=None,
            boxes={
                "app": BoxRecord(
                    zone="trusted",
                    user="devuser",
                    forge=ForgeBoxRecord(
                        kind="gitlab",
                        push=ForgePushRecord(
                            scope_id=1,
                            token_id=2,
                            scope="write_repository",
                            expires_at=expires_at,
                        ),
                    ),
                )
            },
        )

    def test_warns_on_stderr_when_a_token_expires_soon(self, capsys):
        record = self._record_with_expiry(_utc_today().isoformat())
        with patch(f"{_MOD}.load_registry", return_value={"foo": record}):
            SandboxGuideCommand().execute(_args(raw=True))

        err = capsys.readouterr().err
        assert "'foo'" in err
        assert "stx refresh foo" in err

    def test_no_warning_when_nothing_expires_soon(self, capsys):
        far = (_utc_today() + timedelta(days=30)).isoformat()
        record = self._record_with_expiry(far)
        with patch(f"{_MOD}.load_registry", return_value={"foo": record}):
            SandboxGuideCommand().execute(_args(raw=True))

        assert capsys.readouterr().err == ""
