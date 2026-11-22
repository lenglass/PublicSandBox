
#!/usr/bin/env python3

import argparse
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import requests


class GitLabClient:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.api = f"{self.url}/api/v4"
        self.session = requests.Session()
        self.session.headers.update({
            "PRIVATE-TOKEN": token
        })

    def request(self, method, endpoint, **kwargs):
        response = self.session.request(
            method,
            f"{self.api}{endpoint}",
            timeout=30,
            **kwargs
        )

        if not response.ok:
            raise RuntimeError(
                f"GitLab API error: {method} {endpoint}\n"
                f"HTTP {response.status_code}: {response.text}"
            )

        if response.status_code == 204 or not response.content:
            return None

        return response.json()

    def get_group(self, full_path):
        encoded = quote(full_path, safe="")
        try:
            return self.request(
                "GET",
                f"/groups/{encoded}"
            )
        except RuntimeError as e:
            if "HTTP 404" in str(e):
                return None
            raise

    def create_group(self, name, path, parent_id=None):
        data = {
            "name": name,
            "path": path,
            "visibility": "private",
        }

        if parent_id is not None:
            data["parent_id"] = parent_id

        return self.request(
            "POST",
            "/groups",
            json=data
        )

    def find_or_create_group(self, full_path, name, path, parent_id=None):
        group = self.get_group(full_path)

        if group:
            print(f"[EXISTS] Group: {full_path}")
            return group

        print(f"[CREATE] Group: {full_path}")

        return self.create_group(
            name=name,
            path=path,
            parent_id=parent_id
        )

    def get_project(self, full_path):
        encoded = quote(full_path, safe="")
        try:
            return self.request(
                "GET",
                f"/projects/{encoded}"
            )
        except RuntimeError as e:
            if "HTTP 404" in str(e):
                return None
            raise

    def create_project(self, name, path, namespace_id):
        data = {
            "name": name,
            "path": path,
            "namespace_id": namespace_id,
            "visibility": "private",
            "initialize_with_readme": False,
        }

        return self.request(
            "POST",
            "/projects",
            json=data
        )

    def find_or_create_project(
        self,
        full_path,
        name,
        path,
        namespace_id
    ):
        project = self.get_project(full_path)

        if project:
            print(f"[EXISTS] Project: {full_path}")
            return project

        print(f"[CREATE] Project: {full_path}")

        return self.create_project(
            name=name,
            path=path,
            namespace_id=namespace_id
        )


def ensure_namespace(client, namespace_parts):
    """
    namespace_parts:
        ["software-development", "backend"]

    Returns:
        Last Group object
    """

    parent_id = None
    current_path = []

    group = None

    for part in namespace_parts:
        current_path.append(part)
        full_path = "/".join(current_path)

        group = client.find_or_create_group(
            full_path=full_path,
            name=part,
            path=part,
            parent_id=parent_id
        )

        parent_id = group["id"]

    return group


def run_git(repo, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Git command failed: git {' '.join(args)}\n"
            f"{result.stderr.strip()}"
        )

    return result.stdout.strip()


def push_repository(repo, ssh_url, branch=None):
    repo = Path(repo).resolve()

    if not repo.is_dir():
        raise ValueError(f"Repository not found: {repo}")

    # Gitリポジトリか確認
    run_git(repo, "rev-parse", "--git-dir")

    # originの確認
    remotes = run_git(repo, "remote").splitlines()

    if "origin" in remotes:
        print("[UPDATE] origin remote")
        run_git(repo, "remote", "set-url", "origin", ssh_url)
    else:
        print("[ADD] origin remote")
        run_git(repo, "remote", "add", "origin", ssh_url)

    # ブランチ指定がなければ現在のブランチを取得
    if branch is None:
        branch = run_git(
            repo,
            "branch",
            "--show-current"
        )

    if not branch:
        raise RuntimeError(
            "Current branch is empty. "
            "Specify --branch explicitly."
        )

    print(f"[PUSH] Branch: {branch}")

    run_git(
        repo,
        "push",
        "-u",
        "origin",
        branch
    )


def main():
    parser = argparse.ArgumentParser(
        description="Create GitLab groups/projects and push local repository."
    )

    parser.add_argument(
        "--path",
        required=True,
        help="GitLab path: group/subgroup/project"
    )

    parser.add_argument(
        "--repo",
        required=True,
        help="Local Git repository directory"
    )

    parser.add_argument(
        "--name",
        default=None,
        help="Project display name (default: project path)"
    )

    parser.add_argument(
        "--branch",
        default=None,
        help="Branch to push (default: current branch)"
    )

    args = parser.parse_args()

    gitlab_url = os.environ.get("GITLAB_URL")
    gitlab_token = os.environ.get("GITLAB_TOKEN")

    if not gitlab_url or not gitlab_token:
        parser.error(
            "GITLAB_URL and GITLAB_TOKEN must be set."
        )

    parts = args.path.strip("/").split("/")

    if len(parts) < 2:
        parser.error(
            "--path must contain at least group/project"
        )

    if any(not part or part in (".", "..") for part in parts):
        parser.error("Invalid --path")

    project_path = parts[-1]
    namespace_parts = parts[:-1]

    project_name = args.name or project_path

    client = GitLabClient(
        url=gitlab_url,
        token=gitlab_token
    )

    print("=== GitLab namespace ===")

    group = ensure_namespace(
        client,
        namespace_parts
    )

    print("=== GitLab project ===")

    project_full_path = args.path

    project = client.find_or_create_project(
        full_path=project_full_path,
        name=project_name,
        path=project_path,
        namespace_id=group["id"]
    )

    # SSH URLはGitLab APIの返却値を使用
    ssh_url = project["ssh_url_to_repo"]

    print(f"[INFO] Project: {project['web_url']}")
    print(f"[INFO] SSH URL: {ssh_url}")

    print("=== Git push ===")

    push_repository(
        repo=args.repo,
        ssh_url=ssh_url,
        branch=args.branch
    )

    print("=== DONE ===")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)
