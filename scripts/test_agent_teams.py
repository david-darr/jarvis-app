"""Agents phase 5: teams of JARVIS agents on Swarm's engine.

Spec: the vault note "Agents - Phase 5 Teams (Build Spec)". What these prove,
against a stub model server (no provider, no network beyond loopback):

- a teammate linked to an agent takes the agent's name and role, and follows
  a rename; a deleted agent leaves an ordinary teammate behind;
- the agent's identity and notes reach the worker's prompt, while the tools
  it is offered are still only Swarm's;
- a revision note lands in the reviewed agent's Corrections, once;
- a team that needs the owner asks through its lead's agent, the answer from
  the agent inbox resumes it, and a second question is asked again;
- a finished mission is reported to every linked agent with one channel
  notification;
- only an admin can seat agents; owner messages reach one teammate or all.
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
environment = tempfile.TemporaryDirectory(prefix="jarvis-agent-teams-")
os.environ["JARVIS_DATA_DIR"] = environment.name

from fastapi import HTTPException

from core.swarm.models import NotFound
from core.swarm.tools import _LEAD, _MEMORY
from routes import swarm_routes
from services import agent_service as agent_module
from services import swarm_service as swarm_module
from services.agent_service import agent_service
from services.swarm_service import SwarmService

LIMIT = {"ceiling": 20000, "pause_percent": 80, "checkpoint_reserve": 0}
SWARM_TOOLS = set(_LEAD) | set(_MEMORY) | {"finish_shift"}


def member(name="", role="", agent_id=None):
    return {"name": name or "placeholder", "role": role or "placeholder", "instructions": "", "limit": LIMIT,
            "endpoint_id": "endpoint-a", "model": None, "effort": None, "agent_id": agent_id}


def payload(lead, specialists, mode="guided", name="Launch team"):
    return {"name": name, "mission": "Launch the newsletter", "mode": mode,
            "system_limit": {"ceiling": 200000, "pause_percent": 80, "checkpoint_reserve": 0},
            "run_limit": {"ceiling": 100000, "pause_percent": 80, "checkpoint_reserve": 0},
            "pool_limit": {"ceiling": 200000, "pause_percent": 80, "checkpoint_reserve": 0}, "pool_id": None,
            "lead": lead, "specialists": specialists}


class Scripted(BaseHTTPRequestHandler):
    script = None
    requests = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"] or 0)) or "{}")
        Scripted.requests.append(body)
        status, answer = Scripted.script(body)
        encoded = json.dumps(answer).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_args):
        pass


def reply(calls):
    message = {"role": "assistant", "content": None, "tool_calls": [
        {"id": f"call-{index}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
        for index, (name, arguments) in enumerate(calls)]}
    return 200, {"choices": [{"message": message}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


def waiting_id(transcript):
    return transcript.split("use review_work with these ids):")[1].split("- ")[1].split(" ")[0]


class TeamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = tempfile.TemporaryDirectory(dir=environment.name)
        self.server = None
        for agent in agent_service.list_agents():
            agent_service.delete(agent["id"])
        agent_service._inbox.clear()
        self.lead_agent = agent_service.create("Ada", role="Editor in chief", instructions="Keep it short.")
        self.writer = agent_service.create("Bo", role="Writer")
        agent_service.remember(self.writer["id"], "Preferences", "MARKER-NOTES the person likes plain words")
        self.service = SwarmService(Path(self.root.name))
        await self.service.start()
        self.assertIsNone(self.service.fault)
        swarm_module.current = self.service
        self.notify = patch.object(agent_service, "notify", Mock()).start()

    async def asyncTearDown(self):
        patch.stopall()
        swarm_module.current = None
        await self.service.close()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        self.root.cleanup()

    def serve(self, script):
        Scripted.script = staticmethod(script)
        Scripted.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Scripted)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        endpoint = {"id": "endpoint-a", "name": "Stub", "kind": "api", "model": "stub", "api_key": None,
                    "num_ctx": None, "base_url": f"http://127.0.0.1:{self.server.server_address[1]}/v1"}
        patch.object(SwarmService, "_resolved", staticmethod(lambda endpoint_id: endpoint)).start()
        patch.object(SwarmService, "_connection", staticmethod(lambda endpoint_id: endpoint)).start()

    async def create(self, mode="guided", extra=()):
        data = payload(member(agent_id=self.lead_agent["id"]),
                       [member(agent_id=self.writer["id"]), *extra], mode=mode)
        return (await self.service.create("alice", data, f"create-{mode}"))["id"]

    async def start(self, system_id):
        revision = self.service.store.get_system(system_id)["revision"]
        result = await self.service.lifecycle("alice", system_id, "start", f"go-{revision}", revision,
                                              spend_confirmed=True)
        self.assertEqual(result["status"], "active", result)
        await self.settle(system_id)

    async def settle(self, system_id):
        cycle = self.service.cycles.get(system_id)
        if cycle:
            await asyncio.wait_for(cycle, timeout=120)

    def team(self, system_id):
        return {m["name"]: m for m in self.service.store.team(system_id)}

    # -- linking --------------------------------------------------------

    async def test_linked_teammates_take_the_agents_name_and_role(self):
        self.serve(lambda body: reply([]))
        system_id = await self.create(extra=[member("Checker", "Fact checker")])
        team = self.team(system_id)
        self.assertEqual(set(team), {"Ada", "Bo", "Checker"})
        self.assertEqual(team["Ada"]["role"], "Editor in chief")
        self.assertEqual(team["Bo"]["jarvis_agent_id"], self.writer["id"])
        self.assertIsNone(team["Checker"]["jarvis_agent_id"], "an ordinary teammate stays ordinary")
        self.assertEqual([t["id"] for t in self.service.store.teams_for_agent(self.writer["id"])], [system_id])

    async def test_bad_rosters_are_refused(self):
        self.serve(lambda body: reply([]))
        with self.assertRaises(ValueError):   # an unlinked teammate already called Bo
            await self.service.create("alice", payload(member(agent_id=self.lead_agent["id"]),
                                                       [member(agent_id=self.writer["id"]), member("bo", "Other")]), "c1")
        with self.assertRaisesRegex(ValueError, "one seat"):
            await self.service.create("alice", payload(member(agent_id=self.lead_agent["id"]),
                                                       [member(agent_id=self.lead_agent["id"])]), "c2")
        with self.assertRaises(ValueError):   # an agent that does not exist
            await self.service.create("alice", payload(member(agent_id="0123456789"), []), "c3")

    async def test_rename_follows_and_delete_unlinks(self):
        self.serve(lambda body: reply([]))
        system_id = await self.create()
        agent_service.update(self.writer["id"], name="Bea")
        self.service.agent_changed(agent_service.get(self.writer["id"]))
        self.assertIn("Bea", self.team(system_id))
        # A change made while Swarm could not hear it is caught up by the cycle.
        agent_service.update(self.writer["id"], name="Bex", role="Senior writer")
        self.service.refresh_links(system_id)
        self.assertEqual(self.team(system_id)["Bex"]["role"], "Senior writer")
        agent_service.delete(self.writer["id"])
        self.service.refresh_links(system_id)
        self.assertIsNone(self.team(system_id)["Bex"]["jarvis_agent_id"], "the seat stays, the link goes")

    async def test_an_agent_switched_off_blocks_its_team(self):
        self.serve(lambda body: reply([]))
        system_id = await self.create()
        self.assertEqual(self.service.agent_blockers(system_id), [])
        agent_service.update(self.writer["id"], enabled=False)
        self.assertTrue(any("Bo is turned off" in p for p in self.service.agent_blockers(system_id)))

    # -- a full cycle ---------------------------------------------------

    def review_script(self, revise_first=True):
        state = {"revised": not revise_first}

        def script(body):
            transcript = json.dumps(body["messages"])
            is_lead = "the lead of" in body["messages"][0]["content"]
            used_a_tool = any(m["role"] == "tool" for m in body["messages"])
            if not used_a_tool:
                return reply([("get_assigned_work", {})])
            if not is_lead:
                return reply([("submit_result", {"summary": "Draft", "output": "Issue one copy"})])
            if "waiting on your review" in transcript:
                verdict = "accept" if state["revised"] else "revise"
                state["revised"] = True
                note = "Good" if verdict == "accept" else "Use the reader's name in the greeting"
                return reply([("review_work", {"decisions": [{"task": waiting_id(transcript), "verdict": verdict,
                                                               "note": note}]})])
            if "Review what this company has produced" in transcript:
                return reply([("finish_mission", {"summary": "The first issue is written."})])
            return reply([("assign_plan", {"tasks": [{"key": "a", "assignee": "Bo", "objective": "Write issue one"}]})])

        return script

    async def test_identity_reaches_the_prompt_but_tools_stay_swarms(self):
        self.serve(self.review_script(revise_first=False))
        system_id = await self.create()
        await self.start(system_id)
        writer_calls = [r for r in Scripted.requests if "the lead of" not in r["messages"][0]["content"]]
        self.assertTrue(writer_calls, "the writer worked")
        prompt = writer_calls[0]["messages"][0]["content"]
        self.assertIn("MARKER-NOTES", prompt, "its notes came along")
        self.assertIn("working as part of a team", prompt)
        self.assertNotIn("agent_remember", prompt, "it is not told about tools it does not have")
        for request in Scripted.requests:
            offered = {tool["function"]["name"] for tool in request.get("tools") or []}
            self.assertTrue(offered <= SWARM_TOOLS, offered - SWARM_TOOLS)
        unlinked = await self.service.create("alice", payload(member("Lee", "Lead"), [member("Sam", "Writer")]), "plain")
        self.assertIsNone(self.service._identity(self.team(unlinked["id"])["Sam"]))

    async def test_the_lead_sees_accepted_work_when_it_reviews_the_mission(self):
        """Found live: told to review what the team produced, the lead was
        never shown it and asked the owner for an already-accepted draft."""
        self.serve(self.review_script(revise_first=False))
        system_id = await self.create(mode="autonomous")
        await self.start(system_id)
        review = [r for r in Scripted.requests if "Review what this company has produced" in json.dumps(r["messages"])
                  and any(m["role"] == "tool" for m in r["messages"])]
        self.assertTrue(review, "the lead reached its end-of-cycle review")
        shown = json.dumps(review[0]["messages"])
        self.assertIn("Work already accepted", shown)
        self.assertIn("Issue one copy", shown, "the accepted deliverable itself is in front of the lead")
        self.assertEqual([t["objective"] for t in self.service.store.accepted_work(system_id)], ["Write issue one"],
                         "the lead's own plans and reviews are not listed as work")

    async def test_a_revision_note_becomes_a_correction_once(self):
        self.serve(self.review_script())
        system_id = await self.create()
        await self.start(system_id)
        memory = agent_service.read_memory(self.writer["id"])
        corrections = memory.split("## Corrections")[1].split("## ")[0]
        self.assertEqual(corrections.count("Use the reader's name in the greeting"), 1, memory)
        self.assertIn("Launch team", corrections)
        self.service.sync_agents(system_id)
        self.assertEqual(agent_service.read_memory(self.writer["id"]), memory, "carried once, not on every sync")
        self.assertNotIn("reader's name", agent_service.read_memory(self.lead_agent["id"]))

    async def test_a_finished_mission_is_reported_to_every_agent_with_one_notification(self):
        self.serve(self.review_script(revise_first=False))
        system_id = await self.create(mode="autonomous")
        await self.start(system_id)
        self.assertEqual(self.service.store.mission_concluded(system_id)["reason"], "mission_complete")
        for agent in (self.lead_agent, self.writer):
            items = [i for i in agent_service.inbox(agent["id"]) if i.get("team_id") == system_id]
            self.assertEqual([i["kind"] for i in items], ["report"], agent["name"])
            self.assertIn("mission complete", items[0]["title"])
            self.assertIn("first issue is written", items[0]["body"])
        self.assertEqual(self.notify.call_count, 1, "one ending, one message on the phone")
        self.assertEqual(self.notify.call_args[0][0]["agent_id"], self.lead_agent["id"])

    async def test_a_team_that_needs_you_asks_through_its_agent_and_resumes(self):
        state = {"blocked": 2}

        def script(body):
            transcript = json.dumps(body["messages"])
            is_lead = "the lead of" in body["messages"][0]["content"]
            if not any(m["role"] == "tool" for m in body["messages"]):
                return reply([("get_assigned_work", {})])
            if not is_lead:
                if state["blocked"]:
                    return reply([("report_blocker", {"reason": "No subscriber list.",
                                                      "needs": "Paste the subscriber list."})])
                return reply([("submit_result", {"summary": "Sent", "output": "Issue sent"})])
            if "waiting on your review" in transcript:
                if state["blocked"]:
                    return reply([("report_blocker", {"reason": "Blocked on the owner.",
                                                      "needs": "Please paste the subscriber list."})])
                return reply([("review_work", {"decisions": [{"task": waiting_id(transcript), "verdict": "accept",
                                                               "note": "Sent"}]})])
            if "Review what this company has produced" in transcript:
                return reply([("finish_mission", {"summary": "Delivered."})])
            return reply([("assign_plan", {"tasks": [{"key": "a", "assignee": "Bo", "objective": "Send issue one"}]})])

        self.serve(script)
        system_id = await self.create(mode="autonomous")
        await self.start(system_id)
        self.assertEqual(self.service.store.mission_concluded(system_id)["reason"], "needs_owner")
        questions = [i for i in agent_service.inbox(self.lead_agent["id"]) if i["kind"] == "question"]
        self.assertEqual(len(questions), 1, "the lead's agent asks")
        self.assertEqual(questions[0]["team_id"], system_id)
        self.assertIn("subscriber list", questions[0]["body"])
        self.assertIsNone(questions[0]["card_id"], "not pinned to whatever the agent was running")
        self.assertFalse(agent_service.inbox(self.writer["id"]), "one question, not one per agent")
        self.assertIsNotNone(agent_service.resolve_code(agent_module.code_for(self.lead_agent, questions[0]["id"])),
                             "a phone reply to the notification finds it")

        # First answer is not enough: the team must be able to ask again.
        state["blocked"] = 1
        outcome = agent_service.answer_item(questions[0]["id"], "reply", "It is in the shared drive.")
        self.assertIn("sent to the team", outcome)
        await asyncio.sleep(0.2)
        await self.settle(system_id)
        messages = self.service.store.owner_page("alice", system_id, "messages", 0, 50)["items"]
        self.assertTrue(any(m["body"] == "It is in the shared drive." and m["sender_id"] is None for m in messages))
        again = [i for i in agent_service.inbox(self.lead_agent["id"]) if i["kind"] == "question"]
        self.assertEqual(len(again), 1, "the second request was recorded and asked")
        self.assertNotEqual(again[0]["id"], questions[0]["id"])

        state["blocked"] = 0
        agent_service.answer_item(again[0]["id"], "reply", "Here: a@example.com")
        await asyncio.sleep(0.2)
        await self.settle(system_id)
        store = self.service.store
        self.assertFalse(store.blocked_for_owner(system_id), "the held work was released")
        self.assertNotEqual((store.mission_concluded(system_id) or {}).get("reason"), "needs_owner")
        self.assertFalse([i for i in agent_service.inbox(self.lead_agent["id"]) if i["kind"] == "question"],
                         "nothing new was asked once the team had what it needed")
        writer = self.team(system_id)["Bo"]["id"]
        self.assertTrue(store.db.execute("SELECT 1 FROM tasks WHERE agent_id=? AND state='done'", (writer,)).fetchone(),
                        "the writer finished once answered")

    async def test_an_answer_that_cannot_be_delivered_stays_open(self):
        self.serve(lambda body: reply([]))
        system_id = await self.create()
        item = agent_service.add_item(self.lead_agent["id"], "question", "The team needs you", "?", team_id=system_id)

        async def broken(*_args):
            raise NotFound("gone")

        with patch.object(self.service, "answer", broken):
            agent_service.answer_item(item["id"], "reply", "answer")
            await asyncio.sleep(0.05)
        self.assertEqual(agent_service.get_item(item["id"])["status"], "open")

    async def test_answering_on_the_team_page_closes_the_inbox_question(self):
        self.serve(lambda body: reply([]))
        system_id = await self.create()
        self.service.sync_agents(system_id)
        item = agent_service.add_item(self.lead_agent["id"], "question", "The team needs you", "?", team_id=system_id)
        store = self.service.store
        store.conclude_mission(system_id, reason="needs_owner", summary="?")
        with store.transaction() as db:                       # the reopening a real answer writes
            store._event(db, system_id, "mission.reopened", system_id, {"note": "Answered here"})
        self.service.sync_agents(system_id)
        self.assertEqual(agent_service.get_item(item["id"])["status"], "answered")
        self.assertEqual(agent_service.get_item(item["id"])["answer"], "Answered here")

    async def test_a_question_from_a_deleted_team_is_dismissed_not_resent(self):
        self.serve(lambda body: reply([]))
        item = agent_service.add_item(self.lead_agent["id"], "question", "The team needs you", "?", team_id="gone")
        self.assertIn("deleted", agent_service.answer_item(item["id"], "reply", "hello"))
        self.assertEqual(agent_service.get_item(item["id"])["status"], "dismissed")

    # -- owner messages and access ---------------------------------------

    async def test_owner_messages_reach_one_teammate_or_all(self):
        self.serve(lambda body: reply([]))
        system_id = await self.create(extra=[member("Checker", "Fact checker")])
        team = self.team(system_id)
        store = self.service.store
        await self.service.message("alice", system_id, "Lead only", "m1")
        await self.service.message("alice", system_id, "Just Bo", "m2", to=team["Bo"]["id"])
        await self.service.message("alice", system_id, "Everyone", "m3", to="all")
        rows = store.owner_page("alice", system_id, "messages", 0, 50)["items"]
        to = lambda body: sorted(r["recipient_id"] for r in rows if r["body"] == body)
        self.assertEqual(to("Lead only"), [team["Ada"]["id"]])
        self.assertEqual(to("Just Bo"), [team["Bo"]["id"]])
        self.assertEqual(to("Everyone"), sorted(m["id"] for m in team.values()))
        other = await self.service.create("alice", payload(member("Lee", "Lead"), []), "other")
        with self.assertRaises(NotFound):
            await self.service.message("alice", system_id, "Wrong team", "m4", to=self.team(other["id"])["Lee"]["id"])

    def test_only_an_admin_seats_agents(self):
        body = swarm_routes.Create(**payload(member("Lee", "Lead", agent_id=self.lead_agent["id"]), []),
                                   command_id="x")
        with patch.object(swarm_routes.auth_manager, "is_admin", lambda user: False):
            with self.assertRaises(HTTPException) as refused:
                swarm_routes.agents_allowed("guest", body)
            self.assertEqual(refused.exception.status_code, 403)
            swarm_routes.agents_allowed("guest", swarm_routes.Create(**payload(member("Lee", "Lead"), []),
                                                                     command_id="y"))
        with patch.object(swarm_routes.auth_manager, "is_admin", lambda user: True):
            swarm_routes.agents_allowed("admin", body)


if __name__ == "__main__":
    unittest.main()
