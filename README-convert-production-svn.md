# Converting existing git-svn clones into project repositories

`convert-svn-projects.sh` builds one bare Git repository per GitLab project
namespace/path from already-imported git-svn repositories. It does not contact
or modify SVN and does not infer project lineage. Every selected source path
and inclusive SVN revision range must be declared in `sections.tsv`.

The script defaults to requiring all 39 repositories in the SVN-root list.
It validates the input TSVs, every source clone, git-svn revision metadata,
imported authors against `authors.txt`, target refs, and the final project root
before conversion. Unmapped or overlapping rules stop the run.

## Conventions and inputs

The example TSVs under `bin/examples/` show the required headers:

- `svn-roots.tsv`: exactly one `svn_repo` directory name per line. Each must
  have a corresponding fully imported clone at `git/targetdir/{svn_repo}`.
- `project-roots.tsv`: one row per intended final root/ref. `root_id` is the
  stable key joining the other TSV. `project_root` is the SVN path present at
  `target_rev`; `gitlab_project` is the full namespace path, such as
  `group/subgroup/project`. A `trunk` must use `ref_name=main`; branch and tag
  rows use their desired Git ref names. Every GitLab project path must have
  exactly one trunk.
- `sections.tsv`: one row per source-path/revision interval contributing to a
  root. `from_rev` and `to_rev` are inclusive numeric SVN revisions. Each
  source path is relative to the root of its git-svn clone. Add a new row when
  a project moves, is copied, or changes SVN path. Decisions about cross-project
  copies and path reuse must be represented explicitly. In this initial
  implementation, every section contributing to one `root_id` must belong to
  the same `svn_repo` as its project-root row; cross-repository lineage is
  rejected rather than inferred or silently dropped.
- `authors.txt`: standard `git svn --authors-file` format. Since the input
  clones are already imported, the converter verifies that their commit author
  names/emails match the right-hand side of this mapping; it does not rewrite
  authors.

Output is bare and ready to push, with layout
`git/converted/{group}/{subgroup}/{project}.git`. The trunk is Git `main`,
SVN branches become Git branches, and SVN tags become lightweight Git tags.
Each target ref is constructed from its own declared section history.

## Usage

First run validation only:

```bash
./bin/convert-svn-projects.sh \
  --svn-roots svn-roots.tsv \
  --project-roots project-roots.tsv \
  --sections sections.tsv \
  --authors authors.txt \
  --git-svn-dir git/targetdir \
  --output git/converted \
  --check
```

After reviewing the TSVs and a successful check, run the same command without
`--check`. The output directory must be empty or absent. Partial output is
preserved if conversion fails; inspect it before deciding how to continue.

The examples are based on the local artificial fixture, not production
lineage. A production run must replace them with reviewed, complete inventories.
