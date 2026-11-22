#!/usr/bin/env python3
"""Build deterministic GitLab-ready repositories from existing git-svn clones."""

import argparse
import json
import os
import re
import shutil
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


def git_svn_commits(repo):
    result = run(["git", "-C", str(repo), "log", "--all", "--format=%H%x1f%an%x1f%ae%x1f%B%x1e"])
    commits = []
    authors = set()
    for record in result.stdout.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        fields = record.split("\x1f", 3)
        if len(fields) != 4:
            fail(f"Could not parse git log metadata in {repo}")
        oid, name, email, message = fields
        match = re.search(r"^git-svn-id: .+@([0-9]+)(?:\s|$)", message, re.MULTILINE)
        if not match:
            fail(f"Commit {oid} in {repo} has no git-svn-id; expected a complete git-svn clone")
        commits.append({"oid": oid, "rev": int(match.group(1)), "name": name, "email": email})
        authors.add((name, email))
    if not commits:
        fail(f"No git-svn commits found in {repo}")
    return commits, authors


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
    output_dir = Path(args.output).resolve()
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
    commits_by_repo = {}
    for name in names:
        repo = source_dir / name
        if not repo.is_dir():
            fail(f"git-svn clone not found: {repo}")
        git_dir = run(["git", "-C", str(repo), "rev-parse", "--git-dir"], check=False)
        if git_dir.returncode:
            fail(f"Not a Git repository: {repo}")
        shallow = run(
            ["git", "-C", str(repo), "rev-parse", "--is-shallow-repository"]
        ).stdout.strip()
        if shallow == "true":
            fail(f"Shallow git-svn clone is not supported: {repo}")
        remotes = run(["git", "-C", str(repo), "config", "--get-regexp", r"^svn-remote\."], check=False)
        if remotes.returncode:
            fail(f"No git-svn remote configuration found in {repo}")
        commits, repo_authors = git_svn_commits(repo)
        unmapped = repo_authors - authors
        if unmapped:
            fail(
                f"{repo}: imported commit author(s) do not match authors.txt mappings: "
                + ", ".join(f"{n} <{e}>" for n, e in sorted(unmapped))
            )
        commits_by_repo[name] = commits
        max_rev = max(item["rev"] for item in commits)
        for root in roots:
            if root["svn_repo"] != name:
                continue
            if root["target_rev"] > max_rev:
                fail(f"{root['root_id']}: target r{root['target_rev']} exceeds imported HEAD r{max_rev}")
            eligible = [commit for commit in commits if commit["rev"] <= root["target_rev"]]
            if not eligible:
                fail(f"{root['root_id']}: no imported commit at or before r{root['target_rev']}")
            source_state = max(eligible, key=lambda commit: commit["rev"])
            listed = run(
                [
                    "git", "-C", str(repo), "ls-tree", "-r", "--name-only",
                    source_state["oid"], "--", root["project_root"],
                ]
            ).stdout.strip()
            if not listed:
                fail(
                    f"{root['root_id']}: project_root {root['project_root']!r} "
                    f"is absent at imported r{source_state['rev']}"
                )

    if output_dir.exists() and any(output_dir.iterdir()):
        fail(f"Output directory is not empty: {output_dir}")
    if output_dir.is_symlink():
        fail(f"Output directory must not be a symlink: {output_dir}")
    if output_dir.exists() and not output_dir.is_dir():
        fail(f"Output path is not a directory: {output_dir}")
    for name in names:
        repo_path = (source_dir / name).resolve()
        if output_dir == repo_path or output_dir in repo_path.parents or repo_path in output_dir.parents:
            fail("Output directory must not overlap a source git-svn clone")
    if source_dir == output_dir or source_dir in output_dir.parents:
        fail("Output directory must be outside the source git-svn directory")

    return {
        "svn_roots": names,
        "roots": roots,
        "sections_by_root": sections_by_root,
        "commits_by_repo": commits_by_repo,
        "output_dir": output_dir,
    }


def callback_body():
    return r'''import json, os, re
with open(os.environ["SVN_PROJECT_RULES"], "r", encoding="utf-8") as f:
    _rules = json.load(f)
_match = re.search(rb"git-svn-id: [^\r\n]*@([0-9]+)(?:[ \r\n]|$)", commit.message)
if not _match:
    raise RuntimeError("Cannot read git-svn revision from commit message: %r" % commit.message[:200])
_rev = int(_match.group(1))
_selected = {}
if _rev <= _rules["target_rev"]:
    for _change in commit.file_changes:
        _filename = _change.filename
        _matches = []
        for _rule in _rules["sections"]:
            if _rule["from_rev"] <= _rev <= _rule["to_rev"]:
                _prefix = _rule["svn_path"].encode("utf-8").rstrip(b"/")
                if _filename == _prefix or _filename.startswith(_prefix + b"/"):
                    _matches.append(_prefix)
        if len(_matches) > 1:
            raise RuntimeError("Overlapping section prefixes for r%d path %r" % (_rev, _filename))
        if not _matches:
            continue
        _prefix = _matches[0]
        _relative = _filename[len(_prefix):].lstrip(b"/")
        if not _relative:
            raise RuntimeError("Project root itself is a file at r%d: %r" % (_rev, _filename))
        _previous = _selected.get(_relative)
        if _previous is None or (_previous.type == b"D" and _change.type != b"D"):
            _selected[_relative] = _change
        elif _previous.type != b"D" and _change.type == b"D":
            pass
        elif _previous.type == b"D" and _change.type == b"D":
            pass
        else:
            raise RuntimeError("Two non-delete SVN paths map to the same Git path at r%d: %r" % (_rev, _relative))
for _relative, _change in _selected.items():
    _change.filename = _relative
commit.file_changes = list(_selected.values())
'''


def select_tip(repo, target_rev):
    selected = None
    selected_rev = -1
    records = run(
        ["git", "-C", str(repo), "log", "--all", "--format=%H%x1f%B%x1e"]
    ).stdout
    for record in records.split("\x1e"):
        if not record.strip():
            continue
        oid, message = record.strip("\n").split("\x1f", 1)
        match = re.search(r"^git-svn-id: .+@([0-9]+)(?:\s|$)", message, re.MULTILINE)
        if match and selected_rev < int(match.group(1)) <= target_rev:
            selected = oid
            selected_rev = int(match.group(1))
    if selected is None:
        fail(f"No retained project commits at or before target revision r{target_rev}")
    return selected, selected_rev


def tree_entries(repo, commit, prefix=""):
    result = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "-z", "--full-tree", commit],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        fail(f"git ls-tree failed for {repo} at {commit}: {result.stderr.decode('utf-8', 'replace')}")
    entries = {}
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        metadata, path = record.split(b"\t", 1)
        mode, object_type, oid = metadata.split(b" ", 2)
        if object_type != b"blob":
            fail(f"Unsupported Git tree entry at {path!r}: {object_type!r}")
        if prefix:
            encoded_prefix = prefix.encode("utf-8") + b"/"
            if not path.startswith(encoded_prefix):
                continue
            path = path[len(encoded_prefix):]
        entries[path] = (mode, oid)
    return entries


def convert(args):
    config = validate_config(args)
    print(
        f"Preflight passed: {len(config['svn_roots'])} SVN repos, "
        f"{len(config['roots'])} project roots, "
        f"{sum(len(rules) for rules in config['sections_by_root'].values())} lineage sections."
    )
    if args.check:
        print("Check only: no output repositories created.")
        return
    if run(["git", "filter-repo", "--version"], check=False).returncode:
        fail("git-filter-repo is unavailable")

    output = config["output_dir"]
    output.mkdir(parents=True, exist_ok=True)
    marker = output / ".svn-project-conversion-output"
    marker.write_text("generated by convert-svn-projects.sh\n", encoding="utf-8")
    projects = {}
    for root in config["roots"]:
        projects.setdefault(root["gitlab_project"], []).append(root)
    callback = callback_body()
    with tempfile.TemporaryDirectory(prefix="svn-project-convert-") as temp_name:
        temp_root = Path(temp_name)
        for project_path, project_roots in projects.items():
            destination = Path(str(output.joinpath(*project_path.split("/"))) + ".git")
            destination.parent.mkdir(parents=True, exist_ok=True)
            run(["git", "init", "--bare", "--initial-branch=main", str(destination)])
            for root in project_roots:
                root_id = root["root_id"]
                source = Path(args.git_svn_dir).resolve() / root["svn_repo"]
                temp_repo = temp_root / root_id
                rules_path = temp_root / f"{root_id}.json"
                rule_data = {
                    "target_rev": root["target_rev"],
                    "sections": [
                        {
                            "svn_path": section["svn_path"],
                            "from_rev": section["from_rev"],
                            "to_rev": section["to_rev"],
                        }
                        for section in config["sections_by_root"][root_id]
                    ],
                }
                rules_path.write_text(json.dumps(rule_data), encoding="utf-8")
                run(["git", "clone", "--no-hardlinks", str(source), str(temp_repo)])
                env = os.environ.copy()
                env["SVN_PROJECT_RULES"] = str(rules_path)
                run(
                    [
                        "git", "-C", str(temp_repo), "filter-repo", "--force",
                        "--prune-empty", "always", "--commit-callback", callback,
                    ],
                    env=env,
                )
                tip, tip_rev = select_tip(temp_repo, root["target_rev"])
                source_commits = config["commits_by_repo"][root["svn_repo"]]
                eligible = [
                    commit for commit in source_commits if commit["rev"] <= root["target_rev"]
                ]
                if not eligible:
                    fail(f"{root_id}: no imported commit at or before r{root['target_rev']}")
                source_state = max(eligible, key=lambda commit: commit["rev"])
                expected_tree = tree_entries(
                    source, source_state["oid"], root["project_root"]
                )
                converted_tree = tree_entries(temp_repo, tip)
                if converted_tree != expected_tree:
                    fail(
                        f"{root_id}: converted tree at r{tip_rev} differs from "
                        f"{root['project_root']} at imported r{source_state['rev']}"
                    )
                branch_ref = "refs/heads/__converted"
                run(["git", "-C", str(temp_repo), "update-ref", branch_ref, tip])
                run(["git", "-C", str(temp_repo), "symbolic-ref", "HEAD", branch_ref])
                for ref in run(
                    ["git", "-C", str(temp_repo), "for-each-ref", "--format=%(refname)"]
                ).stdout.splitlines():
                    if ref != branch_ref:
                        run(["git", "-C", str(temp_repo), "update-ref", "-d", ref])

                if root["ref_type"] in ("trunk", "branch"):
                    target_ref = "refs/heads/" + root["ref_name"]
                    run(
                        [
                            "git", "--git-dir", str(destination), "fetch", "--no-tags",
                            str(temp_repo), branch_ref + ":" + target_ref,
                        ]
                    )
                else:
                    target_ref = "refs/tags/" + root["ref_name"]
                    run(
                        [
                            "git", "--git-dir", str(destination), "fetch", "--no-tags",
                            str(temp_repo), branch_ref + ":" + target_ref,
                        ]
                    )
                print(
                    f"  {project_path}: {root['ref_type']} {root['ref_name']} "
                    f"from {root['svn_repo']} (tip r{tip_rev}, target r{root['target_rev']})"
                )
            run(["git", "--git-dir", str(destination), "symbolic-ref", "HEAD", "refs/heads/main"])
            run(["git", "--git-dir", str(destination), "fsck", "--no-reflogs"])
    print(f"Conversion complete: {output}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--svn-roots", required=True, help="TSV listing the expected SVN repositories")
    parser.add_argument("--project-roots", required=True, help="TSV declaring final roots and target refs")
    parser.add_argument("--sections", required=True, help="TSV declaring explicit path/revision lineage")
    parser.add_argument("--authors", required=True, help="git-svn authors.txt used for the source imports")
    parser.add_argument("--git-svn-dir", required=True, help="Directory containing {svn_repo} git-svn clones")
    parser.add_argument("--output", required=True, help="Destination for GitLab-ready bare repositories")
    parser.add_argument("--expected-repos", type=int, default=39)
    parser.add_argument("--check", action="store_true", help="Validate inputs without creating output")
    args = parser.parse_args()
    try:
        convert(args)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
