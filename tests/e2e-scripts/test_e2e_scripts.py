"""Unit tests for the per-worktree E2E test harness scripts (issue #112).

Covers the Docker-free contracts of:
  - scripts/e2e/free-port.py   (free port allocation / arg validation)
  - scripts/e2e/common.sh      (RUN_ID slug + name/bucket/env-file builders)
  - scripts/e2e/teardown.sh    (no-op safety when nothing was provisioned, E2E_KEEP)
  - Makefile RUN_ID derivation  (parity with common.sh:e2e_run_id)

provision.sh / reap.sh require a real Docker daemon and are intentionally NOT
exercised here so the default suite stays Docker-free and CI-safe. Python 3.11,
stdlib only.
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

# tests/e2e-scripts/test_e2e_scripts.py -> parents[2] == repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]
E2E_DIR = REPO_ROOT / "scripts" / "e2e"
FREE_PORT = E2E_DIR / "free-port.py"
COMMON_SH = E2E_DIR / "common.sh"
TEARDOWN_SH = E2E_DIR / "teardown.sh"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _run_free_port(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(FREE_PORT), *args],
        capture_output=True,
        text=True,
    )


def _eval_common(func: str, env_overrides: dict[str, str]) -> str:
    """Source common.sh, run a single helper function, return stripped stdout."""
    env = dict(os.environ)
    env.update(env_overrides)
    result = subprocess.run(
        ["bash", "-c", f". {COMMON_SH}; {func}"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"`{func}` exited {result.returncode}; stderr={result.stderr!r}"
    )
    return result.stdout.strip()


# --------------------------------------------------------------------------- #
# 1. free-port.py
# --------------------------------------------------------------------------- #
def test_free_port_returns_five_distinct_bindable_ports():
    proc = _run_free_port(["5"])
    assert proc.returncode == 0, proc.stderr

    # Single line of output.
    assert proc.stdout.count("\n") == 1
    tokens = proc.stdout.split()
    assert len(tokens) == 5, f"expected 5 tokens, got {tokens!r}"

    ports = [int(t) for t in tokens]  # all parse as ints
    assert all(0 < p < 65536 for p in ports)
    assert len(set(ports)) == 5, f"ports not distinct: {ports}"

    # Each is (currently) bindable. A tiny TOCTOU window is acceptable.
    for p in ports:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", p))
        finally:
            s.close()


def test_free_port_default_is_one_port():
    proc = _run_free_port([])
    assert proc.returncode == 0, proc.stderr
    tokens = proc.stdout.split()
    assert len(tokens) == 1
    assert proc.stdout.count("\n") == 1
    int(tokens[0])  # parses


def test_free_port_zero_exits_two():
    proc = _run_free_port(["0"])
    assert proc.returncode == 2
    assert proc.stdout.strip() == ""
    assert "usage" in proc.stderr.lower()


# --------------------------------------------------------------------------- #
# 2. e2e_run_id derivation
# --------------------------------------------------------------------------- #
def test_e2e_run_id_primary_is_empty():
    out = _eval_common("e2e_run_id", {"ROOT_DIR": "/repo", "MAIN_ROOT": "/repo"})
    assert out == ""


@pytest.mark.parametrize(
    "root_dir, prefix",
    [
        ("/x/Feature_Foo", "feature-foo"),   # uppercase + underscore slug
        ("/x/--wt 1.2--", "wt-1-2"),         # punctuation + trim dashes
        ("/x/___", "wt"),                    # all-punctuation basename -> wt-<hash>
    ],
)
def test_e2e_run_id_worktree_is_slug_plus_hash(root_dir, prefix):
    # A worktree RUN_ID is a readable slug prefix + a full-path hash suffix.
    out = _eval_common("e2e_run_id", {"ROOT_DIR": root_dir, "MAIN_ROOT": "/repo"})
    assert re.fullmatch(rf"{re.escape(prefix)}-[0-9]+", out), out


def test_e2e_run_id_is_deterministic():
    env = {"ROOT_DIR": "/x/Feature_Foo", "MAIN_ROOT": "/repo"}
    assert _eval_common("e2e_run_id", env) == _eval_common("e2e_run_id", env)


def test_e2e_run_id_distinct_for_same_basename_different_path():
    # The bug the hash fixes: two worktrees sharing a leaf name must NOT collide.
    a = _eval_common("e2e_run_id", {"ROOT_DIR": "/a/feature", "MAIN_ROOT": "/repo"})
    b = _eval_common("e2e_run_id", {"ROOT_DIR": "/b/feature", "MAIN_ROOT": "/repo"})
    assert a != b, (a, b)
    assert a.startswith("feature-") and b.startswith("feature-")


# --------------------------------------------------------------------------- #
# 3. name / bucket / env-file builders branch on primary vs worktree
# --------------------------------------------------------------------------- #
def test_pg_container_primary():
    out = _eval_common("e2e_pg_container", {"ROOT_DIR": "/repo", "MAIN_ROOT": "/repo"})
    assert out == "par-e2e-postgres"


def test_pg_container_worktree():
    out = _eval_common(
        "e2e_pg_container", {"ROOT_DIR": "/x/Feature_Foo", "MAIN_ROOT": "/repo"}
    )
    assert re.fullmatch(r"par-e2e-postgres-feature-foo-[0-9]+", out), out


def test_s3_bucket_primary():
    out = _eval_common("e2e_s3_bucket", {"ROOT_DIR": "/repo", "MAIN_ROOT": "/repo"})
    assert out == "platform-artifacts"


def test_s3_bucket_worktree():
    out = _eval_common(
        "e2e_s3_bucket", {"ROOT_DIR": "/x/Feature_Foo", "MAIN_ROOT": "/repo"}
    )
    assert re.fullmatch(r"platform-artifacts-feature-foo-[0-9]+", out), out


def test_env_file_path_uses_root_dir():
    out = _eval_common(
        "e2e_env_file", {"ROOT_DIR": "/x/Feature_Foo", "MAIN_ROOT": "/repo"}
    )
    assert out == "/x/Feature_Foo/.tmp/e2e.env"


# --------------------------------------------------------------------------- #
# 4. Make <-> script parity for RUN_ID
# --------------------------------------------------------------------------- #
def test_make_run_id_matches_script_for_worktree():
    if shutil.which("make") is None:
        pytest.skip("make not on PATH")

    make_proc = subprocess.run(
        [
            "make",
            "--no-print-directory",
            "-p",
            "ROOT_DIR=/x/Feature_Foo",
            "MAIN_ROOT=/repo",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    # `make -p` dumps the database even when no default goal runs; non-zero exit
    # can occur if a goal fails, but the variable database still prints. Parse it.
    run_id_lines = [
        ln for ln in make_proc.stdout.splitlines() if ln.startswith("RUN_ID :=")
    ]
    assert run_id_lines, (
        f"no `RUN_ID :=` line in make -p output; stderr={make_proc.stderr!r}"
    )
    make_run_id = run_id_lines[-1].split(":=", 1)[1].strip()

    script_run_id = _eval_common(
        "e2e_run_id", {"ROOT_DIR": "/x/Feature_Foo", "MAIN_ROOT": "/repo"}
    )
    # Parity is the point: Make and the script must derive the SAME RUN_ID
    # (slug + full-path hash) on this machine.
    assert make_run_id == script_run_id
    assert re.fullmatch(r"feature-foo-[0-9]+", script_run_id), script_run_id


# --------------------------------------------------------------------------- #
# 5. teardown.sh safety (Docker-free paths)
# --------------------------------------------------------------------------- #
def test_teardown_noop_when_env_file_absent(tmp_path):
    """No .tmp/e2e.env -> exit 0, no docker calls. We shadow `docker` with a
    stub on PATH that fails loudly if invoked, proving the script never reaches
    a docker call on this path."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker_stub = bin_dir / "docker"
    docker_stub.write_text("#!/bin/sh\necho 'DOCKER WAS CALLED' >&2\nexit 99\n")
    docker_stub.chmod(0o755)

    env = dict(os.environ)
    env["ROOT_DIR"] = str(tmp_path)
    env["MAIN_ROOT"] = str(tmp_path)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"

    proc = subprocess.run(
        ["bash", str(TEARDOWN_SH)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "DOCKER WAS CALLED" not in proc.stderr
    # Silent no-op: nothing of substance on stdout.
    assert proc.stdout.strip() == ""


def test_teardown_keep_preserves_env_file(tmp_path):
    """E2E_KEEP=1 -> exit 0, leave everything (env file must survive)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker_stub = bin_dir / "docker"
    docker_stub.write_text("#!/bin/sh\necho 'DOCKER WAS CALLED' >&2\nexit 99\n")
    docker_stub.chmod(0o755)

    tmp_dir = tmp_path / ".tmp"
    tmp_dir.mkdir()
    env_file = tmp_dir / "e2e.env"
    env_file.write_text("RUN_ID=feature-foo\nE2E_PG_CONTAINER=par-e2e-postgres-feature-foo\n")

    env = dict(os.environ)
    env["ROOT_DIR"] = str(tmp_path)
    env["MAIN_ROOT"] = str(tmp_path)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["E2E_KEEP"] = "1"

    proc = subprocess.run(
        ["bash", str(TEARDOWN_SH)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "DOCKER WAS CALLED" not in proc.stderr
    # The keep path must not delete the contract file.
    assert env_file.exists(), "E2E_KEEP=1 must not remove .tmp/e2e.env"


# --------------------------------------------------------------------------- #
# Docker-gated smoke test (skipped in CI / default suite)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(
    shutil.which("docker") is None,
    reason="docker not available; provision/reap require a real daemon",
)
def test_common_sh_is_sourceable_with_docker_present():
    """When docker exists, common.sh still sources cleanly and exposes helpers.
    This does NOT call docker; it only guards against syntax regressions in the
    Docker-aware portion of common.sh. Provision/reap themselves are excluded."""
    out = _eval_common(
        "type e2e_ensure_localstack >/dev/null && echo ok",
        {"ROOT_DIR": "/repo", "MAIN_ROOT": "/repo"},
    )
    assert out == "ok"
