#!/usr/bin/env python
"""
Command-Line Utility: Merged Branch EUC File Tracing & Database Cleanup.

Productized CLI for Git Walk:
  1. Identifies merged branches.
  2. Traces local EUC Excel copies on C: or D: drive.
  3. Safely unlinks the local file from disk.
  4. Marks the branch DELETED in the SQLite database and revokes working copy leases.

Usage:
  py scripts/merge_branch_cleanup.py --branch-id BR_12345
  py scripts/merge_branch_cleanup.py --all-merged
  py scripts/merge_branch_cleanup.py --all-merged --dry-run
"""

import sys
from pathlib import Path

# Ensure backend package is in python path
backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from app.services.branch_lifecycle_manager import main

if __name__ == "__main__":
    main()
