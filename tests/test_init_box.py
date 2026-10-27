"""init-box.sh's rule-building functions: pure (no iptables/ip6tables calls
of their own), sourceable without root or NET_ADMIN — this is the dry-run
path these tests exercise. The iptables/ip6tables application itself needs a
real container and is not covered here."""

from __future__ import annotations

import ipaddress
import json
import os
import subprocess
from pathlib import Path

_INIT_BOX = Path(__file__).parent.parent / "src" / "stx" / "box" / "init-box.sh"


def _run(
    script: str, *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f'source "{_INIT_BOX}"\n{script}'],
        capture_output=True,
        text=True,
        env=env,
    )


def _resolv_conf(tmp_path: Path, *nameservers: str) -> Path:
    path = tmp_path / "resolv.conf"
    path.write_text("".join(f"nameserver {ns}\n" for ns in nameservers))
    return path


def test_sourcing_the_script_alone_has_no_side_effect():
    result = _run("true")
    assert result.returncode == 0
    assert result.stderr == ""


def test_home_dir_for_resolves_via_getent_not_a_shell(tmp_path):
    # No `eval echo ~user` — that evaluates whatever the home-dir lookup
    # forwards straight back through the shell. getent's own output
    # flows through `cut`, never eval. A fake `getent` on PATH tests
    # that parsing on any host OS — the real one only runs inside a
    # Linux box, and macOS hosts have none at all.
    fake_getent = tmp_path / "getent"
    fake_getent.write_text(
        '#!/bin/sh\necho "testuser:x:1000:1000:Test User:/home/testuser:/bin/bash"\n'
    )
    fake_getent.chmod(0o755)

    result = _run(
        '_home_dir_for "testuser"',
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "/home/testuser"


class TestIpv6KernelAvailable:
    def test_present_path_is_available(self, tmp_path):
        probe = tmp_path / "if_inet6"
        probe.write_text("")
        result = _run(f'_ipv6_kernel_available "{probe}"')
        assert result.returncode == 0

    def test_missing_path_is_unavailable(self, tmp_path):
        probe = tmp_path / "does-not-exist"
        result = _run(f'_ipv6_kernel_available "{probe}"')
        assert result.returncode == 1


class TestApplyIpv6Drop:
    def test_skips_when_kernel_has_no_ipv6(self):
        result = _run(
            "_ipv6_kernel_available() { return 1; }\n"
            "ip6tables() { echo called-ip6tables >&2; return 0; }\n"
            "apply_ipv6_drop"
        )
        assert result.returncode == 0
        assert "called-ip6tables" not in result.stderr

    def test_fails_closed_when_ip6tables_fails(self):
        result = _run(
            "_ipv6_kernel_available() { return 0; }\n"
            "ip6tables() { return 1; }\n"
            "apply_ipv6_drop"
        )
        assert result.returncode == 1
        assert "IPv6" in result.stderr

    def test_succeeds_when_ip6tables_succeeds(self):
        result = _run(
            "_ipv6_kernel_available() { return 0; }\n"
            "ip6tables() { return 0; }\n"
            "apply_ipv6_drop"
        )
        assert result.returncode == 0


class TestDnsResolverIps:
    def test_parses_nameserver_lines(self, tmp_path):
        resolv = _resolv_conf(tmp_path, "127.0.0.11", "192.168.65.7")
        result = _run(f'_dns_resolver_ips "{resolv}"')
        assert result.returncode == 0
        assert sorted(result.stdout.split()) == ["127.0.0.11", "192.168.65.7"]

    def test_missing_file_yields_nothing(self, tmp_path):
        result = _run(f'_dns_resolver_ips "{tmp_path / "missing"}"')
        assert result.returncode == 0
        assert result.stdout == ""


class TestMainFixedPath:
    # Every security step runs binaries (iptables, getent, jq, ss, …) by name;
    # the box user owns ~/.local/bin, which the image puts ahead on PATH, so a
    # root-run exec must resolve from a fixed system PATH, not the inherited one.
    _NOOP_STEPS = (
        "render_runtime_configs",
        "start_ulogd",
        "apply_deny_host_set",
        "apply_egress_zone",
        "install_claude_json",
        "install_box_guide",
        "setup_forge_git_credentials",
        "setup_claude_api_key_approval",
        "run_setup_scripts",
    )

    def _run_main(self) -> subprocess.CompletedProcess:
        noops = "".join(f"{fn}() {{ :; }}\n" for fn in self._NOOP_STEPS)
        return _run(noops + 'PATH=/poison/bin\nmain\necho "RESULT=$PATH"')

    def test_main_replaces_an_inherited_path_with_a_fixed_system_path(self):
        result = self._run_main()
        assert result.returncode == 0
        (line,) = [s for s in result.stdout.splitlines() if s.startswith("RESULT=")]
        path = line.removeprefix("RESULT=")
        assert "/poison/bin" not in path
        assert "/usr/bin" in path.split(":")
        assert "/sbin" in path.split(":")


class TestDefaultRouteGateway:
    def test_parses_the_via_address_from_the_default_route(self):
        result = _run(
            'ip() { echo "default via 172.20.0.1 dev eth0"; }\n_default_route_gateway'
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "172.20.0.1"

    def test_empty_when_no_default_route(self):
        result = _run("ip() { return 0; }\n_default_route_gateway")
        assert result.returncode == 0
        assert result.stdout.strip() == ""


class TestApplyDenyHostSet:
    # Stub iptables to print its args; the deny set is applied directly so we
    # read it back off stdout rather than from a real OUTPUT chain.
    _IPTABLES_STUB = 'iptables() { echo "$*"; }\n'

    def _deny_set(self, extra: str = "") -> subprocess.CompletedProcess:
        return _run(
            self._IPTABLES_STUB
            + 'ip() { echo "default via 172.20.0.1 dev eth0"; }\n'
            + "getent() { return 1; }\n"
            + extra
            + "apply_deny_host_set"
        )

    def test_drops_the_default_route_gateway(self):
        result = self._deny_set()
        assert result.returncode == 0
        lines = result.stdout.splitlines()
        assert "-A OUTPUT -d 172.20.0.1 -j DROP" in lines

    def test_logs_then_drops_the_gateway_in_that_order(self):
        result = self._deny_set()
        lines = result.stdout.splitlines()
        nflog = next(
            i for i, s in enumerate(lines) if "172.20.0.1" in s and "NFLOG" in s
        )
        drop = next(
            i for i, s in enumerate(lines) if s == "-A OUTPUT -d 172.20.0.1 -j DROP"
        )
        assert nflog < drop

    def test_also_drops_host_docker_internal_when_it_resolves(self):
        result = self._deny_set(
            extra='getent() { echo "192.168.65.2 host.docker.internal"; }\n'
        )
        lines = result.stdout.splitlines()
        assert "-A OUTPUT -d 192.168.65.2 -j DROP" in lines

    def test_skips_host_docker_internal_when_it_does_not_resolve(self):
        result = self._deny_set()
        assert "host.docker.internal" not in result.stdout


class TestPortCarveoutRules:
    # Exercised through the same sourceable-rule-builder seam as
    # _trusted_allow_rules: the function prints bare iptables args, so the
    # harness can read them back without NET_ADMIN or a real OUTPUT chain.
    def test_emits_an_accept_for_a_declared_port(self):
        result = _run('STX_EGRESS_PORTS="22/tcp"\n_port_carveout_rules 1000')
        assert result.returncode == 0
        assert (
            "-m owner --uid-owner 1000 -p tcp --dport 22 -j ACCEPT"
            in result.stdout.splitlines()
        )

    def test_wildcard_port_accepts_the_whole_protocol(self):
        result = _run('STX_EGRESS_PORTS="*/udp"\n_port_carveout_rules 1000')
        assert result.returncode == 0
        assert (
            "-m owner --uid-owner 1000 -p udp -j ACCEPT" in result.stdout.splitlines()
        )

    def test_no_carveouts_emit_no_rules(self):
        result = _run("_port_carveout_rules 1000")
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_drops_host_owned_private_ranges_before_any_carveout_accept(self):
        # A carve-out's --dport match carries no destination limit; on Linux
        # every host-owned address (docker0, other bridge gateways, the LAN) is
        # RFC1918, so each private/CGNAT/link-local range must be dropped before
        # the ACCEPT can match it — otherwise a 22/tcp carve-out reaches
        # 172.17.0.1:22, a host service on docker0.
        result = _run('STX_EGRESS_PORTS="22/tcp"\n_port_carveout_rules 1000')
        assert result.returncode == 0
        lines = result.stdout.splitlines()
        accept = lines.index("-m owner --uid-owner 1000 -p tcp --dport 22 -j ACCEPT")
        for rng in (
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
            "100.64.0.0/10",
            "169.254.0.0/16",
        ):
            drop = f"-m owner --uid-owner 1000 -d {rng} -j DROP"
            assert drop in lines
            assert lines.index(drop) < accept


class TestTrustedAllowRules:
    def test_builds_subnet_and_resolver_rules(self, tmp_path):
        resolv = _resolv_conf(tmp_path, "127.0.0.11")
        result = _run(
            '_own_ipv4_subnets() { printf "%s\\n" "172.20.0.0/16"; }\n'
            f'_trusted_allow_rules "{resolv}"'
        )
        assert result.returncode == 0
        assert result.stdout.splitlines() == [
            "-d 172.20.0.0/16 -j ACCEPT",
            "-d 127.0.0.11 -p udp --dport 53 -j ACCEPT",
            "-d 127.0.0.11 -p tcp --dport 53 -j ACCEPT",
        ]

    def test_never_emits_a_bare_rfc1918_supernet(self, tmp_path):
        # Regression guard: only the box's own narrow subnet(s) should ever
        # appear, never a blanket RFC1918 ACCEPT.
        resolv = _resolv_conf(tmp_path, "127.0.0.11")
        result = _run(
            '_own_ipv4_subnets() { printf "%s\\n" "172.20.0.0/16"; }\n'
            f'_trusted_allow_rules "{resolv}"'
        )
        for blanket in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"):
            assert blanket not in result.stdout

    def test_real_own_subnets_are_well_formed(self):
        result = _run("_own_ipv4_subnets")
        assert result.returncode == 0
        # A host route (a bare address, as on some cloud VMs) is as valid an
        # iptables destination as a subnet.
        for line in result.stdout.splitlines():
            ipaddress.IPv4Network(line)

    def test_a_linked_box_with_two_networks_gets_an_accept_rule_for_each(
        self, tmp_path
    ):
        # a box with a link attaches to its own stx-net plus the
        # target's — two interfaces, each with its own scope-link route.
        # `ip ... route show scope link` (no `dev` filter) already lists
        # every interface's subnet, so no code change was needed here —
        # this pins that down.
        resolv = _resolv_conf(tmp_path, "127.0.0.11")
        result = _run(
            '_own_ipv4_subnets() { printf "%s\\n" "172.20.0.0/16" "172.21.0.0/16"; }\n'
            f'_trusted_allow_rules "{resolv}"'
        )
        assert result.returncode == 0
        assert "-d 172.20.0.0/16 -j ACCEPT" in result.stdout.splitlines()
        assert "-d 172.21.0.0/16 -j ACCEPT" in result.stdout.splitlines()


class TestRunSetupScripts:
    # Stands in for the real `su -s SHELL -c CMD [--] USER [ARG...]`, printing
    # what it received instead of running it — real `su` forwards ARG... as
    # the invoked shell's positional parameters, never through CMD's text.
    _SU_STUB = (
        "su() {\n"
        "    shift 2  # drop -s SHELL\n"
        "    shift    # drop -c\n"
        '    printf "CMD:%s\\n" "$1"; shift\n'
        '    [ "$1" = "--" ] && shift\n'
        "    shift    # drop USER\n"
        '    for a in "$@"; do printf "ARG:%s\\n" "$a"; done\n'
        "}\n"
    )

    def test_script_path_with_quote_reaches_argv_intact_not_spliced_into_cmd(
        self, tmp_path
    ):
        script = "weird'hook.sh"
        status_file = tmp_path / "setup-status.json"
        log_file = tmp_path / "setup.log"
        result = _run(
            self._SU_STUB
            + f'STX_BOX_SETUP="{script}"\n'
            + "STX_ZONE=trusted\n"
            + "STX_BOX_USER=devuser\n"
            + f'STX_SETUP_STATUS_FILE="{status_file}"\n'
            + f'STX_SETUP_LOG_FILE="{log_file}"\n'
            + "run_setup_scripts\n"
        )
        assert result.returncode == 0
        log_lines = log_file.read_text().splitlines()
        assert f"ARG:/workspace/{script}" in log_lines
        for line in log_lines:
            if line.startswith("CMD:"):
                assert "'" not in line.removeprefix("CMD:")

    def test_untrusted_zone_runs_no_scripts(self, tmp_path):
        status_file = tmp_path / "setup-status.json"
        result = _run(
            self._SU_STUB
            + 'STX_BOX_SETUP="hook.sh"\n'
            + "STX_ZONE=dmz\n"
            + "STX_BOX_USER=devuser\n"
            + f'STX_SETUP_STATUS_FILE="{status_file}"\n'
            + "run_setup_scripts\n"
        )
        assert result.returncode == 0
        assert result.stdout == ""

    def test_untrusted_zone_still_records_done(self, tmp_path):
        status_file = tmp_path / "setup-status.json"
        result = _run(
            self._SU_STUB
            + 'STX_BOX_SETUP="hook.sh"\n'
            + "STX_ZONE=dmz\n"
            + "STX_BOX_USER=devuser\n"
            + f'STX_SETUP_STATUS_FILE="{status_file}"\n'
            + "run_setup_scripts\n"
        )
        assert result.returncode == 0
        assert json.loads(status_file.read_text()) == {"state": "done"}

    def test_empty_setup_list_records_done(self, tmp_path):
        status_file = tmp_path / "setup-status.json"
        result = _run(
            self._SU_STUB
            + "STX_ZONE=trusted\n"
            + "STX_BOX_USER=devuser\n"
            + f'STX_SETUP_STATUS_FILE="{status_file}"\n'
            + "run_setup_scripts\n"
        )
        assert result.returncode == 0
        assert json.loads(status_file.read_text()) == {"state": "done"}

    def test_successful_scripts_record_done(self, tmp_path):
        status_file = tmp_path / "setup-status.json"
        result = _run(
            self._SU_STUB
            + 'STX_BOX_SETUP="one.sh two.sh"\n'
            + "STX_ZONE=trusted\n"
            + "STX_BOX_USER=devuser\n"
            + f'STX_SETUP_STATUS_FILE="{status_file}"\n'
            + f'STX_SETUP_LOG_FILE="{tmp_path / "setup.log"}"\n'
            + "run_setup_scripts\n"
        )
        assert result.returncode == 0
        assert json.loads(status_file.read_text()) == {"state": "done"}

    def test_failing_script_records_exit_code_and_name_then_stops(self, tmp_path):
        status_file = tmp_path / "setup-status.json"
        log_file = tmp_path / "setup.log"
        su_stub_that_fails_first = (
            "su() {\n"
            "    shift 2; shift\n"
            '    printf "CMD:%s\\n" "$1"; shift\n'
            '    [ "$1" = "--" ] && shift\n'
            "    shift\n"
            '    printf "ARG:%s\\n" "$1"\n'
            '    case "$1" in *one.sh) return 7 ;; *) return 0 ;; esac\n'
            "}\n"
        )
        result = _run(
            su_stub_that_fails_first
            + 'STX_BOX_SETUP="one.sh two.sh"\n'
            + "STX_ZONE=trusted\n"
            + "STX_BOX_USER=devuser\n"
            + f'STX_SETUP_STATUS_FILE="{status_file}"\n'
            + f'STX_SETUP_LOG_FILE="{log_file}"\n'
            + "run_setup_scripts\n"
        )
        assert result.returncode == 0
        assert json.loads(status_file.read_text()) == {
            "state": "failed",
            "script": "one.sh",
            "exit_code": 7,
        }
        log_lines = log_file.read_text().splitlines()
        assert "ARG:/workspace/one.sh" in log_lines
        assert "ARG:/workspace/two.sh" not in log_lines


class TestProvisionRun:
    def test_false_when_unset(self):
        result = _run("_provision_run")
        assert result.returncode == 1

    def test_true_when_set(self):
        result = _run("STX_PROVISION_RUN=1\n_provision_run")
        assert result.returncode == 0


class TestExecAsBoxUser:
    # Same stand-in as TestRunSetupScripts — see its comment.
    _SU_STUB = (
        "su() {\n"
        "    shift 2  # drop -s SHELL\n"
        "    shift    # drop -c\n"
        '    printf "CMD:%s\\n" "$1"; shift\n'
        '    [ "$1" = "--" ] && shift\n'
        "    shift    # drop USER\n"
        '    for a in "$@"; do printf "ARG:%s\\n" "$a"; done\n'
        "}\n"
    )

    def test_forwards_full_argv_as_distinct_words(self):
        result = _run(
            self._SU_STUB
            + "STX_BOX_USER=devuser\n"
            + '_exec_as_box_user bash "weird\'script.sh" --flag\n'
        )
        assert result.returncode == 0
        lines = result.stdout.splitlines()
        assert "ARG:bash" in lines
        assert "ARG:weird'script.sh" in lines
        assert "ARG:--flag" in lines

    def test_argv_never_spliced_into_cmd_text(self):
        result = _run(
            self._SU_STUB
            + "STX_BOX_USER=devuser\n"
            + '_exec_as_box_user bash "weird\'script.sh"\n'
        )
        assert result.returncode == 0
        for line in result.stdout.splitlines():
            if line.startswith("CMD:"):
                assert "'" not in line.removeprefix("CMD:")


def test_proxy_log_lives_in_a_directory_the_runtime_creates():
    conf = (_INIT_BOX.parent / "tinyproxy.conf").read_text()
    (log_file,) = [
        line.split(None, 1)[1].strip('"')
        for line in conf.splitlines()
        if line.startswith("LogFile ")
    ]
    log_dir = str(Path(log_file).parent)
    # A distro package may own /var/log/tinyproxy with modes root can't write past
    # once every capability is dropped.
    assert log_dir != "/var/log/tinyproxy"
    assert f"mkdir -p {log_dir}\n" in _INIT_BOX.read_text()


def test_egress_log_prefixes_are_namespaced_to_stx():
    # The dropped-packet log lines are grepped by name on the host — they
    # must carry the [STX-*] prefix, never the old [SANDBOX-*] one.
    text = _INIT_BOX.read_text()
    assert "[SANDBOX-DENY]" not in text
    assert "[SANDBOX-DROP]" not in text
    assert "[STX-DENY] " in text
    assert "[STX-DROP] " in text


def test_connect_tunnels_are_restricted_to_https():
    # Without this, a CONNECT to an allowlisted host's port 22 (or any
    # other open port) would tunnel straight through — the hostname filter
    # says nothing about the port.
    conf = (_INIT_BOX.parent / "tinyproxy.conf").read_text()
    assert "ConnectPort 443" in conf.splitlines()
