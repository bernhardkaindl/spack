..
   Copyright Spack Project Developers. See COPYRIGHT file for details.

   SPDX-License-Identifier: (Apache-2.0 OR MIT)

.. _concretizer-internals:

Concretizer Architecture and APIs
=================================

This page describes how the current Clingo-based concretizer is integrated into Spack.
It is a developer guide to control flow, API ownership, state, and extension boundaries.
For generated signatures and member lists, see :doc:`spack.solver`.
For commands that inspect ASP facts and run Clingo directly, see :ref:`debugging-concretization`.

The interfaces described here are internal Python APIs unless another document declares them public.
Update this page when a change moves concretization ownership or alters the sequence below.

Conceptual Layers
-----------------

The integration has five layers:

.. list-table:: Current concretizer layers
   :header-rows: 1
   :widths: 25 35 40

   * - Layer
     - Primary owner
     - Responsibility
   * - Commands
     - ``spack.cmd``, ``spack.cmd.spec``, ``spack.cmd.concretize``, and
       ``spack.cmd.install``
     - Parse user input, select an active environment, and present results.
   * - Environment adaptation
     - ``spack.environment.environment.EnvironmentConcretizer``
     - Select a unification strategy, supply reusable specs, and add validated concrete roots to
       an environment.
   * - High-level concretization
     - ``spack.concretize``
     - Provide one-spec, together, when-possible, and separate solve operations and emit frontend
       progress events.
   * - Solver orchestration
     - ``spack.solver.asp.Solver`` and ``PyclingoDriver``
     - Select reusable specs, prepare solver setup, manage the concretization cache, invoke Clingo,
       reconstruct specs, and post-process results.
   * - Problem construction
     - ``spack.solver.asp.SpackSolverSetup``
     - Convert input specs, configuration, repositories, package recipes, compilers, externals, and
       reuse candidates into ASP facts.

Normal Command Paths
--------------------

``spack spec`` with explicit specs uses ``spack.cmd.parse_specs(concretize=True)``.
An active environment without explicit specs calls ``Environment.concretize()``.

.. code-block:: text

   spack.cmd.spec.spec()
     -> spack.cmd.parse_specs(concretize=True), or Environment.concretize()
     -> a shared spack.concretize strategy
     -> Solver.solve() or Solver.solve_in_rounds()

``spack concretize`` requires an active environment and delegates to ``Environment.concretize()``.

.. code-block:: text

   spack.cmd.concretize.concretize()
     -> Environment.concretize()
     -> EnvironmentConcretizer.concretize()
     -> a shared spack.concretize strategy
     -> Solver.solve() or Solver.solve_in_rounds()

``spack install`` uses the same paths when its inputs are not already concrete.
With an active environment it adds or resolves environment roots and calls ``Environment.concretize()``.
Without an active environment it calls ``spack.cmd.parse_specs(concretize=True)``.

Command modules do not implement solver behavior.
Changes intended to cover every normal concretization caller should normally enter through ``spack.concretize`` or a lower shared layer.

High-Level Concretization API
-----------------------------

``spack.concretize`` is the normal integration API for callers that need concrete specs.
Its inputs use ``SpecPairInput``, a pair of an abstract root and either an existing concrete root or ``None``.
Its multi-spec functions return ``SpecPair`` values associating each abstract input with a concrete result.

``concretize_one(spec, tests=False, factory=None)``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Concretizes one string or ``Spec``.
It resolves hash references, returns a copy of an already concrete input, rejects anonymous nodes, and otherwise performs one ``Solver.solve()`` call.
For a virtual root, it selects the provider node from the best answer.

Use this function when the caller needs one concrete spec and does not need environment mutation or multi-root unification.

``concretize_together(spec_list, ...)``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Runs one solve for all inputs.
Every root must be satisfiable in one unified answer.
Existing concrete roots in the pairs constrain that answer.

``concretize_together_when_possible(spec_list, ...)``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Calls ``Solver.solve_in_rounds()``.
Each round solves the largest compatible subset selected by the ASP encoding, adds its concrete results to the reuse candidates, and continues with unsolved inputs.
The function returns one association for every solved user root.

``concretize_separately(spec_list, ...)``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Runs one independent solve per abstract root.
On platforms where Spack enables multiprocessing, roots can be solved in parallel through ``spack.util.parallel.imap_unordered()``.
Before starting children, the parent ensures Clingo is importable, prepares the provider index, and initializes compiler configuration to avoid bootstrap and write-lock races.
Each pool task handles at most one root.

``tests`` and ``factory``
~~~~~~~~~~~~~~~~~~~~~~~~~

Every high-level operation accepts ``tests``:

* ``False`` excludes test dependencies;
* ``True`` enables test dependencies for every package; and
* an iterable of package names enables them for those packages.

The optional ``factory`` implements the reuse-source contract consumed by ``ReusableSpecsSelector``.
Environment concretization supplies ``ReusableSpecsFactory`` so the solver can consider retained environment roots, included environments, installed specs, build caches, externals, and configured reuse policy as appropriate.

Frontend Contract
-----------------

Multi-spec helpers accept a ``ConcretizerUI``.
``HeadlessUI`` is the no-op default and ``TerminalUI`` reports group and per-spec progress.
The frontend receives ``on_group_started()``, ``on_concretization_started()``, and ``on_spec_concretized()`` events.

The process that owns the frontend emits every event from one thread.
Parallel solve children do not call the frontend or write progress directly.
Preserve this ownership when changing process or concurrency boundaries.

Environment Integration
-----------------------

``EnvironmentConcretizer`` adapts environment state to the shared API:

#. synchronize or clear existing concrete state;
#. process manifest groups in dependency order;
#. apply each group's configuration override;
#. partition new roots from concrete roots that should be retained;
#. construct a ``ReusableSpecsFactory``;
#. choose ``together``, ``when_possible``, or ``separately`` from ``concretizer:unify``;
#. call the corresponding ``spack.concretize`` function; and
#. add new concrete roots and unify shared nodes in the environment.

The shared solver functions return data; ``EnvironmentConcretizer`` owns environment mutation and enhances selected errors with environment-specific guidance.
Writing the manifest or lockfile remains outside the low-level solver.

Solver API
----------

``Solver`` is the main external interface of ``spack.solver.asp``.
Construction initializes compiler configuration, a ``ConcretizationCache``, implicit external configuration, and ``ReusableSpecsSelector``.
A ``Solver`` instance can perform one solve or a related sequence of rounds.

``Solver.solve(specs, **kwargs)``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Returns a ``Result`` and hides timing and Clingo statistics.
It delegates to ``solve_with_stats()``.

``Solver.solve_with_stats(specs, ...)``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Returns ``(result, timer, statistics)``.
It resolves hash references, combines concrete input nodes with selected reusable specs, creates ``SpackSolverSetup``, and delegates to ``PyclingoDriver.solve()``.

The optional ``out`` stream receives generated ASP.
``setup_only=True`` stops after problem construction and is used by solver diagnostics.
``timers`` and ``stats`` control presentation in the driver; ordinary callers should normally use the high-level API and its UI instead.

``Solver.solve_in_rounds(specs, ...)``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Yields one ``Result`` per when-possible round.
It configures setup so not every input must be solved in one model.
After each result, solved DAGs become reuse candidates and only unsolved inputs continue.

Solver Execution Sequence
-------------------------

For a normal one-shot solve:

#. ``Solver`` resolves input hashes and selects reusable specs.
#. ``SpackSolverSetup`` is created for the requested test-dependency policy.
#. ``PyclingoDriver`` creates a Clingo control and chooses the applicable ``.lp`` control files.
#. ``SpackSolverSetup.setup()`` validates inputs, discovers possible packages, imports package classes, and emits configuration, recipe, compiler, external, reuse, and root facts.
#. The driver strips and deterministically orders the generated problem.
#. If enabled, the concretization cache is queried using the problem and control-file contents.
#. On a cache miss, Clingo loads, grounds, and solves the program.
#. ``SpecBuilder`` reconstructs native ``Spec`` DAGs from the best model, using reusable-spec and package state retained by setup.
#. Errors encoded in the model are raised, and optimization criteria and model counts are recorded.
#. A cacheable result is stored before post-processing.
#. Post-processing applies compiler-runtime, splicing, external, develop-spec, and final validation behavior.

Recipe Evaluation
-----------------

Recipe evaluation occurs during ``SpackSolverSetup.setup()`` before Clingo can solve the complete problem.
Setup discovers possible package names and loads classes with ``spack.repo.PATH.get_pkg_class()``.
It reads recipe versions, variants, dependencies, conflicts, providers, requirements, and other directive-derived metadata.

Compiler package recipes can provide ``runtime_constraints()`` callbacks.
Those callbacks receive a ``RuntimePropertyRecorder`` tied to the current setup and emit solve-local constraints and facts.
Directive dictionaries and runtime constraints therefore need to remain available to setup and result construction, but they are not a persistent environment or command API.

Recipe code also observes the effective Spack context, including configuration, repositories, platform, active environment, compilers, externals, store, and reuse sources.
Changes that move recipe evaluation across a process boundary must preserve or deliberately replace that context and must account for imports that occurred before the boundary.

Driver and ASP Boundary
-----------------------

``ProblemInstanceBuilder.asp_problem`` is a list of ASP statement strings.
The driver selects base control files such as ``concretize.lp``, ``heuristic.lp``, ``display.lp``, and ``direct_dependency.lp``, then adds compatibility, splicing, or when-possible files according to the setup.

Although the ASP problem is serializable text, it is not the complete current interface between setup and result construction.
``SpecBuilder`` uses ``setup.reusable_and_possible`` to reconnect reused specs, errors and possible dependency reporting use setup-derived state, and post-processing operates on reconstructed native specs.
Treat a split at this boundary as an architectural change, not a transport-only refactor.

``Result`` Contract
-------------------

``Result`` stores the abstract inputs, satisfiability, optimization answers and criteria, warnings, model count, and possible dependencies.
Its main derived views are:

* ``specs``: concrete roots satisfying solved inputs;
* ``specs_by_input``: abstract-to-concrete associations; and
* ``unsolved_specs``: unsolved inputs paired with any candidate returned by the solver.

These views are computed from the best answer.
``Result.to_dict()`` serializes satisfiable solver results for the concretization cache; ``Result.from_dict()`` restores them using the caller's input specs as authoritative.
The cache representation is an internal persistence format, not by itself a command-worker trust protocol.

State, I/O, and Side Effects
----------------------------

Concretization is not a pure function of the input spec string.
Normal solving reads configuration, package repositories, provider indexes, compiler properties, external and installed specs, build-cache metadata, active environment state, and Clingo control files.

It can also require preparation or writes:

* Clingo may be bootstrapped before solving;
* repository and provider indexes can be refreshed;
* compiler configuration can be initialized;
* compiler-property detection can execute a selected compiler and populate its cache; and
* the concretization cache can be read and updated.

Callers that add process isolation, concurrency, or read-only operation must inventory these effects explicitly.
The current separate-solve implementation provides examples of parent-side preparation used to avoid races, but it is a multiprocessing optimization rather than a security boundary.

Extension Guidance
------------------

When changing concretization integration:

* put command parsing and presentation in command modules;
* keep environment mutation in ``EnvironmentConcretizer`` or ``Environment``;
* use ``spack.concretize`` for behavior shared by commands, environments, and direct callers;
* keep reuse selection and solve-session state in ``Solver``;
* keep recipe-to-ASP translation in ``SpackSolverSetup``;
* keep Clingo lifecycle, cache lookup, model reconstruction, and post-processing coordinated in ``PyclingoDriver`` unless a separately reviewed architecture changes that boundary;
* preserve ``ConcretizerUI`` ownership when adding parallelism; and
* test all three unification strategies when changing a shared boundary.

Do not add a command-specific solver path for behavior that should also apply to environments, implicit installation, or direct callers.

Tests and Diagnostics
---------------------

Focused concretizer tests live under ``lib/spack/spack/test/concretization/``.
Command and environment suites cover their adapters and mutation behavior.
For changes to shared orchestration, start with the narrow affected concretization test, then cover together, when-possible, and separate strategies as applicable.

Use ``spack solve --show=asp`` to inspect generated facts.
See :ref:`debugging-concretization` for direct Clingo commands and the focused solver test command.

Related Documentation
---------------------

* :doc:`spack.solver`: generated Python API reference.
* :ref:`debugging-concretization`: ASP and Clingo debugging workflow.
* :doc:`environments`: user-facing environment concretization behavior.
* :doc:`build_settings`: user-facing concretizer configuration.
* :doc:`sandbox/concretizer-worker`: proposed confined-worker architecture.
* :doc:`sandbox/concretizer-worker-review`: worker-specific implementation review guide.
