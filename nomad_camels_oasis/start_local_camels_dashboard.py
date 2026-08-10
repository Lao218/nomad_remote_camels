"""Start the local classic OASIS instance with the CAMELS plugin.

Run from the repository root:

    python nomad_camels_oasis/start_local_camels_dashboard.py

Docker Desktop must be running. The script builds the local plugin image (using
Docker's cache when unchanged), then starts the single project compose stack.
"""

from __future__ import annotations

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parent.parent
IMAGE = "ghcr.io/lao218/nomad_remote_camels:main"


def main() -> None:
    try:
        subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        raise SystemExit(
            "Docker Desktop is not accessible. Start Docker Desktop and ensure "
            "this Windows user can run 'docker info'."
        )

    print("Building the local OASIS image with the CAMELS and GUI v2 plugins ...")
    subprocess.run(
        ["docker", "build", "--target", "final", "-t", IMAGE, "."],
        cwd=ROOT,
        check=True,
    )

    print("Starting classic OASIS ...")
    subprocess.run(["docker", "compose", "up", "-d"], cwd=ROOT, check=True)
    print("\nReady:")
    print("  CAMELS setup: http://localhost/nomad-oasis/camels/setup")
    print("  GUI v2:       http://localhost/nomad-oasis/gui/v2/")


if __name__ == "__main__":
    main()
