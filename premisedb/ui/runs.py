"""A research run: a turn too long for one session, done as a plan, short fresh sessions that each take
one piece of work from a queue, several at a time when the user asks, and a report. Workers share
nothing but the graph."""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path
import time

from .harness import HarnessError, UsageLimit
from .threads import USE_OF_TIME, Agent, ThreadStore, document

ITEM_MINUTES = 15  # What a worker is told to spend; it is stopped at twice that.
REPORT_MINUTES = 10  # Kept back from the run's time for the report, and how far the report may run over.
MAX_ITEMS = 40  # For each agent.
FAILURES = 2  # This many pieces of work failing in a row ends the work early.
RETRY_MINUTES = 15  # How long to wait at a usage limit when the harness does not say when it resets.
REPORT_WAIT_HOURS = 6  # How long a finished run's report will wait for a usage limit to reset.
_sleep = time.sleep
REVIEW_MINUTES = 10  # What a review of the new records for duplicates may take.
START_MINUTES = 5  # A piece of work is not started with less working time left than this: it would only be cut off.


def run_research(store: ThreadStore, turn, *, db_path: Path, harness, effort: int) -> None:
    """Carry out a started turn as a run of `effort` minutes: plan pieces of work, do them in fresh
    sessions, as many at a time as the turn has agents, until the queue is empty or the time is used,
    then report as a checked answer. A usage limit is waited out, inside the run's time for the work
    and beyond it for the report."""
    turn_id, brief, agents = turn["id"], turn["question"], turn["agents"]
    agent = Agent(store, turn_id, db_path, harness, web=True, seconds=effort * 60)
    started = time.monotonic()
    work_ends = started + (effort - REPORT_MINUTES) * 60

    def left() -> float:
        """Seconds of working time left before the report must start."""
        return work_ends - time.monotonic()

    def rest(limit: UsageLimit) -> None:
        resets = limit.resets_at
        store.add_progress(turn_id, "Waiting for your usage limit to reset" + (
            f" (about {datetime.fromtimestamp(resets):%H:%M})" if resets else ""))
        _sleep(_pause(limit))

    def patient(call, until: float):
        """A harness call that waits out a usage limit and tries again, unless the wait would pass `until`."""
        while True:
            try:
                return call()
            except UsageLimit as limit:
                if time.monotonic() + _pause(limit) > until:
                    raise
                rest(limit)

    def queue(texts) -> int:
        """Add pieces of work to the queue, up to the run's limit; returns how many were added."""
        texts = [" ".join(text.split()) for text in texts or [] if isinstance(text, str) and text.strip()]
        texts = texts[:max(0, MAX_ITEMS * agents - len(store.items(turn_id)))]
        store.add_items(turn_id, texts)
        return len(texts)

    def planned(reply) -> int:
        return queue((document(reply.text) or {}).get("items"))

    def work(item) -> HarnessError | None:
        """One piece of work in a fresh session; returns the error when it fails. A piece stopped by a
        usage limit goes back in the queue."""
        store.add_progress(turn_id, f"Working on: {item['text']}")
        try:
            reply = agent.run(_work(brief, item["text"], store.items(turn_id), agent.name), None,
                              min(2 * ITEM_MINUTES * 60, max(1.0, left())))
        except UsageLimit as limit:
            store.requeue_item(item["id"])
            return limit
        except HarnessError as failure:
            store.finish_item(item["id"], error=str(failure))
            return failure
        result = document(reply.text) or {}
        store.finish_item(item["id"], result=str(result.get("summary") or reply.text).strip()[:2000])
        queue((result.get("new_items") or [])[:2])
        return None

    def review() -> None:
        """Have one session fold duplicates among the records the run has added into the records they repeat."""
        store.add_progress(turn_id, "Checking the new records for duplicates")
        try:
            agent.run(_review(brief, turn["started_at"], agent.name), None, REVIEW_MINUTES * 60)
        except HarnessError:  # The review is a tidy-up; the work and the report do not depend on it.
            pass

    store.add_progress(turn_id, "Planning the research")
    try:
        if not planned(patient(lambda: agent.run(_plan(brief, effort, agents), turn["session_id"]), work_ends)):
            return agent.fail("The agent did not return a plan for this research.")
    except HarnessError as error:
        return agent.fail(str(error))
    failures, error, working, limit = 0, "The time ran out before any of the work was done.", set(), None
    reviewed, reviewing, done_count = 0, None, lambda: sum(item["status"] == "done" for item in store.items(turn_id))
    with ThreadPoolExecutor(agents) as pool, ThreadPoolExecutor(1) as reviewer:
        while True:
            if limit and not working:  # Everyone has stopped at the limit: wait it out if the time allows.
                if _pause(limit) > left() - START_MINUTES * 60:
                    break
                rest(limit)
                limit = None
            while (not limit and len(working) < agents and left() > START_MINUTES * 60 and failures < FAILURES
                   and (item := store.next_item(turn_id))):
                working.add(pool.submit(work, item))
            if working:  # Wait for a worker to finish; it may have queued more.
                finished, working = wait(working, return_when=FIRST_COMPLETED)
                for failure in (future.result() for future in finished):
                    if isinstance(failure, UsageLimit):
                        limit, error = failure, str(failure)
                    else:
                        failures, error = (0, error) if failure is None else (failures + 1, str(failure))
                # Every few finished pieces, while the work goes on, a review folds duplicates into what they repeat.
                if (reviewing is None or reviewing.done()) and done_count() - reviewed >= 2 * agents and left() > START_MINUTES * 60:
                    reviewed, reviewing = done_count(), reviewer.submit(review)
                continue
            # Nobody is working and nothing was claimed: stop, or with time in hand ask what else would serve the brief.
            if left() <= START_MINUTES * 60 or failures >= FAILURES or time.monotonic() - started >= USE_OF_TIME * effort * 60:
                break
            store.add_progress(turn_id, "Looking for what else would serve the brief")
            try:
                if not planned(patient(lambda: agent.run(_replan(brief, store.items(turn_id), round(left() / 60))), work_ends)):
                    break
            except HarnessError:
                break
        if reviewing is not None:
            reviewing.result()
    done = [item for item in store.items(turn_id) if item["status"] == "done"]
    if not done:
        return agent.fail(error)
    if len(done) > reviewed:  # Records added since the last review are checked before the report reads the graph.
        review()

    def report():
        store.add_progress(turn_id, "Writing the report")
        agent.stop_at = time.monotonic() + 2 * REPORT_MINUTES * 60  # The report has its own allowance.
        return agent.answer(_report(brief, done, agent.name), None)

    try:  # The work is done, so a usage limit here is worth waiting out even past the run's time.
        check, session_id, _ = patient(report, time.monotonic() + REPORT_WAIT_HOURS * 3600)
    except HarnessError as failure:
        return agent.fail(str(failure))
    store._set_session(turn["thread_id"], session_id)  # A follow-up resumes the session that wrote the report.
    agent.finish(turn, check)


def _pause(limit: UsageLimit) -> float:
    """Seconds to wait before trying again: until the reset the harness named, else a fixed interval."""
    if limit.resets_at and limit.resets_at > time.time():
        return limit.resets_at - time.time() + 30
    return RETRY_MINUTES * 60


def _plan(brief: str, effort: int, agents: int) -> str:
    return ("Plan research for the brief below. Do not do the research yet: with the premisedb-research skill, look "
            "only at what the graph (the database PREMISEDB_DB names) already holds on it, so the plan starts from "
            f"there. The work will take about {effort} minutes and be done by workers that each start fresh, take "
            f"one item and spend about {ITEM_MINUTES} minutes on it, "
            f"{'one after another' if agents == 1 else f'{agents} at a time, so items must not overlap'}. Write each item so a worker "
            "can act on it without this conversation: a specific question to settle or a concrete addition to the "
            "graph, naming the companies, periods and documents involved. Put first what matters most to the brief; "
            "later items may not be reached, and workers can add items as they learn. Reply with JSON alone: "
            f'{{"items": ["...", "..."]}}, {max(agents, min(effort // 20, 6))} to {max(2 * agents, min(effort // 10, 12))} items.'
            f"\n\nBrief: {brief}")


def _listing(items: list[dict], results: int = 400) -> str:
    """The pieces of work, one a line, with what the finished ones found cut to `results` characters."""
    return "\n".join(f"- [{item['status']}] {item['text']}" + (
        f" — {item['result'][:results]}" if item["status"] == "done" and results else "") for item in items)


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
            f"\n\nBrief: {brief}\n\nYour item: {text}\n\nAll items in this research, so you do not repeat one:\n{_listing(items, 0)}")


def _review(brief: str, since: str, author: str) -> str:
    return ("Research on the brief below is under way, and its workers have been adding records to the graph (the "
            f"database PREMISEDB_DB names) since {since}. Review what they added for duplicates, with the "
            f"premisedb-research skill: `premisedb list --since {since} --kind Entity` lists the new Entities; check "
            "the new Questions the same way. For each, search the graph by name and aliases for an existing record of "
            "the same company, campus, person or question. Where one exists, keep the record that is older or in the "
            "base layer and fold the newer one into it: `premisedb merge KEEP_ID DUPLICATE_ID --input FILE` with "
            f'{{"author_id": "{author}"}}. Merge only records that are plainly the same thing; leave anything doubtful '
            'as it is, and do no other research. Reply with JSON alone: {"merged": N}.'
            f"\n\nBrief: {brief}")


def _report(brief: str, done: list[dict], author: str) -> str:
    return ("Research on the brief below has finished. The workers put what they found in the graph (the database "
            f"PREMISEDB_DB names, mostly under the author ID {author}); their summaries are below, but the graph is the "
            "record. Recall from it with the premisedb-research skill, then answer the brief as an answer document "
            "with the premisedb-answer skill, checked with check-answer. Answer the brief itself; do not narrate the "
            f"work or list what was not done.\n\nBrief: {brief}\n\nWork done:\n{_listing(done)}")
