# Supplied data inventory

Audit date: 2026-08-30.

The supplied `/root/workspace/weiyi/data` directory is a research/resource
catalog, not a flat question bank. It contains five physical files:

- one planning note;
- two DOCX revisions of the resource catalog;
- two byte-identical Markdown copies of the same resource catalog.

The canonical Markdown catalog contains 104 unique external URLs, including 69
unique GitHub URLs. Those links cover knowledge bases, interview-experience
indexes, resume resources, communities, and mock-interview projects. Counting
each URL, table row, heading, or question mark as a “real question” would
materially overstate the usable bank and would also mix incompatible licenses.

The production application therefore uses a narrower reviewed layer:

| Pinned source | Independent question concepts |
|---|---:|
| JavaGuide (Apache-2.0) | 60 |
| interview-go (Apache-2.0) | 20 |
| Tech Interview Handbook (MIT) | 16 |
| ARIS-in-AI-Offer (MIT) | 12 |
| **Total** | **108** |

Every concept has a Chinese and English runtime variant, so the loader returns
216 records. This is **108 independent questions, not 216 questions**. The
first bank file contains 66 concepts; the extended bank contains another 42.
Exact revisions, paths, licenses, and counts are recorded in
`resources/practice_source_manifest.json` and `THIRD_PARTY_NOTICES.md`.

Company experience links from the supplied catalog are used to distill pacing,
topic priority, follow-up style, and report advice. They are not copied into the
fixed question bank and are not presented as company-authenticated questions.
