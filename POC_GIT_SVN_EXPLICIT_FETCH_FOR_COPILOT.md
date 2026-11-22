# PoC指示書：git-svn の明示的 `fetch` によるプロジェクト別インポート

> **対象：VS Code GitHub Copilot / Copilot Agent**  
> **目的：実装・実行・結果報告まで行う技術PoC**  
> **ステータス：実施前**  
> **重要：本番SVN、100GBの実データ、GitLabには接続しないこと。**

## 0. Copilotへの作業指示

この文書に従って、現在のワークスペース内に `git-svn` の小規模PoCを構築し、実行してください。

単にサンプルコマンドを提示するだけではなく、次を実施してください。

1. ワークスペースの既存ファイル・既存ルールを確認する。
2. 下記のテスト用SVNリポジトリをローカルに自動生成する。
3. プロジェクトごとに複数の明示的 `svn-remote.<name>.fetch` を設定したGitリポジトリを生成する。
4. `git svn fetch` を実行する。
5. ref、コミットグラフ、SVN copy-fromの継承、Git treeを検証する。
6. テストごとに PASS / FAIL / UNSUPPORTED / NOT_RUN を記録する。
7. 最後に、今回の本番移行方式として採用可能か、制約と追加対応を含めて結論をレポートする。

不明点があっても作業を止めて質問するのではなく、ここに記載した前提でPoCを進めてください。ただし、実装上の前提や制約はレポートに明記してください。

## 1. 背景・目的

SVNリポジトリには約50のプロジェクトが混在しており、SVN全体のサイズは約100GBである。プロジェクトごとに独立したGitリポジトリを作り、最終的にGitLabへ登録する計画である。

従来案では、最初にSVN全体をレイアウト指定なしで巨大なGitリポジトリへ変換し、その後 `git-filter-repo` でプロジェクトを分割することを想定していた。

このPoCでは、その巨大Gitリポジトリを作る工程を省略できるかを検証する。

### 1.1 検証する方式（C案）

**各プロジェクト用のGitリポジトリを最初から作り、そのGitリポジトリに必要なSVN project-rootを明示的な `fetch` 設定として複数登録する。**

- SVN repository rootを基準に、各SVNパスを相対パスで明示する。
- `trunk`、branch相当のディレクトリ、tag相当のディレクトリを、globで探索せず個別に列挙する。
- 1つのプロジェクトに複数のtrunk / branch / tag相当のrootを登録できるようにする。
- branch相当のrootが `branches/` の外にあっても登録できるようにする。
- 各SVN rootの内容がGitリポジトリのトップレベルに配置されることを確認する。
- SVN copyによるコピー元の履歴を `git-svn` がどこまで自動追跡できるか確認する。
- `git-filter-repo` はPoCの基本経路では使用しない。

### 1.2 今回のPoCで使う設定方式

`--branches` / `--tags` はブランチ・タグ格納場所をglob等で列挙するレイアウト設定である。今回のように、個々のbranch-root / tag-rootを完全なパスで指定したい場合は、`svn-remote.<name>.fetch` を複数登録する方式を検証する。

Git公式マニュアルは、`fetch` にglobを使わないこと、複数の `fetch` キーを設定できること、各 `fetch` がSVNパスとGit側refの対応を表すことを説明している。

例：

```ini
[svn-remote "svn"]
    url = file:///path/to/svn-repository
    fetch = products/A/trunk:refs/remotes/svn/trunk
    fetch = products/A/branches/release-1:refs/remotes/svn/branches/release-1
    fetch = archive/A-hotfix:refs/remotes/svn/branches/archive-hotfix
    fetch = special/maint-A:refs/remotes/svn/branches/special-maint
    fetch = products/A/tags/1.0:refs/remotes/svn/tags/1.0
```

左辺はSVN repository rootからの相対パス、右辺はGit内の追跡refである。`fetch` の左辺にはglobを使用しない。右辺のrefはすべて一意にする。

**注意：`fetch` はrevision期間を指定する機能ではない。** 同一SVNパスが削除・再利用された場合の期間分離は別途検証する。

## 2. 参照する既存要件

ワークスペース内に既存の「SVN → GitLab プロジェクト分割・履歴再構成 要件」文書があれば必ず読み、矛盾しないようにする。少なくとも次の要求に関係するPoCとする。

- FR-01：最終状態のproject-rootをプロジェクトに割り当て、1プロジェクトに複数のtrunk / branch / tagを指定できる。
- FR-02：SVN copy-from情報に基づく系譜追跡。別プロジェクト由来のcopyも対象とし、コピー元のcopy後の変更をコピー先に混入させない。
- FR-03：project-rootの祖先ディレクトリがcopy / moveされた場合の子孫パス追跡。同一リビジョン内の子孫変更も対象。
- FR-04：履歴をSVNパスと有効リビジョン範囲の組み合わせで識別し、同一パスの再利用を混同しない。
- FR-05：OS copy + `svn add` はSVN copy-fromがない限りコピー元履歴を継承しない。
- FR-06：他プロジェクトの変更だけのリビジョンを対象プロジェクトの履歴へ混入させない。
- FR-07：採用する履歴パスをGitルートへ正規化する。
- FR-08：Git branch / tagは最終割り当て設定に基づき作成する。SVNの全構造を機械的にGit refへ変換しない。
- NFR-03：プロジェクト単位で再実行でき、ログ・設定・検証結果を保存する。

このPoCは上記すべての要件を実装するものではない。`git-svn` の標準機能で満たせる範囲と、独自処理が必要な範囲を識別する。

## 3. 厳守事項・スコープ

### 3.1 安全条件

- ローカルの一時SVNリポジトリ（`svnadmin create`）だけを使う。
- 本番SVN URL、社内SVN、実際の100GBデータには接続しない。
- GitLabプロジェクトの作成、push、APIアクセスを行わない。
- 既存の作業ファイルを削除・上書きしない。
- すべての生成物をPoC専用work directory配下に置く。
- work directoryを削除する場合は、PoCが作成した専用ディレクトリであることを確認してから行う。
- `git svn dcommit` は使用しない。SVNへの書き戻しは禁止。
- `--no-metadata` は使用しない。`git-svn-id` とrev-mapを検証に使用する。
- `git-filter-repo` は基本経路で実行しない。比較実験が必要になった場合も任意・別枠とする。

### 3.2 使用ツール

次のコマンドが利用できることを最初に確認し、バージョンを記録する。

```text
git --version
git svn --version
svn --version --quiet
svnadmin --version --quiet
python --version
```

Pythonは標準ライブラリだけを使用する。追加パッケージのインストールは原則不要とする。

いずれかの必須ツールがない場合は、インストールを試みず、環境確認結果と不足ツールをレポートして、その環境で可能な範囲の静的検証を行う。

### 3.3 PoC対象外

- 100GB実データでの性能・ディスク容量測定
- GitLab登録・push
- SVN mergeinfo / merge topologyの完全再現
- SVNプロパティ全般の完全移行
- production用のmanifestスキーマ確定
- ファイル単位copyの独自履歴再構成エンジンの実装

## 4. 実装してほしい成果物

既存プロジェクト構成を確認したうえで、可能なら次の構成を作る。既存規約と衝突する場合は適切に読み替え、変更理由を記録する。

```text
poc-git-svn-explicit-fetch/
├── README.md
├── config/
│   └── project-fetch-manifest.tsv
├── scripts/
│   ├── create_svn_fixture.py
│   ├── generate_git_svn_config.py
│   ├── run_import.py
│   ├── verify_trees.py
│   └── run_poc.py
├── tests/
│   ├── test_manifest.py
│   ├── test_explicit_fetch.py
│   ├── test_directory_copy.py
│   ├── test_ancestor_copy.py
│   ├── test_file_copy.py
│   └── test_path_reuse.py
└── artifacts/
    ├── tool-versions.txt
    ├── svn-fixture-revisions.txt
    ├── generated-fetch-config.txt
    ├── command-logs/
    └── report.md
```

生成物をGit管理するかどうかは既存リポジトリの方針に従う。SVN fixture、Git repo、export treeなどの大きな一時生成物は原則Git管理しない。

## 5. 入力manifest

PoCでは、プロジェクトごとの複数rootを登録する最小TSVを作る。

必須列：

| 列 | 意味 | 例 |
|---|---|---|
| `project_id` | Gitリポジトリを作るプロジェクト識別子 | `project-A` |
| `svn_path` | SVN repository rootからの相対パス | `products/A/trunk` |
| `kind` | rootの分類 | `trunk` / `branch` / `tag` |
| `git_ref` | git-svn fetch先の完全なref名 | `refs/remotes/svn/trunk` |
| `expected_head_path` | HEAD tree比較対象のSVN相対パス | `products/A/trunk` |

サンプルTSVには、少なくとも20件の明示的rootを定義すること。`branches/`配下だけでなく、`branches/`と並列または全く別階層にあるbranch相当rootを複数含める。

例（抜粋）：

```tsv
project_id	svn_path	kind	git_ref	expected_head_path
project-A	products/A/trunk	trunk	refs/remotes/svn/trunk	products/A/trunk
project-A	products/A/branches/release-1	branch	refs/remotes/svn/branches/release-1	products/A/branches/release-1
project-A	archive/A-hotfix	branch	refs/remotes/svn/branches/archive-hotfix	archive/A-hotfix
project-A	special/maint-A	branch	refs/remotes/svn/branches/special-maint	special/maint-A
project-A	products/A/tags/1.0	tag	refs/remotes/svn/tags/1.0	products/A/tags/1.0
```

### 5.1 manifest検証

次を実装する。

- `project_id` / `svn_path` / `kind` / `git_ref` の必須値検査。
- `svn_path` はrepository rootからの相対パスとし、先頭 `/`、`..`、空パスを拒否する。
- パス区切りは `/` に統一する。
- 末尾 `/` は正規化して取り除く。
- 同一プロジェクト内の重複 `svn_path` を拒否する。
- 同一Gitリポジトリ内の重複 `git_ref` を拒否する。
- `git_ref` は `refs/` で始まることを確認し、Git refとして不正な文字・形式を拒否する。
- SVNパスとGit refの対応が曖昧な場合は自動命名で上書きせずエラーにする。
- project_idごとに独立したGit repositoryを生成する。
- 1つのproject_idに複数のtrunk / branch / tagが混在できる。

## 6. git-svn設定の生成・実行

### 6.1 基本方針

各project_idについて空のGit repositoryを作成し、そのrepositoryの `[svn-remote "svn"]` に複数の `fetch` を登録する。

生成する設定イメージ：

```ini
[svn-remote "svn"]
    url = file:///.../fixture-repository
    fetch = products/A/trunk:refs/remotes/svn/trunk
    fetch = products/A/branches/release-1:refs/remotes/svn/branches/release-1
    fetch = archive/A-hotfix:refs/remotes/svn/branches/archive-hotfix
    fetch = special/maint-A:refs/remotes/svn/branches/special-maint
    fetch = products/A/tags/1.0:refs/remotes/svn/tags/1.0
```

すべての `fetch` 左辺は完全パスであり、globを含めない。

`fetch` 設定は複数値なので、Git configでは `--add` を使うか、Git configファイルを安全に編集する。値を通常の `--replace-all` で上書きして複数行を消さないこと。

### 6.2 推奨初期化手順

次の手順を実装する。`git svn init` の実際の生成設定を確認し、不要な自動生成fetchがあれば初回fetchの前に除去する。

```bash
git svn init "$SVN_REPOSITORY_URL" "$PROJECT_GIT_DIR"

# 既存の自動生成fetchを除去して、manifestから再生成する。
# fetchが未定義の場合の終了コードも適切に扱う。
git -C "$PROJECT_GIT_DIR" config --unset-all svn-remote.svn.fetch || true

# 以降はmanifestの各行を繰り返す。
git -C "$PROJECT_GIT_DIR" config --add svn-remote.svn.fetch \
  'products/A/trunk:refs/remotes/svn/trunk'

git -C "$PROJECT_GIT_DIR" config --add svn-remote.svn.fetch \
  'archive/A-hotfix:refs/remotes/svn/branches/archive-hotfix'

# copy parent追跡を明示的に有効化する。
git -C "$PROJECT_GIT_DIR" config svn-remote.svn.followparent true

# 初回fetch前に、最終的な設定値を記録・検査する。
git -C "$PROJECT_GIT_DIR" config --get svn-remote.svn.url
git -C "$PROJECT_GIT_DIR" config --get-all svn-remote.svn.fetch
git -C "$PROJECT_GIT_DIR" config --get svn-remote.svn.followparent

git -C "$PROJECT_GIT_DIR" svn fetch
```

実際のコマンド引数・`git svn init` の生成設定は、使用するGitバージョンで検証する。初回fetch後にfetch設定を変更して結果を継ぎ足す運用は避ける。設定変更テストが必要な場合は新しい空repositoryを作り直す。

### 6.3 `--trunk` / `--branches` / `--tags` の扱い

このPoCの主経路では、個別rootの列挙に `--branches` / `--tags` を使用しない。個別のSVN project-rootはすべて明示的な `fetch` によって登録する。

必要なら比較用の小テストとして、次を追加してよい。

- `--trunk=<path>` で初期化し、その後、他のbranch/tag相当rootを明示的 `fetch` で追加する方式。
- すべてを `fetch` に列挙する方式。

この2方式で生成されるref・copy親追跡の差を記録する。主経路の合否は「すべてを明示的 `fetch` に列挙する方式」で判断する。

### 6.4 `--follow-parent` の検証上の注意

Git公式マニュアルは `--follow-parent` を、`--trunk` / `--tags` / `--branches` / `--stdlayout` のようなレイアウト指定でブランチを追跡する場合の機能として説明している。一方、git-svn実装では各fetch対象の処理中にcopy元を探す処理が呼び出される。

このため、今回のような静的 `fetch` の複数登録で、期待する親履歴追跡が実際に行われるかはPoCで確認する。マニュアルの説明だけから成功を決めつけない。

最低限、以下を記録する。

- `svn-remote.svn.followparent=true` が有効であること。
- `git svn fetch` の標準出力・標準エラー。
- `Initializing parent` 等のログ。
- 作成された `refs/remotes/...` と `branch@revision` 等の補助ref。
- copy先refのコミット親子関係。
- copy元commitがcopy先commitの祖先として接続されているか。

## 7. ローカルSVN fixtureに作るテストケース

`svnadmin create` で一時SVN repositoryを作成し、SVN CLIから再現可能な履歴を作る。revision番号はテストコードで取得し、固定値を決め打ちしない。

### TC-01：不規則な複数rootをglobなしでfetch

次のような異なる階層のrootを作る。

```text
/repo-root
├── products/project-A/trunk/
├── products/project-A/branches/release-1/
├── archive/project-A-hotfix/
├── maintenance/project-A/
├── legacy/active/project-A/
├── releases/project-A/2025-Q4/
└── products/project-A/tags/1.0/
```

同様のrootを20件以上作成し、manifestに登録する。

**期待結果**

- 20件以上の `fetch` 値がGit configに登録されている。
- `fetch` 左辺に `*`、`?`、`{...}` などのglob構文がない。
- `branches/` 外のrootも取得できる。
- 指定したrootに対応するrefだけが作成され、未指定の兄弟rootが勝手にブランチとして列挙されない。
- 各refのtreeにSVN root自身の親ディレクトリ名が含まれず、rootの中身がGit top-levelに存在する。
- 異なるSVNパスを異なるGit refへ割り当てられる。

### TC-02：同一プロジェクトに複数のtrunk / branch / tag相当root

1つのproject_idに複数のtrunk相当root、複数のbranch相当root、複数のtag相当rootを登録する。

**期待結果**

- すべてのrootを1つのGit repositoryの別々のremote refとして取得できる。
- ref名が一意であり、branch名・tag名のbasename衝突が起きない。
- 各rootのHEAD treeが正しいSVNパスのsnapshotと一致する。
- PoC中のtag相当refはgit-svn追跡refとして存在していればよい。Git tagへの最終変換は本PoCの主目的ではない。

### TC-03：別プロジェクト由来のディレクトリcopy

SVN履歴を次のように作る。

```text
rN:   /project-A/trunk/lib/file.txt を作成
rN+1: /project-A/trunk/lib/file.txt を変更
rN+2: /project-A/trunk/lib を /special/project-B-lib に svn copy
rN+3: /special/project-B-lib/file.txt を変更
rN+4: /project-A/trunk/lib/file.txt を再度変更
```

Project B用Git repositoryには `/special/project-B-lib` のfetchだけを定義し、copy元 `/project-A/trunk/lib` は明示fetchに含めないケースを必ず実施する。

追加で、copy元を同一Git repositoryの別fetch refとしても登録した比較ケースを実施する。

**期待結果**

- Project Bのfetchで、SVN copy-fromの履歴がGit parentとして取り込まれるかを確認する。
- copy元をB側のmanifestに含めない場合でも、git-svnがcopy元の必要履歴をオンデマンド取得するか確認する。
- コピー先のcopy時点のtreeが、SVNのcopy-from revisionにおけるコピー元treeと一致する。
- copy後のProject A側変更（rN+4）がProject Bの履歴・treeへ混入しない。
- copy先の変更（rN+3）はProject Bだけに反映される。
- 自動生成された補助refの名前と用途を記録する。

**重要：このテストが失敗した場合、ディレクトリcopyの履歴継承を標準機能で満たすとは結論しない。**

### TC-04：多段階のディレクトリcopy

```text
/source-A/component
       | svn copy
       v
/intermediate/component
       | svn copy
       v
/final/component
```

Project finalのGit repositoryでは `/final/component` のfetchのみを定義するケースを作る。

**期待結果**

- 中間パス・最初のコピー元まで系譜を遡れるか確認する。
- 最終コピーのcopy-from revisionより後に発生した中間・コピー元側の変更が最終プロジェクトに混入しない。
- 追跡できない場合は、どのコピー段階で切れるかを記録する。

### TC-05：祖先ディレクトリのcopyと子孫rootの追跡

```text
rN:
/old-area/project-C/trunk/src/main.txt

rN+1:
svn copy /old-area /archive/old-area
```

Project C用Git repositoryのfetch対象は次の子孫rootだけにする。

```text
/archive/old-area/project-C/trunk
```

少なくとも次の2ケースを作る。

1. 親ディレクトリcopyのみ。
2. 親ディレクトリcopyと同一SVN revision内で、コピー先子孫ファイルにも変更を加える。

**期待結果**

- 親ディレクトリのcopy-from情報と子孫相対パスから、旧rootの履歴へ接続されるか確認する。
- 同一revision内の子孫変更を含むHEAD treeがSVNと一致する。
- コピー元側の後続変更はコピー先に混入しない。

### TC-06：ファイル単位のcopy / move

プロジェクトrootの内部で、ファイルを `svn copy` し、元ファイルとコピー先ファイルの双方を後から変更する。

**期待結果**

- Git treeにコピー先ファイルの内容が正しく反映される。
- Git commit graphがSVNファイル単位のcopy-from系譜を親子関係として保持するか確認する。
- `git log --follow` 等のrename/copy detectionで見える結果と、SVNのcopy-from事実を混同しない。
- FR-02のファイル単位copy要件をgit-svn標準機能だけで保証できるか、PASS / PARTIAL / FAILを明記する。

このケースは標準git-svnで完全な系譜継承を前提にしない。できない場合も無理に補完せず、標準機能の限界として記録する。

### TC-07：OS copy + svn add

SVN working copy上でファイルをOSレベルでコピーし、`svn add` してcommitする。元ファイルにも履歴を追加する。

**期待結果**

- SVN上にcopy-from情報がないことを確認する。
- git-svnがOSコピー元の履歴を親commitとして誤接続しないことを確認する。
- Gitの類似内容検出や `--find-copies` 等の表示は、git-svnの履歴継承とは分けて記録する。

### TC-08：同一SVNパスの削除・再利用

```text
rN:   /shared/component がProject Aのroot
rN+1: /shared/component を削除
rN+2: /shared/component を新規作成しProject Bのrootとして利用
```

Project A / Project B双方にこのパスを含めた場合、または異なるrootへのcopyとして再利用した場合を作る。

**期待結果**

- 静的 `fetch = path:ref` がパスの全存続期間をどのように取り込むか確認する。
- Project AのGit repositoryにrN+2のProject B内容が混入しないか確認する。
- Project BのGit repositoryにrN以前のProject A内容が不要に混入しないか確認する。
- `fetch` 設定だけでrevision期間による分離ができるか判定する。
- できない場合はFR-04に対する制約として明示し、期間限定fetch / 追加変換 / 独自処理のいずれが必要か整理する。

### TC-09：他プロジェクトだけが変更されたSVN revision

Project Aのrootに変更がないrevisionでProject Bだけを変更する。

**期待結果**

- Project AのGit refにtree変化のない不要なcommitが生成されるか確認する。
- 生成される場合はcommit数・tree変化・原因を記録する。
- treeに影響しないcommitを許容するか、後段で除去が必要かを明記する。

## 8. 検証方法

### 8.1 refとfetch設定

各プロジェクトについて次を保存する。

```bash
git config --get-all svn-remote.svn.fetch
git config --get svn-remote.svn.followparent
git for-each-ref --format='%(refname) %(objectname)' refs/remotes/
git show-ref
```

期待するrefの有無だけでなく、想定外のref（特に `@<revision>` 補助ref）も記録する。

### 8.2 SVN snapshotとGit treeの比較

各テストの対象root・対象revisionについて、次の2つを比較する。

1. `svn export -r <revision> <repository-url>/<svn-path>` で出力したsnapshot
2. 対応するGit refまたはGit commitを `git archive` 等で展開したsnapshot

比較はPythonで行い、少なくとも次を確認する。

- 相対ファイルパス集合
- ファイル内容のSHA-256
- 実行属性（検証可能な環境の場合）
- symlink / `svn:special` が存在する場合は、別途差異を記録

空ディレクトリはGit treeでは通常表現されないため、空ディレクトリ差はファイルtree比較から分けて報告する。

Git repository rootに余分なSVN親階層が残っていないことも確認する。

### 8.3 copy親の確認

copy先の最初のGit commitについて、次を確認する。

- commitのparent一覧
- parent側の `git-svn-id` metadata
- metadataのSVN URL/pathとrevision
- parent treeとSVN copy-from path@revisionのsnapshotの一致
- copy後のsource-side変更がtarget ancestry / target treeへ入っていないこと

必要に応じて `git log --graph --decorate --oneline --all`、`git show -s --format=raw <commit>`、`git cat-file -p <commit>` を保存する。

単にGitの `--follow` やcopy detectionの表示があるだけでは、copy-from履歴の継承成功と判定しない。

### 8.4 再実行性

- 同じfixture・manifestで新規に作成したGit repositoryへ再実行できる。
- 既存のfetch済みrepositoryの設定をその場で変更しない。
- 失敗時に対象プロジェクトのみ再作成・再実行できる。
- 実行ログ、manifest、ツールバージョン、fixture revision番号が保存される。

## 9. 判定ルール

各テストを次のいずれかで判定する。

| 判定 | 意味 |
|---|---|
| PASS | 期待結果を満たし、ログ・Git graph・tree比較で確認できた |
| PARTIAL | 一部は成立するが、要件の一部を満たさない、または条件がある |
| FAIL | 期待結果を満たさない |
| UNSUPPORTED | git-svn標準機能では要件を表現できないことを確認した |
| NOT_RUN | 環境不備などにより実行できなかった。理由を必ず記録する |

推測でPASSにしない。証拠がない場合はNOT_RUNまたはPARTIALとする。

## 10. 完了条件（Definition of Done）

次のすべてを満たした場合にPoC完了とする。

### 10.1 実行可能な成果物

- [ ] ローカルSVN fixtureを自動生成できる。
- [ ] 20件以上の個別rootを含むmanifestを持つ。
- [ ] manifestから複数の `fetch` 設定を生成できる。
- [ ] すべての `fetch` 左辺にglobが含まれていない。
- [ ] プロジェクトごとに独立したGit repositoryを作成できる。
- [ ] `git svn fetch` を実行し、ログを保存できる。
- [ ] PoC全体を1つのコマンドで再実行できる。

### 10.2 技術検証

- [ ] TC-01〜TC-09を実行し、すべてに判定と根拠がある。
- [ ] TC-01で20件以上の静的fetchが正常に処理される。
- [ ] `branches/` 外のbranch相当rootがfetchできる。
- [ ] 各rootのGit treeが正しいSVN rootのsnapshotと比較できる。
- [ ] SVNディレクトリcopyについて、copy元履歴が継承される条件と失敗条件を特定する。
- [ ] 親ディレクトリcopyについて、子孫相対パスで履歴が接続されるか確認する。
- [ ] コピー元のcopy後の変更がコピー先に混入しないことを確認する。
- [ ] ファイルcopy、OS copy + svn add、同一パス再利用について、標準機能での可否を明確にする。
- [ ] 補助ref（例：`branch@revision`）が生成される条件と、最終ref作成時の扱いを記録する。

### 10.3 レポート

`artifacts/report.md` を作成し、次を含める。

1. 結論：C案を採用できるか（全面採用 / 条件付き採用 / 不採用）。
2. 実行環境とGit / git-svn / SVNのバージョン。
3. 実際に使用したmanifestと生成されたfetch設定。
4. TC-01〜TC-09のPASS / PARTIAL / FAIL / UNSUPPORTED / NOT_RUN一覧。
5. copy元・copy先の具体的なGit commit graphとSVN revision対応。
6. SVN snapshotとGit tree比較の結果。
7. git-svn標準機能で満たせる要件。
8. 独自処理が必要な要件。
9. 100GB・50プロジェクトへ進む前に必要な追加PoC / 性能測定。
10. 次の設計判断と推奨アーキテクチャ。

### 10.4 完了とみなさない状態

次のいずれかに該当する場合、PoCは未完了とする。

- fetch設定が作られただけで、実際の `git svn fetch` を実行していない。
- Git treeの比較をしていない。
- copy履歴をGitのログ表示だけで判断し、parent graphを確認していない。
- ファイルcopyやパス再利用のテストを省略した。
- 失敗したテストを理由なく無視した。
- 本番データに対する実行が必要な状態のまま、ローカルfixtureで検証できていない。

## 11. 最終レポートで答えるべき設計判断

PoCの最後に、必ず以下の問いに明確に回答する。

1. 1プロジェクトのGit repositoryに20〜50個の静的 `fetch` を登録する方式は、実用上問題なく動くか。
2. `fetch` で指定したrootの内容は、期待通りGit repository rootに配置されるか。
3. 静的 `fetch` に `followparent=true` を適用した場合、別プロジェクト由来のディレクトリcopyの履歴を接続できるか。
4. copy元rootをmanifestに含めない場合でも、必要なcopy元履歴を取得できるか。
5. 親ディレクトリcopy、複数段階copy、同一revision内の子孫変更に対応できるか。
6. ファイル単位copyの履歴継承は実現できるか。できない場合、FR-02の要件を満たすには何が必要か。
7. 同じSVN pathが削除・再利用された場合、静的fetchだけでプロジェクト所属を分離できるか。
8. `branch@revision` 等の補助refを含むref構成を、最終的な割り当てTSVに基づいて整理できるか。
9. 本番の約50プロジェクトに展開するとき、project単位でfetchを並列化できそうか。どの測定が必要か。
10. 巨大Git + `git-filter-repo` を廃止し、C案を基本設計にできるか。あるいは、特定の難しい履歴だけB案・独自変換へ逃がすべきか。

## 12. 参考資料

- Git公式 `git-svn` マニュアル：  
  https://git-scm.com/docs/git-svn
  - `fetch` 設定はglobを取らない。
  - 複数の `fetch` / `branches` / `tags` キーを設定できる。
  - `--follow-parent` の説明と適用条件。
  - 未追跡のコピー元ディレクトリの履歴を読み込む場合や、`branch@revision` 補助refに関する説明。
- git-svn実装（Git source / Git::SVN.pm）：  
  https://kernel.googlesource.com/pub/scm/git/git/+/HEAD/perl/Git/SVN.pm
  - 複数のfetch設定を個別の追跡対象として処理するコード。
  - `find_parent_branch` / `do_fetch` の実装。

---

**PoCの最重要原則：** `git-svn` の標準機能で実際に再現できたことだけを成功とする。特にディレクトリcopyとファイルcopy、パスと期間の管理は別問題として検証し、標準機能で対応できない部分を曖昧に残さない。
