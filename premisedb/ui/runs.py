"""A research run: a turn too long for one session, done as a plan, short fresh sessions that each take
one piece of work from a queue, several at a time when the user asks, and a report. Workers share
nothing but the graph."""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
import time

from .harness import HarnessError
from .threads import USE_OF_TIME, Agent, ThreadStore, document

ITEM_MINUTES = 15  # What a worker is told to spend; it is stopped at twice that.
REPORT_MINUTES = 10  # Kept back from the run's time for the report, and how far the report may run over.
MAX_ITEMS = 40  # For each agent.
FAILURES = 2  # This many pieces of work failing in a row ends the work early.


def run_research(store: ThreadStore, turn, *, db_path: Path, harness, effort: int) -> None:
    """Carry out a started turn as a run of `effort` minutes: plan pieces of work, do them in fresh
    sessions, as many at a time as the turn has agents, until the queue is empty or the time is used,
    then report as a checked answer."""
    turn_id, brief, agents = turn["id"], turn["question"], turn["agents"]
    agent = Agent(store, turn_id, db_path, harness, web=True, seconds=(effort + REPORT_MINUTES) * 60)
    started = time.monotonic()

    def left() -> float:
        """Seconds of working time left before the report must start."""
        return started + (effort - REPORT_MINUTES) * 60 - time.monotonic()

    def queue(texts) -> int:
        """Add pieces of work to the queue, up to the run's limit; returns how many were added."""
        texts = [" ".join(text.split()) for text in texts or [] if isinstance(text, str) and text.strip()]
        texts = texts[:max(0, MAX_ITEMS * agents - len(store.items(turn_id)))]
        store.add_items(turn_id, texts)
        return len(texts)

    def planned(reply) -> int:
        return queue((document(reply.text) or {}).get("items"))

    store.add_progress(turn_id, "Planning the research")
    try:
        if not planned(agent.run(_plan(brief, effort, agents), turn["session_id"])):
            return agent.fail("The agent did not return a plan for this research.")
    except HarnessError as error:
        return agent.fail(str(error))
    def work(item) -> str | None:
        """One piece of work in a fresh session; returns the error when it fails."""
        store.add_progress(turn_id, f"Working on: {item['text']}")
        try:
            reply = agent.run(_work(brief, item["text"], store.items(turn_id), agent.name), None,
                              min(2 * ITEM_MINUTES * 60, max(1.0, left())))
        except HarnessError as failure:
            store.finish_item(item["id"], error=str(failure))
            return str(failure)
        result = document(reply.text) or {}
        store.finish_item(item["id"], result=str(result.get("summary") or reply.text).strip()[:2000])
        queue((result.get("new_items") or [])[:2])
        return None

    failures, error, working = 0, "The time ran out before any of the work was done.", set()
    with ThreadPoolExecutor(agents) as pool:
        while True:
            while len(working) < agents and left() > 60 and failures < FAILURES and (item := store.next_item(turn_id)):
                working.add(pool.submit(work, item))
            if working:  # Wait for a worker to finish; it may have queued more.
                finished, working = wait(working, return_when=FIRST_COMPLETED)
                for failure in (future.result() for future in finished):
                    failures, error = (0, error) if failure is None else (failures + 1, failure)
                continue
            # Nobody is working and nothing was claimed: stop, or with time in hand ask what else would serve the brief.
            if left() <= 60 or failures >= FAILURES or time.monotonic() - started >= USE_OF_TIME * effort * 60:
                break
            store.add_progress(turn_id, "Looking for what else would serve the brief")
            try:
                if not planned(agent.run(_replan(brief, store.items(turn_id), round(left() / 60)))):
                    break
            except HarnessError:
                break
    done = [item for item in store.items(turn_id) if item["status"] == "done"]
    if not done:
        return agent.fail(error)
    store.add_progress(turn_id, "Writing the report")
    try:
        check, session_id, _ = agent.answer(_report(brief, done, agent.name), None)
    except HarnessError as failure:
        return agent.fail(str(failure))
    store._set_session(turn["thread_id"], session_id)  # A follow-up resumes the session that wrote the report.
    agent.finish(turn, check)


def _plan(brief: str, effort: int, agents: int) -> str:
    return ("Plan research for the brief below. Do not do the research yet: with the premisedb-research skill, look "
            "only at what the graph (the database PREMISEDB_DB names) already holds on it, so the plan starts from "
            f"there. The work will take about {effort} minutes and be done by workers that each start fresh, take "
            f"one item and spend about {ITEM_MINUTES} minutes on it, "
            f"{'one after another' if agents == 1 else f'{agents} at a time, so items must not overlap'}. Write each item so a worker "
            "can act on it without this conversation: a specific question to settle or a concrete addition to the "
            "graph, naming the companies, periods and documents involved. Put first what matters most to the brief; "
            "later items may not be reached, and workers can add items as they learn. Reply with JSON alone: "
            f'{{"items": ["...", "..."]}}, {max(effort // 20, agents)} to {max(effort // 10, 2 * agents)} items.\n\nBrief: {brief}')


def _listing(items: list[dict]) -> str:
    return "\n".join(f"- [{item['status']}] {item['text']}" + (f" — {item['result']}" if item["status"] == "done" else "")
                     for item in items)


def _replan(brief: str, items: list[dict], minutes: int) -> str:
    return (f"Research on the brief below has done the work listed, and about {minutes} minutes of working time "
            "remain. With the premisedb-research skill, look at what the graph now holds and decide what further "
            "pieces of work would most improve the answer to the brief: a thin or single-sourced claim, a figure not "
            "checked against a primary source, a counterparty or period not yet covered, an open Question that "
            f"bears on it. Each will be done by a fresh worker in about {ITEM_MINUTES} minutes. Reply with JSON "
            'alone: {"items": ["...", "..."]}, or {"items": []} if the brief is already well served.'
            f"\n\nBrief: {brief}\n\nWork so far:\n{_listing(items)}")


def _work(brief: str, text: str, items: list[dict], author: str) -> str:
    return ("You are one worker in a longer piece of research. Other workers take the other items, each starting "
            "fresh like you, so only what you put in the graph carries forward. Use the premisedb-research skill; the "
            f"research database is the one PREMISEDB_DB names; use the author ID {author} for anything you write. "
            f"Spend about {ITEM_MINUTES} minutes on your item. Recall what the graph already holds before going "
            "outside it. Ingest the sources you rely on with `premisedb ingest --url`, record Findings with "
            "citations, link them to the Entities they are about, and open a Question for anything material you "
            'could not settle. Then reply with JSON alone: {"summary": "two or three sentences on what you found '
            'and added", "new_items": ["at most two further pieces of work this turned up that are not on the list '
            'and matter to the brief"]}.'
            f"\n\nBrief: {brief}\n\nYour item: {text}\n\nAll items in this research, so you do not repeat one:\n{_listing(items)}")


def _report(brief: str, done: list[dict], author: str) -> str:
    return ("Research on the brief below has finished. The workers put what they found in the graph (the database "
            f"PREMISEDB_DB names, mostly under the author ID {author}); their summaries are below, but the graph is the "
            "record. Recall from it with the premisedb-research skill, then answer the brief as an answer document "
            "with the premisedb-answer skill, checked with check-answer. Answer the brief itself; do not narrate the "
            f"work or list what was not done.\n\nBrief: {brief}\n\nWork done:\n{_listing(done)}")
