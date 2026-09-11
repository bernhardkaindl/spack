..
   Copyright Spack Project Developers. See COPYRIGHT file for details.

   SPDX-License-Identifier: (Apache-2.0 OR MIT)

Design Decisions
================

This page records unresolved sandbox design questions for review.
It is not a statement of implemented behavior.

Download destination policy
---------------------------

The checksum command intentionally permits arbitrary public source authorities.
Normal installation needs an administrator-controlled policy before it grants a network worker.

.. list-table:: Open download-policy questions
   :header-rows: 1
   :widths: 30 70

   * - Topic
     - Question
   * - Policy source
     - Should the administrator configure URL prefixes, configured mirrors, or
       both?
   * - Package scope
     - How does policy identify packages permitted to make build-time downloads?
   * - Redirects
     - Current checksum behavior accepts redirects from an authorized server.
       Decide how a future restricted policy treats a changed authority or path.
   * - HTTPS paths
     - HTTPS ``CONNECT`` does not expose a path to the proxy.
       Decide whether authority-only policy is sufficient or TLS termination is
       ever acceptable.
   * - Precedence
     - Define interaction among command options, configuration, mirrors, and
       package metadata.
   * - ``spack checksum`` policy
     - Should future checksum policy remain limited to arbitrary public source
       authorities, or allow an administrator to restrict it?
