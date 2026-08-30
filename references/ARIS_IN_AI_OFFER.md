# ARIS-in-AI-Offer reference integration

This project uses a small, manually curated and rewritten subset of knowledge
points from **ARIS-in-AI-Offer** for the optional “AI 工程后端 / LLM Infra”
specialization. The default Java / Go / database / networking interview path
does not depend on this material.

- Website: https://wanshuiyin.github.io/ARIS-in-AI-Offer/
- Repository: https://github.com/wanshuiyin/ARIS-in-AI-Offer
- Pinned reference commit: `6f60d728ae290982f7bddd88d9816073dd64d045`
- License: MIT, Copyright (c) 2026 Ruofeng Yang (杨若峰)
- Reproduce the research checkout with:
  `git clone --depth 1 https://github.com/wanshuiyin/ARIS-in-AI-Offer.git`
  and check out the pinned commit above.

Selected upstream chapters:

- `docs/tutorials/llm_inference_serving_tutorial.md`
- `docs/tutorials/kv_cache_speculative_decoding_tutorial.md`
- `docs/tutorials/quantization_tutorial.md`
- `docs/tutorials/distributed_training_tutorial.md`
- `docs/tutorials/agent_foundations_tutorial.md`
- `docs/tutorials/llm_evaluation_benchmarking_tutorial.md`
- `docs/tutorials/tokenization_tutorial.md`

Runtime integration is intentionally static: selected concepts are rewritten
as JSON questions in `questions/aris_ai_backend.json`. The application does
not index, retrieve, or send the long-form upstream tutorials to the model,
so this remains consistent with the project’s no-RAG constraint.
