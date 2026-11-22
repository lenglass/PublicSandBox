# Converting existing git-svn clones into project repositories

`inspect-svn-projects.sh` and `convert-svn-projects.sh` are separate phases.
The first checks the input formats, git-svn repositories, author mappings,
lineage rules, and target trees, then writes a JSON conversion plan. The second
consumes that plan and builds one bare Git repository per GitLab project path.
Neither script contacts or modifies SVN, and neither infers project lineage.
Every selected source path and inclusive SVN revision range must be declared
in `sections.tsv`.

Inspection defaults to requiring all 39 repositories in the SVN-root list.
It validates the input TSVs, every source clone, git-svn revision metadata,
imported authors against `authors.txt`, target refs, and the final project root.
Unmapped or overlapping rules stop inspection. The plan contains hashes for
all input files and source ref fingerprints; conversion rejects stale inputs.

## Conventions and inputs

The example TSVs under `config/examples/` show the required headers:

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

### 1. Inspect formats and create a plan

```bash
./bin/inspect-svn-projects.sh \
  --svn-roots config/svn-roots.tsv \
  --project-roots config/project-roots.tsv \
  --sections config/sections.tsv \
  --authors config/authors.txt \
  --git-svn-dir git/targetdir \
  --report config/conversion-plan.json \
  --jobs 4
```

Review the generated plan and the inspection summary before conversion.

### 2. Convert repositories

```bash
./bin/convert-svn-projects.sh \
  --plan config/conversion-plan.json \
  --output git/converted \
  --work-dir git/convert-scratch \
  --jobs 4
```

`--jobs` is limited to 1–4 and defaults to 4. Inspection scans distinct SVN
clones concurrently. During conversion, work on the same SVN clone or GitLab
project is serialized; up to four independent source repositories can be
processed concurrently. Use a scratch directory with sufficient free space.
The output directory must be empty or absent. Partial output is preserved and
per-root scratch directories are cleaned if conversion fails; inspect output
before restarting.

## Storage and I/O behavior

Inspection streams commit metadata and author identities; it does not check out
files or copy Git objects. The converter uses
`git clone --shared --no-checkout` for each disposable filtered repository.
This creates an alternate-object reference to the original clone instead of
copying its complete object store or checking out its working tree.
`git-filter-repo` writes the selected history into temporary storage; the
result is fetched into the final bare repository and the temporary repository
is removed. Temporary filtered history can still be sizable, and the final
repositories necessarily consume their own storage. The converter reports
source object-store sizes, free space on the scratch/output filesystems, a
source-size-based estimate for concurrent scratch, and the actual temporary
object-store size after each extraction. A weighted admission budget leaves
20% of currently free space unreserved and can reduce effective concurrency;
an oversized single extraction is still allowed to run alone. This is a
concurrency guard, not a strict upper bound on conversion output, so select a
scratch/output filesystem with adequate headroom. Tree comparisons are streamed
to keep memory use independent of project file count. Final integrity checks
use `git fsck --connectivity-only` to avoid reading every blob payload.

The examples are based on the local artificial fixture, not production
lineage. A production run must replace them with reviewed, complete inventories.
