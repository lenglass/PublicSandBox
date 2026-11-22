#!/usr/bin/env python3
"""Import reviewed SVN roots that exist at the selected final revision."""

import argparse
import csv
import hashlib
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse


FIELDS = ("project_id", "svn_path", "kind", "fetch_ref", "target_ref")
PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def fail(message):
    raise RuntimeError(message)


def run(command, *, cwd=None, log=None, check=True, binary=False):
    result = subprocess.run(
        [str(part) for part in command],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=not binary,
        errors="replace" if not binary else None,
    )
    if log is not None:
        with open(log, "a", encoding="utf-8") as stream:
            stream.write("$ " + " ".join(map(str, command)) + "\n")
            if binary:
                stream.write(f"binary output: {len(result.stdout)} bytes\n")
                stream.write(result.stderr.decode("utf-8", "replace"))
            else:
                stream.write(result.stdout)
                stream.write(result.stderr)
    if check and result.returncode:
        stdout = result.stdout if not binary else f"{len(result.stdout)} binary bytes"
        stderr = result.stderr if not binary else result.stderr.decode("utf-8", "replace")
        fail(
            f"Command failed ({result.returncode}): {' '.join(map(str, command))}\n"
            f"{stdout}{stderr}"
        )
    return result


def read_manifest(path):
    with open(path, encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream, delimiter="\t")
        if reader.fieldnames != list(FIELDS):
            fail(f"Manifest header must be: {' '.join(FIELDS)}")
        rows = list(reader)
    if not rows:
        fail("Manifest has no final SVN roots")

    by_project = {}
    for line_number, row in enumerate(rows, start=2):
        for field in FIELDS:
            if not row[field] or not row[field].strip():
                fail(f"Manifest line {line_number}: {field} is required")
            row[field] = row[field].strip()
        project_id = row["project_id"]
        if not PROJECT_ID.fullmatch(project_id):
            fail(f"Manifest line {line_number}: invalid project_id {project_id!r}")

        svn_path = row["svn_path"].replace("\\", "/").rstrip("/")
        if not svn_path or svn_path.startswith("/") or any(
            part in ("", ".", "..") for part in svn_path.split("/")
        ):
            fail(f"Manifest line {line_number}: invalid relative svn_path {svn_path!r}")
        if any(char in svn_path for char in "*?{}"):
            fail(f"Manifest line {line_number}: svn_path must be literal, not a glob")
        row["svn_path"] = svn_path

        if row["kind"] not in ("trunk", "branch", "tag"):
            fail(f"Manifest line {line_number}: invalid kind {row['kind']!r}")
        fetch_ref = row["fetch_ref"]
        target_ref = row["target_ref"]
        if not fetch_ref.startswith("refs/remotes/svn/"):
            fail(f"Manifest line {line_number}: fetch_ref must be under refs/remotes/svn/")
        for ref_name in (fetch_ref, target_ref):
            result = run(["git", "check-ref-format", ref_name], check=False)
            if result.returncode:
                fail(f"Manifest line {line_number}: invalid Git ref {ref_name!r}")
        if row["kind"] == "trunk" and target_ref != "refs/heads/main":
            fail(f"Manifest line {line_number}: trunk must map to refs/heads/main")
        if row["kind"] == "branch" and not target_ref.startswith("refs/heads/"):
            fail(f"Manifest line {line_number}: branch target_ref must be a Git branch")
        if row["kind"] == "tag" and not target_ref.startswith("refs/tags/"):
            fail(f"Manifest line {line_number}: tag target_ref must be a Git tag")

        group = by_project.setdefault(
            project_id,
            {"paths": set(), "fetch_refs": set(), "target_refs": set(), "trunks": 0},
        )
        for field, value, seen in (
            ("svn_path", svn_path, group["paths"]),
            ("fetch_ref", fetch_ref, group["fetch_refs"]),
            ("target_ref", target_ref, group["target_refs"]),
        ):
            if value in seen:
                fail(f"Manifest line {line_number}: duplicate {field} {value!r} in {project_id}")
            seen.add(value)
        if row["kind"] == "trunk":
            group["trunks"] += 1

    for project_id, group in by_project.items():
        if group["trunks"] != 1:
            fail(f"{project_id}: expected exactly one trunk mapped to refs/heads/main")
    return rows


def svn_url(root_url, path, revision):
    encoded_path = urllib.parse.quote(path, safe="/")
    return f"{root_url.rstrip('/')}/{encoded_path}@{revision}"


def verify_final_roots(root_url, rows, revision, log_dir):
    for row in rows:
        log_path = log_dir / f"{row['project_id']}.log"
        result = run(
            [
                "svn",
                "--non-interactive",
                "info",
                "--show-item",
                "kind",
                "--revision",
                revision,
                svn_url(root_url, row["svn_path"], revision),
            ],
            log=log_path,
            check=False,
        )
        if result.returncode or result.stdout.strip() != "dir":
            fail(
                f"Final root does not exist as a directory at r{revision}: "
                f"{row['svn_path']}"
            )


def snapshot_records(directory):
    records = {}
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory).as_posix()
        if path.is_dir():
            continue
        if path.is_symlink():
            records[relative] = ("symlink", os.readlink(path), 0o120000)
        else:
            mode = 0o100755 if path.stat().st_mode & 0o111 else 0o100644
            records[relative] = ("file", hashlib.sha256(path.read_bytes()).hexdigest(), mode)
    return records


def compare_ref_to_svn(repo, target_ref, root_url, svn_path, revision, scratch, log):
    with tempfile.TemporaryDirectory(prefix="svn-tree-", dir=scratch) as temporary:
        export_path = pathlib.Path(temporary) / "export"
        run(
            [
                "svn",
                "--non-interactive",
                "export",
                "--ignore-externals",
                "--quiet",
                "--revision",
                revision,
                svn_url(root_url, svn_path, revision),
                export_path,
            ],
            log=log,
        )
        svn_tree = snapshot_records(export_path)
        archive = run(
            ["git", "-C", repo, "archive", "--format=tar", target_ref],
            log=log,
            binary=True,
        )
        archive_path = pathlib.Path(temporary) / "git-tree.tar"
        archive_path.write_bytes(archive.stdout)
        git_tree = {}
        with tarfile.open(archive_path) as bundle:
            for member in bundle.getmembers():
                if not member.isfile() and not member.issym():
                    continue
                relative = member.name.rstrip("/")
                if member.issym():
                    git_tree[relative] = ("symlink", member.linkname, member.mode)
                else:
                    stream = bundle.extractfile(member)
                    mode = 0o100755 if member.mode & 0o111 else 0o100644
                    git_tree[relative] = (
                        "file",
                        hashlib.sha256(stream.read()).hexdigest(),
                        mode,
                    )
        return svn_tree == git_tree, len(svn_tree), len(git_tree)


def import_project(repo, root_url, rows, revision, authors_file, log):
    repo.mkdir()
    run(["git", "init", "--quiet", repo], log=log)
    run(["git", "-C", repo, "config", "user.name", "SVN Import"], log=log)
    run(["git", "-C", repo, "config", "user.email", "svn-import@localhost"], log=log)
    run(["git", "-C", repo, "svn", "init", root_url], log=log)
    run(
        ["git", "-C", repo, "config", "--unset-all", "svn-remote.svn.fetch"],
        log=log,
        check=False,
    )
    for row in rows:
        run(
            [
                "git",
                "-C",
                repo,
                "config",
                "--add",
                "svn-remote.svn.fetch",
                f"{row['svn_path']}:{row['fetch_ref']}",
            ],
            log=log,
        )
    run(
        ["git", "-C", repo, "config", "svn-remote.svn.followparent", "true"],
        log=log,
    )
    if authors_file:
        run(
            ["git", "-C", repo, "config", "svn.authorsfile", authors_file],
            log=log,
        )
    run(["git", "-C", repo, "svn", "fetch"], log=log)

    for row in rows:
        oid = run(
            ["git", "-C", repo, "rev-parse", "--verify", f"{row['fetch_ref']}^{{commit}}"],
            log=log,
        ).stdout.strip()
        run(["git", "-C", repo, "update-ref", row["target_ref"], oid], log=log)
    run(
        ["git", "-C", repo, "symbolic-ref", "HEAD", "refs/heads/main"],
        log=log,
    )
    run(["git", "-C", repo, "fsck", "--connectivity-only"], log=log)


def main():
    parser = argparse.ArgumentParser(
        description="Import only manifest roots that exist at the selected final SVN revision."
    )
    parser.add_argument("--svn-url", required=True, help="SVN repository root URL")
    parser.add_argument("--manifest", required=True, type=pathlib.Path)
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--output", required=True, type=pathlib.Path)
    parser.add_argument("--authors-file", type=pathlib.Path)
    args = parser.parse_args()

    rows = read_manifest(args.manifest)
    if args.authors_file and not args.authors_file.is_file():
        fail(f"Authors file not found: {args.authors_file}")
    output = args.output.expanduser().absolute()
    if output.is_symlink():
        fail(f"Output directory must not be a symlink: {output}")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        fail(f"Output directory must be empty or absent: {output}")
    output.mkdir(parents=True, exist_ok=True)
    log_dir = output / "logs"
    log_dir.mkdir()
    scratch = output / ".verification"
    scratch.mkdir()
    shutil.copy2(args.manifest, output / "input-manifest.tsv")
    if args.authors_file:
        shutil.copy2(args.authors_file, output / "authors.txt")

    verify_final_roots(args.svn_url, rows, args.revision, log_dir)
    projects = {}
    for row in rows:
        projects.setdefault(row["project_id"], []).append(row)

    failures = []
    summary = []
    for project_id, project_rows in projects.items():
        repo = output / project_id
        log = log_dir / f"{project_id}.log"
        try:
            import_project(
                repo,
                args.svn_url.rstrip("/"),
                project_rows,
                args.revision,
                str(args.authors_file.resolve()) if args.authors_file else None,
                log,
            )
            for row in project_rows:
                matches, svn_count, git_count = compare_ref_to_svn(
                    repo,
                    row["target_ref"],
                    args.svn_url.rstrip("/"),
                    row["svn_path"],
                    args.revision,
                    scratch,
                    log,
                )
                summary.append(
                    {
                        "project_id": project_id,
                        "svn_path": row["svn_path"],
                        "kind": row["kind"],
                        "target_ref": row["target_ref"],
                        "tree_match": matches,
                        "svn_files": svn_count,
                        "git_files": git_count,
                    }
                )
                if not matches:
                    fail(
                        f"Tree mismatch for {project_id}:{row['svn_path']} "
                        f"at r{args.revision} (SVN={svn_count}, Git={git_count})"
                    )
        except Exception as error:
            failures.append(f"{project_id}: {error}")
            print(f"ERROR: {failures[-1]}", file=sys.stderr)

    shutil.rmtree(scratch)
    summary_path = output / "conversion-summary.tsv"
    with open(summary_path, "w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=(
                "project_id",
                "svn_path",
                "kind",
                "target_ref",
                "tree_match",
                "svn_files",
                "git_files",
            ),
            delimiter="\t",
        )
        writer.writeheader()
        writer.writerows(summary)
    if failures:
        fail(
            "One or more projects failed. Partial output and logs were preserved:\n"
            + "\n".join(failures)
        )
    print(f"Imported {len(projects)} final-state projects into {output}")
    print(f"Tree checks: {summary_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Conversion failed: {error}", file=sys.stderr)
        raise SystemExit(1)
