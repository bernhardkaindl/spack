..
   Copyright Spack Project Developers. See COPYRIGHT file for details.

   SPDX-License-Identifier: (Apache-2.0 OR MIT)

Sandbox Glossary
================

.. _sandbox-term-mount-authority-window:

.. glossary::

   mount-authority window
      The interval after the install child has entered a private user and
      mount namespace, but before Landlock has confined it. In that interval,
      code with namespace ``CAP_SYS_ADMIN`` can create bind mounts beneath a
      directory that a later recursive Landlock grant will allow. This can
      expose host content through an otherwise permitted path. The current
      backend closes this window by completing trusted mount setup and dropping
      namespace capabilities before any recipe-controlled Python runs.

   trusted mount setup
      The pre-thread install-child work that enters the user and mount
      namespace, makes mount propagation private, and creates the selected
      mask mounts. It is performed only by Spack-owned code.

   recipe-controlled Python
      Package hooks, patching, builder construction, and similar work that can
      execute package-defined Python. It is not trusted to hold mount
      authority.

   constrained fallback
      A sandbox backend that still restricts the worker. For Linux namespace
      selection, Landlock-only is constrained; direct unconstrained execution
      is not a namespace fallback.