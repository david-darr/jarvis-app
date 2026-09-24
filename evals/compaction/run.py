"""What does "Compact this chat" cost in recall?

Modelled on Hermes Agent's evals/compaction (https://github.com/NousResearch/
hermes-agent, MIT), adapted to JARVIS: it runs JARVIS's own compaction
(services/chat_service.compact_session) on a throwaway copy, then asks a fresh
model questions about the part of the chat that compaction folded away,
answering from only what the chat keeps afterwards. A control arm answers from
the full uncompacted transcript, the ceiling. A judge grades each answer 2/1/0
against the gold answer.

Everything runs through an OpenAI-compatible endpoint you name, so a local
model (Ollama) makes a run free. It never touches your real data: it builds a
temporary data folder and deletes it afterwards.

What the answerer gets is closed-book on purpose: JARVIS's model-facing
search_sessions excludes the current chat, so after compaction the archived
messages are not reachable to the model at all (unlike Hermes, whose summary
carries a search pointer into the archived region). This measures that.

Usage (from the repo root):
    python evals/compaction/run.py --synthetic 40 --base-url http://127.0.0.1:11434/v1 \\
        --model qwen2.5-coder:32b --num-ctx 16384 --out evals/compaction/results/run1
    python evals/compaction/run.py --transcript lineage.json --questions 12 ... (a COPY of a real chat)

A transcript file is {"messages": [{"role": "user"|"assistant", "content": "..."}]}.
Real transcripts are never committed.
"""
import argparse
import asyncio
import json
import os
import random
import re
import shutil
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

GENERATE_PROMPT = (
    "Below is part of a conversation. Write {n} factual recall questions that can be answered ONLY from it, "
    "each with a short exact gold answer (a name, number, date, decision or short phrase). Prefer specific details "
    "over general themes. Reply with a JSON list only: [{{\"q\": \"...\", \"a\": \"...\"}}, ...]\n\n{text}"
)
ANSWER_PROMPT = (
    "Using only the conversation context below, answer the question in a few words. If the context does not "
    "contain the answer, reply exactly: unknown\n\n[Context]\n{context}\n\n[Question]\n{question}"
)
TERMS_PROMPT = (
    "You need to look something up in the earlier, compacted part of a conversation with a keyword search. "
    "Give 1 to 3 distinctive keywords likely to appear in the message that answers the question below. "
    "Reply with the keywords only, separated by spaces.\n\nQuestion: {question}"
)
JUDGE_PROMPT = (
    "Grade an answer against the gold answer. Reply with a single digit: 2 if it matches the gold answer's "
    "meaning, 1 if partly right, 0 if wrong or unknown.\n\nQuestion: {question}\nGold answer: {gold}\n"
    "Given answer: {answer}"
)

FACT_TEMPLATES = [
    ("The project codename is {w}.", "What is the project codename?", "{w}"),
    ("We agreed the launch date is {d}.", "What launch date was agreed?", "{d}"),
    ("The budget cap is {n} dollars.", "What is the budget cap in dollars?", "{n}"),
    ("{p} owns the database migration.", "Who owns the database migration?", "{p}"),
    ("Decision: we are using {t} for the job queue.", "What was chosen for the job queue?", "{t}"),
    ("The staging server port is {n}.", "What is the staging server port?", "{n}"),
    ("My sister's name is {p}.", "What is the user's sister's name?", "{p}"),
    ("The API key rotation happens every {n} days.", "How often, in days, are API keys rotated?", "{n}"),
]
WORDS = ["amber-falcon", "cobalt-otter", "velvet-heron", "quartz-lynx", "saffron-moth", "juniper-wolf"]
PEOPLE = ["Priya", "Marcus", "Lena", "Tomas", "Aiko", "Dmitri"]
TOOLS = ["Redis Streams", "RabbitMQ", "SQS", "Celery", "NATS"]
FILLER = [
    "Can you help me think through the next step?", "Sounds good, let's keep going.",
    "Let me check a couple of things first.", "That makes sense to me.",
    "What would you do differently here?", "Okay, noted. Anything else I should consider?",
]


def synthetic(turns: int, seed: int = 7, facts_wanted: int = 0) -> tuple[list[dict], list[dict]]:
    """A chat of `turns` exchanges with facts planted early, and their gold
    questions. Facts land only in the first two thirds, the region a
    compaction folds away. facts_wanted above the 8 varied templates adds
    look-alike reference codes, too many for a summary to keep - the case a
    search of the archive exists for."""
    rng = random.Random(seed)
    facts, messages = [], []
    pool = list(FACT_TEMPLATES) + [(f"The door code for room {k} is {{n}}.", f"What is the door code for room {k}?", "{n}")
                                   for k in range(101, 101 + max(0, facts_wanted - len(FACT_TEMPLATES)))]
    fact_turns = set(rng.sample(range(max(1, turns * 2 // 3)), min(len(pool), max(1, turns * 2 // 3))))
    templates = iter(rng.sample(pool, len(pool)))
    for i in range(turns):
        if i in fact_turns:
            statement, question, answer = next(templates)
            values = {"w": rng.choice(WORDS), "d": f"2026-{rng.randint(10, 12)}-{rng.randint(10, 28)}",
                      "n": str(rng.randint(100, 9999)), "p": rng.choice(PEOPLE), "t": rng.choice(TOOLS)}
            messages.append({"role": "user", "content": f"{rng.choice(FILLER)} Also: {statement.format(**values)}"})
            facts.append({"q": question, "a": answer.format(**values)})
        else:
            messages.append({"role": "user", "content": rng.choice(FILLER)})
        messages.append({"role": "assistant", "content": "Understood. " + rng.choice(FILLER)})
    return messages, facts


def transcript_text(messages: list[dict]) -> str:
    return "\n\n".join(f"{m['role']}: {m['content']}" for m in messages if m.get("content"))


def approx_tokens(text: str) -> int:
    return len(text) // 4


async def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="JARVIS compaction recall eval")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--synthetic", type=int, metavar="TURNS")
    parser.add_argument("--facts", type=int, default=0, help="synthetic: plant this many facts (default: the 8 varied ones)")
    source.add_argument("--transcript")
    parser.add_argument("--questions", type=int, default=10, help="questions to generate (real transcripts)")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--api-key")
    parser.add_argument("--num-ctx", type=int)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    data_dir = tempfile.mkdtemp(prefix="jarvis-compaction-eval-")
    os.environ["JARVIS_DATA_DIR"] = data_dir
    sys.path.insert(0, REPO)
    from core import model_endpoints, session_manager_store
    from core.providers import openai_compatible
    from core.session_manager import session_manager
    from services import chat_service

    async def ask(prompt: str) -> str:
        return (await openai_compatible.run_turn(args.base_url.rstrip("/"), args.model, args.api_key,
                                                 [{"role": "user", "content": prompt}], num_ctx=args.num_ctx)).strip()

    try:
        os.makedirs(args.out, exist_ok=True)
        if args.synthetic:
            messages, questions = synthetic(args.synthetic, facts_wanted=args.facts)
        else:
            with open(args.transcript, encoding="utf-8") as f:
                messages = [m for m in json.load(f)["messages"] if m.get("role") in ("user", "assistant")]
            questions = None

        endpoint = model_endpoints.create_endpoint("eval", base_url=args.base_url, model=args.model,
                                                   api_key=args.api_key, kind="local" if args.num_ctx else "api",
                                                   num_ctx=args.num_ctx)
        sid = session_manager.create_session("compaction eval")["id"]
        for m in messages:
            session_manager.append_message(sid, m["role"], m["content"])
        session_manager.set_model_endpoint(sid, endpoint["id"])

        started = time.time()
        result = await chat_service.compact_session(sid)
        compaction_seconds = round(time.time() - started, 1)
        folded = messages[:result["compacted_through"]]

        cache = os.path.join(args.out, "questions.json")
        if questions is None:
            if os.path.exists(cache):
                with open(cache, encoding="utf-8") as f:
                    questions = json.load(f)
            else:
                raw = await ask(GENERATE_PROMPT.format(n=args.questions, text=transcript_text(folded)))
                match = re.search(r"\[.*\]", raw, re.S)
                questions = [q for q in json.loads(match.group(0) if match else "[]") if q.get("q") and q.get("a")]
        with open(cache, "w", encoding="utf-8") as f:
            json.dump(questions, f, indent=2)

        after = session_manager.effective_messages(sid)
        arms = {"compacted": transcript_text(after), "compacted+search": transcript_text(after),
                "uncompacted": transcript_text(messages)}
        scorecard = {"model": args.model, "messages": len(messages), "folded_messages": len(folded),
                     "questions": len(questions), "compaction_seconds": compaction_seconds,
                     "summary_tokens": approx_tokens(result["summary"]), "arms": {}}
        details = []
        from core import memory_tools
        for arm, context in arms.items():
            points, searches = 0, 0
            for q in questions:
                answer = await ask(ANSWER_PROMPT.format(context=context, question=q["q"]))
                if arm == "compacted+search" and "unknown" in answer.lower():
                    # The model's way back in, used only when the summary
                    # lacks the answer: one search of this chat's archive.
                    searches += 1
                    terms = await ask(TERMS_PROMPT.format(question=q["q"]))
                    hits = memory_tools.search_this_chat_archive(sid, terms)
                    found = memory_tools.format_archive_hits(hits)
                    answer = await ask(ANSWER_PROMPT.format(context=f"{context}\n\n[Search results:]\n{found}", question=q["q"]))
                if q["a"].lower() in answer.lower():
                    # The gold answer verbatim is correct; the judge model
                    # once scored "Dmitri" against gold "Dmitri" as wrong.
                    digit = "2"
                else:
                    verdict = await ask(JUDGE_PROMPT.format(question=q["q"], gold=q["a"], answer=answer))
                    digit = next((c for c in verdict if c in "012"), "0")
                points += int(digit)
                details.append({"arm": arm, "q": q["q"], "gold": q["a"], "answer": answer, "score": int(digit)})
            scorecard["arms"][arm] = {"recall_pct": round(100 * points / (2 * len(questions)), 1) if questions else None,
                                      "context_tokens": approx_tokens(context)}
            if arm == "compacted+search":
                scorecard["arms"][arm]["searches"] = searches

        with open(os.path.join(args.out, "scorecard.json"), "w", encoding="utf-8") as f:
            json.dump(scorecard, f, indent=2)
        with open(os.path.join(args.out, "answers.json"), "w", encoding="utf-8") as f:
            json.dump(details, f, indent=2)
        with open(os.path.join(args.out, "summary.txt"), "w", encoding="utf-8") as f:
            f.write(result["summary"])
        print(json.dumps(scorecard, indent=2))
        return 0
    finally:
        await chat_service.shutdown()
        session_manager_store.close()
        shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
