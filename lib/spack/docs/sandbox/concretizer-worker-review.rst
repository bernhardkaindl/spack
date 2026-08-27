..
   Copyright Spack Project Developers. See COPYRIGHT file for details.

   SPDX-License-Identifier: (Apache-2.0 OR MIT)

Concretizer Worker Reviewer Guide
=================================

This guide gives reviewers a code-oriented map of the proposed :doc:`concretizer-worker` project.
It explains how normal commands reach the solver, where recipes run, why the whole-concretizer worker is recommended, and which claims and invariants should be checked during implementation.

This is supporting review material, not a second plan or an implementation status page.
The plan owns architecture approval, milestones, and acceptance criteria.
The guide should be updated when implementation changes the call paths or resolves a proof point.
See :ref:`concretizer-internals` for the current solver integration independent of this worker proposal.

Correction to the Initial Exploration
-------------------------------------

An initial exploration reached this conclusion:

   Package classes must be evaluated in the parent because their behavior depends on configuration, compilers, the host platform, the active environment, and solver-local side effects.

The observations about dependencies are useful, but the process conclusion does not follow.
The accurate conclusion is:

   Package classes must be evaluated in the same effective Spack context as solver setup, and all recipe-derived state needed by Clingo and result construction must remain available to those consumers.

A forked worker inherits the parent's in-memory configuration, repository path, active environment, platform state, store handles, and imported runtime modules.
If the worker performs recipe discovery, setup, Clingo, and result construction together, it does not need to serialize those Python objects or return recipe-mutated class state to the parent.

.. list-table:: Assessment of the initial claims
   :header-rows: 1
   :widths: 28 20 52

   * - Initial claim
     - Assessment
     - Reviewer interpretation
   * - Dynamic recipe behavior uses active configuration, compilers, platform, and environment.
     - Correct.
     - This requires equivalent worker context, not parent execution.
       ``fork()`` provides an initial copy; tests must prove that parent preflight has prepared all
       required state without importing selected recipes.
   * - Package directives inject dependencies and other metadata.
     - Correct, but local.
     - Directive descriptors lazily populate package-class dictionaries in the process that reads
       them.
       Keeping setup and its consumers in one worker preserves this state.
   * - ``runtime_constraints()`` modifies global solver state.
     - Imprecise.
     - Compiler recipe callbacks receive a ``RuntimePropertyRecorder`` tied to one
       ``SpackSolverSetup``.
       It records injected dependencies, version constraints, and ASP rules for that solve.
       That state can remain worker-local.
   * - Recipes register external packages during setup.
     - Not established by the inspected path.
     - ``Solver`` derives implicit externals from trusted configuration.
       Review implementation evidence before granting any parent mutation to recipe code.
   * - Side effects require parent execution.
     - Incorrect.
     - Side effects are the reason to confine recipes.
       Required solver-local effects must occur in the worker; persistent parent effects require an
       explicit trusted protocol and must not be inferred from worker output.
   * - Therefore a whole-concretizer worker is contradicted.
     - Incorrect.
     - A whole-concretizer worker avoids exporting the coupled setup state.
       Splitting after setup is the option that requires a new semantic protocol.

What Cannot Be Deferred
~~~~~~~~~~~~~~~~~~~~~~~

Recipe evaluation cannot be deferred until after ASP setup because setup reads recipe versions, variants, dependencies, conflicts, providers, requirements, and compiler runtime constraints.
It can be relocated with setup into a confined worker.

Likewise, model reconstruction cannot be cleanly separated from setup by sending only ASP text.
``PyclingoDriver._run_clingo()`` uses setup state for reusable-spec lookup and possible-package reporting, and error and post-processing paths are coupled to native specs and setup behavior.
An ASP-only transport would therefore need more than the already serializable problem string.

The important distinction for reviewers is:

* **ordering constraint:** recipes must run before Clingo receives the complete problem; and
* **process constraint:** recipes should run after worker confinement, not necessarily in the original parent.

Normal Call Paths
-----------------

``spack spec``
~~~~~~~~~~~~~~

.. code-block:: text

   spack.cmd.spec.spec()
     -> spack.cmd.parse_specs(concretize=True)
     -> spack.cmd._concretize_spec_pairs()
     -> spack.concretize.concretize_together(),
        concretize_together_when_possible(), or concretize_separately()
     -> spack.solver.asp.Solver.solve() or solve_in_rounds()

``spack concretize``
~~~~~~~~~~~~~~~~~~~~

.. code-block:: text

   spack.cmd.concretize.concretize()
     -> Environment.concretize()
     -> EnvironmentConcretizer.concretize()
     -> one of the shared spack.concretize strategies
     -> Solver.solve() or solve_in_rounds()

Implicit ``spack install`` concretization
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: text

   spack.cmd.install.install()
     -> active environment: Environment.concretize()
     -> no active environment: spack.cmd.parse_specs(concretize=True)
     -> one of the shared spack.concretize strategies
     -> Solver.solve() or solve_in_rounds()

The commands do not need independent worker implementations.
The shared integration belongs below command parsing and environment mutation, around the solve operations selected by ``spack.concretize``.

Recipe Evaluation and Solve Sequence
------------------------------------

The current one-shot sequence is:

#. ``Solver.__init__()`` initializes compiler configuration, the concretization cache, implicit externals, and reusable-spec selection.
#. ``Solver.solve_with_stats()`` resolves hash references and reusable specs and creates ``SpackSolverSetup``.
#. ``PyclingoDriver.solve()`` creates the Clingo control and selects control files.
#. ``SpackSolverSetup.setup()`` validates inputs and discovers possible packages.
#. Setup imports package classes through ``spack.repo.PATH.get_pkg_class()`` and evaluates package metadata and compiler runtime constraints.
#. The driver orders and optionally caches the ASP problem.
#. Clingo grounds and solves the problem.
#. ``SpecBuilder`` reconstructs concrete native specs using setup state.
#. The driver applies post-concretization transformations and cache updates.

In the proposed architecture, steps 1 through 9 execute in one confined worker after trusted parent preflight.
The parent receives only a bounded response containing concrete native specs and approved metadata.

State and Side-Effect Analysis
------------------------------

Recipe directives
~~~~~~~~~~~~~~~~~

``DirectiveMeta`` records directive callables while a package class is defined.
``DirectiveDictDescriptor`` lazily executes the relevant callables when setup reads dictionaries such as dependencies, conflicts, versions, or providers.
Those mutations populate the package class in the current process.

This behavior supports the whole-worker design:

* the confined worker imports the class;
* setup reads and populates its directive dictionaries;
* later setup operations in that worker see the populated state; and
* worker exit discards the class and its mutations.

It also creates the documented fork limitation.
If the parent imported a package before forking, the child inherits that already evaluated class and confinement cannot protect the earlier import.

Runtime constraints
~~~~~~~~~~~~~~~~~~~

``SpackSolverSetup.define_runtime_constraints()`` imports compiler package classes and calls an optional ``runtime_constraints()`` callback.
The callback receives ``RuntimePropertyRecorder``, which writes rules and constraints into the current setup object.
This is solve-local state, not evidence that the parent must run the callback.

Configuration, environment, and platform
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Setup reads ``spack.config.CONFIG``, ``active_environment()``, compiler configuration, platform defaults, repository indexes, the store, and reuse sources.
A forked worker initially sees the same objects and process environment.

Reviewers must still check:

* parent preflight does not import selected or transitive recipes;
* the worker does not depend on a mutable parent action after ``fork()``;
* file-backed inputs cannot change between policy selection and worker reads without detection; and
* worker-local configuration or environment mutation cannot affect later parent actions.

Real Preflight and Confinement Proof Points
-------------------------------------------

Compiler-property detection
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Solver setup calls ``CompilerPropertyDetector.default_libc()`` for candidate compilers.
On a compiler-cache miss, ``FileCompilerCache`` can:

* create a temporary directory and source file;
* execute the selected C or C++ compiler;
* write a compiler-property cache entry; and
* read dynamic-loader and library paths.

This conflicts with a worker policy that unconditionally denies all writes and process execution.
The initial implementation must choose and test one narrow solution:

* trusted parent preflight populates the compiler-property cache without importing package recipes;
* the worker receives narrowly selected compiler execution and temporary/cache write capabilities; or
* solver setup is changed to consume a parent-prepared, validated compiler-property input.

The first option best matches the approved preflight direction, but code and tests must prove it is complete.
Missing preflight must fail clearly; it must not cause unrestricted fallback or broad worker grants.

Repository and provider indexes
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Possible-dependency analysis queries package classes and merged provider indexes.
The existing separate-concretization path prepares the provider index before starting parallel children to avoid lock and write races.

Reviewers should require an inventory of index operations that may write.
Indexes should be prepared by the trusted parent only when preparation does not import selected recipes, or exposed to the worker through separately documented narrow cache paths.

Concretization cache
~~~~~~~~~~~~~~~~~~~~

The architecture permits the worker to read and write dedicated concretization-cache paths.
The parent must derive those paths from trusted configuration before recipe evaluation.
Recipe data must not select or widen a cache path.

Bootstrap
~~~~~~~~~

Clingo must be importable before irreversible process and filesystem restrictions prevent bootstrap execution.
If bootstrap necessarily imports recipes, only an explicit small trusted bootstrap set with dedicated review may run in the parent.
This exception must not become a route for ordinary requested recipes.

Worker transport and ordering
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``run_json_worker()`` currently forks, closes inherited descriptors, redirects standard streams, calls the setup function, and only then reads and dispatches the request.
That ordering can apply confinement before request parsing or worker dispatch imports a recipe.

Reviewers must verify that:

* the worker callable's module import in the parent does not import package recipes;
* setup receives parent-selected paths only;
* no lazy object representation or error formatting imports a recipe before setup;
* raw worker standard output remains diagnostic data, never trusted terminal output; and
* timeout and cancellation kill the worker process group and reap all descriptors and children.

Architecture Comparison for Review
----------------------------------

The plan compares two real alternatives:

* **recipe-data split:** worker evaluates recipes and exports ASP plus reconstruction state; parent runs Clingo and reconstructs specs; and
* **whole concretizer:** worker performs setup through post-processing and exports concrete native specs.

The ASP problem is text, but text serializability does not make the recipe-data split small.
The split must also preserve reusable-spec lookup, possible packages, diagnostics, criteria, statistics, cache semantics, and post-processing behavior.

Review estimates are intentionally ranges:

.. list-table:: Planning estimates
   :header-rows: 1
   :widths: 35 30 35

   * - Area
     - Recipe-data split
     - Whole concretizer
   * - Production
     - 800--1,500 lines
     - 350--700 lines
   * - Tests
     - 700--1,300 lines
     - 450--900 lines
   * - Documentation
     - 200--350 lines
     - 150--250 lines
   * - Total
     - 1,700--3,150 lines
     - 950--1,850 lines

These estimates are not acceptance targets.
Reviewers should challenge them after the launcher-neutral one-shot prototype measures the actual protocol, diagnostics, cache, and preflight changes.

Implementation Ownership Map
----------------------------

Expected ownership, with names still provisional:

* ``spack.concretize``: shared high-level integration and strategy behavior;
* ``spack.solver.asp``: unchanged existing solver behavior plus only narrow adapter points that are proven necessary;
* a dedicated concretizer-worker module: request and response validation, policy selection, launcher-neutral worker entry point, and result restoration;
* ``spack.sandbox``: concretizer-specific filesystem, network, process, IPC, and resource policy;
* ``spack.util.sandbox``: shared bounded byte transport and lifecycle behavior; and
* dedicated worker tests: protocol, policy, confinement, lifecycle, and parity.

Command modules should receive no worker policy.
Environment lockfile mutation, install continuation, scheduling, UI, and terminal presentation stay in the parent.

Review Sequence
---------------

Review the implementation in vertical increments:

1. Contract and measurements
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

* Confirm abstract requests and concrete responses are native-spec data, not pickle or recipe objects.
* Require rejection of duplicate keys, unknown fields, stale protocol versions, malformed specs, invalid or oversized frames, and root mismatches.
* Require scalable transport for large valid requests and concrete DAGs rather than deriving a supported maximum from fixtures.
* Preserve existing solver timeout behavior and inherited resource limits; do not add default solver memory or wall-clock ceilings merely because execution moved into a worker.
* Keep frame, diagnostic, and total transport-resource safeguards independent of expected solve complexity and require a clear diagnostic rather than in-process retry when one is exceeded.
* Validate ordered input/result association and native-spec integrity in the parent.
  Keep virtual-provider and other recipe-dependent satisfaction checks in the worker; rebuilding provider metadata in the parent would execute the recipe code being confined.

2. Launcher-neutral parity
~~~~~~~~~~~~~~~~~~~~~~~~~~

* Run the unchanged ``Solver`` through a worker with no confinement first.
* Compare DAG hashes and errors for versions, variants, virtuals, conflicts, externals, reuse, splicing, compilers, targets, tests, and unsatisfiable inputs.
* Prove cache-enabled and cache-disabled behavior.

3. Confinement
~~~~~~~~~~~~~~

* Instrument recipe imports and prove setup runs before the first selected recipe import.
* Test denied writes, sockets, arbitrary execution, IPC, inactive repositories, and unrelated host paths at the kernel boundary.
* Resolve compiler probing, index preparation, bootstrap, and cache writes with individual policy tests rather than broad directory grants.

4. Shared strategy integration
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

* Cover one-shot, together, when-possible multi-round, and separate solves.
* Keep one worker for a together or multi-round operation.
* Allow one worker per separate root while capping concurrency at ``-j`` or configured jobs.
* Avoid worker grandchildren and a persistent service in the initial implementation.

5. Command and API coverage
~~~~~~~~~~~~~~~~~~~~~~~~~~~

* Verify ``spack spec`` text, YAML, JSON, and custom formats.
* Verify environment concretization and atomic lockfile mutation.
* Verify implicit installation uses the same returned concrete specs and existing installer path.
* Verify unsupported-host fallback only under ``config:sandbox:allow_fallback``.
* Verify user-visible warning and error ordering and machine-readable output cleanliness.

Reviewer Failure Checklist
--------------------------

Reject or request changes if an implementation:

* imports a selected or transitive recipe in the parent before confinement;
* sends pickle, package instances, configuration objects, or arbitrary Python object graphs across the boundary;
* introduces a second solver, concretizer command, environment mutation path, or installer path;
* trusts worker-provided paths, repository roots, cache roots, exception types, or terminal output;
* retries in-process after worker recipe evaluation begins;
* grants broad execution or write access to accommodate an unmeasured compiler or cache operation;
* nests sandbox workers under the existing separate-solve process pool without explicit lifecycle design;
* updates an environment or starts installation before validating the complete response; or
* claims confinement on fallback or on recipe imports that occurred before ``fork()``.

Prior Branch Material
---------------------

The abandoned ``progress-v3+sandbox-fixes-v2`` branch is not a concretizer-worker template.
Its relevant lessons concern Landlock path policy, compiler and support-tool discovery, process supervision, and direct kernel-boundary tests.

Do not revive its separate sandbox-install command or broad installer architecture.
Concretization remains integrated with normal commands and the existing ``spack.concretize`` and ``Solver`` infrastructure.

Reviewer Source Map
-------------------

Start with these implementation surfaces:

* ``spack.concretize`` for shared together, when-possible, separate, and one-spec strategies;
* ``spack.environment.environment.EnvironmentConcretizer`` for environment strategy selection and mutation ordering;
* ``spack.solver.asp.Solver`` for reusable-spec and setup ownership;
* ``spack.solver.asp.PyclingoDriver`` for setup, cache, Clingo, reconstruction, and post-processing;
* ``spack.solver.asp.SpackSolverSetup`` for recipe and compiler metadata evaluation;
* ``spack.solver.input_analysis`` for possible-dependency recipe access;
* ``spack.solver.runtimes.RuntimePropertyRecorder`` for compiler recipe callbacks;
* ``spack.compilers.libraries.CompilerPropertyDetector`` for execution and cache proof points;
* ``spack.directives_meta`` for lazy recipe directive mutation;
* ``spack.util.sandbox`` for process transport and setup ordering; and
* ``spack.sandbox`` for Landlock, seccomp, and rlimit policy.

Use :doc:`concretizer-worker` as the authority when this guide and the approved plan differ.
