# Namespace mount-plan implementation alternatives

This design note records the threat model and rejected or deferred variants
behind the mount-plan scratch decisions in
[phase-4-input-selection.md](phase-4-input-selection.md). The Phase 4 ledger
is authoritative for implementation scope; this note is supporting material
for revisiting those decisions.

## Scratch and detached-mount threat model

The current durable-scratch sequence validates a source pathname, then later
bind-mounts that pathname onto a scratch endpoint. Once the bind succeeds, the
mount pins the selected filesystem object: renaming or replacing the original
pathname does not redirect the preserved mount. The exposed interval is only
between validation and that first bind. Recipe and build-tool code is not
running then, so this is not an escape available to the package being built.
The relevant adversary is another process able to mutate a selected path or
its ancestors concurrently, commonly another process under the same UID or a
compromised writable source directory.

A detached mount narrows that interval by converting the source pathname to a
mount file descriptor before masks are applied. It does not eliminate the
pathname lookup race by itself: `open_tree(path, OPEN_TREE_CLONE)` can resolve
a substituted path. Full anchoring needs descriptor-relative lookup of trusted
ancestors, for example `openat2` with suitable `RESOLVE_*` restrictions,
followed by an API that clones the verified mount. It also does not snapshot
file contents. A host process that can write the selected inode or filesystem
can still change data visible through either a bind mount or a detached clone.
Read-only mount attributes prevent sandbox writes through that view; they do
not prevent writes through another host view.

Durable scratch must be inaccessible to build code and other users, but its
directory entries are ordinary host-filesystem objects and can remain after a
crash. The mounts themselves remain private to the child's mount namespace.
Detached mounts remove those host-visible endpoint directories and are
destroyed when their file descriptors and namespace references close, but
introduce more capability-bearing descriptors whose inheritance and cleanup
must be audited.

## Alternatives

### 1. Durable scratch with pathname bind mounts

This is the accepted Phase 4 baseline.

- Security benefit: validates the complete plan before namespace mutation;
  isolates each worker's endpoints in a mode-0700 directory; pins every
  source after its first successful bind; keeps mask sources on read-only
  tmpfs; and preserves the existing fatal-after-mutation rule. This protects
  against package code because all binds finish and mount authority is dropped
  before `Tee` or recipe-controlled work starts.
- Residual risk: a mutable source or ancestor can be replaced between
  validation and its first bind. A same-UID process can potentially manipulate
  an inadequately protected scratch base. Stale empty directories can remain
  after supervisor death. Source contents remain mutable through other host
  views unless the underlying source is trusted immutable state.
- Code cost: low. It extends the existing plan, `mount(MS_BIND)`, tmpfs, and
  cleanup paths rather than adding a second mount engine. Required changes are
  secure allocation, ownership/lifecycle metadata, collision checks, parent
  cleanup, and concurrency/abnormal-exit tests.
- Compatibility cost: lowest. It uses operations already covered by the
  namespace capability probe and current live tests. Filesystem requirements
  are limited to creating private directories and mount endpoints.

### 2. Detached mask tmpfs only

This variant uses `fsopen`/`fsconfig`/`fsmount` for mask filesystems.

- Security benefit: removes the host-visible `spack-empty-host-dirs` tmpfs
  mount point and its stale-directory cleanup. The empty mask filesystem can
  be configured and made read-only before it is attached.
- Residual risk: preserved sources still use pathname binds and durable
  `spack-preserved-host-paths`, so the source-selection race remains. Final
  attachment targets are still path-resolved unless separately anchored.
- Code cost: medium to high for a small gain. It needs ctypes definitions,
  syscall error normalization, descriptor lifecycle, capability probing, and
  duplicate old/new application paths while retaining most scratch code.
- Compatibility cost: higher than the baseline. Container seccomp profiles
  or kernels may permit legacy `mount` but reject the newer mount API; every
  operation needs a disposable probe and fallback decision.
- Decision: do not implement as an intermediate step unless endpoint cleanup
  becomes an observed reliability problem.

### 3. Detached clones attached through durable scratch

This variant uses `open_tree` and then `move_mount` for preserved sources.

- Security benefit: after `open_tree` returns, later source-path replacement
  cannot redirect the cloned mount. This shortens the source race and can make
  mount attributes apply to a private clone before exposure.
- Residual risk: the `open_tree` lookup itself is still racy without trusted
  descriptor-relative resolution; source contents can still change; durable
  endpoint allocation and cleanup remain; and final targets remain subject to
  target-path validation.
- Code cost: high. It adds mount-FD ownership and move semantics while keeping
  the old scratch tree and cleanup. Plans can no longer be purely immutable
  pathname data if descriptors are opened during compilation, so validation,
  acquisition, application, and cleanup need distinct types and failure
  boundaries.
- Compatibility cost: requires successful clone and move operations from an
  unprivileged user namespace on each supported host/filesystem combination.
- Decision: reject for Phase 4; it carries most detached-mount complexity
  without removing scratch or fully closing the race.

### 4. Fully detached plan

This variant combines cloned preserved sources with detached mask filesystems
moved directly to final targets. It is the only detached design selected for a
possible future prototype.

- Security benefit: removes both durable endpoint trees, minimizes
  host-filesystem setup artifacts, pins each selected mount after acquisition,
  allows attributes on private clones before exposure, and makes abnormal
  cleanup mostly descriptor/namespace lifetime. Combined with trusted
  descriptor-relative source and target resolution, this can close the
  validation-to-bind substitution window rather than merely shorten it.
- Residual risk: without `openat2`-style ancestor restrictions it still opens
  the wrong object if substitution wins before acquisition. It does not make
  mutable compiler, repository, or runtime contents immutable. Destination
  directories and generated endpoints still require safe creation and type
  checks. Leaked descriptors can retain mounts and consume kernel resources.
- Code cost: very high. Add wrappers and probes for `open_tree`, `move_mount`,
  `fsopen`, `fsconfig`, `fsmount`, and probably `openat2`; model mount FDs and
  ownership; enforce close-on-exec; define rollback for every partially
  acquired/applied state; update capability diagnostics; and duplicate or
  replace most of `_apply_namespace_mount_plan`. Tests must cover descriptor
  leaks, ordering, partial failure, recursive mounts, files, directories,
  generated paths, and source/target substitution.
- Compatibility cost: greatest. The effective platform floor may already be
  high enough for the syscalls, but availability must be tested rather than
  inferred from kernel version or libc symbols. LSMs, container profiles,
  filesystems, and user-namespace ownership rules can reject clone or attach
  operations independently of legacy bind mounts.
- Decision: strongest eventual hardening, but defer to a separate increment
  after the durable-scratch policy is active and measured. Adopt only if its
  threat reduction justifies a second mount implementation or a complete
  replacement of the first one.

### 5. Descriptor-verified sources with legacy bind mounts

- Security benefit: `openat2`/`O_PATH` can verify source identity and reject
  symlink or mount traversal before application.
- Residual risk: Linux legacy bind mount accepts a pathname, not an arbitrary
  source FD. Binding `/proc/self/fd/N` reintroduces procfs and magic-link
  semantics and still needs careful identity verification; it is not a clean
  descriptor-only mount. Scratch and final target paths remain.
- Code cost: medium to high, with subtle procfs dependencies and less clear
  assurance than the fully detached design.
- Decision: use descriptor-relative validation to harden pathname selection
  only if it can be followed by an identity recheck immediately before the
  bind. Do not advertise it as closing the race.

## Decision record

Phase 4 implements option 1 and accepts its narrow same-UID TOCTOU residual
risk. Option 4 is the only detached design worth a future prototype because it
offers a coherent security and cleanup benefit. Options 2, 3, and 5 add
substantial code without delivering that complete property. None of these
options changes the host-backed stage and prefix contract.
