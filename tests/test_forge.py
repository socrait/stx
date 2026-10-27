from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from stx.core.registry import (
    BoxRecord,
    ForgeBoxRecord,
    ForgePackageRecord,
    ForgePushRecord,
    ForgeSourceRecord,
    SandboxRecord,
    SandboxState,
)
from stx.forge import (
    Forge,
    ForgeCredential,
    ForgeCredentialKind,
    ForgeError,
    forge_for,
    forge_for_source,
    mint_all,
)
from stx.forge.env import render_box_env
from stx.forge.registry import (
    box_forge_expired,
    box_forge_expires_soon,
    build_forge_record,
    earliest_box_expiry,
    revoke_targets,
    sandboxes_with_expired_forge_tokens,
    sandboxes_with_expiring_forge_tokens,
)


class FakeForge:
    """A minimal `Forge` double driving `mint_all`/`forge_for` tests without
    touching GitLab — exercises the protocol's contract, not its gitlab.py
    implementation."""

    host = "forge.fictional.example"
    git_user = "oauth2"

    def __init__(self, *, package=None, package_raises=None, revoked=None):
        self._package = package
        self._package_raises = package_raises
        self.revoked = revoked if revoked is not None else []

    def mint_push(self, slug: str, *, read_only: bool) -> ForgeCredential:
        scope = "read_repository" if read_only else "write_repository"
        return ForgeCredential(
            kind=ForgeCredentialKind.PUSH,
            scope_id=111,
            token_id=222,
            scope=scope,
            secret="glpat-fictional-push-secret",
            expires_at="2026-07-09",
        )

    def mint_package(self, slug: str) -> ForgeCredential | None:
        if self._package_raises is not None:
            raise self._package_raises
        return self._package

    def revoke(self, kind: ForgeCredentialKind, scope_id: int, token_id: int) -> bool:
        self.revoked.append((kind, scope_id, token_id))
        return True

    def username(self) -> str | None:
        return "fictional-user"


def _package_credential(*, expires_at: str = "2026-07-09") -> ForgeCredential:
    return ForgeCredential(
        kind=ForgeCredentialKind.PACKAGE,
        scope_id=333,
        token_id=444,
        scope="read_package_registry",
        secret="gldt-fictional-package-secret",
        username="gitlab+deploy-token-444",
        expires_at=expires_at,
    )


def test_fake_forge_satisfies_the_forge_protocol():
    forge: Forge = FakeForge()
    assert isinstance(forge.host, str)
    assert isinstance(forge.git_user, str)


class TestMintAll:
    def test_push_only_when_no_package_registry(self):
        forge = FakeForge(package=None)

        credentials = mint_all(forge, "widgetshop-fix", read_only=False)

        assert [c.kind for c in credentials] == [ForgeCredentialKind.PUSH]
        assert forge.revoked == []

    def test_push_and_package_when_configured(self):
        forge = FakeForge(package=_package_credential())

        credentials = mint_all(forge, "widgetshop-fix", read_only=False)

        assert [c.kind for c in credentials] == [
            ForgeCredentialKind.PUSH,
            ForgeCredentialKind.PACKAGE,
        ]

    def test_read_only_threaded_into_push_scope(self):
        forge = FakeForge(package=None)

        [push] = mint_all(forge, "widgetshop-fix", read_only=True)

        assert push.scope == "read_repository"

    def test_package_mint_failure_revokes_push_and_reraises(self):
        forge = FakeForge(package_raises=ForgeError("package mint failed"))

        with pytest.raises(ForgeError, match="package mint failed"):
            mint_all(forge, "widgetshop-fix", read_only=False)

        assert forge.revoked == [(ForgeCredentialKind.PUSH, 111, 222)]


class TestForgeFor:
    def test_none_without_forge_key(self):
        manifest = SimpleNamespace(forge=None)
        assert forge_for(manifest) is None

    def test_none_manifest(self):
        assert forge_for(None) is None

    def test_unsupported_kind_raises(self):
        manifest = SimpleNamespace(
            forge=SimpleNamespace(
                kind="fictionalforge", project="x", package_registry=None
            )
        )
        with pytest.raises(ForgeError, match="fictionalforge"):
            forge_for(manifest)

    def test_gitlab_kind_builds_gitlab_forge(self, monkeypatch):
        from stx.forge.gitlab import GitLabForge

        monkeypatch.setenv("STX_GITLAB_MINTER_TOKEN", "glpat-fictional-minter")
        manifest = SimpleNamespace(
            forge=SimpleNamespace(
                kind="gitlab", project="widgetshop/widgetshop", package_registry=None
            )
        )

        forge = forge_for(manifest)

        assert isinstance(forge, GitLabForge)


class TestForgeForSource:
    """Same dispatch as `forge_for`, but from a `ForgeSourceRecord` (registry
    data) rather than a manifest — the path `rm` uses for revocation."""

    def test_none_source(self):
        assert forge_for_source(None) is None

    def test_unsupported_kind_raises(self):
        source = ForgeSourceRecord(kind="fictionalforge", host="x", project="y")
        with pytest.raises(ForgeError, match="fictionalforge"):
            forge_for_source(source)

    def test_gitlab_kind_builds_gitlab_forge(self, monkeypatch):
        from stx.forge.gitlab import GitLabForge

        monkeypatch.setenv("STX_GITLAB_MINTER_TOKEN", "glpat-fictional-minter")
        source = ForgeSourceRecord(
            kind="gitlab", host="gitlab.com", project="widgetshop/widgetshop"
        )

        forge = forge_for_source(source)

        assert isinstance(forge, GitLabForge)
        assert forge.host == "gitlab.com"


class TestRegistryRecordRoundTrip:
    def test_push_only_record(self):
        push = ForgeCredential(
            kind=ForgeCredentialKind.PUSH,
            scope_id=111,
            token_id=222,
            scope="write_repository",
            secret="glpat-fictional-push-secret",
            expires_at="2026-07-09",
        )

        record = build_forge_record("gitlab", [push])

        assert record == ForgeBoxRecord(
            kind="gitlab",
            push=ForgePushRecord(
                scope_id=111,
                token_id=222,
                scope="write_repository",
                expires_at="2026-07-09",
            ),
        )
        assert revoke_targets(record) == [(ForgeCredentialKind.PUSH, 111, 222)]

    def test_push_and_package_record(self):
        push = ForgeCredential(
            kind=ForgeCredentialKind.PUSH,
            scope_id=111,
            token_id=222,
            scope="write_repository",
            secret="glpat-fictional-push-secret",
            expires_at="2026-07-09",
        )
        package = _package_credential()

        record = build_forge_record("gitlab", [push, package])

        assert record == ForgeBoxRecord(
            kind="gitlab",
            push=ForgePushRecord(
                scope_id=111,
                token_id=222,
                scope="write_repository",
                expires_at="2026-07-09",
            ),
            package=ForgePackageRecord(
                scope_id=333, token_id=444, expires_at="2026-07-09"
            ),
        )
        assert revoke_targets(record) == [
            (ForgeCredentialKind.PUSH, 111, 222),
            (ForgeCredentialKind.PACKAGE, 333, 444),
        ]

    def test_revoke_targets_of_push_only_record_omits_package(self):
        record = ForgeBoxRecord(
            kind="gitlab",
            push=ForgePushRecord(
                scope_id=1, token_id=2, scope="x", expires_at="2026-07-09"
            ),
        )
        assert revoke_targets(record) == [(ForgeCredentialKind.PUSH, 1, 2)]

    def test_revoke_targets_of_empty_record_is_empty(self):
        assert revoke_targets(ForgeBoxRecord(kind="gitlab")) == []

    def test_expires_at_carries_from_the_credential_to_the_record(self):
        push = ForgeCredential(
            kind=ForgeCredentialKind.PUSH,
            scope_id=111,
            token_id=222,
            scope="write_repository",
            secret="glpat-fictional-push-secret",
            expires_at="2026-07-09",
        )
        package = _package_credential(expires_at="2026-07-14")

        record = build_forge_record("gitlab", [push, package])

        assert record.push.expires_at == "2026-07-09"
        assert record.package.expires_at == "2026-07-14"


class TestExpiringForgeTokens:
    def _box(
        self, *, push_expires_at: str = "", package_expires_at: str = ""
    ) -> BoxRecord:
        return BoxRecord(
            zone="trusted",
            user="devuser",
            forge=ForgeBoxRecord(
                kind="gitlab",
                push=ForgePushRecord(
                    scope_id=1,
                    token_id=2,
                    scope="write_repository",
                    expires_at=push_expires_at,
                )
                if push_expires_at
                else None,
                package=ForgePackageRecord(
                    scope_id=3, token_id=4, expires_at=package_expires_at
                )
                if package_expires_at
                else None,
            ),
        )

    def test_box_with_no_forge_record_never_expires_soon(self):
        box = BoxRecord(zone="trusted", user="devuser")
        assert box_forge_expires_soon(box) is False

    def test_push_expiring_today_counts_as_soon(self):
        today = date(2026, 7, 10)
        box = self._box(push_expires_at="2026-07-10")
        assert box_forge_expires_soon(box, now=today) is True

    def test_push_expiring_tomorrow_counts_as_soon(self):
        today = date(2026, 7, 10)
        box = self._box(push_expires_at="2026-07-11")
        assert box_forge_expires_soon(box, now=today) is True

    def test_push_expiring_in_three_days_is_not_soon(self):
        today = date(2026, 7, 10)
        box = self._box(push_expires_at="2026-07-13")
        assert box_forge_expires_soon(box, now=today) is False

    def test_package_expiring_soon_counts_even_when_push_does_not(self):
        today = date(2026, 7, 10)
        box = self._box(push_expires_at="2026-07-20", package_expires_at="2026-07-10")
        assert box_forge_expires_soon(box, now=today) is True

    def test_custom_window_widens_what_counts_as_soon(self):
        today = date(2026, 7, 10)
        box = self._box(push_expires_at="2026-07-13")
        assert box_forge_expires_soon(box, within=timedelta(days=5), now=today) is True

    def test_sandboxes_with_expiring_forge_tokens_names_only_the_affected_slugs(self):
        today = date(2026, 7, 10)
        soon = SandboxRecord(
            slug="soon",
            created="2026-07-01T00:00:00+00:00",
            state=SandboxState.READY,
            redis_index=None,
            tailnet_port=None,
            boxes={"app": self._box(push_expires_at="2026-07-10")},
        )
        fine = SandboxRecord(
            slug="fine",
            created="2026-07-01T00:00:00+00:00",
            state=SandboxState.READY,
            redis_index=None,
            tailnet_port=None,
            boxes={"app": self._box(push_expires_at="2026-08-01")},
        )

        slugs = sandboxes_with_expiring_forge_tokens(
            {"soon": soon, "fine": fine}, within=timedelta(hours=24), now=today
        )

        assert slugs == ["soon"]


class TestEarliestBoxExpiry:
    def test_no_forge_record_is_none(self):
        box = BoxRecord(zone="trusted", user="devuser")
        assert earliest_box_expiry(box) is None

    def test_none_box_is_none(self):
        assert earliest_box_expiry(None) is None

    def test_push_only(self):
        box = BoxRecord(
            zone="trusted",
            user="devuser",
            forge=ForgeBoxRecord(
                kind="gitlab",
                push=ForgePushRecord(
                    scope_id=1,
                    token_id=2,
                    scope="write_repository",
                    expires_at="2026-07-12",
                ),
            ),
        )
        assert earliest_box_expiry(box) == date(2026, 7, 12)

    def test_earliest_of_push_and_package(self):
        box = BoxRecord(
            zone="trusted",
            user="devuser",
            forge=ForgeBoxRecord(
                kind="gitlab",
                push=ForgePushRecord(
                    scope_id=1,
                    token_id=2,
                    scope="write_repository",
                    expires_at="2026-07-20",
                ),
                package=ForgePackageRecord(
                    scope_id=3, token_id=4, expires_at="2026-07-12"
                ),
            ),
        )
        assert earliest_box_expiry(box) == date(2026, 7, 12)


class TestExpiredForgeTokens:
    def _box(self, *, push_expires_at: str) -> BoxRecord:
        return BoxRecord(
            zone="trusted",
            user="devuser",
            forge=ForgeBoxRecord(
                kind="gitlab",
                push=ForgePushRecord(
                    scope_id=1,
                    token_id=2,
                    scope="write_repository",
                    expires_at=push_expires_at,
                ),
            ),
        )

    def test_box_with_no_forge_record_never_expired(self):
        box = BoxRecord(zone="trusted", user="devuser")
        assert box_forge_expired(box) is False

    def test_past_date_is_expired(self):
        today = date(2026, 7, 10)
        box = self._box(push_expires_at="2026-07-09")
        assert box_forge_expired(box, now=today) is True

    def test_today_is_not_expired(self):
        today = date(2026, 7, 10)
        box = self._box(push_expires_at="2026-07-10")
        assert box_forge_expired(box, now=today) is False

    def test_future_date_is_not_expired(self):
        today = date(2026, 7, 10)
        box = self._box(push_expires_at="2026-07-11")
        assert box_forge_expired(box, now=today) is False

    def test_sandboxes_with_expired_forge_tokens_names_only_the_affected_slugs(self):
        today = date(2026, 7, 10)
        expired = SandboxRecord(
            slug="expired",
            created="2026-07-01T00:00:00+00:00",
            state=SandboxState.READY,
            redis_index=None,
            tailnet_port=None,
            boxes={"app": self._box(push_expires_at="2026-07-09")},
        )
        fine = SandboxRecord(
            slug="fine",
            created="2026-07-01T00:00:00+00:00",
            state=SandboxState.READY,
            redis_index=None,
            tailnet_port=None,
            boxes={"app": self._box(push_expires_at="2026-08-01")},
        )

        slugs = sandboxes_with_expired_forge_tokens(
            {"expired": expired, "fine": fine}, now=today
        )

        assert slugs == ["expired"]


class TestRenderBoxEnv:
    def _manifest(self, *, username_var=None, password_var=None):
        package_registry = None
        if username_var is not None:
            package_registry = SimpleNamespace(
                group="widgetshop",
                env={"username": username_var, "password": password_var},
            )
        return SimpleNamespace(
            forge=SimpleNamespace(
                kind="gitlab",
                project="widgetshop/widgetshop",
                package_registry=package_registry,
            )
        )

    def test_push_only(self):
        forge = FakeForge()
        push = ForgeCredential(
            kind=ForgeCredentialKind.PUSH,
            scope_id=111,
            token_id=222,
            scope="write_repository",
            secret="glpat-fictional-push-secret",
            expires_at="2026-07-09",
        )

        env = render_box_env(forge, [push], self._manifest())

        assert env == {
            "STX_FORGE_TOKEN": "glpat-fictional-push-secret",
            "STX_FORGE_HOST": "forge.fictional.example",
            "STX_FORGE_GIT_USER": "oauth2",
        }

    def test_push_and_package(self):
        forge = FakeForge()
        push = ForgeCredential(
            kind=ForgeCredentialKind.PUSH,
            scope_id=111,
            token_id=222,
            scope="write_repository",
            secret="glpat-fictional-push-secret",
            expires_at="2026-07-09",
        )
        package = _package_credential()
        manifest = self._manifest(
            username_var="UV_INDEX_FICTIONAL_USERNAME",
            password_var="UV_INDEX_FICTIONAL_PASSWORD",
        )

        env = render_box_env(forge, [push, package], manifest)

        assert env["UV_INDEX_FICTIONAL_USERNAME"] == "gitlab+deploy-token-444"
        assert env["UV_INDEX_FICTIONAL_PASSWORD"] == "gldt-fictional-package-secret"

    def test_no_credentials_yields_empty_env(self):
        assert render_box_env(FakeForge(), [], self._manifest()) == {}

    def test_package_credential_without_package_registry_manifest_is_ignored(self):
        forge = FakeForge()
        package = _package_credential()

        env = render_box_env(forge, [package], self._manifest())

        assert env == {}
