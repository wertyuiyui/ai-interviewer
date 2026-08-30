---
name: repository-reader
description: Read an application or technical GitHub repository for interview preparation by locating its product or technical mainline, tracing representative code paths, and separating core implementation from supporting infrastructure. Do not use for research papers.
---

# Repository reader

Turn an unfamiliar repository into a candidate-facing explanation and focused
interview practice. Treat summaries and repository documents as orientation;
ground claims and questions in the live source snapshot.

Use three phases:

1. Detect: identify the project type, runnable entry points, main modules,
   persistence or external integrations, and supporting build/deployment files.
2. Trace: choose one or two representative mainlines and follow them from an
   input or trigger through the core implementation to an observable result.
   A mainline can be an HTTP request, user action, job, message, CLI command or
   library call; do not force every repository into an HTTP-shaped flow.
3. Synthesize: explain what problem the project solves, why its design makes
   sense, how the central mechanism works, the candidate's responsibility and
   the most important trade-offs. Keep evidence review separate from the
   candidate-facing introduction.

Question priorities:

- application projects: user or business problem, design motivation, core
  feature flow, product/engineering trade-offs, then failure handling;
- technical projects: technical constraint, central mechanism and its concrete
  implementation, correctness/performance boundaries, alternatives and
  validation;
- partial responsibility: stay on the selected work and its interfaces.

Dockerfiles, dependency manifests, CI and deployment configuration describe
how a project is built or run. Use them as supporting context, not as the
default interview subject. Ask about them only when deployment/infrastructure
is itself the project's main contribution, or when no stronger core source is
available. Prefer entry, service/domain and data/integration code for questions.

Do not expose evidence-policy language, missing-material warnings, internal
confidence labels or analysis instructions inside the candidate's project
introduction. Those belong in the separate review area.
