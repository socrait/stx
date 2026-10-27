#!/bin/bash
# Container init — see docs/box-contract.md for the sequence. Runs as root,
# requires NET_ADMIN; execs the container CMD once done.
set -eu

render_runtime_configs() {
    # ulogd's plugin dir is arch-specific; resolve it here, not at build time.
    mkdir -p /run/stx
    local plugin_dir
    plugin_dir=$(ls -d /usr/lib/*/ulogd 2>/dev/null | head -1)
    if [ -z "${plugin_dir}" ]; then
        echo "error: could not resolve the ulogd plugin directory (expected /usr/lib/*/ulogd)" >&2
        exit 1
    fi
    sed "s#@ULOGD_PLUGIN_DIR@#${plugin_dir}#g" /opt/stx/box/ulogd.conf > /run/stx/ulogd.conf
}

start_ulogd() {
    # Pre-create the log file world-readable before ulogd starts. ulogd opens
    # existing files without changing permissions, so the box user can read it.
    mkdir -p /var/log/iptables
    touch /var/log/iptables/iptables.log
    chmod 644 /var/log/iptables/iptables.log
    # ulogd runs in the foreground by default; background it so the script
    # continues to apply iptables rules.
    ulogd -c /run/stx/ulogd.conf &
}

_default_route_gateway() {
    # The box's default-route nexthop. On a Linux bridge network this is the
    # host itself, and it sits inside the box's own scope-link subnet — so the
    # trusted zone's subnet ACCEPT (and nothing in the dmz zone) would reach
    # it unless it is dropped first.
    ip -4 route show default 2>/dev/null \
        | awk '{for (i = 1; i < NF; i++) if ($i == "via") print $(i + 1)}' | head -1
}

_host_gateway_ips() {
    # The two host addresses resolvable by name here: the default-route gateway
    # (on a Linux bridge, the host itself) and host.docker.internal (Docker
    # Desktop's host gateway, a DNS name resolved now; absent on bare Linux,
    # skipped). Not every host-owned address on Linux — docker0, other bridge
    # gateways and the LAN are RFC1918 and reach host services too. Those are
    # kept off a port carve-out by _port_carveout_rules and dropped wholesale
    # by the dmz zone.
    _default_route_gateway
    getent hosts host.docker.internal 2>/dev/null | awk '{print $1}' || true
}

apply_deny_host_set() {
    iptables -A OUTPUT -d 100.64.0.0/10 -j NFLOG --nflog-group 1 --nflog-prefix "[STX-DENY] "
    iptables -A OUTPUT -d 100.64.0.0/10 -j DROP   # Tailscale CGNAT
    iptables -A OUTPUT -d 169.254.0.0/16 -j NFLOG --nflog-group 1 --nflog-prefix "[STX-DENY] "
    iptables -A OUTPUT -d 169.254.0.0/16 -j DROP  # cloud metadata / link-local

    # Applied in main() before apply_egress_zone, so these land ahead of the
    # trusted zone's subnet ACCEPT — the gateway and host.docker.internal are
    # unreachable in either zone. The host's other RFC1918 addresses on Linux
    # (docker0 etc.) are not in this set; they are kept off carve-outs by
    # _port_carveout_rules and dropped wholesale by the dmz zone.
    local host_ip
    for host_ip in $(_host_gateway_ips); do
        iptables -A OUTPUT -d "$host_ip" -j NFLOG --nflog-group 1 --nflog-prefix "[STX-DENY] "
        iptables -A OUTPUT -d "$host_ip" -j DROP
    done
}

_ipv6_kernel_available() {
    local probe_path="${1:-/proc/net/if_inet6}"
    [ -e "$probe_path" ]
}

apply_ipv6_drop() {
    if ! _ipv6_kernel_available; then
        return  # no IPv6 stack — nothing to drop
    fi
    if ! ip6tables -A OUTPUT -j DROP; then
        echo "error: failed to apply the IPv6 egress DROP; refusing to start with an IPv6 bypass open" >&2
        exit 1
    fi
}

_own_ipv4_subnets() {
    # "scope link" routes are the directly-connected subnets, already in network/prefix form.
    ip -4 -o route show scope link 2>/dev/null | awk '{print $1}' | sort -u
}

_dns_resolver_ips() {
    # Resolver address varies by host and isn't always inside the box's own subnet.
    local resolv_conf="${1:-/etc/resolv.conf}"
    [ -r "$resolv_conf" ] || return 0
    awk '/^nameserver[[:space:]]/ {print $2}' "$resolv_conf" | sort -u
}

# Host-owned ranges a port carve-out must never open a path to: RFC1918, the
# Tailscale CGNAT range and link-local. On Linux every address that reaches the
# host (docker0, other bridge gateways, the LAN) falls in one of these, and a
# carve-out's --dport match carries no destination limit of its own — so
# without the drops below a `22/tcp` carve-out would let the box user reach
# 172.17.0.1:22.
_PRIVATE_RANGES="10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 169.254.0.0/16"

_port_carveout_rules() {
    # Prints bare iptables args for the egress.ports carve-outs, one rule per
    # line — no iptables call, so a test harness can exercise this without
    # NET_ADMIN or root. Each carve-out is preceded by a DROP of the host-owned
    # private ranges (scoped to the box user) so it can only ever reach a public
    # destination. The box's own subnet and the DNS resolver are ACCEPTed before
    # these rules apply, so their traffic never reaches the drops.
    local box_uid="$1"
    [ -n "${STX_EGRESS_PORTS:-}" ] || return 0
    local range token port proto
    for range in $_PRIVATE_RANGES; do
        echo "-m owner --uid-owner $box_uid -d $range -j DROP"
    done
    for token in ${STX_EGRESS_PORTS}; do
        port="${token%%/*}"
        proto="${token#*/}"
        if [ "$port" = "*" ]; then
            echo "-m owner --uid-owner $box_uid -p $proto -j ACCEPT"
        else
            echo "-m owner --uid-owner $box_uid -p $proto --dport $port -j ACCEPT"
        fi
    done
}

_trusted_allow_rules() {
    # Prints bare iptables args, one per line — no iptables call, so a test
    # harness can exercise this without NET_ADMIN or root.
    local resolv_conf="${1:-/etc/resolv.conf}"
    local subnet resolver
    for subnet in $(_own_ipv4_subnets); do
        echo "-d $subnet -j ACCEPT"
    done
    for resolver in $(_dns_resolver_ips "$resolv_conf"); do
        echo "-d $resolver -p udp --dport 53 -j ACCEPT"
        echo "-d $resolver -p tcp --dport 53 -j ACCEPT"
    done
}

apply_egress_zone() {
    local zone="${STX_ZONE:-trusted}"

    if [ "$zone" = "dmz" ]; then
        # Open internet, RFC1918 blocked — see docs/box-contract.md (step 4),
        # docs/security.md. DNS exempted first: Docker Desktop's resolver is RFC1918.
        iptables -A OUTPUT -p udp --dport 53 -j ACCEPT
        iptables -A OUTPUT -p tcp --dport 53 -j ACCEPT
        iptables -A OUTPUT -d 10.0.0.0/8 -j NFLOG --nflog-group 1 --nflog-prefix "[STX-DROP] "
        iptables -A OUTPUT -d 10.0.0.0/8 -j DROP
        iptables -A OUTPUT -d 172.16.0.0/12 -j NFLOG --nflog-group 1 --nflog-prefix "[STX-DROP] "
        iptables -A OUTPUT -d 172.16.0.0/12 -j DROP
        iptables -A OUTPUT -d 192.168.0.0/16 -j NFLOG --nflog-group 1 --nflog-prefix "[STX-DROP] "
        iptables -A OUTPUT -d 192.168.0.0/16 -j DROP
        apply_ipv6_drop
        # Default iptables policy remains ACCEPT — rest of public internet is reachable.
        return
    fi

    # trusted zone: tinyproxy CONNECT allowlist proxy — see docs/box-contract.md (step 4).

    # Background jobs bypass set -eu, so poll for the bound socket to catch
    # a startup failure early instead of a confusing ECONNREFUSED later.
    # Not /var/log/tinyproxy: a distro package may own it (Debian 13: tinyproxy:adm,
    # 0750), and with every capability dropped root cannot write there.
    mkdir -p /var/log/stx
    tinyproxy -d -c /opt/stx/box/tinyproxy.conf >> /var/log/stx/tinyproxy.log 2>&1 &
    _i=0
    while ! ss -tlnp 2>/dev/null | grep -q ':3128'; do
        if [ $_i -ge 10 ]; then
            echo "error: tinyproxy did not bind on :3128 after 2s" >&2
            cat /var/log/stx/tinyproxy.log >&2 2>/dev/null || true
            exit 1
        fi
        sleep 0.2
        _i=$((_i + 1))
    done

    # Loopback covers tinyproxy and Docker's embedded DNS resolver.
    iptables -A OUTPUT -o lo -j ACCEPT
    iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

    # Box's own subnet(s) plus the DNS resolver — see docs/box-contract.md (step 4).
    local rule
    while IFS= read -r rule; do
        [ -n "$rule" ] || continue
        # shellcheck disable=SC2086
        iptables -A OUTPUT $rule
    done < <(_trusted_allow_rules)

    # tinyproxy runs as root (uid 0); its own outbound goes via eth0, not loopback.
    iptables -A OUTPUT -m owner --uid-owner 0 -j ACCEPT

    # Port carve-outs for the box user, Docker notation — see docs/manifest.md
    # (egress.ports), docs/security.md. Each carve-out is preceded by drops for
    # the host-owned private ranges, so it can only reach a public destination:
    # a carve-out has no destination limit of its own and on Linux every host
    # address is RFC1918. The box's own subnet and the resolver are ACCEPTed
    # above, so their traffic never reaches those drops.
    local box_uid
    box_uid=$(id -u "${STX_BOX_USER}")
    while IFS= read -r rule; do
        [ -n "$rule" ] || continue
        # shellcheck disable=SC2086
        iptables -A OUTPUT $rule
    done < <(_port_carveout_rules "$box_uid")

    # Log then drop everything else the box user sends directly.
    iptables -A OUTPUT -j NFLOG --nflog-group 1 --nflog-prefix "[STX-DROP] "
    iptables -A OUTPUT -j DROP

    apply_ipv6_drop  # tinyproxy only listens on IPv4
}

_home_dir_for() {
    getent passwd "$1" | cut -d: -f6
}

install_claude_json() {
    # Pre-accepts the workspace-trust dialog — see docs/box-contract.md (step 5).
    local home
    home=$(_home_dir_for "${STX_BOX_USER}")
    if [ ! -f "${home}/.claude.json" ]; then
        su -s /bin/bash -c "cp /opt/stx/box/claude.json '${home}/.claude.json'" "${STX_BOX_USER}"
    fi
}

install_box_guide() {
    # /opt/stx/box is read-only and shared by both zones, so the symlink
    # naming the zone that's only known at runtime lives under /run/stx
    # instead — see docs/box-contract.md (step 5).
    local box_dir="${1:-/opt/stx/box}"
    local run_dir="${2:-/run/stx}"
    local zone="${STX_ZONE:-trusted}"
    local guide="${box_dir}/guide-${zone}.md"

    mkdir -p "$run_dir"
    ln -sf "$guide" "${run_dir}/AGENTS.md"

    local home
    home=$(_home_dir_for "${STX_BOX_USER}")
    [ -n "$home" ] || return 0
    [ -f "${home}/.claude/CLAUDE.md" ] && return 0
    # Runs as the box user via su, same as install_claude_json, so the
    # installed file is owned by them, not root.
    su -s /bin/bash -c "
        mkdir -p '${home}/.claude' && cp '${guide}' '${home}/.claude/CLAUDE.md'
    " "${STX_BOX_USER}"
}

setup_forge_git_credentials() {
    # Rewrites git@ to HTTPS+token — see docs/box-contract.md (step 6), docs/secrets.md.
    if [ "${STX_ZONE:-trusted}" != "trusted" ] || [ -z "${STX_FORGE_TOKEN:-}" ]; then
        return
    fi
    local origin_url
    origin_url=$(su -s /bin/bash -c "git -C /workspace remote get-url origin" "${STX_BOX_USER}" 2>/dev/null) || return
    case "${origin_url}" in
        git@"${STX_FORGE_HOST}":*)
            su -s /bin/bash -c "
                git config --global url.'https://${STX_FORGE_GIT_USER}:${STX_FORGE_TOKEN}@${STX_FORGE_HOST}/${origin_url#git@${STX_FORGE_HOST}:}'.insteadOf '${origin_url}'
            " "${STX_BOX_USER}"
            ;;
        *)
            # Already HTTPS, or not an SSH remote on this forge host —
            # nothing to rewrite.
            ;;
    esac
}

setup_claude_api_key_approval() {
    # Pre-approves the one-time API-key prompt — see docs/box-contract.md (step 7).
    if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
        return
    fi
    local home last20
    home=$(_home_dir_for "${STX_BOX_USER}")
    last20="${ANTHROPIC_API_KEY: -20}"
    su -s /bin/bash -c "
        jq --arg key '${last20}' '.customApiKeyResponses.approved = ((.customApiKeyResponses.approved // []) + [\$key] | unique)' '${home}/.claude.json' > /tmp/claude.json.tmp &&
        mv /tmp/claude.json.tmp '${home}/.claude.json'
    " "${STX_BOX_USER}"
}

run_setup_scripts() {
    # Trusted only, after every security step — see docs/box-contract.md (step 8).
    # Always ends by recording an outcome record at $STX_SETUP_STATUS_FILE,
    # root-owned so the host can read it with a plain `docker exec` — see
    # docs/reference.md (Setup). Written for the dmz zone too (trivially
    # "done", since it never runs setup scripts) so the host side has one
    # record shape to wait on regardless of zone.
    local status_file="${STX_SETUP_STATUS_FILE:-/run/stx/setup-status.json}"
    local log_file="${STX_SETUP_LOG_FILE:-/logs/setup.log}"
    mkdir -p "$(dirname "$status_file")"
    if [ "${STX_ZONE:-trusted}" != "trusted" ]; then
        jq -n '{state: "done"}' > "$status_file"
        return
    fi
    local script exit_code
    for script in ${STX_BOX_SETUP:-}; do
        # The path reaches the nested shell as $0, a positional argument —
        # never spliced into the -c command text — so a path containing a
        # quote or space can't break out of it. `&& continue` — not
        # `if ...; then continue; fi` — because bash's own `if` resets $? to
        # 0 when the condition is false and there's no `else`, which would
        # otherwise lose the real exit code read right below. Output is
        # appended to `log_file`, mounted at /logs — already a host bind —
        # so a failure's host-side error can point straight at a readable
        # file instead of `docker logs`.
        su -s /bin/bash -c 'cd /workspace && export PATH="$1" && exec "$0"' \
            -- "${STX_BOX_USER}" "/workspace/${script}" "$(_box_user_path)" \
            >>"$log_file" 2>&1 && continue
        exit_code=$?
        jq -n --arg script "$script" --argjson exit_code "$exit_code" \
            '{state: "failed", script: $script, exit_code: $exit_code}' \
            > "$status_file"
        return
    done
    jq -n '{state: "done"}' > "$status_file"
}

_provision_run() {
    [ -n "${STX_PROVISION_RUN:-}" ]
}

_exec_as_box_user() {
    # A provision run's argv is repo-supplied (`provision.steps`) — never
    # execed as root, unlike the long-running box's own `sleep infinity`
    # CMD. "stx-provision" is a throwaway $0 placeholder so every real
    # argv element lands in "$@" inside the nested script, each as its
    # own word — never spliced into the -c command text, so a path
    # containing a quote or space can't break out of it.
    su -s /bin/bash -c 'cd /workspace && export PATH="$1" && shift && exec "$@"' \
        -- "${STX_BOX_USER}" stx-provision "$(_box_user_path)" "$@"
}

#: The image's own PATH, saved before `main` pins the system one: commands
#: run as the box user get it back, so the image's tools (a venv, a
#: per-user bin dir) resolve for them exactly as in an interactive shell.
_box_user_path() {
    printf '%s' "${_IMAGE_PATH:-$_SYSTEM_PATH}"
}

#: The box user owns a directory the image puts ahead on PATH (~/.local/bin
#: in the dmz image; repos do the same), so an inherited PATH would let them
#: shadow iptables/getent/jq/ss and have this root entrypoint — which holds
#: NET_ADMIN — run their binary instead. Pin a fixed system PATH before any
#: command is resolved, so every tool comes from a root-owned system dir.
_SYSTEM_PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

main() {
    _IMAGE_PATH="$PATH"
    export PATH="$_SYSTEM_PATH"
    render_runtime_configs
    start_ulogd
    apply_deny_host_set
    apply_egress_zone
    install_claude_json
    install_box_guide
    setup_forge_git_credentials
    setup_claude_api_key_approval
    run_setup_scripts
    if _provision_run; then
        _exec_as_box_user "$@"
        exit $?
    fi
    exec "$@"
}

# Guarded so a test harness can source this for the rule-builders without running main().
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    main "$@"
fi
