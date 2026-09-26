# Namespace sandbox Phase 4 review: trusted input selection

This ledger tracks the Phase 4 work item "Materialize the complete trusted
input selection" from `refactor-review.md`. It follows that ledger's rules:
findings are recorded before repair, completion boxes require focused test
evidence, and deferred items remain explicit. The single unchecked item in
"Next selected work" is the accepted scope of the next commit; when it lands,
tick it, add a worklog entry, and move the next item from "Proposed sequence".

## Review boundary

The series selects, validates, and activates the namespace filesystem policy
in the install worker. Build stages and install prefixes must remain
host-backed and visible from the supervisor's mount namespace; no
namespace-private copy or tmpfs may replace their persistent contents.

## Compatibility directive

Do not add namespace-sandbox settings to `config.yaml` or its schema in this
project. Bootstrap configuration is shared across Spack versions, so a newer
key would make an older Spack reject an otherwise usable bootstrap config.
Policy selection and validation are therefore unconditional whenever trusted
inputs are supplied. The namespace backend is selected automatically when its
capability probe succeeds; Landlock remains the constrained fallback when the
namespace capability is unavailable, with no new opt-in or fallback setting.

The design target is namespace-native confinement. Mechanisms that only
compensated for Landlock's inability to hide paths are not ported (see
"Excluded Landlock-era mechanisms").

## Sub-phase status

- [x] Phase A, policy data and compiler facts.
- [x] Phase B, namespace model extensions required by real inputs.
- [x] Phase C, whole-child selection, pre-thread validation, real-build
  evidence, and automatic activation.

## Evidence

- The live worker prepares the namespace in `_start_worker_output`, before
  `_install` fetches, stages, patches, builds, installs, and archives
  metadata. An activated tree must therefore support the whole install child,
  not only build phases. The old Landlock implementation confined only build
  phases, so its grant list is incomplete for this boundary.
- The earlier `namespace_filesystem_policy_and_plan_from_inputs` derived hidden
  roots from mount targets. The selected policy now uses explicit hidden roots:
  read-only paths outside them remain visible through the recursively read-only
  inherited view, while explicit writable paths receive writable identity
  mounts even when they are outside a hidden root.
- Before staging, the stage directory does not exist, and `PrefixPivoter` has
  not yet moved an existing prefix. `rename(2)` or `rmdir(2)` on a mount point
  fails with `EBUSY`, so identity mounts of the stage or prefix conflict with
  `PrefixPivoter`, restaging, and stage removal.
- `spack.stage.AbstractStage.__exit__` destroys a stage only on success when
  `keep` is false; on an exception, it leaves the stage behind. Preserve that
  host-visible failure behavior, including `spack-src` and `config.log`.
- The existing `mount_setattr` requirement implies Linux 5.12 or later and
  glibc 2.36 or later for the libc wrapper.
- `spack.util.ld_so_conf.host_dynamic_linker_search_paths()` exists in this
  tree and reports the host library search path. The jobserver FIFO is created
  by `tempfile.mkdtemp()` in `spack.installer.posix`, below the temporary
  directory.
- Policy data is tracked in `share/spack/sandbox/sandbox.yaml` and
  `share/spack/sandbox/linux-header-policy.yaml`. The namespace policy excludes
  the earlier Landlock `commands` list and `df` stub.

## Case-by-case review

### Policy data

- [x] Keep one central, versioned policy source. Move the namespace constants
  (hidden roots, replacement roots, device nodes, stage-tool programs) into
  `sandbox.yaml` instead of adding module constants. Keep the header policy
  in its own file for this series.
- [x] Do not port `commands` or `commands/df`; the stub answered
  Landlock-denied filesystem queries. Revisit only if a real build fails under
  the namespace tree.
- [x] Treat `host_runtime_read_paths` as candidates, not grants. Library
  directories remain visible passthrough trees. `/etc` entries need a mount
  only if a selected root hides their parent.

### Compiler, header, and tool selection

- [x] Select compilers from concrete language edges and compiler nodes on the
  DAG. Exclude unselected languages and deduplicate repeated edges. Call
  `supported_compilers(repo=spack.repo.PATH)`.
- [x] Select exact glibc, Linux UAPI, libstdc++, and GCC-internal headers from
  the header policy. Keep the libstdc++ major-version cap for non-GCC C++, and
  mask GCC installations that no selected language requires.
- [x] Resolve helpers from the compiler (`-print-prog-name`,
  `-print-file-name`), not from ambient `PATH`. Ignore bare-name answers,
  prefer real binutils over `libexec/spack` wrappers, and include `cc1` for
  `cpp`, the magic database for `file`, and Git's `--exec-path`.
- [x] Select stage tools for fetch and expansion (`tar`, `unzip`, `gzip`,
  `gunzip`, `bunzip2`, `xz`, `7z`, `patch`, `sh`, `git`), including script
  helper chains such as `gunzip` to `gzip` and `sh`. For Spack-built tools,
  add the owning prefix and its link/run dependency closure.
- [x] Record each searched spelling separately from its resolved source.
  Aliases such as `cc` remain represented at their original path inside a
  masked directory, while canonical source paths are used for validation.
  B1 represents aliases as generated symlink entries. Do not select every
  hardlink or same-file entry in a directory.

### Host, device, and worker-state exposure

- [x] Replace derived parent masks with explicit hidden roots from policy data.
  A selected path outside every hidden root is visible passthrough and needs
  no mount; a path below one is restored. This prevents incidental masking of
  `/usr/lib` or `/tmp`.
- [x] Add replacement-root policy entries: a hidden directory whose source is a
  caller-provided directory instead of an empty tmpfs. Mounting the scoped
  worker directory at `/tmp` (and `/var/tmp`) gives `tmpfile()` and every
  hard-coded `/tmp` user a private writable tree without a host `/tmp` grant.
  Selected paths below a replacement root, such as a default stage root, are
  preserved before replacement and restored afterward. The scoped worker
  source allocation remains a later host/device and worker-state selection.
- [x] Replace Landlock write rules with a recursively read-only view plus
  explicit writable mounts. Selected-tree plans set `MOUNT_ATTR_RDONLY`
  recursively on the inherited mount tree, then clear it only on explicit
  read-write bind mounts. The capability probe and disposable namespace tests
  prove recursive setup, `EROFS` for inherited passthrough writes, and writes
  through selected writable mounts. The live worker activates the selected
  policy when namespace capability probing succeeds.
- [x] Hide `/home`, `/root`, and `/run/user`; restore the Spack source, store,
  repository, configuration, and cache paths located there. Point `HOME`,
  `XDG_CACHE_HOME`, and Java `user.home`/`java.io.tmpdir` at the scoped
  worker tree, alongside `TMPDIR`, `TMP`, `TEMP`, and `tempfile.tempdir`.
  Apply environment changes only at activation.
- [ ] Hide `/dev`; restore `null`, `zero`, `full`, `random`, `urandom`, and
  `tty` as device bind mounts. Mount a fresh tmpfs at `/dev/shm`, and generate
  the `/dev/fd`, `/dev/stdin`, `/dev/stdout`, and `/dev/stderr` symlinks.
- [ ] Inventory the whole-child writable set: stage, install prefix,
  `request.log_path`, jobserver FIFO, fetch cache, scoped worker tree, and the
  explicitly configured `allow_write` paths. Inventory the read-only set:
  dependency prefixes, repositories and their Python paths, partitioned Spack
  source, system and user configuration, `sbang`, and the running Python
  (`sys.executable`, standard library, `lib-dynload`, and active
  site-packages, including a virtual environment). In particular, Spack's
  package recipes and core source tree are read-only inside the sandbox,
  regardless of where the checkout is hosted; the stage and chosen install
  prefix are separate writable locations, not writable grants to their
  containing repository or store roots.
- [x] Keep `/proc`, `/sys`, and network access visible and out of scope. A
  network namespace would have to be created in trusted pre-thread setup,
  before mount authority is dropped; network policy needs its own design.

### Stage and prefix lifecycle

- [x] Keep the stage on the host-backed filesystem used by `spack stage` and
  existing installs. The worker's writes to `spack-src` and `config.log`
  must be visible to the supervisor and user after failure; a successful
  build still removes its stage unless keep-stage is requested. The install
  prefix likewise remains host-backed and visible after successful install,
  with existing keep-prefix and rollback semantics on failure.
- [x] Preserve the child-owned stage lifecycle with a stable, host-backed,
  per-build parent created by trusted setup. Bind that parent read-write into
  the namespace and place the removable stage directory below it, so
  `Stage.destroy`, restaging, success cleanup, and failure retention operate
  on a child of the mount point rather than on the mount point itself. Keep
  the path visible in the supervisor namespace and avoid granting the shared
  stage root or another build's directory.
- [x] Move only prefix pivot and rollback to the supervisor. Before launching
  the worker, pivot any old prefix and create the empty host-backed target;
  the child bind-mounts that exact prefix read-write and never renames or
  removes the mount point. After the worker exits and its namespace is gone,
  the supervisor finalizes success or applies the existing keep-prefix,
  BinaryCacheMiss, garbage-removal, and old-prefix restoration rules. Keep
  prefix locking across the transaction and preserve the final path seen by
  build tools, so embedded paths and RPATHs do not change.
- [x] Keep child-owned stage cleanup because the stable-parent layout preserves
  the current stage path and lock semantics. Do not expose a writable whole
  store, stage root, or Spack checkout to avoid the mount-point constraint.

### Mount-plan scratch

- [x] Distinguish *mount sources* (host files and directories selected as
  inputs to bind mounts, including read-only Spack repositories) from build
  *source stages* (`spack-src` and logs). `spack-preserved-host-paths` and
  `spack-empty-host-dirs` are private setup scratch for mount endpoints, not
  the build stage and not copies of Spack's package sources. Its location or
  removal has no bearing on host visibility or retention of the build stage.
- [x] Keep durable scratch for the first policy activation. Trusted setup
  allocates a mode-0700, per-worker scratch directory outside every hidden,
  replacement, stage, prefix, and writable-policy root; the child uses it
  only for mount endpoints. The supervisor owns cleanup after the worker is
  reaped, including setup failure and abnormal exit, and never removes a
  directory still referenced by a live child. Test concurrent workers,
  symlinked scratch bases, collision rejection, and stale cleanup.
- [ ] Keep descriptor-anchored detached mounts (`open_tree`/`move_mount` and
  `fsopen`/`fsmount`) as optional later hardening. They are not required for
  Phase 4 completion and do not replace stage or prefix persistence. Revisit
  them only with a separate capability probe, source-selection race analysis,
  and real-kernel tests.

#### Accepted implementation decisions

- Implement durable, supervisor-cleaned scratch with pathname bind mounts for
  Phase 4. Allocate each worker's mode-0700 endpoint tree outside every policy
  root, complete all binds before package code runs, and drop mount authority
  before starting untrusted work.
- Accept and document the narrow pre-build TOCTOU risk: another process able
  to mutate a selected source or ancestor, commonly one under the same UID,
  can replace it between validation and the first bind. Once bound, the mount
  pins the selected filesystem object. Read-only attributes do not prevent a
  host process from changing mutable source contents through another view.
- Do not add detached-mount APIs in Phase 4. If the decision is revisited,
  prototype only a fully detached plan with trusted descriptor-relative
  resolution, a separate capability probe, audited descriptor ownership and
  cleanup, and real-kernel substitution and failure-path tests. Do not
  implement partial detached hybrids.
- Keep mount-plan scratch independent from host-backed stage and prefix
  persistence; changing the mount engine must not alter their lifecycle.

The threat model, security and compatibility costs, and rejected variants are
recorded in
[namespace-mount-plan-alternatives.md](namespace-mount-plan-alternatives.md).

### Validation

- [x] Validate selected inputs and compile the immutable policy in pre-thread
  setup before any mount. A failure aborts that worker before mutation and
  never becomes an unconstrained fallback. Validation has no config.yaml
  switch; it is unconditional whenever the selected inputs are supplied.
- [x] Obtain real-build evidence by applying the compiled plan in a
  disposable namespace child (not the worker). Run representative external
  compiler builds (GCC C/C++, LLVM C/C++ with GCC runtime, and Fortran where
  available) and a `configure`/`make` source build with fetch and expansion.
  Evidence recorded below covers the available toolchain and host layout.

## Excluded Landlock-era mechanisms

- Store-`bin` symlink farm, `PATH` rewriting, and `sanitized_host_paths`
  (`allow_sandbox_commands`). Masks already restore selected executables at
  their original paths, so shebangs and `PATH` lookups need no rewriting.
  The conflicting-link, concurrent-link, and shebang-after-stage-removal tests
  guarded hazards of the farm itself and are not ported.
- The `df` stub and `commands` policy list.
- The broad host `/tmp` write grant and the `LD_PRELOAD` or Seccomp
  `O_TMPFILE` ideas for `tmpfile()`. The `/tmp` replacement root supersedes
  them.
- Per-path read grants for library directories, which remain visible
  passthrough trees.
- `GIT_CONFIG_GLOBAL=/dev/null`; hidden home directories already make global
  Git configuration absent.
- `EACCES` or `EPERM` denial assertions. Namespace tests assert `ENOENT` for
  hidden paths and `EROFS` for read-only paths.
- Seccomp, learning whitelists, proxy and Maven configuration, Landlock ABI
  fallbacks, and resource limits, which are outside this series.

## Test learnings from the earlier tree

The earlier `lib.sandbox/spack/spack/test/sandbox.py` repeats its header,
`df`, and masking tests (lines 48-370, again at 371-672); port each case
once. Port behavior, not implementation details.

- Policy data: adapt `test_sandbox_policy_is_loaded_from_yaml`, and add
  malformed version, list, alias, and unsafe relative-path cases.
- Header selection: port
  `test_system_compiler_headers_allow_only_safe_libstdcxx`,
  `test_gcc_installations_other_than_permitted_libstdcxx_are_masked`,
  `test_mask_preserves_gcc_installation_selected_for_fortran`,
  `test_gcc_16_compiler_gets_gcc_16_libstdcxx`,
  `test_system_compiler_headers_allow_gcc_internal_headers_for_c`, and
  `test_newest_libstdcxx_used_when_no_older_headers_exist` with their
  synthetic GCC layout. Convert
  `test_system_header_policy_denies_unselected_libstdcxx` into a disposable
  namespace test: the selected header is readable and the unselected one
  returns `ENOENT`.
- Compiler selection: port `test_allow_selected_compiler_paths` (unselected
  Fortran, duplicate edges) and `test_allow_compiler_package_paths_for_runtime`
  (compiler node on the DAG; monkeypatch with the `repo` keyword).
- Helpers: port `test_compiler_support_paths_queries_all_build_tools`,
  `test_file_executable_support_paths`, `test_cpp_executable_support_paths`,
  and `test_allow_git_support_paths_uses_configured_exec_path`. Convert the
  binutils test to selecting real binutils instead of the `libexec/spack`
  wrapper. Convert the `cc` alias test to alias visibility in a masked
  directory. Convert the build-environment `PATH` test to: dependency `PATH`
  entries are visible; an ambient tool in a hidden directory is absent.
- Stage worker (`test/install_worker/test_stage.py`): port
  `test_expansion_roots_include_gzip_helper_chain`,
  `test_tool_runtime_roots_include_selected_tool_and_dependency_closure`, and
  a namespace version of `test_stage_policy_allows_git_version`: `git
  --version` succeeds, `/dev/urandom` is readable, and `mkdtemp` lands in the
  scoped tree.
- Installer grants (`test_enable_sandbox_paths`): derive the writable
  inventory cases (jobserver FIFO, scoped `TMPDIR`/`TMP`/`TEMP`,
  `tempfile.tempdir`, `HOME`, `XDG_CACHE_HOME`, `JAVA_TOOL_OPTIONS`) and the
  symlinked `allow_read` case (original and resolved path). Drop its broad
  `/tmp` and resource-limit assertions.
- Stage lifecycle: in disposable install-child tests, inspect `spack-src` and
  `config.log` from the supervisor after a failed build, verify `spack stage`
  remains browsable, and verify success removes the stage unless keep-stage
  was requested. Check that a successful prefix is visible, failed-prefix
  keep/rollback works, and attempted writes into package recipes or Spack
  core source fail with `EROFS` while stage/prefix writes succeed.
- Keep live tests in a disposable `sys.executable -c` child, as in
  `test_sandbox_namespaces.py`. Add a shared helper only when three or more
  tests need it.

## Next selected work

- [x] A1, `sandbox: load namespace policy data`. Track `sandbox.yaml` without
  `commands` and track `linux-header-policy.yaml`; do not add
  `commands/df`. Add lazy loaders in `spack.installer.build` that validate
  the version, list types, alias keys, and safe relative header paths, and
  report the file and key in an `InstallError`. Add the namespace sections
  (hidden roots, replacement roots, device nodes, stage programs), validated
  by the loader and unused by the live worker. Tests cover the shipped files
  and each malformed case. Documentation: the policy-data paragraph in
  `lib/spack/docs/sandbox/namespace-backend.rst`.

- [x] A2, `sandbox: select concrete compilers and system headers`. Use the
  loaded policy data to select only the concrete compiler languages and exact
  header trees required by the DAG; keep this selection dormant and focused
  on compiler/header evidence. The helpers deduplicate language-edge and
  compiler-node selections, apply the non-GCC libstdc++ major cap, and expose
  unselected GCC installations for later masking. Tests cover synthetic DAGs,
  mixed language selection, system versus non-system compilers, and GCC versus
  non-GCC header selection.

- [x] A3, `sandbox: resolve compiler helpers, aliases, and stage tools`. Use
  the selected compiler paths to resolve subordinate programs and files,
  preserve searched spellings and aliases, and select the fetch/expansion
  tool closure without activating the policy. The dormant selectors reject
  bare compiler answers, skip Spack binutils wrappers, preserve aliases,
  include executable support data, add Git's helper directory, and include
  Spack tool prefixes with link/run dependencies.

- [x] B1, `sandbox: add passthrough, replacement, and generated-symlink
  policy entries`. Use the A3 spelling/source records to materialize aliases
  inside masked directories while replacing derived parent masks. The dormant
  policy model now validates replacement sources at explicit hidden roots,
  preserves lexical alias paths, and creates generated symlinks in the private
  mask source without activating the worker.

- [x] B2, `sandbox: allocate durable mount-plan scratch`, including
  supervisor-owned cleanup and collision tests. Allocate a unique mode-0700
  lease from a canonical non-symlink base outside all hidden, replacement,
  stage, prefix, and writable-policy roots. Require the supervisor to own
  cleanup, reject live workers and stale-path replacement, and cover
  concurrent allocation, setup cleanup, symlinked bases, and collisions.

- [x] B3, `sandbox: prove a read-only view with explicit writable mounts`.
  Selected-tree plans now mark inherited mounts recursively read-only and
  explicitly restore writable bind mounts. Capability, fake-libc, installer,
  and real-namespace tests cover `MOUNT_ATTR_RDONLY`, `EROFS`, and writable
  restoration without changing the live worker.

- [x] C1, `installer: preserve host-visible stage and prefix lifecycles`,
  using a stable per-build stage parent and supervisor-owned prefix pivot.

- [ ] D3, complete private `/dev` construction. Keep the existing selected
  device bind mounts, including `/dev/urandom`; add a fresh per-worker tmpfs
  at `/dev/shm` and generated `/dev/fd`, `/dev/stdin`, `/dev/stdout`, and
  `/dev/stderr` links. First extend and validate the immutable mount plan,
  then update capability probing and trusted pre-thread activation, and prove
  descriptor-link behavior, writable shared memory, worker/host isolation,
  and fail-closed setup in disposable namespaces. Do not broaden host device
  exposure or add config settings. Whole-install lifecycle proof is separate.

## Proposed sequence

### D3 commit steps

1. [x] D3.1: represent fresh writable tmpfs mounts in immutable policy/plan
  data, validate conflicts before mutation, retain literal generated symlink
  targets such as `/proc/self/fd`, and apply tmpfs after read-only setup.
2. [x] D3.2: select private shared memory and descriptor links from versioned
  policy data; extend capability probing and test fatal activation failures
  before authority drop and `Tee`. Preserve the device allowlist and fallback.
3. [ ] D3.3: prove real device/link/shared-memory behavior, host and concurrent
  worker isolation, and post-setup authority removal; update documentation,
  close D3 with evidence, and select whole-child lifecycle verification.

Each item is one commit with focused tests and documentation. Suggested PR
grouping: A1-A3, B1-B3, and C1-C4.

- [x] C2, `sandbox: select host, device, and worker-state inputs`.
- [x] C3, `sandbox: validate selected policies before worker threads`.
- [x] C4, `sandbox: record real compiler build evidence`. A disposable
  namespace child applied the compiled policy, expanded a tar archive, ran
  Git, configure, Make, GCC C/C++, Clang C/C++, and GNU Fortran, and left all
  build outputs visible in the parent source directory. Host evidence is
  recorded below.
- [x] Select activation: apply the complete selected tree automatically in
  the worker when namespaces are available. Keep Landlock as the existing
  capability fallback only; do not add a config.yaml opt-in or diagnostic
  switch.
- [x] D1, make the production-selected activation policy compilable. Treat
  visible read-only candidates as inherited passthrough, restore writable
  paths outside hidden roots after the recursive read-only remount, classify
  devices only as writable, canonicalize Python runtime paths, and pass the
  configured fetch cache rather than the misc cache. Focused production-path,
  planner, and installer tests cover the resulting policy.
- [x] D2, configure the scoped worker home and temporary environment at
  activation. Carry the worker root in the immutable activation payload; set
  `HOME`, `XDG_CACHE_HOME`, `TMPDIR`, `TMP`, `TEMP`, `tempfile.tempdir`, and
  Java `user.home`/`java.io.tmpdir` before `Tee` and recipe-controlled setup.
  Prove all generated paths stay below the scoped worker root and remain
  writable through the selected policy.

  ### D2 commit steps

  Each step is a separate commit; all three are now verified.

  1. [x] D2.1, carry the worker root and configure the confined child. Validate
    writable identity-mount coverage before mounting; create private home,
    cache, and temporary directories after mount setup and authority drop.
    Set POSIX, Python, and Java defaults before `Tee`; preserve inherited Java
    options. The focused activation regression includes paths with spaces.
  2. [x] D2.2, prove containment and failure isolation. Cover invalid roots,
    pre-existing child paths, setup failures, unchanged fallback and parent
    state, environment cleaning, and real-namespace writable state.
  3. [x] D2.3, reconcile the backend documentation and both review ledgers with
    verified evidence and limitations. Close D2 and select private `/dev`
    construction as D3, keeping full install-child lifecycle proof separate.

Next is D3 `/dev` construction, followed by the whole writable inventory and
stage/prefix success and failure semantics through a complete install-child
lifecycle. D2's disposable activation test does not close that lifecycle gap.

## Accepted improvements

- Accepted durable, supervisor-cleaned mount-plan scratch for the first
  activation. Detached mounts are optional later hardening.
- Accepted the current stage contract: host-visible contents remain after
  failure, success removes them unless keep-stage is requested, and restaging
  remains supported. Use a stable per-build mount parent so the child retains
  this lifecycle without making the stage itself a mount point.
- Accepted supervisor-owned prefix pivot and rollback around the worker while
  preserving the exact install path, keep-prefix behavior, BinaryCacheMiss
  restoration, and prefix locking.
- Spack repositories and core source remain read-only; writable stage and
  prefix mounts do not imply writable grants to their parent roots.

## Worklog

- 2026-09-26: Reviewed the earlier one-commit and phased drafts. Split the
  item into Phases A-C with one commit per step. Identified that the worker
  prepares the namespace before staging, that derived parent masks would hide
  `/usr/lib` and `/tmp`, and that identity mounts of the stage and prefix
  conflict with `PrefixPivoter` because mount points cannot be renamed or
  removed.
- 2026-09-26: Replaced Landlock-era mechanisms with namespace-native designs:
  a `/tmp` replacement root, a recursively read-only view with explicit
  writable mounts, hidden `/dev` with device restores, and alias symlinks
  instead of a symlink farm. Proposed detached mounts in place of durable
  scratch.
- 2026-09-26: Extracted reusable cases from the earlier `sandbox.py`,
  `install_worker/test_stage.py`, and `util/ld_so_conf.py` tests, noted the
  duplicated block in `sandbox.py`, and converted denial assertions to
  `ENOENT` and `EROFS`. Selected A1 as the next commit.
- 2026-09-26: Clarified that mount-plan "sources" are bind-mount inputs and
  setup scratch, not staged build sources. Recorded host-visible stage and
  prefix retention as independent requirements; Spack recipes and core
  sources remain read-only. The detached-mount proposal is still a separate
  unaccepted implementation choice, and C1 now requires a proven lifecycle
  design rather than assuming that moving prefix pivoting alone solves it.
- 2026-09-26: Accepted the compatibility-first choices after review. Keep
  durable scratch with supervisor cleanup; preserve child-owned stage cleanup
  through a stable per-build host-backed parent; move prefix pivot, rollback,
  and failed-prefix cleanup to the supervisor because the exact prefix is a
  child mount point. Deferred detached mounts to optional hardening.
- 2026-09-26: Documented the detached-mount threat model and five design
  variants in
  [namespace-mount-plan-alternatives.md](namespace-mount-plan-alternatives.md).
  Durable scratch protects against package code but retains a narrow pre-build
  same-UID pathname-substitution window. Detached mounts shorten that window
  and eliminate host endpoint artifacts, but do not snapshot mutable source
  contents or close the lookup race without trusted descriptor-relative
  resolution. Kept option 1 as the baseline; identified the fully detached
  option 4 as the only coherent later prototype and rejected partial hybrids
  for Phase 4 due to poor security benefit relative to code, probe,
  failure-path, and compatibility cost.
- 2026-09-26: Completed A1 by adding lazy, fail-closed loaders for the
  versioned sandbox and Linux header YAML files. Removed the Landlock command
  stub from the tracked policy, added validated hidden-root, replacement-root,
  device-node, and stage-program sections, and kept all data unused by the
  live worker. Added malformed version/list/alias/header-path tests and
  documented the policy-data boundary. Selected A2, concrete compiler and
  system-header selection, as the next work item.
- 2026-09-26: Completed A2 with dormant compiler-language and system-header
  selectors. Language edges are filtered by the loaded compiler vocabulary,
  supported compiler nodes are discovered with the repository-aware compiler
  API, and duplicate selections are removed deterministically. Header paths
  come from the validated policy; system GCC installations, GCC-internal
  headers, Linux UAPI headers, and the non-GCC libstdc++ major cap are handled
  without changing the live worker. Synthetic DAG and header-layout tests
  passed. Selected A3, compiler helper, alias, and stage-tool resolution.
- 2026-09-26: Completed A3 with dormant compiler helper and stage-tool
  selectors. Compiler `-print-*` answers are accepted only when absolute;
  reported Spack binutils wrappers are ignored, while canonical source paths
  remain separate from each searched spelling. Selected compiler aliases,
  `cpp`'s `cc1`, `file` magic data, Git's `--exec-path`, the `gunzip` helper
  chain, and Spack-built tool link/run dependency prefixes are covered by
  focused tests. The live worker remains unchanged. Selected B1, explicit
  passthrough/replacement entries and generated alias symlinks.
- 2026-09-26: Completed B1 with explicit hidden-root and passthrough policy
  inputs, caller-provided replacement mounts, and generated symlink entries.
  Alias paths remain lexical while their sources are canonicalized; the mount
  planner creates aliases in the private mask source and mounts replacements
  before restoring nested selections. The installer no longer derives parent
  masks for the selected-tree compiler. Focused policy, mount-plan, and
  installer tests passed. Selected B2, durable supervisor-cleaned mount-plan
  scratch and collision validation.
- 2026-09-26: Completed B2 with a supervisor-owned durable scratch lease for
  mount endpoints. Allocation uses a canonical non-symlink base, creates
  unique mode-0700 directories outside policy and lifecycle roots, and rejects
  root overlap and active allocation collisions. Cleanup is owner-only,
  refuses live attached workers and replaced scratch inodes, and remains
  usable for setup failure and abnormal worker exit. Concurrent allocation,
  symlinked-base, collision, live-worker, stale-path, and cleanup tests pass.
  The live worker remains unchanged. Selected B3, a recursively read-only
  namespace view with explicit writable mounts.
- 2026-09-26: Completed B3 with an explicit read-only-view flag for dormant
  selected-tree plans. The plan makes the inherited mount tree recursively
  read-only after trusted endpoint preparation, clears read-only only on
  explicit writable bind mounts, and leaves the legacy narrow planner order
  and live worker unchanged. The capability probe, fake-libc assertions,
  installer-plan checks, and disposable real-namespace test prove recursive
  setup, `EROFS` passthrough denial, and writable restoration. Selected C1,
  host-visible stage and prefix lifecycle preservation.
- 2026-09-26: Completed C1 by adding a unique host-backed stage parent for
  each build, moving any existing named stage below that parent, and passing
  the parent as the writable sandbox view. The child retains stage contents on
  failure while the supervisor removes successful stages after reaping it;
  cache-miss retries discard unused stage parents. The supervisor now prepares
  an empty exact prefix target, owns prefix pivot and rollback finalization,
  preserves keep-prefix and BinaryCacheMiss behavior, and keeps the prefix
  lock across the transaction. Focused lifecycle, installer, and prefix
  integration tests passed; C2 host/device and worker-state input selection is
  now selected.
- 2026-09-26: Completed C2 with a dormant, fail-closed selector for explicit
  hidden and replacement roots, dynamic-linker and policy runtime candidates,
  real device nodes, Spack source and configuration paths, repositories,
  dependency prefixes, caches, stage/prefix/log paths, jobserver FIFOs, and a
  scoped worker directory. Missing explicit worker paths and non-canonical
  spellings fail before policy compilation; unavailable host candidates are
  omitted. Focused synthetic-host tests pass, and the live worker remains
  unchanged. Selected C3, pre-thread policy validation.
- 2026-09-26: Completed C3 with a configuration-free pre-thread validation
  wrapper for the immutable selected policy and mount plan. Validation is
  unconditional whenever selected paths and mount-plan scratch are supplied;
  invalid paths fail before capability freeze, sandbox acquisition, or mount
  preparation. The dormant live worker remains on the narrow mask because C4
  must first provide complete real-build inputs. Namespace and installer policy
  suites passed. Selected C4, disposable real compiler build evidence.
- 2026-09-26: Completed C4 with a disposable real-kernel namespace test. On
  Linux 6.18.33.2-microsoft-standard-WSL2, GLIBC 2.39, and Python 3.12.3, the
  compiled policy applied a recursively read-only view with an explicit
  writable source mount. GCC 13.3.0 C/C++, Clang 18.1.3 C/C++, GNU Fortran
  13.3.0, Make 4.3, Git 2.43.0, GNU tar 1.35, and a configure/Make source
  build all passed; compiled outputs were visible from the parent. The live
  worker remains unchanged. Selected activation as the next commit; no
  config.yaml option is added.
- 2026-09-26: Activated the selected namespace policy automatically from
  trusted installer setup. The parent now selects compiler, tool, header,
  runtime, device, repository, stage, prefix, cache, log, and jobserver inputs,
  allocates supervisor-cleaned mount-plan scratch, and passes only the
  immutable policy to the child. Namespace activation does not construct or
  apply Landlock; the existing Landlock backend remains the capability-only
  fallback. Added a project-wide directive against new config.yaml options
  because older Spack versions reject unknown bootstrap keys. Selected full
  install-child lifecycle evidence as the next work item.
- 2026-09-26: Completed D1 after the first production-path activation test
  exposed that `/dev/null` was selected both read-only and writable, the
  configured misc cache was passed as the fetch cache, `sys.executable` could
  be non-canonical, and visible read-only candidates were compiled as invalid
  restoration mounts. Read-only passthrough now relies on the recursively
  read-only inherited view, explicit writable identity mounts may be outside
  hidden roots, an exact read-only selection cancels the matching hidden-root
  candidate, a narrower writable mount may override a selected read-only
  ancestor, and duplicate runtime/device selections resolve to writable device
  access. `/usr/lib*` remain policy candidates pending real-install evidence,
  while their exact runtime selections currently keep them as read-only
  passthrough trees. Selected
  D2 worker-local home and temporary environment setup next.
- 2026-09-26: Completed D2.1. The immutable activation payload now carries
  the worker root. The child validates its canonical directory and writable
  identity-mount coverage before mounting, then creates mode-0700 home,
  cache, and temporary directories after dropping mount authority. It sets
  `HOME`, `XDG_CACHE_HOME`, `TMPDIR`, `TMP`, `TEMP`, `tempfile.tempdir`, and
  quoted Java home/temporary defaults before `Tee`, retaining inherited Java
  options. The activation regression and affected namespace, shared sandbox,
  and installer suites passed (141 tests). D2.2 failure, containment, and
  real-kernel evidence is next; D2 is not yet complete.
- 2026-09-26: Completed D2.2. Focused checks reject missing, non-directory,
  relative, symlinked, non-normalized, and unselected worker roots, and refuse
  pre-existing home/cache/temp paths without publishing environment changes.
  Mount/authority failures never start `Tee` or configure the environment;
  disabled/fallback paths and trusted parent preparation remain unchanged.
  Standard environment cleaning preserves the scoped defaults. A disposable
  namespace proves mode-0700 directories, writable HOME/XDG/POSIX/Python temp
  paths, hidden home contents, inherited `EROFS`, replacement-temp writes,
  host-visible worker files, and parent environment/tempfile isolation.
  The installed Java runtime reports the scoped properties even with spaces
  and both quote types in paths, overriding older values in inherited
  `JAVA_TOOL_OPTIONS` while retaining other options.
- 2026-09-26: D2.2 testing found two blockers and repaired them: worker-root
  validation accepted `..` spellings, and masking a replacement source's
  ancestor removed that source before it was bound. The root must now equal
  its canonical absolute spelling. Replacement sources are pinned in durable
  scratch before masks, just like other selected sources. This is trusted
  pre-thread mount authority; recipe code still starts only after authority
  drop. Failed setup remains fatal, never an unconstrained retry. The accepted
  same-UID pre-bind substitution limitation is unchanged. All 161 affected
  tests and Ruff checks pass. D2.3 documentation reconciliation is next;
  complete install-child lifecycle evidence remains deferred.
- 2026-09-26: Completed D2.3 and closed D2 after its three separately
  committed steps. Documented the activation sequence, inherited Java option
  handling, parent/fallback isolation, and focused real-kernel/JVM evidence in
  `namespace-backend.rst`. Environment values are defaults, not confinement:
  recipes and other Java option channels can override them while mounts still
  enforce access. Isolated Sphinx validation checks this page without running
  repository-update or generated-API hooks. Selected D3 private `/dev`
  construction next; retained the full install-child lifecycle gap and the
  accepted same-UID pre-bind substitution limitation.
- 2026-09-26: Completed D3.1. Immutable policy and plan now carry fresh tmpfs
  targets below hidden roots, reject invalid/conflicting targets before entry,
  and mount mode-1777 shared memory with `nosuid,nodev` after read-only tree
  setup. Generated symlink targets remain literal: resolving `/proc/self/fd`
  in the supervisor would incorrectly pin the supervisor PID. Trusted
  pre-thread creation retains child-relative resolution; inherited descriptor
  minimization remains out of scope. Thirteen focused model/alias tests and
  Ruff pass. Production selection and capability probing are D3.2.
- 2026-09-26: Completed D3.2. Versioned policy now selects `/dev/shm` and
  literal fd/stdin/stdout/stderr links without changing the device allowlist.
  Trusted production assembly passes them through immutable policy validation.
  The disposable probe checks writable tmpfs, descriptor-link access, and a
  device-file bind after recursive read-only setup and capability drop. Device
  probing uses `O_WRONLY`, avoiding unnecessary create/truncate flags on its
  synthetic shared-memory alias. Actual link/tmpfs setup failures remain fatal
  before `Tee`, environment publication, or fallback. All 198 affected tests
  pass, including live namespace checks, and Ruff passes. D3.3 real `/dev`
  behavior and concurrent isolation are next. Skip Sphinx builds for this work
  per the user's directive; retain documentation and whitespace checks.
