# Evals

Measurements that tests cannot give: what a change costs in recall, and what the codebase costs an agent to
work in. Modelled on Hermes Agent's `evals/` (https://github.com/NousResearch/hermes-agent, MIT). Nothing here
ships in the app, and neither eval touches your real data.

## `compaction/run.py` - what "Compact this chat" costs in recall

Runs JARVIS's own `compact_session` on a throwaway copy of a chat, asks a fresh model questions about the folded
region using only what the chat keeps, and grades the answers 2/1/0 against gold. The `uncompacted` arm is the
ceiling. Any OpenAI-compatible endpoint works; a local Ollama model makes a run free.

```bash
python evals/compaction/run.py --synthetic 40 --base-url http://127.0.0.1:11434/v1 \
    --model qwen2.5-coder:32b --num-ctx 16384 --out evals/compaction/results/run1
python evals/compaction/run.py --transcript my-chat-copy.json --questions 12 --base-url ... --model ... --out ...
```

`--synthetic N` builds an N-turn chat with facts planted where compaction folds, with known answers.
`--transcript` takes `{"messages": [...]}` exported from a COPY of a real chat; questions are generated once
and cached in `--out/questions.json` so reruns compare like with like. Results are `scorecard.json`,
`answers.json` and the summary itself. Real transcripts and results are git-ignored.

Three arms: `compacted` answers from the summary alone; `compacted+search` answers from the summary and, only
when that answer is "unknown", picks keywords and searches the chat's compacted messages once
(`search_sessions` with `this_chat`, the way back in the summary now points the model to, as Hermes's does);
`uncompacted` is the ceiling. `--facts N` plants more facts than a summary can keep. An answer containing the
gold answer verbatim is scored correct without asking the judge, which once marked "Dmitri" wrong against gold
"Dmitri". The region check that needs no model runs with the normal tests (`CompactionRegionTests`).

Runs on qwen2.5-coder:32b, 2026-09-23. 40 turns, 8 facts: compacted 87.5% (777 context tokens) vs 100%
uncompacted (1,139); the lost fact was a personal aside. Later runs with 8 and with 40 facts in 120 turns: the
summary kept 39-40 of 40, so the search arm rarely had to search. The one miss exposed that the archive search
required every keyword ("job queue chosen" found nothing against "the job queue"); it now falls back to any
keyword, ranked. A recovery through search is not yet shown live: qwen's summaries keep nearly every planted
fact, so a harder transcript (a real long chat) is the next test.

## `codebase_navigability/static_metrics.py` - what the code costs an agent to navigate

Offline and standard-library only (Hermes's version needs radon and tiktoken). Reports lines split into
code/comment/docstring/blank, file and function sizes, cyclomatic complexity, nesting, if/elif ladders and the
first-party import graph with its cycles, and flags the roadmap's split thresholds: a file over 2,000 lines, a
function over 300 lines or complexity 30, an if/elif ladder of four or more. Pass `--baseline` a previous
result to hold a refactor to numbers.

```bash
python evals/codebase_navigability/static_metrics.py . head --out out/
python evals/codebase_navigability/static_metrics.py . after --baseline out/head.json
```
