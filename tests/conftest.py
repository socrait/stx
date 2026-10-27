import os

import pytest


@pytest.fixture(autouse=True, scope="package")
def _isolate_git_env(tmp_path_factory):
    """A sandbox box's compose.yaml sets GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE/
    GIT_OBJECT_DIRECTORY (and GIT_CONFIG_COUNT/GIT_CONFIG_KEY_N/
    GIT_CONFIG_VALUE_N) to pin every git invocation onto the platform's own
    shared repo. Every fixture here builds a throwaway git repo under
    `tmp_path` and assumes a plain `cwd=` scopes every git call to it, so
    these overrides must be removed from the environment outright before any
    such fixture runs — letting `stx`'s own git-fixture tests run anywhere,
    including inside a sandbox box where `stx` itself is developed, rather
    than skipping the whole package there. `git config user.name`/`email`
    resolve relative to GIT_DIR, not a helper's own `cwd=` — a leaked GIT_DIR
    silently redirects that write into whatever repo it names (see
    tests/test_git_env_isolation.py); deleting it here is the fix, same as
    for GIT_WORK_TREE.

    GIT_CONFIG_GLOBAL/GIT_CONFIG_NOSYSTEM isolate `--global`/system config
    reads and writes the same way, and GIT_AUTHOR_*/GIT_COMMITTER_* give
    every commit a fixed identity — together these mean no fixture here
    needs its own `git config user.*` call at all, removing that write
    (local or global) as an attack surface outright rather than relying on
    every call site remembering to scope it.

    A box also sets STX_ZONE, which Command.execute()'s host-only guard
    reads via in_sandbox_box() — every test here exercises a command's own
    behavior assuming host execution, so that ambient var must be cleared
    the same way; a test for the guard itself sets it back locally.
    """
    git_config_global = tmp_path_factory.mktemp("git-config") / "gitconfig"
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("GIT_DIR", raising=False)
        mp.delenv("GIT_WORK_TREE", raising=False)
        mp.delenv("GIT_INDEX_FILE", raising=False)
        mp.delenv("GIT_OBJECT_DIRECTORY", raising=False)
        mp.delenv("GIT_CONFIG_COUNT", raising=False)
        mp.delenv("STX_ZONE", raising=False)
        for key in list(os.environ):
            if key.startswith("GIT_CONFIG_KEY_") or key.startswith("GIT_CONFIG_VALUE_"):
                mp.delenv(key, raising=False)
        mp.setenv("GIT_CONFIG_GLOBAL", str(git_config_global))
        mp.setenv("GIT_CONFIG_NOSYSTEM", "1")
        mp.setenv("GIT_AUTHOR_NAME", "stx-test")
        mp.setenv("GIT_AUTHOR_EMAIL", "stx-test@example.invalid")
        mp.setenv("GIT_COMMITTER_NAME", "stx-test")
        mp.setenv("GIT_COMMITTER_EMAIL", "stx-test@example.invalid")
        yield


@pytest.fixture(autouse=True)
def _isolate_user_config(tmp_path_factory, monkeypatch):
    # The operator's real config may name a secret source (1Password) that
    # prompts, or a dotenv file with real secrets; no test may read either.
    config_home = tmp_path_factory.mktemp("xdg-config")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("STX_CONFIG_PATH", str(config_home / "stx" / "config.toml"))


@pytest.fixture(autouse=True)
def _every_host_port_free(monkeypatch):
    # Which host ports are busy depends on the machine running the tests; a
    # test that cares about a busy port injects its own probe.
    from stx.commands import new, refresh

    monkeypatch.setattr(new, "port_bindable", lambda *a, **kw: True)
    monkeypatch.setattr(refresh, "port_bindable", lambda *a, **kw: True)
