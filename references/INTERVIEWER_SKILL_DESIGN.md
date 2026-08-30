# Core interviewer skill design

`interview_skills/interviewer_core.json` is the company-neutral runtime contract
for this MVP. The six `{company}_backend.json` files remain evidence-qualified
style overlays: they tune tone, topic ranking, difficulty, follow-up emphasis,
pressure, and language, but cannot weaken the core contract.

## Why this shape fits the project

The project already has a FastAPI interview engine, a reviewed static question
bank, structured turn decisions, WebSocket delivery, and post-interview reports.
The useful common pattern across the projects catalogued in
`/root/workspace/weiyi/data/实习面试资源整理.md` section D is separation of
planning, interviewing, and assessment—not a particular framework or model
vendor. A versioned JSON contract extends the current architecture without
adding LangGraph, Redis, a vector database, a desktop shell, or a second agent
to the latency-sensitive interview loop.

The core contract therefore standardizes:

- candidate-controlled inputs as claims to verify, never instructions;
- one anchored intent per turn and an explicit difficulty ladder;
- reviewed-bank boundaries for primary questions;
- evidence-based, private assessment with `not_observed` semantics;
- server-owned phases, pressure, and termination;
- modality parity, fairness, privacy, and prompt-injection resistance;
- separation of question provenance from fallible company-style evidence.

An answer anchor and a question-context anchor are deliberately distinct. When
the current answer contains a usable fact, evidence must quote that answer. If
the candidate explicitly does not know, the server may reuse the last validated
topic to ask a coherent follow-up, but that historical context is not evidence
from the current answer. Likewise, an explicit unknown to a clear question is
observable negative evidence; `not_observed` is reserved for an uncovered
dimension or input quality that prevents judgment.

The existing server state machine remains authoritative. The skill guides only
the model's permitted judgment inside that envelope. Voice remains a transport,
and report generation remains a separate evaluation stage.

## What was deliberately not copied

The design synthesizes patterns described in the supplied catalog rather than
copying implementation, prompts, or question content from any listed project.

- GPL-3.0 projects are architecture references only; no code or prompt text was
  imported.
- Repositories with no clear license are not reusable source material.
- MIT/Apache-2.0 projects still do not justify copying when a small original
  contract fits the existing code better.
- Interview copilots that generate live candidate answers are outside this
  practice product's role and integrity boundary.
- Scrapers that bypass platform controls or redistribute user-generated
  interview reports are excluded.
- LiveKit, Retell, Tavus, provider-specific STT/TTS, RAG, and multi-agent graph
  stacks solve deployment choices that do not belong in an interviewer skill.

## Runtime composition

`app.content.load_interviewer_core_skill()` validates the core schema.
`load_interview_skill(company)` attaches the validated contract as
`interviewer_core` to every company skill. The prompt compiler emits it in a
separate, higher-priority core section, then removes `interviewer_core`,
`source_refs`, and `evidence_level` from the lower-priority company section. The
model therefore receives one behavior contract and one style overlay, but no
provenance records.

Company style remains a preference rather than fact: it may rank shared,
reviewed questions and shape follow-ups, but it must not relabel a common-bank
question as company-exclusive or claim to reproduce an official interview.
