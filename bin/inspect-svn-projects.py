#!/usr/bin/env python3
"""Inspect conversion inputs and write a reviewed, deterministic conversion plan."""

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT_COLUMNS = [
    "root_id",
    "svn_repo",
    "project_root",
    "ref_type",
    "gitlab_project",
    "ref_name",
    "target_rev",
]
SECTION_COLUMNS = ["root_id", "svn_repo", "svn_path", "from_rev", "to_rev"]
SVN_ROOT_COLUMNS = ["svn_repo"]


def fail(message):
    raise RuntimeError(message)


def run(command, *, check=True, cwd=None, env=None, capture=True):
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
        errors="replace",
    )
    if check and result.returncode:
        fail(
            f"Command failed ({result.returncode}): {' '.join(map(str, command))}\n"
            f"{result.stdout or ''}{result.stderr or ''}"
        )
    return result


def read_tsv(path, expected_columns):
    path = Path(path)
    if not path.is_file():
        fail(f"TSV file not found: {path}")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeError as exc:
        fail(f"TSV is not valid UTF-8 ({path}): {exc}")
    lines = text.splitlines()
    header_line = next((line for line in lines if line and not line.startswith("#")), None)
    if header_line is None:
        fail(f"TSV has no header: {path}")
    headers = header_line.split("\t")
    if headers != expected_columns:
        fail(
            f"Unexpected TSV header in {path}.\n"
            f"  expected: {' | '.join(expected_columns)}\n"
            f"  actual:   {' | '.join(headers)}"
        )
    rows = []
    header_seen = False
    for line_number, line in enumerate(lines, 1):
        if not line or line.startswith("#"):
            continue
        if not header_seen:
            header_seen = True
            continue
        cells = line.split("\t")
        if len(cells) != len(expected_columns):
            fail(f"{path}:{line_number}: expected {len(expected_columns)} tab-separated fields")
        row = dict(zip(expected_columns, (cell.strip() for cell in cells)))
        if any(value == "" for value in row.values()):
            fail(f"{path}:{line_number}: empty fields are not allowed")
        rows.append(row)
    return rows


def safe_relative_path(value, label):
    if value.startswith("/") or "\\" in value:
        fail(f"{label} must be a slash-separated relative path: {value!r}")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        fail(f"{label} contains an unsafe path component: {value!r}")
    return value


def parse_revision(value, label):
    if not re.fullmatch(r"[0-9]+", value):
        fail(f"{label} must be a non-negative numeric SVN revision, got {value!r}")
    return int(value)


def parse_authors(path):
    if not Path(path).is_file():
        fail(f"authors.txt not found: {path}")
    mappings = set()
    seen = set()
    try:
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    except UnicodeError as exc:
        fail(f"authors.txt is not valid UTF-8 ({path}): {exc}")
    for line_number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([^=]+?)\s*=\s*(.+?)\s*<([^<>]+)>", line)
        if not match:
            fail(f"{path}:{line_number}: expected 'svn-user = Full Name <email>'")
        svn_user, name, email = (value.strip() for value in match.groups())
        if svn_user in seen:
            fail(f"{path}:{line_number}: duplicate SVN author {svn_user!r}")
        seen.add(svn_user)
        mappings.add((name, email))
    if not mappings:
        fail(f"No author mappings found in {path}")
    return mappings


def git_svn_commits(repo, target_roots):
    command = [
        "git", "-C", str(repo), "log", "--all",
        "--format=%H%x00%an%x00%ae%x00%B%x00",
    ]
    error_file = tempfile.TemporaryFile()
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=error_file)
    if process.stdout is None:
        fail(f"Could not read git log output for {repo}")
    pending = b""
    fields = []
    authors = set()
    source_states = {}
    seen_revisions = {}
    max_rev = -1

    def consume(record_fields):
        nonlocal max_rev
        oid, name, email, message = record_fields
        oid = oid.strip()
        match = re.search(rb"^git-svn-id: .+@([0-9]+)(?:\s|$)", message, re.MULTILINE)
        if not match:
            fail(
                f"Commit {oid.decode('ascii', 'replace')} in {repo} has no git-svn-id; "
                "expected a complete git-svn clone"
            )
        revision = int(match.group(1))
        old_oid = seen_revisions.get(revision)
        if old_oid is not None and old_oid != oid:
            fail(f"Multiple distinct git-svn commits map to r{revision} in {repo}")
        seen_revisions[revision] = oid
        author_name = name.decode("utf-8", "replace")
        author_email = email.decode("utf-8", "replace")
        authors.add((author_name, author_email))
        max_rev = max(max_rev, revision)
        for root in target_roots:
            root_id = root["root_id"]
            if revision <= root["target_rev"]:
                old = source_states.get(root_id)
                if old is None or revision > old["rev"]:
                    source_states[root_id] = {"rev": revision, "oid": oid.decode("ascii")}

    while True:
        chunk = process.stdout.read(1024 * 1024)
        if not chunk:
            break
        chunks = (pending + chunk).split(b"\0")
        pending = chunks.pop()
        fields.extend(chunks)
        complete = len(fields) - (len(fields) % 4)
        for offset in range(0, complete, 4):
            consume(fields[offset:offset + 4])
        if complete:
            del fields[:complete]
    return_code = process.wait()
    if return_code:
        error_file.seek(0)
        fail(
            f"git log failed for {repo}: "
            f"{error_file.read().decode('utf-8', 'replace')}"
        )
    if pending.strip() or fields or max_rev < 0:
        fail(f"No git-svn commits found in {repo}")
    return max_rev, authors, source_states


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_source_metadata(repo):
    head_oid = run(["git", "-C", str(repo), "rev-parse", "HEAD"]).stdout.strip()
    refs = run(["git", "-C", str(repo), "show-ref"], check=False)
    refs_text = refs.stdout if refs.returncode == 0 else ""
    refs_sha256 = hashlib.sha256(refs_text.encode("utf-8")).hexdigest()
    object_stats = run(["git", "-C", str(repo), "count-objects", "-v"]).stdout
    object_bytes = 0
    for line in object_stats.splitlines():
        if line.startswith("size-pack: ") or line.startswith("size: "):
            object_bytes += int(line.split(":", 1)[1].strip()) * 1024
    return {
        "head_oid": head_oid,
        "refs_sha256": refs_sha256,
        "object_bytes": object_bytes,
    }


def inspect_repository(name, roots, source_dir, authors):
    repo = source_dir / name
    git_dir = run(["git", "-C", str(repo), "rev-parse", "--git-dir"], check=False)
    if git_dir.returncode:
        fail(f"Not a Git repository: {repo}")
    if run(
        ["git", "-C", str(repo), "rev-parse", "--is-shallow-repository"]
    ).stdout.strip() == "true":
        fail(f"Shallow git-svn clone is not supported: {repo}")
    remotes = run(
        ["git", "-C", str(repo), "config", "--get-regexp", r"^svn-remote\."],
        check=False,
    )
    if remotes.returncode:
        fail(f"No git-svn remote configuration found in {repo}")
    repo_roots = [root for root in roots if root["svn_repo"] == name]
    max_rev, repo_authors, source_states = git_svn_commits(repo, repo_roots)
    head_message = run(["git", "-C", str(repo), "show", "-s", "--format=%B", "HEAD"]).stdout
    head_match = re.search(
        r"^git-svn-id: .+@([0-9]+)(?:\s|$)", head_message, re.MULTILINE
    )
    if not head_match or int(head_match.group(1)) != max_rev:
        fail(f"HEAD is not the latest imported SVN revision in {repo}")
    unmapped = repo_authors - authors
    if unmapped:
        fail(
            f"{repo}: imported commit author(s) do not match authors.txt mappings: "
            + ", ".join(f"{n} <{e}>" for n, e in sorted(unmapped))
        )
    for root in repo_roots:
        if root["target_rev"] > max_rev:
            fail(f"{root['root_id']}: target r{root['target_rev']} exceeds imported HEAD r{max_rev}")
        source_state = source_states.get(root["root_id"])
        if source_state is None:
            fail(f"{root['root_id']}: no imported commit at or before r{root['target_rev']}")
        kind = run(
            [
                "git", "-C", str(repo), "cat-file", "-t",
                f"{source_state['oid']}:{root['project_root']}",
            ],
            check=False,
        )
        if kind.returncode or kind.stdout.strip() != "tree":
            fail(
                f"{root['root_id']}: project_root {root['project_root']!r} "
                f"is absent at imported r{source_state['rev']}"
            )
    return {
        **git_source_metadata(repo),
        "max_rev": max_rev,
        "source_states": source_states,
    }


def validate_config(args):
    svn_roots = read_tsv(args.svn_roots, SVN_ROOT_COLUMNS)
    if len(svn_roots) != args.expected_repos:
        fail(
            f"Expected {args.expected_repos} SVN repositories in {args.svn_roots}, "
            f"found {len(svn_roots)}"
        )
    names = [row["svn_repo"] for row in svn_roots]
    if len(set(names)) != len(names):
        fail("svn-roots.tsv contains duplicate svn_repo values")
    for name in names:
        safe_relative_path(name, "svn_repo")
        if "/" in name:
            fail(f"svn_repo must be one directory name, got {name!r}")

    roots = read_tsv(args.project_roots, ROOT_COLUMNS)
    sections = read_tsv(args.sections, SECTION_COLUMNS)
    if not roots:
        fail("project-roots.tsv has no project roots")
    root_ids = [row["root_id"] for row in roots]
    if len(set(root_ids)) != len(root_ids):
        fail("project-roots.tsv contains duplicate root_id values")
    known_repos = set(names)
    referenced_repos = set()
    roots_by_id = {}
    project_paths = {}
    ref_keys = set()
    ref_names_by_namespace = {}
    for row in roots:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", row["root_id"]):
            fail(f"root_id must contain only letters, digits, dot, underscore, or hyphen: {row['root_id']!r}")
        row["project_root"] = safe_relative_path(row["project_root"], "project_root")
        row["gitlab_project"] = safe_relative_path(row["gitlab_project"], "gitlab_project")
        if row["svn_repo"] not in known_repos:
            fail(f"{row['root_id']}: svn_repo {row['svn_repo']!r} is not in svn-roots.tsv")
        referenced_repos.add(row["svn_repo"])
        if row["ref_type"] not in ("trunk", "branch", "tag"):
            fail(f"{row['root_id']}: ref_type must be trunk, branch, or tag")
        if row["ref_type"] == "trunk" and row["ref_name"] != "main":
            fail(f"{row['root_id']}: trunk ref_name must be 'main'")
        if row["ref_type"] != "trunk" and row["ref_name"] in ("main", "HEAD"):
            fail(f"{row['root_id']}: {row['ref_type']} ref_name cannot be {row['ref_name']!r}")
        row["target_rev"] = parse_revision(row["target_rev"], f"{row['root_id']}.target_rev")
        ref_key = (row["gitlab_project"], row["ref_type"], row["ref_name"])
        if ref_key in ref_keys:
            fail(f"Duplicate target ref: {row['gitlab_project']} {row['ref_type']} {row['ref_name']}")
        ref_keys.add(ref_key)
        project_paths.setdefault(row["gitlab_project"], []).append(row)
        namespace = (row["gitlab_project"], "heads" if row["ref_type"] in ("trunk", "branch") else "tags")
        ref_names_by_namespace.setdefault(namespace, []).append(row["ref_name"])
        roots_by_id[row["root_id"]] = row
        check_ref = "refs/heads/" if row["ref_type"] in ("trunk", "branch") else "refs/tags/"
        run(["git", "check-ref-format", check_ref + row["ref_name"]])

    for (project, namespace), ref_names in ref_names_by_namespace.items():
        for index, first in enumerate(ref_names):
            for second in ref_names[index + 1:]:
                if first.startswith(second + "/") or second.startswith(first + "/"):
                    fail(
                        f"{project}: incompatible nested Git {namespace} refs "
                        f"{first!r} and {second!r}"
                    )

    unused = known_repos - referenced_repos
    if unused:
        fail(f"SVN repositories have no project-root entries: {', '.join(sorted(unused))}")
    for project, project_roots in project_paths.items():
        trunks = [row for row in project_roots if row["ref_type"] == "trunk"]
        if len(trunks) != 1:
            fail(f"{project}: expected exactly one trunk (Git main), found {len(trunks)}")

    sections_by_root = {root_id: [] for root_id in roots_by_id}
    for row in sections:
        root_id = row["root_id"]
        if root_id not in roots_by_id:
            fail(f"sections.tsv references unknown root_id {root_id!r}")
        root = roots_by_id[root_id]
        if row["svn_repo"] != root["svn_repo"]:
            fail(f"{root_id}: section svn_repo does not match project-roots.tsv")
        row["svn_path"] = safe_relative_path(row["svn_path"], "svn_path")
        row["from_rev"] = parse_revision(row["from_rev"], f"{root_id}.from_rev")
        row["to_rev"] = parse_revision(row["to_rev"], f"{root_id}.to_rev")
        if row["from_rev"] > row["to_rev"]:
            fail(f"{root_id}: section from_rev exceeds to_rev")
        if row["to_rev"] > root["target_rev"]:
            fail(f"{root_id}: section extends past target_rev")
        sections_by_root[root_id].append(row)

    for root_id, root in roots_by_id.items():
        rules = sections_by_root[root_id]
        if not rules:
            fail(f"{root_id}: no section rules")
        final_rules = [
            rule for rule in rules
            if rule["svn_path"] == root["project_root"]
            and rule["from_rev"] <= root["target_rev"] <= rule["to_rev"]
        ]
        if not final_rules:
            fail(
                f"{root_id}: project_root must be an active svn_path at target_rev "
                f"(r{root['target_rev']})"
            )
        for index, first in enumerate(rules):
            for second in rules[index + 1:]:
                overlaps = first["from_rev"] <= second["to_rev"] and second["from_rev"] <= first["to_rev"]
                paths_overlap = (
                    first["svn_path"] == second["svn_path"]
                    or first["svn_path"].startswith(second["svn_path"] + "/")
                    or second["svn_path"].startswith(first["svn_path"] + "/")
                )
                if overlaps and paths_overlap:
                    fail(
                        f"{root_id}: overlapping section rules can select the same path "
                        f"at the same revision: {first['svn_path']} and {second['svn_path']}"
                    )

    source_dir = Path(args.git_svn_dir).resolve()
    if not source_dir.is_dir():
        fail(f"git-svn directory not found: {source_dir}")
    actual_clones = {
        entry.name for entry in source_dir.iterdir()
        if entry.is_dir() and (entry / ".git").exists()
    }
    if actual_clones != known_repos:
        missing_clones = sorted(known_repos - actual_clones)
        unexpected_clones = sorted(actual_clones - known_repos)
        fail(
            "git-svn clone directory inventory differs from svn-roots.tsv."
            f" Missing: {missing_clones}; unexpected: {unexpected_clones}"
        )
    authors = parse_authors(args.authors)
    source_info = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as executor:
        futures = {
            executor.submit(inspect_repository, name, roots, source_dir, authors): name
            for name in names
        }
        for future in concurrent.futures.as_completed(futures):
            source_info[futures[future]] = future.result()

    input_files = {
        "svn_roots": str(Path(args.svn_roots).resolve()),
        "project_roots": str(Path(args.project_roots).resolve()),
        "sections": str(Path(args.sections).resolve()),
        "authors": str(Path(args.authors).resolve()),
    }
    return {
        "format_version": 1,
        "git_svn_dir": str(source_dir),
        "expected_repos": args.expected_repos,
        "svn_roots": names,
        "roots": roots,
        "sections_by_root": sections_by_root,
        "sources": source_info,
        "input_files": {
            name: {"path": path, "sha256": file_sha256(path)}
            for name, path in input_files.items()
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--svn-roots", required=True, help="TSV listing the expected SVN repositories")
    parser.add_argument("--project-roots", required=True, help="TSV declaring final roots and target refs")
    parser.add_argument("--sections", required=True, help="TSV declaring explicit path/revision lineage")
    parser.add_argument("--authors", required=True, help="git-svn authors.txt used for the source imports")
    parser.add_argument("--git-svn-dir", required=True, help="Directory containing {svn_repo} git-svn clones")
    parser.add_argument("--report", required=True, help="Output path for the validated conversion plan (JSON)")
    parser.add_argument("--expected-repos", type=int, default=39)
    parser.add_argument("--jobs", type=int, default=4, choices=range(1, 5))
    args = parser.parse_args()
    try:
        report_path = Path(args.report).resolve()
        if report_path.exists():
            fail(f"Inspection report already exists: {report_path}")
        plan = validate_config(args)
        source_dir = Path(plan["git_svn_dir"])
        if any(source_dir / name in report_path.parents for name in plan["svn_roots"]):
            fail("Inspection report must be outside source git-svn clones")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(plan, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        total_bytes = sum(source["object_bytes"] for source in plan["sources"].values())
        print(
            f"Inspection passed: {len(plan['svn_roots'])} SVN repos, "
            f"{len(plan['roots'])} roots, "
            f"{sum(len(rules) for rules in plan['sections_by_root'].values())} sections."
        )
        print(f"Indexed source object stores: {total_bytes / (1024 ** 3):.1f} GiB")
        print(f"Conversion plan: {report_path}")
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
