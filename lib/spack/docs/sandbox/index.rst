..
   Copyright Spack Project Developers. See COPYRIGHT file for details.

   SPDX-License-Identifier: (Apache-2.0 OR MIT)

Sandbox
=======

This documentation tracks incremental hardening of normal Spack commands that import and evaluate package recipes.

Documentation map
-----------------

* :doc:`overview`: scope and shared trust boundary.
* :doc:`status`: implemented behavior and focused validation.
* :doc:`roadmap`: immediate command-hardening milestones.
* :doc:`planned-work`: later work, including namespace isolation.
* :doc:`design-decisions`: open design questions for review.
* :doc:`info-command`: the ``spack info`` worker contract.
* :doc:`checksum-command`: the ``spack checksum`` worker contract.
* :doc:`network-supervisor`: proxy and download-worker network contract.
* :doc:`install-worker`: staged design for the existing installer worker option.
* :doc:`concretizer-worker`: staged design for confined recipe evaluation during solving.
* :doc:`memory-pressure-scheduling`: planned Linux build admission and adaptive parallelism.

.. toctree::
   :maxdepth: 1

   overview
   status
   roadmap
   planned-work
   design-decisions
   info-command
   checksum-command
   network-supervisor
   install-worker
   concretizer-worker
   memory-pressure-scheduling
