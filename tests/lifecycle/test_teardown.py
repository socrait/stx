from __future__ import annotations

import subprocess

from stx.lifecycle import bring_down_containers, container_ids_for_slug


def _completed(stdout: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout)


class FakeDocker:
    """A runner seam standing in for `subprocess.run`. Records every argv and
    answers `docker ps` from a slug->container-ids map that shrinks as
    `docker rm -f`/`docker compose down` remove things."""

    def __init__(self, containers: dict[str, list[str]], *, down_rc: int = 0):
        self.containers = {slug: list(ids) for slug, ids in containers.items()}
        self.down_rc = down_rc
        self.calls: list[list[str]] = []
        # When set, the compose `down` removes the project's containers; when
        # it fails (down_rc != 0) they survive for the label fallback.
        self.compose_down_removes = down_rc == 0

    def __call__(self, argv, *, capture_output=False, text=False, **kwargs):
        self.calls.append(argv)
        if argv[:3] == ["docker", "compose", "--env-file"]:
            if self.compose_down_removes:
                project = argv[argv.index("-p") + 1]
                slug = project.removeprefix("stx-")
                self.containers.pop(slug, None)
            return _completed(returncode=self.down_rc)
        if argv[:3] == ["docker", "ps", "-aq"]:
            flt = argv[argv.index("--filter") + 1]
            slug = flt.split("=")[-1]
            return _completed(stdout="\n".join(self.containers.get(slug, [])))
        if argv[:3] == ["docker", "rm", "-f"]:
            ids = set(argv[3:])
            for slug, present in self.containers.items():
                self.containers[slug] = [c for c in present if c not in ids]
            return _completed()
        return _completed()


def test_compose_down_clears_the_stack_without_touching_a_manifest():
    docker = FakeDocker({"demo": ["c1", "c2"]})
    result = bring_down_containers("demo", runner=docker)
    assert result.down_ok
    assert not result.used_label_fallback
    assert result.clean
    # Addressed purely by project name — no `-f <compose file>`, so no manifest.
    down = next(c for c in docker.calls if c[:2] == ["docker", "compose"])
    assert "-p" in down and "stx-demo" in down
    assert "-f" not in down


def test_failed_compose_down_falls_back_to_removing_by_label():
    docker = FakeDocker({"demo": ["c1", "c2"]}, down_rc=1)
    result = bring_down_containers("demo", runner=docker)
    assert not result.down_ok
    assert result.used_label_fallback
    assert result.clean  # the label fallback removed the survivors
    assert ["docker", "rm", "-f", "c1", "c2"] in docker.calls


def test_remaining_reports_containers_nothing_could_remove():
    # A container that survives the compose down and even `docker rm -f`.
    def stubborn(argv, **kwargs):
        if argv[:3] == ["docker", "ps", "-aq"]:
            return _completed(stdout="c1")
        return _completed(returncode=1 if argv[:2] == ["docker", "compose"] else 0)

    result = bring_down_containers("demo", runner=stubborn)
    assert result.remaining == ["c1"]
    assert not result.clean


def test_container_ids_none_when_docker_cannot_be_queried():
    # A failed `docker ps` is "unknown", not "no containers" — callers must be
    # able to tell the two apart, so this returns None rather than [].
    def failing(argv, **kwargs):
        return _completed(returncode=1)

    assert container_ids_for_slug("demo", runner=failing) is None


def test_unqueryable_docker_is_not_clean():
    # If `docker ps` can't answer, teardown has no idea what survived — it must
    # report not-clean so the caller keeps the entry, never claim success.
    def failing(argv, **kwargs):
        return _completed(returncode=1)

    result = bring_down_containers("demo", runner=failing)
    assert not result.clean


def test_bounded_calls_pass_a_timeout():
    # Every docker call teardown makes is bounded, so a wedged daemon fails the
    # command instead of hanging it forever.
    seen: list[float | None] = []

    def recording(argv, **kwargs):
        seen.append(kwargs.get("timeout"))
        return _completed(stdout="")

    bring_down_containers("demo", runner=recording)
    assert seen and all(t is not None for t in seen)
