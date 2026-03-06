#!/usr/bin/env python3
"""
push_to_github.py — Push SEO outline content to GitHub using TokenManager.

Replaces manual `cat credentials/github_token.txt` approach.
Uses git_push_helper (wraps TokenManager) for auto-selection + fallback.

Usage:
    python3 push_to_github.py --repo owner/repo --dir /path/to/repo
    python3 push_to_github.py --repo owner/repo --dir . --branch main --msg "add outline"
"""

import argparse
import logging
import sys
from pathlib import Path

# Add workspace root to path so we can import git_push_helper
_SCRIPT_DIR = Path(__file__).resolve().parent
_WORKSPACE = _SCRIPT_DIR.parents[2]  # skills/seo-outline/scripts → seo-outline → skills → workspace
if str(_WORKSPACE) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE))

from git_push_helper import git_push, get_token_for_repo

logger = logging.getLogger("seo-outline.push")


def main():
    parser = argparse.ArgumentParser(
        description="Push SEO outline to GitHub (auto-select token via TokenManager)",
    )
    parser.add_argument(
        "--repo", required=True,
        help="GitHub repo in owner/repo format",
    )
    parser.add_argument(
        "--dir", default=".",
        help="Local git repo directory (default: current dir)",
    )
    parser.add_argument(
        "--branch", default="main",
        help="Branch to push to (default: main)",
    )
    parser.add_argument(
        "--msg", default="add: seo outline",
        help="Commit message",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Verbose output",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    logger.info("🚀 SEO Outline push — repo: %s, branch: %s", args.repo, args.branch)

    success = git_push(
        repo=args.repo,
        repo_dir=args.dir,
        branch=args.branch,
        commit_msg=args.msg,
        verbose=args.verbose,
    )

    if success:
        logger.info("✅ SEO outline pushed successfully")
    else:
        logger.error("❌ Push failed — check token config or network")
        sys.exit(1)


if __name__ == "__main__":
    main()
