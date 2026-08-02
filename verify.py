"""Goal verification: runs selftest + dry-run and exits 0 only if both pass.

Usage: python verify.py
"""

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import main as bot  # noqa: E402


def main() -> int:
    cfg = bot.load_config()
    bot.setup_logging(cfg)
    print("=== SELFTEST ===")
    selftest_rc = bot.cmd_selftest(cfg)
    print("=== DRY-RUN (demo seed) ===")
    dryrun_rc = bot.cmd_dry_run(cfg, seed_demo=True)

    if selftest_rc == 0 and dryrun_rc == 0:
        print("\nVERIFY PASSED")
        return 0
    print(f"\nVERIFY FAILED (selftest={selftest_rc}, dryrun={dryrun_rc})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
