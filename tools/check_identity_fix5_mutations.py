"""Serial resolver mutations: enforce the hop bound and expand before '..'."""
import argparse
from pathlib import Path

from check_identity_fix4_mutations import run_mutations

MUTATIONS = (
    ("F5 hop counter allows a 41st link", "subfleet/daemon.py",
     "if hops > 40:", "if hops > 41:",
     "tests/fake/test_canonical_identity_fix5.py",
     "test_output_hop_limit_counts_repeated_noncyclic_links"),
    ("F5 target dotdot applied lexically before symlink expansion", "subfleet/daemon.py",
     "pending.extend(target.split(os.sep)[::-1])",
     "pending.extend(os.path.normpath(target).split(os.sep)[::-1])",
     "tests/fake/test_canonical_identity_fix5.py",
     "test_submit_refuses_hidden_loop_before_target_dotdot"),
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    return run_mutations(MUTATIONS, parser.parse_args().output)


if __name__ == "__main__":
    raise SystemExit(main())
