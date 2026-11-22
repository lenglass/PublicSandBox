#!/usr/bin/env python3
"""Convert a validated inspection plan into GitLab-ready repositories."""

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from contextlib import ExitStack
from pathlib import Path


def fail(message):
    # 変換は「推測して続行」すると別プロジェクトの履歴を混ぜるため、異常は必ず停止にする。
    raise RuntimeError(message)


def run(command, *, check=True, env=None):
    # shellを介さず引数配列で実行し、パス名やメッセージをシェル解釈させない。
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
        errors="replace",
    )
    if check and result.returncode:
        # stderrだけでなくstdoutも残し、git-filter-repo等の失敗原因を診断可能にする。
        fail(
            f"Command failed ({result.returncode}): {' '.join(map(str, command))}\n"
            f"{result.stdout}{result.stderr}"
        )
    return result


def file_sha256(path):
    # 検査後にTSVやauthorsが差し替わっていないか確認する。大きな入力でも全読込しない。
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_plan(plan_path, output_path, work_path):
    # 変換は検査済みplanだけを受け取る。フォーマット判断をこの段階で再発明しない。
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"Could not read inspection plan {plan_path}: {exc}")
    # planの構造が変わったのに古い変換器で処理する事故を避ける。
    if plan.get("format_version") != 1:
        fail(f"Unsupported inspection plan version: {plan.get('format_version')!r}")

    # TSV/authorsが検査後に変更されたら、planと入力の食い違いを許さず止める。
    for input_info in plan["input_files"].values():
        path = Path(input_info["path"])
        if not path.is_file() or file_sha256(path) != input_info["sha256"]:
            fail(f"Input file changed since inspection: {path}")

    # git-svn cloneの追加・欠落も履歴設定の変更と同じく、再検査を要求する。
    source_dir = Path(plan["git_svn_dir"]).resolve()
    if not source_dir.is_dir():
        fail(f"git-svn directory not found: {source_dir}")
    expected_repos = set(plan["svn_roots"])
    actual_repos = {
        entry.name for entry in source_dir.iterdir()
        if entry.is_dir() and (entry / ".git").exists()
    }
    if expected_repos != actual_repos:
        fail("Source clone inventory changed since inspection")
    # HEADとrefの指す先が変わっていないことを確認する。objectの再packだけなら影響しない。
    for name in plan["svn_roots"]:
        repo = source_dir / name
        expected = plan["sources"][name]
        head = run(["git", "-C", str(repo), "rev-parse", "HEAD"]).stdout.strip()
        if head != expected["head_oid"]:
            fail(f"Source clone HEAD changed since inspection: {repo}")
        refs = run(["git", "-C", str(repo), "show-ref"], check=False)
        refs_text = refs.stdout if refs.returncode == 0 else ""
        refs_hash = hashlib.sha256(refs_text.encode("utf-8")).hexdigest()
        if refs_hash != expected["refs_sha256"]:
            fail(f"Source clone refs changed since inspection: {repo}")
        if run(
            ["git", "-C", str(repo), "rev-parse", "--is-shallow-repository"]
        ).stdout.strip() == "true":
            fail(f"Shallow git-svn clone is not supported: {repo}")

    # symlink経由の意図しない上書きや、検査対象clone内への一時ファイル作成を防ぐ。
    if output_path.is_symlink() or work_path.is_symlink():
        fail("Output and temporary work directories must not be symlinks")
    output = output_path.resolve()
    work_dir = work_path.resolve()
    if (output.exists() and not output.is_dir()) or (work_dir.exists() and not work_dir.is_dir()):
        fail(f"Output must be a real directory path: {output}")
    # 既存データを誤って混ぜたり上書きしたりしないよう、出力先は空だけ許す。
    if output.exists() and any(output.iterdir()):
        fail(f"Output directory is not empty: {output}")
    # source cloneと出力先を重ねると、filter-repoや出力作成が入力を壊し得る。
    if source_dir == output or source_dir in output.parents or output in source_dir.parents:
        fail("Output directory must not overlap source git-svn clones")
    # 作業用cloneは一時削除するので、source cloneの中には置かない。
    if source_dir == work_dir or source_dir in work_dir.parents:
        fail("Temporary work directory must not be inside a source git-svn clone")
    # scratchがoutput内だと部分失敗時に出力物と一時物を区別できなくなる。
    if output == work_dir or output in work_dir.parents:
        fail("Temporary work directory must be outside the output directory")
    return plan, source_dir, output, work_dir


def callback_body():
    # git-filter-repoはcommit単位のcallbackを受け取るため、その仕様のPythonコードを文字列で渡す。
    # 実体のpath/revision判断は検査済みplanから作るrules.jsonだけに基づく。
    return r'''import json, os, re
with open(os.environ["SVN_PROJECT_RULES"], "r", encoding="utf-8") as f:
    _rules = json.load(f)
# git-svn-idにあるSVN revisionを使う。Gitのcommit順やメッセージ日時から推測しない。
_match = re.search(rb"git-svn-id: [^\r\n]*@([0-9]+)(?:[ \r\n]|$)", commit.message)
# revisionが読めないcommitはsections.tsvに照合できないため、捨てずに失敗させる。
if not _match:
    raise RuntimeError("Cannot read git-svn revision from commit message: %r" % commit.message[:200])
_rev = int(_match.group(1))
_selected = {}
# target_revより後の変更はrefの状態に属さないため、空変更にして履歴から落とす。
if _rev <= _rules["target_rev"]:
    for _change in commit.file_changes:
        _filename = _change.filename
        _matches = []
        # 変更が対象になるのは、revision範囲内かつ宣言済みのpath prefixだけ。
        for _rule in _rules["sections"]:
            if _rule["from_rev"] <= _rev <= _rule["to_rev"]:
                _prefix = _rule["svn_path"].encode("utf-8").rstrip(b"/")
                if _filename == _prefix or _filename.startswith(_prefix + b"/"):
                    _matches.append(_prefix)
        # 2つのsectionが同じファイルに一致するなら優先順位を推測せず、設定誤りとして止める。
        if len(_matches) > 1:
            raise RuntimeError("Overlapping section prefixes for r%d path %r" % (_rev, _filename))
        if not _matches:
            continue
        _prefix = _matches[0]
        _relative = _filename[len(_prefix):].lstrip(b"/")
        # project-root自体をファイルとして変換するルールは、root正規化と両立しない。
        if not _relative:
            raise RuntimeError("Project root itself is a file at r%d: %r" % (_rev, _filename))
        _previous = _selected.get(_relative)
        # 親directory moveでは旧pathの削除と新pathの追加が同じGit pathになる。
        # その場合だけ、削除を追加/変更で置き換える。
        if _previous is None or (_previous.type == b"D" and _change.type != b"D"):
            _selected[_relative] = _change
        elif _previous.type != b"D" and _change.type == b"D":
            # 新path側が既に選ばれているので旧path由来の削除は捨てる。
            pass
        elif _previous.type == b"D" and _change.type == b"D":
            # 同じ正規化pathへの削除が重なっても結果は削除のまま。
            pass
        else:
            # 2つの実体変更が同じGit pathへ衝突する場合、どちらかを黙って選ばない。
            raise RuntimeError("Two non-delete SVN paths map to the same Git path at r%d: %r" % (_rev, _relative))
for _relative, _change in _selected.items():
    # path prefixを剥がして、選択したプロジェクトをGit repository rootへ正規化する。
    _change.filename = _relative
# 対象外変更も含めてcallbackへ返すと他プロジェクトが混ざるため、選択分だけに置換する。
commit.file_changes = list(_selected.values())
'''


def select_tip(repo, target_rev):
    # filter-repoは無関係なcommitをpruneするため、元のrN commitがそのまま残るとは限らない。
    # logをchunk単位で読むことで、履歴全件をPythonメモリへ載せない。
    process = subprocess.Popen(
        ["git", "-C", str(repo), "log", "--all", "--format=%H%x00%B%x00"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    pending = b""
    fields = []
    selected = None
    selected_rev = -1
    while True:
        # NUL区切りにしてcommit message内の改行を安全に扱い、分割途中の末尾だけ保持する。
        chunk = process.stdout.read(1024 * 1024)
        if not chunk:
            break
        parts = (pending + chunk).split(b"\0")
        pending = parts.pop()
        fields.extend(parts)
        complete = len(fields) - (len(fields) % 2)
        for offset in range(0, complete, 2):
            oid = fields[offset].strip()
            message = fields[offset + 1]
            match = re.search(rb"^git-svn-id: .+@([0-9]+)(?:\s|$)", message, re.MULTILINE)
            # logの並び順に依存せず、target_rev以下で最大のSVN revisionを選ぶ。
            if match and selected_rev < int(match.group(1)) <= target_rev:
                selected = oid.decode("ascii")
                selected_rev = int(match.group(1))
        if complete:
            del fields[:complete]
    error = process.stderr.read()
    if process.wait():
        fail(f"git log failed for filtered repository {repo}: {error.decode('utf-8', 'replace')}")
    # 出力が途中で切れていたり、対象commitが1つもなければ空repoで続行しない。
    if selected is None or pending.strip() or fields:
        fail(f"No retained project commits at or before target revision r{target_rev}")
    return selected, selected_rev


def tree_records(process, prefix):
    # 巨大なproject treeも全ファイル一覧をメモリ保持せず、ls-treeのNUL区切りを順次返す。
    encoded_prefix = prefix.encode("utf-8") + b"/" if prefix else b""
    pending = b""
    while True:
        chunk = process.stdout.read(1024 * 1024)
        if not chunk:
            break
        records = (pending + chunk).split(b"\0")
        pending = records.pop()
        for record in records:
            metadata, path = record.split(b"\t", 1)
            mode, object_type, oid = metadata.split(b" ", 2)
            if object_type != b"blob":
                fail(f"Unsupported Git tree entry at {path!r}: {object_type!r}")
            if encoded_prefix:
                # source側のprefixだけ剥がし、Git root直下のpathと1件ずつ比較できる形にする。
                if not path.startswith(encoded_prefix):
                    fail(f"Unexpected path outside {prefix!r}: {path!r}")
                path = path[len(encoded_prefix):]
            yield path, mode, oid
    if pending:
        fail("Malformed non-NUL-terminated git ls-tree output")


def compare_trees(source_repo, source_commit, prefix, filtered_repo, filtered_commit):
    # project rootの中身だけをliteral pathspecで比較し、巨大なrepo全体を走査しない。
    source_error = tempfile.TemporaryFile()
    filtered_error = tempfile.TemporaryFile()
    source_command = [
        "git", "-C", str(source_repo), "ls-tree", "-r", "-z", "--full-tree", source_commit,
        "--", f":(literal){prefix}",
    ]
    filtered_command = [
        "git", "-C", str(filtered_repo), "ls-tree", "-r", "-z", "--full-tree", filtered_commit,
    ]
    processes = [
        # stderrを一時fileへ逃がし、stdoutを順次比較中にpipeが詰まるのを避ける。
        subprocess.Popen(source_command, stdout=subprocess.PIPE, stderr=source_error),
        subprocess.Popen(filtered_command, stdout=subprocess.PIPE, stderr=filtered_error),
    ]
    try:
        expected = tree_records(processes[0], prefix)
        actual = tree_records(processes[1], "")
        while True:
            # 同じ順序のGit treeをストリーム比較する。最初の差分で変換を失敗扱いにする。
            expected_entry = next(expected, None)
            actual_entry = next(actual, None)
            if expected_entry is None and actual_entry is None:
                break
            if expected_entry != actual_entry:
                fail(
                    f"Snapshot mismatch for {prefix!r}: "
                    f"source={expected_entry!r}, converted={actual_entry!r}"
                )
        for process, error_file, label in (
            (processes[0], source_error, "source"),
            (processes[1], filtered_error, "converted"),
        ):
            if process.wait():
                error_file.seek(0)
                fail(
                    f"git ls-tree failed for {label} repository: "
                    f"{error_file.read().decode('utf-8', 'replace')}"
                )
    except Exception:
        # 差分や読み込み失敗時に子gitを残さない。scratchは後段で一時directoryごと消える。
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            process.wait()
        raise
    finally:
        source_error.close()
        filtered_error.close()


def object_store_bytes(repo):
    # Gitのpacked/loose object容量をscratch見積りと実測表示に使う。
    output = run(["git", "-C", str(repo), "count-objects", "-v"]).stdout
    total = 0
    for line in output.splitlines():
        if line.startswith("size-pack: ") or line.startswith("size: "):
            total += int(line.split(":", 1)[1].strip()) * 1024
    return total


class ScratchBudget:
    def __init__(self, budget_bytes, work_dir, output_dir):
        # 予約値はsource object容量による概算。実使用量の上限ではない。
        self.budget_bytes = max(1, budget_bytes)
        self.work_dir = work_dir
        self.output_dir = output_dir
        self.used_bytes = 0
        self.condition = threading.Condition()

    def acquire(self, estimate_bytes):
        # 空き容量の80%を全jobで共有する。残り20%はGitの一時増加などの余白にする。
        reservation = min(max(1, estimate_bytes), self.budget_bytes)
        with self.condition:
            while True:
                free_bytes = min(
                    shutil.disk_usage(self.work_dir).free,
                    shutil.disk_usage(self.output_dir).free,
                )
                current_budget = min(self.budget_bytes, int(free_bytes * 0.8))
                # 見積り超過の巨大repoも単独なら実行可能にし、並列時だけ待たせる。
                if not self.used_bytes or self.used_bytes + reservation <= current_budget:
                    break
                self.condition.wait()
            self.used_bytes += reservation
        return reservation

    def release(self, reservation):
        with self.condition:
            self.used_bytes -= reservation
            # 完了jobの枠を返し、容量待ちの別jobを起こす。
            self.condition.notify_all()


def convert_one_inner(root, plan, source_dir, output, work_dir, callback):
    project = root["gitlab_project"]
    # 1 rootずつ使い捨てscratchを分け、成功・失敗どちらでもTemporaryDirectoryが掃除する。
    temp_prefix = re.sub(r"[^A-Za-z0-9._-]", "_", root["root_id"])
    with tempfile.TemporaryDirectory(prefix=f"svn-project-{temp_prefix}-", dir=work_dir) as temp_name:
        temp_root = Path(temp_name)
        temp_repo = temp_root / "filtered.git"
        rules_path = temp_root / "rules.json"
        rules = {
            "target_rev": root["target_rev"],
            "sections": plan["sections_by_root"][root["root_id"]],
        }
        rules_path.write_text(json.dumps(rules), encoding="utf-8")
        source = source_dir / root["svn_repo"]

        # checkoutもobject複製も避ける。数十GBの元cloneをもう一組作らないため。
        run(["git", "clone", "--shared", "--no-checkout", str(source), str(temp_repo)])
        alternates = temp_repo / ".git" / "objects" / "info" / "alternates"
        if not alternates.is_file():
            fail(f"Shared object reference missing after clone: {temp_repo}")
        copied_source_bytes = object_store_bytes(temp_repo)
        if copied_source_bytes:
            fail(
                f"Shared clone unexpectedly copied {copied_source_bytes} bytes "
                f"of source objects: {temp_repo}"
            )

        # 想定外のGit設定による通常cloneへの退行を検知し、容量を使い切る前に止める。
        env = os.environ.copy()
        env["SVN_PROJECT_RULES"] = str(rules_path)
        # 宣言済みpath/revisionだけ残して履歴を抽出し、空commitは履歴から除く。
        run(
            [
                "git", "-C", str(temp_repo), "filter-repo", "--force",
                "--prune-empty", "always", "--commit-callback", callback,
            ],
            env=env,
        )
        # 抽出後のtip treeがSVN上の指定revisionのsubtreeと一致することを必ず検証する。
        tip, tip_rev = select_tip(temp_repo, root["target_rev"])
        source_state = plan["sources"][root["svn_repo"]]["source_states"][root["root_id"]]
        compare_trees(
            source,
            source_state["oid"],
            root["project_root"],
            temp_repo,
            tip,
        )

        # 検証を通ったrefだけを最終bare repoへ登録する。中間履歴はscratchに留める。
        destination = Path(str(output.joinpath(*project.split("/"))) + ".git")
        target_ref = (
            "refs/tags/" if root["ref_type"] == "tag" else "refs/heads/"
        ) + root["ref_name"]
        run(
            [
                "git", "--git-dir", str(destination), "fetch", "--no-tags",
                str(temp_repo), f"{tip}:{target_ref}",
            ]
        )
        scratch_bytes = object_store_bytes(temp_repo)
        print(
            f"  {project}: {root['ref_type']} {root['ref_name']} "
            f"from {root['svn_repo']} (tip r{tip_rev}; "
            f"filtered scratch {scratch_bytes / (1024 ** 3):.2f} GiB)"
        )


def convert_one(root, plan, source_dir, output, work_dir, locks, callback, scratch_budget):
    lock_names = sorted((f"source:{root['svn_repo']}", f"project:{root['gitlab_project']}"))
    # 同じsourceの並行filterや同じprojectへの同時ref書き込みを防ぐ。
    # 名前順にlockを取ることで、異なるsourceが同一projectを共有する場合もdeadlockしない。
    with ExitStack() as stack:
        for name in lock_names:
            stack.enter_context(locks[name])
        estimate = plan["sources"][root["svn_repo"]]["object_bytes"]
        # 同一source/projectは変換自体を直列化する。異なる組み合わせは容量待ち中も進める。
        reservation = scratch_budget.acquire(estimate)
        try:
            convert_one_inner(root, plan, source_dir, output, work_dir, callback)
        finally:
            scratch_budget.release(reservation)


def convert(args):
    # 事前検査で確定した入力と出力境界を受け取り、変換中に系譜を推測しない。
    plan_path = Path(args.plan).resolve()
    output_arg = Path(args.output)
    output, work_arg = output_arg, Path(args.work_dir) if args.work_dir else output_arg.parent
    plan, source_dir, output, work_dir = verify_plan(plan_path, output, work_arg)
    output.parent.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    if run(["git", "filter-repo", "--version"], check=False).returncode:
        fail("git-filter-repo is unavailable")

    # markerと空のbare repo群を先に作る。複数refが同じprojectへ来ても初期化は一度だけ。
    output.mkdir(parents=True, exist_ok=True)
    (output / ".svn-project-conversion-output").write_text(
        "generated by convert-svn-projects.sh\n", encoding="utf-8"
    )
    projects = {}
    for root in plan["roots"]:
        projects.setdefault(root["gitlab_project"], []).append(root)
    destinations = {}
    for project in projects:
        destination = Path(str(output.joinpath(*project.split("/"))) + ".git")
        destination.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "init", "--bare", "--initial-branch=main", str(destination)])
        destinations[project] = destination
    # source/projectごとに直列化する。入れ子で再取得しないため通常のLockで足りる。
    locks = {}
    for root in plan["roots"]:
        locks.setdefault(f"source:{root['svn_repo']}", threading.Lock())
        locks.setdefault(f"project:{root['gitlab_project']}", threading.Lock())
    source_sizes = [
        plan["sources"][name]["object_bytes"] for name in plan["svn_roots"]
    ]
    # jobs件のsource-size上位値は初期の容量判断用。実行中はfree-spaceを再確認する。
    concurrent_bound = sum(sorted(source_sizes, reverse=True)[:args.jobs])
    free_bytes = shutil.disk_usage(work_dir).free
    output_free_bytes = shutil.disk_usage(output.parent).free
    scratch_capacity = int(min(free_bytes, output_free_bytes) * 0.8)
    scratch_budget = ScratchBudget(scratch_capacity, work_dir, output.parent)
    print(
        f"Source objects: {sum(source_sizes) / (1024 ** 3):.1f} GiB total; "
        f"temporary clones use shared object stores (no full clone copy)."
    )
    print(
        f"Scratch filesystem free: {free_bytes / (1024 ** 3):.1f} GiB; "
        f"output filesystem free: {output_free_bytes / (1024 ** 3):.1f} GiB; "
        f"source-size estimate for {args.jobs} concurrent jobs: "
        f"{concurrent_bound / (1024 ** 3):.1f} GiB."
    )
    print(
        f"Scratch admission budget (80% of currently free space): "
        f"{scratch_capacity / (1024 ** 3):.1f} GiB."
    )

    callback = callback_body()
    # 別source/projectだけ並列化し、完了順に例外を拾って全体を失敗にする。
    futures = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as executor:
        for root in plan["roots"]:
            future = executor.submit(
                convert_one, root, plan, source_dir, output, work_dir, locks, callback,
                scratch_budget,
            )
            futures[future] = root["root_id"]
        try:
            for future in concurrent.futures.as_completed(futures):
                future.result()
        except Exception:
            # 未開始jobは止める。実行中jobは一時directoryを自分で片付けてから終了する。
            for future in futures:
                future.cancel()
            raise

    # bare repoごとに既定branchとobject graphを確認してから成功を返す。
    for destination in destinations.values():
        run(["git", "--git-dir", str(destination), "symbolic-ref", "HEAD", "refs/heads/main"])
        run(["git", "--git-dir", str(destination), "fsck", "--connectivity-only", "--no-reflogs"])
    print(f"Conversion complete: {output}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, help="Validated JSON plan from inspect-svn-projects.sh")
    parser.add_argument("--output", required=True, help="Destination for GitLab-ready bare repositories")
    parser.add_argument("--work-dir", help="Scratch directory; defaults to the output parent")
    parser.add_argument("--jobs", type=int, default=4, choices=range(1, 5))
    args = parser.parse_args()
    try:
        convert(args)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
