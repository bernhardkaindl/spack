..
   Copyright Spack Project Developers. See COPYRIGHT file for details.

   SPDX-License-Identifier: (Apache-2.0 OR MIT)

Adaptive Build Memory Scheduling
================================

This page plans Linux memory-aware admission and parallelism control for the existing installer
scheduler.  It is not implemented.  Until this project lands, installer stage and build workers do
not impose an address-space, data, or stack ceiling; limits inherited from the invoking process or
service still apply.

Goals
-----

The installer should admit a build only when the host can absorb its memory demand without entering
memory pressure.  It should defer rather than fail a build when pressure is temporary.  While builds
are running, it should reduce their jobserver allocations promptly under pressure and restore
parallelism gradually after recovery.

Linux Memory Model
------------------

At each admission decision, the trusted installer parent will read a consistent sample from
``/proc/meminfo``.  The initial model will consider at least:

* ``MemTotal``, ``MemAvailable``, ``SwapTotal``, and ``SwapFree``;
* reclaimable page cache and slab memory;
* resident memory that cannot be displaced into currently free swap; and
* a reserve that protects the operating system and non-Spack workloads.

The candidate per-build ceiling is relative to host memory, not a fixed byte count.  When free swap
can hold the currently swappable allocation, the ceiling may approach ``MemTotal`` after subtracting
the allocated memory that cannot be swapped and the safety reserve.  The exact accounting formula,
reserve, minimum admission size, and treatment of cgroup limits require measurements and focused
tests before implementation.

Pressure Signals
----------------

Admission must stop when the host is already under pressure.  Initial signals to evaluate are:

* active swap-in or swap-out, sampled as rates rather than lifetime counters;
* Linux pressure stall information from ``/proc/pressure/memory``;
* page reclaim, scan, and steal rates from ``/proc/vmstat``;
* very small buffer, cache, or reclaimable memory relative to active allocations; and
* cgroup v2 ``memory.current``, ``memory.max``, ``memory.events``, and pressure data when Spack runs
  inside a constrained cgroup.

No single instantaneous counter should cause oscillation.  Thresholds, sampling intervals,
hysteresis, and recovery windows remain design decisions.  Missing optional kernel metrics must be
reported through debug diagnostics and handled by a documented conservative fallback.

Admission And Retry
-------------------

The installer scheduler owns admission.  Before each individual build job starts, it will calculate
the current host budget and proposed per-build rlimit.  Under pressure or below the required reserve,
it will:

* leave the DAG node pending without acquiring or retaining its prefix lock;
* report that launch is deferred because of host memory pressure;
* retry on later scheduler samples after other work releases memory; and
* preserve dependency ordering, cancellation, failure propagation, and UI state.

This is scheduler deferral, not a build failure and not recursive installer invocation.  Fairness and
starvation bounds must be specified before implementation.

Running Build Throttling
------------------------

When pressure appears after builds have started, the scheduler will temporarily reduce every active
build's jobserver target to one.  It must use the existing structured jobserver control channel and
observe actual token uptake; signals or environment rewrites are not a substitute.

After pressure has remained below recovery thresholds, parallelism will return in controlled steps:

#. Select the longest-running build that has not recovered its requested job count.
#. Raise its target by one bounded step.
#. Observe its actual job count and host memory signals through a settling interval.
#. Continue that build toward the user-requested value only while pressure remains controlled.
#. Move to the next-longest-running build and repeat.

Any renewed pressure immediately returns all active targets to one.  Recovery state must survive
normal scheduler iterations without allowing a newly launched build to bypass throttling.

Trust And Failure Boundaries
----------------------------

Only the trusted installer parent reads host pressure data, computes limits, changes jobserver
targets, and decides admission.  Package recipes and build processes cannot provide authoritative
pressure values or request a larger limit.  Malformed or inconsistent kernel data must fail the
admission calculation conservatively without converting already running builds into package
failures.

Validation Plan
---------------

Implementation requires focused tests for:

* deterministic ``/proc/meminfo``, PSI, vmstat, and cgroup parsers;
* swappable and unswappable accounting, reserves, overflow, and unavailable metrics;
* pressure admission deferral followed by successful retry without lock leakage;
* immediate reduction of all running jobserver targets to one;
* longest-running-first, stepwise recovery with observed actual job counts;
* renewed pressure during recovery and repeated pressure/recovery cycles;
* cancellation, dependency ordering, fairness, UI reporting, and database behavior; and
* Linux integration tests that induce bounded memory and swap pressure without risking the test
  host.

No adaptive rlimit should become the default until synthetic scheduler tests and controlled Linux
integration tests demonstrate stable admission, throttling, and recovery.