# DeepSeek V4 template verification

Verified against the installed Poolside/llama.cpp source and binary at commit
`030ebb558a5820b444a8f836ed5cdd46c9b4bd7a`; no template was replaced.

The first published GGUF shard contains a 13,772-byte embedded Jinja template
with SHA-256
`e643c31fcec17f342f72296e02c46d35846bf4c70f6a0271f23bad73fd4eb645`.
Its metadata identifies the base model as `DeepSeek V4 Flash 0731` version
`0731`. Deterministic inspection found:

- user and assistant transitions use `<｜User｜>` and `<｜Assistant｜>`;
- `enable_thinking` is mapped into the template's `thinking` variable;
- generation from a user turn emits `<｜Assistant｜><think>` when thinking is
  enabled, otherwise `<｜Assistant｜></think>`;
- retained assistant reasoning is serialized from `reasoning_content` between
  `<think>` and `</think>` when `--reasoning-preserve` is active;
- tool results and the DeepSeek DSML markers are handled by the same embedded
  template.

The pinned server source in `tools/server/server-common.cpp` reads canonical
`reasoning_budget_tokens` first, then compatibility alias
`thinking_budget_tokens`. A request value overrides the process option. The
installed binary documents `--reasoning-budget -1` as unrestricted, `0` as an
immediate end, and positive values as token budgets.

Live qualification logs provide the application test: for budgets 384, 2048,
4096, and 8192 the server logged `generation_prompt='<｜Assistant｜><think>'`,
activated the corresponding budget, streamed reasoning separately under
`reasoning_content`, and forced the end sequence when a finite budget was
exhausted. Thus `--jinja --reasoning on --reasoning-preserve` remains correct.
