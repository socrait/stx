import pytest

from stx.core.box_env import reserved_env_names


@pytest.mark.parametrize(
    "name",
    [
        "STX_EGRESS_PORTS",
        "STX_ZONE",
        "GIT_DIR",
        "HTTP_PROXY",
        "https_proxy",
        "NO_PROXY",
        "ALL_PROXY",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "LD_AUDIT",
        "ld_preload",
        "PATH",
        "path",
        "BASH_ENV",
        "ENV",
        "PROMPT_COMMAND",
        "PYTHONSTARTUP",
        "PERL5OPT",
        "NODE_OPTIONS",
        "RUBYOPT",
        # A bash-exported function definition: `BASH_FUNC_iptables%%=() { ...; }`
        # makes the root entrypoint's bash define `iptables` as a function.
        "BASH_FUNC_iptables%%",
        "BASH_FUNC_foo",
        "bash_func_foo",
        # Any `%` at all — the `%%` suffix is bash's own, but a lone `%` is
        # never a legitimate env name a repo needs and is refused outright.
        "FOO%BAR",
        "weird%",
    ],
)
def test_reserved_names_refused(name):
    assert reserved_env_names([name]) == [name]


@pytest.mark.parametrize("name", ["DATABASE_URL", "APP_ENV", "HOME"])
def test_non_reserved_names_allowed(name):
    assert reserved_env_names([name]) == []


def test_results_are_sorted_regardless_of_input_order():
    assert reserved_env_names(["GIT_DIR", "BASH_ENV"]) == ["BASH_ENV", "GIT_DIR"]
