..
   Copyright Spack Project Developers. See COPYRIGHT file for details.

   SPDX-License-Identifier: (Apache-2.0 OR MIT)

Concretizer Worker Plan
=======================

This page plans confinement of recipe evaluation performed during concretization.
It extends the existing concretizer instead of adding a command, solver, or package-data model.
See :doc:`concretizer-worker-review` for the code paths, analysis of the initial exploration, and implementation review checklist.

The target covers all normal paths through ``spack.concretize`` and the Clingo-based solver, including:

* ``spack spec``;
* ``spack concretize`` for an active environment;
* implicit concretization by ``spack install``; and
* direct callers of the shared concretization API.

Checklist notation:

* [x] planning work is complete;
* [ ] implementation or validation work remains.

Planning status:

* [x] Trace the command, environment, and install paths to the shared concretizer.
* [x] Compare a recipe-data worker with a whole-concretizer worker.
* [x] Review applicable material from ``progress-v3+sandbox-fixes-v2``.
* [x] Prepare a recommended implementation direction and architecture comparison.
* [x] Resolve fallback, initial scope, preflight, cache, and worker-lifetime decisions.
* [x] Resolve the remaining decision gates at the end of this page.
* [x] Record final approval of the architecture decision package.
* [ ] Implement the scalable worker contract without changing solve semantics.
* [ ] Apply confinement before the first recipe import in the worker.
* [ ] Integrate every shared concretization strategy.
* [ ] Validate command, environment, install, fallback, and direct-API behavior.

Current Concretization Boundary
-------------------------------

``spack spec`` and implicit command concretization call the helpers in ``spack.concretize``.
Environment concretization selects one of the same together, when-possible, or separate helpers.
Those helpers construct ``spack.solver.asp.Solver`` and call ``solve()`` or ``solve_in_rounds()``.

``Solver`` currently keeps recipe evaluation and solving closely coupled.
``SpackSolverSetup.setup()`` discovers possible packages and imports package classes to emit ASP facts.
``PyclingoDriver`` then:

#. orders the generated problem;
#. consults the concretization cache;
#. loads and runs Clingo;
#. reconstructs concrete specs using setup state;
#. reports recipe-aware errors; and
#. applies post-concretization transformations.

Model reconstruction depends on state accumulated while evaluating recipes, including reusable specs and possible package names.
There is no versioned, recipe-independent solver-input contract at this boundary today.

The existing ``concretize_separately`` path already uses child processes for parallel solves.
That is useful process-lifecycle experience, but it is not a security boundary: it serializes broader Spack state, does not apply recipe-import confinement before solving, and does not use the bounded JSON worker protocol.

Architecture Alternatives
-------------------------

Alternative A: extract recipe data, solve in the parent
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

In this design, a confined worker imports recipes and emits all data needed for a solve.
The trusted parent runs Clingo and reconstructs concrete specs.

The smallest plausible output is not a simple list of versions, dependencies, and conflicts.
It is the complete ordered ASP problem plus the setup-derived state required by model reconstruction, diagnostics, reuse, splicing, externals, compiler handling, and post-processing.

Benefits:

* Clingo remains in the trusted parent and can reuse a long-lived solver process.
* A validated recipe-fact representation could support inspection, caching, or remote solving.
* The parent could impose additional semantic validation before passing facts to Clingo.
* The worker lifetime could end immediately after recipe evaluation.

Costs and risks:

* It creates a new intermediate protocol coupled to Spack's internal ASP encoding.
* The protocol must represent setup state used after Clingo returns, not only ASP text.
* Parent validation of relationships among facts is substantially more complex than structural JSON validation.
* Solver setup, result construction, errors, cache keys, statistics, and post-processing must be separated without changing semantics.
* Every new recipe directive or solver feature can require a protocol change.
* Repeating recipe-derived behavior in parent validation risks importing recipes in the parent and defeating the boundary.

Estimated implementation size, based on the current coupling in ``spack.solver.asp``:

.. list-table:: Alternative A rough cost
   :header-rows: 1
   :widths: 35 20 45

   * - Area
     - Estimated lines
     - Main work
   * - Production
     - 800--1,500
     - Split setup, define and validate the intermediate protocol, restore setup state,
       and integrate policy and fallback.
   * - Tests
     - 700--1,300
     - Protocol rejection, semantic parity, every solver feature family, lifecycle, and commands.
   * - Documentation
     - 200--350
     - Protocol, validation, trust boundary, compatibility, and maintenance rules.
   * - Total
     - 1,700--3,150
     - Approximately four to eight engineering weeks before broad solver regression testing.

These ranges exclude redesign prompted by an intermediate representation that cannot reconstruct all current ``Result`` behavior.
The first implementation milestone would need a prototype before accepting the estimate.

Alternative B: run the existing concretizer in one worker
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

In this design, one confined worker performs solver setup, recipe import and evaluation, Clingo execution, result construction, and post-processing.
It returns concrete specs and bounded solve metadata through a versioned native-spec protocol.

The trusted parent still selects policy, prepares safe prerequisites, launches the process, validates the response, updates an environment or continues installation, and presents output.
The worker inherits the already resolved Spack configuration and active repository state at ``fork()``; the request does not serialize arbitrary Python configuration objects.

Benefits:

* Recipe imports occur after confinement is active.
* Existing ``Solver``, ``SpackSolverSetup``, ``PyclingoDriver``, and ``SpecBuilder`` behavior stays together.
* The protocol contains user inputs and final native specs rather than internal ASP facts.
* Changes to recipe directives and the ASP encoding usually remain internal to the worker.
* Worker termination also reclaims Clingo and recipe-import process state.
* This is the shortest path to coverage of normal command and environment behavior.

Costs and risks:

* The worker must be allowed to read Clingo modules and control files in addition to repositories and the Spack runtime.
* Clingo bootstrap, repository indexes, compiler configuration, and other required writes must be completed by the parent or narrowly designed before confinement.
* The current single-message four-MiB worker transport is not suitable for arbitrarily large valid environments and concrete DAGs.
  The concretizer needs streaming, multiple bounded frames, or an equivalent scalable transport.
* The generic 120-second worker timeout and fixed one-GiB recipe-import memory limit are not valid concretizer defaults.
  Existing solver timeout semantics and limits inherited from the invoking process or service must be preserved unless an administrator explicitly configures a narrower resource policy.
* Solver progress, timers, statistics, warnings, and rich exception details need explicit structured transport rather than raw worker standard output.
* Fork-only launch means unsupported hosts need the existing in-process path.

Estimated implementation size:

.. list-table:: Alternative B rough cost
   :header-rows: 1
   :widths: 35 20 45

   * - Area
     - Estimated lines
     - Main work
   * - Production
     - 350--700
     - Worker contract and launcher, confinement policy, result restoration, capability selection,
       and adapters for all solve strategies.
   * - Tests
     - 450--900
     - Contract rejection, parity, confinement, fallback, lifecycle, commands, and environments.
   * - Documentation
     - 150--250
     - Contract, bounds, limitations, tests, and future uses.
   * - Total
     - 950--1,850
     - Approximately two to five engineering weeks before broad solver regression testing.

The lower bound covers one-shot ``solve()`` with ordinary concrete-spec responses.
The upper bound includes ``solve_in_rounds()``, output modes, cancellation, and representative large-environment tests.

Recommendation
--------------

Implement Alternative B first.
It protects the risky recipe evaluation while preserving the existing concretizer as one cohesive unit.
It also has the smaller implementation and compatibility surface.

Do not split recipe data from Clingo merely to keep Clingo in the parent.
Clingo itself is not the motivating threat, and the split would make an unstable internal ASP boundary into a security protocol.

Alternative A remains reasonable only if a second demonstrated use requires a stable solver-input artifact, such as remote solving, cross-solve caching, or independent fact auditing.
If that need appears, first add an internal setup/result split with parity tests, then design a versioned protocol from measured data.

Architecture Decision Proposal
------------------------------

Status: **proposed; final approval is not yet recorded**.

Approve Alternative B as the initial architecture:

* run recipe discovery, recipe evaluation, ASP setup, Clingo, model reconstruction, and post-processing in the same confined worker;
* return bounded native-spec data rather than an intermediate recipe-fact or ASP protocol; and
* retain Alternative A only as a possible later project when an independently justified consumer needs a stable solver-input artifact.

The decision is intentionally about ownership and trust boundaries, not a second concretizer.
The worker calls the existing ``Solver`` implementation.
The parent continues to call the existing high-level concretization APIs.

.. list-table:: Architecture decision scorecard
   :header-rows: 1
   :widths: 24 38 38

   * - Criterion
     - Alternative A: recipe data in worker
     - Alternative B: whole concretizer in worker
   * - Recipe-import confinement
     - Yes, but recipe-derived state crosses a new semantic boundary.
     - Yes, while setup and all consumers of setup state remain together.
   * - New security protocol
     - Complete ASP input plus reconstruction, diagnostics, reuse, and post-processing state.
     - Abstract inputs, concrete native specs, and bounded diagnostics and metadata.
   * - Existing solver changes
     - High: split ``SpackSolverSetup`` from result construction and post-processing.
     - Low: add a launcher-neutral adapter around the existing solve operation.
   * - Parent validation burden
     - Structural and semantic validation of internal solver facts and relationships.
     - Structural validation, native-spec restoration, and checks that outputs are concrete and
       correspond to requested roots.
   * - Compatibility risk
     - High: solver and recipe-feature changes can alter the cross-process protocol.
     - Moderate: output, errors, cache access, limits, and process lifecycle cross the boundary.
   * - Estimated change
     - 1,700--3,150 production, test, and documentation lines.
     - 950--1,850 production, test, and documentation lines.
   * - Estimated initial effort
     - Four to eight engineering weeks before broad regression testing.
     - Two to five engineering weeks before broad regression testing.
   * - Future remote-solving support
     - Strong foundation, but paid for before it has a required consumer.
     - Requires a later protocol if remote solving becomes a requirement.
   * - Reversibility
     - Low: an internal ASP protocol becomes a maintained compatibility surface.
     - Higher: the adapter can fall back to the unchanged in-process ``Solver`` path.

Why this is the recommended tradeoff
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Both alternatives confine the motivating risk: importing and evaluating recipes.
Alternative A does not provide a material security advantage for that goal.
It moves Clingo into the parent but expands the trusted parser and validator to include a new, solver-internal semantic protocol.

Alternative B has the smaller trusted cross-process contract and leaves tightly coupled solver state in one process.
Its principal cost is that Clingo and result construction share the recipe worker's restrictions.
The current code evidence indicates that this is less risky than splitting setup state from its consumers.

Consequences of approval
~~~~~~~~~~~~~~~~~~~~~~~~

Approval means:

* the first implementation must not introduce a recipe-fact DTO, exported ASP protocol, remote solver, persistent solver service, or separate concretizer command;
* worker output is untrusted until the parent validates the complete structured response;
* parent-side validation must verify protocol shape, transport safety limits, concrete native-spec structure, requested-root association, and all invariants that do not require importing recipes;
* recipe-dependent semantic validation remains inside the confined worker;
* cache writes are part of the initial confinement policy, not a reason to move solving back into the parent;
* unsupported-host compatibility uses the existing in-process implementation only according to ``config:sandbox:allow_fallback``; and
* an Alternative A prototype requires a new architecture decision and a demonstrated consumer.

Fork boundary limitation
~~~~~~~~~~~~~~~~~~~~~~~~

The initial launcher uses ``fork()`` on supported POSIX hosts.
Forking inherits modules and package classes already loaded by the parent.
Confinement therefore prevents side effects only for recipe imports and evaluation that occur after the worker applies its policy; it cannot undo recipe code that the parent ran earlier.

Normal command paths must launch the worker before importing any selected or transitive recipe.
Focused tests must prove that ordering.
A direct API caller that imported recipes before requesting concretization is outside this guarantee for those earlier imports.
Providing isolation from inherited Python state would require a fresh-interpreter or ``exec`` protocol and is not approved in the initial architecture.

Approval record
~~~~~~~~~~~~~~~

The final reviewer should change only the applicable approval boxes after reviewing this page:

* [x] Approve Alternative B as the initial concretizer-worker architecture.
* [x] Approve the parent and worker ownership described in :ref:`concretizer-worker-trust-boundary`.
* [x] Approve the agreed supporting decisions in :ref:`concretizer-worker-supporting-decisions`.
* [x] Accept the documented fork limitation for the initial implementation.
* [x] Reject Alternative A for the initial implementation without rejecting it for a future, separately justified solver-input protocol.

If the primary architecture is not approved, record which scorecard criterion changes the decision and reopen the affected cost, protocol, and trust-boundary sections before implementation.

.. _concretizer-worker-trust-boundary:

Trust Boundary
--------------

The trusted parent owns:

* command parsing and concretization policy selection;
* active environment, repository, configuration, compiler, store, and reuse selection;
* safe preflight work required before irreversible confinement;
* worker launch, existing solver timeout semantics, cancellation, cleanup, and fallback selection;
* transport resource policy and structural response validation;
* environment and lockfile mutation;
* continuation into the existing installer; and
* terminal and machine-readable output.

The confined worker owns:

* restoration and validation of abstract input specs;
* package discovery and recipe import;
* recipe directive and package metadata evaluation;
* ASP setup, Clingo execution, and model reconstruction;
* post-concretization transformations; and
* serialization of concrete specs and structured solve metadata.

The worker receives no direct network access and no arbitrary process-execution capability.
It reads all configured active repository roots, Spack and Python runtime files, Clingo modules and control files, and trusted configuration or database inputs proven necessary by focused tests.
Inactive repositories and unrelated host paths remain inaccessible.
The initial worker may write only its dedicated concretization-cache paths.
No other filesystem write access is granted.

Recipe-controlled text is untrusted data.
Raw worker standard output is never forwarded to the terminal or interpreted as a response.
All returned fields are validated before the parent constructs native specs or takes an action.
Recipe-controlled diagnostics and individual transport frames are bounded.
The protocol must also prevent an untrusted worker from exhausting parent memory or storage with an unending response, but this safety policy must not encode an expected maximum environment or DAG size.

Fallback Contract
-----------------

Worker use should be automatic when the required sandbox and process capabilities are available.
There is no new command and no per-command worker flag.

Use the shared ``config:sandbox:allow_fallback`` policy and retain its strict default.
When fallback is allowed, a missing ``fork()``, unsupported operating system, unavailable Landlock backend, or failed preflight capability probe selects the existing in-process concretizer.

Do not retry in-process after a worker has started recipe evaluation.
A timeout, malformed response, confinement denial, solver error, or worker crash must fail the operation with a bounded diagnostic.
Retrying would execute untrusted recipe code outside confinement and could duplicate side effects.

The fallback path is the current implementation, not a second concretizer.
Tests must force capability outcomes; they must not silently pass by falling back on a development host expected to support the worker.

Incremental Delivery Plan
-------------------------

1. Define and prove a scalable contract
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Start with the smallest one-shot ``concretize_one`` solve over test repositories.
Use scalable checked-in test-repository DAGs to prove that the protocol does not assume the current four-MiB single-message limit or another expected maximum solve size.
A representative real environment benchmark may record serialization overhead, peak memory, and runtime, but it does not define a supported maximum.

Moving the solver into a worker must not add a default limit on valid request size, concrete-DAG size, solver memory, or solve duration.
The implementation still needs transport safety because the worker response is untrusted:

* reject an individual frame whose declared size exceeds the protocol's frame limit before allocating it;
* bound recipe-controlled diagnostics;
* process large native-spec data incrementally or through multiple bounded frames; and
* apply an administrative total transport-resource ceiling only if needed to prevent unbounded parent memory or storage consumption.

Any total ceiling must be configurable, documented as a security resource policy rather than a normal solve-size expectation, and produce a specific diagnostic.
It must not silently select in-process fallback.

The versioned request should contain only JSON-compatible values:

* abstract native-spec dictionaries;
* test-dependency selection;
* allow-deprecated selection;
* solve strategy; and
* protocol version.

The response should contain:

* concrete native-spec dictionaries associated with their inputs;
* bounded warnings and user-facing solver diagnostics;
* only the timers or statistics required by an existing caller; and
* protocol version.

Acceptance checks:

* [ ] native abstract and concrete specs round trip without importing a recipe in the parent;
* [ ] duplicate keys, unknown fields, wrong types, stale versions, malformed specs, and invalid or oversized frames fail before parent-side mutation;
* [ ] a worker cannot use raw standard output as a response channel;
* [ ] large valid requests and concrete DAGs are not rejected because they exceed the shared command worker's single-message limit;
* [ ] moving to a worker adds no default solve-duration or solver-memory limit;
* [ ] existing ``concretizer:timeout`` and ``concretizer:error_on_timeout`` behavior is preserved;
* [ ] transport and diagnostic safety limits are independent of expected solve complexity; and
* [ ] Python 3.6-compatible syntax and existing native-spec formats are retained where feasible.

2. Prove one-shot solve parity before confinement
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Add a launcher-neutral worker function around the existing ``Solver.solve()``.
Run it through ``run_json_worker`` with a no-op setup first, then restore its concrete result in the parent.

Acceptance checks:

* [ ] direct and worker solves produce identical DAG hashes for representative versions, variants, virtuals, conflicts, externals, reuse, splicing, compiler, target, and test-dependency cases;
* [ ] unsatisfiable, invalid-variant, unknown-package, timeout, and internal errors retain useful exception categories and bounded messages;
* [ ] concretization-cache enabled and disabled behavior is explicitly tested; and
* [ ] interrupts, worker timeout, truncated responses, crashes, and descriptor cleanup are tested.

3. Apply recipe-import confinement
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Prepare all operations that legitimately write or execute before launching the worker.
This includes ensuring Clingo is importable and determining whether repository indexes, compiler configuration, reuse indexes, or cache files need parent-side preparation.

Parent preflight may prepare Clingo, indexes, configuration, and reuse inputs only where that work does not import ordinary package recipes.
If Clingo bootstrap cannot avoid recipes, define the selected bootstrap recipes as a small trusted set with dedicated review and tests; do not turn bootstrap into a general parent-side recipe-import exception.
Compiler-property detection may execute a selected compiler and write temporary and cache files on a cache miss.
The initial implementation must prove that parent preflight can populate this data without ordinary recipe imports, or document and test a narrower supervised capability before granting it to the worker.

Apply rlimits, Landlock, network denial, denial of unapproved process execution, and IPC denial before request parsing can cause a recipe import.
Add read paths only after a focused failure identifies a normal concretizer dependency.

Acceptance checks:

* [ ] the first selected and transitive recipe imports occur only after confinement is active;
* [ ] recipe attempts to write files, open sockets, execute unapproved programs, or use blocked IPC fail;
* [ ] inactive repositories and unrelated host paths are inaccessible;
* [ ] normal Clingo import and control-file reads succeed;
* [ ] compiler-property cache hits need no worker process execution or unrelated writes;
* [ ] a cache miss fails clearly unless a separately approved narrow mechanism handles it;
* [ ] only dedicated concretization-cache paths are writable;
* [ ] cache path selection cannot be influenced by recipe data; and
* [ ] direct kernel-boundary tests run on a supported Linux Landlock host.

4. Integrate all shared solve strategies
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Put capability selection and launch in a dedicated concretizer-worker module.
Keep ``spack.concretize`` as the shared behavioral entry point and give its current helper functions small adapters.
Do not put worker policy in ``cmd/spec.py``, ``cmd/concretize.py``, ``cmd/install.py``, or ``environment.py``.

Support in order:

* [ ] ``concretize_one`` and one-shot together solves;
* [ ] together solves with reuse factories;
* [ ] ``solve_in_rounds()`` for ``unify: when_possible``;
* [ ] separate solves without nesting the existing unconstrained process pool around sandbox workers; and
* [ ] direct shared-API output modes required by ``spack solve`` and tests.

A together or multi-round operation initially uses one newly forked worker for the operation.
The separate strategy uses one newly forked worker per root so independent solves can run in parallel.
The parent must cap concurrent workers at the command's ``-j`` value or Spack's configured parallel job limit.
Do not add a persistent service, worker reuse, or worker grandchildren in the first implementation.
Scheduling, cancellation, and UI progress remain parent-owned.

Acceptance checks:

* [ ] ``spack spec`` matches direct output in text, YAML, JSON, and format modes;
* [ ] ``spack concretize`` preserves together, when-possible, and separate environment behavior;
* [ ] ``spack install`` implicitly concretizes through the same worker and passes the returned concrete spec to the existing installer;
* [ ] warnings and errors preserve user-visible order and machine-readable output remains clean;
* [ ] timers and solver statistics retain semantic fields without requiring byte-for-byte presentation parity;
* [ ] already-concretized environments do not launch a solver worker unnecessarily;
* [ ] lockfile updates occur only after the complete response is validated; and
* [ ] worker and permitted fallback modes have focused command and environment coverage.

5. Broaden API and regression coverage
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

After focused command paths pass, exercise direct ``spack.concretize`` callers and representative solver suites in both modes.
Do not mechanically run every unit test in a worker; use parity tests at the shared boundary and retain detailed solver tests against the unchanged solver implementation.

Acceptance checks:

* [ ] bootstrap, buildcache, mirror, developer, and other direct concretization callers preserve behavior through the shared policy;
* [ ] a CI lane runs representative concretization suites with a real worker and no fallback;
* [ ] unsupported-host tests prove the configured direct fallback without claiming confinement;
* [ ] performance and peak memory regressions are recorded for small and large environments without turning those observations into compatibility ceilings; and
* [ ] status and roadmap documentation are updated as each milestone lands.

Prior Branch Assessment
-----------------------

``progress-v3+sandbox-fixes-v2`` did not implement a reusable concretizer-worker boundary.
Its useful material concerns build-worker filesystem policy, compiler tools, process supervision, and installer integration.

Do not revive its separate sandbox-install shape.
Concretization must remain in the normal ``spack spec``, environment, and install paths.
Reuse only general Landlock, lifecycle, diagnostics, and focused kernel-test lessons that still match the current sandbox modules.

.. _concretizer-worker-supporting-decisions:

Supporting Decisions
--------------------

The following constraints were agreed while preparing the architecture proposal.
They become implementation decisions when the architecture approval record is completed:

* [x] Concretization follows the strict shared ``config:sandbox:allow_fallback`` policy.
* [x] The first public surface covers normal commands and high-level ``spack.concretize`` callers; low-level solver diagnostics and setup-only modes may follow.
* [x] The parent may prepare non-recipe prerequisites before confinement.
  Unavoidable Clingo-bootstrap recipes require an explicit trusted and specially reviewed set.
* [x] The confined solver retains read and write access to dedicated concretization-cache paths.
* [x] Together and multi-round solves use one new worker per operation.
  Separate solves may fork one worker per root, with concurrency capped by ``-j`` or configured jobs.
* [x] The initial worker may read every configured active repository root, but not inactive repositories or unrelated host paths.
* [x] Scalable checked-in fixtures prove transport compatibility beyond ordinary command-worker message sizes.
  A representative real-environment benchmark may track overhead but does not define a supported maximum.
* [x] User-visible warning and error order is preserved.
  Timers and solver statistics require structured semantic equivalence rather than byte-for-byte presentation parity.

Future Uses
-----------

A successful whole-concretizer worker provides a confined source of concrete native specs for the existing installer worker path.
It can also support command hardening wherever a normal Spack operation calls the shared concretization helpers.

Potential later work includes a persistent solver service, remote solving, or a validated recipe-fact artifact.
Each would need a separate threat model and protocol; none is required to protect recipe evaluation in the first implementation.
