"""Bounded, read-only retrieval for an explicitly opted-in Swarm company.

This is retrieval augmentation over JARVIS's existing memory APIs. Those
APIs are keyword based today; this module deliberately does not call them
semantic search. The two-step search/read shape keeps whole vaults, chats and
documents out of every worker prompt and gives a later vector index one stable
contract to implement.
"""
from core import memory_tools, projects
from core.session_manager import session_manager
from services import documents_service

MAX_QUERY = 500
MAX_READ = 4000


def _snippet(text, query, radius=200):
    index = text.casefold().find(query.casefold())
    if index < 0:
        return None
    start = max(0, index - radius)
    end = min(len(text), index + len(query) + radius)
    return ("..." if start else "") + text[start:end].strip() + ("..." if end < len(text) else "")


class SwarmMemory:
    def __init__(self, settings):
        settings = settings or {}
        self.sources = tuple(settings.get("sources") or ())
        self.project_id = settings.get("project_id")
        self.max_results = min(5, max(1, int(settings.get("max_results") or 5)))

    @property
    def enabled(self):
        return bool(self.sources)

    def search(self, query):
        query = str(query or "").strip()[:MAX_QUERY]
        if not query:
            raise ValueError("memory query is required")
        results = []

        def add(source, reference, title, snippet):
            if len(results) < self.max_results:
                results.append({"source": source, "ref": reference, "title": title,
                                "snippet": (snippet or "")[:600]})

        if "project" in self.sources:
            project = projects.get_project(self.project_id) if self.project_id else None
            if project:
                hit = _snippet(project.get("instructions") or "", query)
                if hit:
                    add("project", f"project:{project['id']}", project["name"], hit)
                for document_id in project.get("document_ids") or []:
                    if len(results) >= self.max_results:
                        break
                    document = documents_service.get_document(document_id)
                    if not document:
                        continue
                    hit = _snippet(document.get("content") or "", query)
                    if hit or query.casefold() in document.get("title", "").casefold():
                        add("project", f"document:{document_id}", document.get("title") or document_id, hit or "")

        if "vault" in self.sources and len(results) < self.max_results:
            for item in memory_tools.search_vault(query, max_results=self.max_results - len(results)):
                add("vault", "vault:" + item["path"], item["path"], item["snippet"])

        if "sessions" in self.sources and len(results) < self.max_results:
            for item in memory_tools.search_sessions(query, max_results=self.max_results - len(results)):
                add("sessions", "session:" + item["session_id"], item["session_title"], item["snippet"])

        if "library" in self.sources and len(results) < self.max_results:
            for item in documents_service.search_documents(query, max_results=self.max_results - len(results)):
                add("library", "document:" + item["id"], item["title"], item["snippet"])
        return results

    def read(self, reference):
        reference = str(reference or "").strip()
        kind, separator, identifier = reference.partition(":")
        if not separator or not identifier:
            raise ValueError("Use a memory reference returned by search_memory")
        if kind == "vault" and "vault" in self.sources:
            return memory_tools.read_vault_file(identifier, max_chars=MAX_READ)
        if kind == "session" and "sessions" in self.sources:
            session = session_manager.get_session(identifier)
            if not session:
                raise ValueError("Memory item was not found")
            text = "\n".join(f"{item.get('role', 'unknown')}: {item.get('content', '')}"
                             for item in session.get("messages", []))
            return text[:MAX_READ] + ("...[truncated]" if len(text) > MAX_READ else "")
        if kind == "document" and ({"library", "project"} & set(self.sources)):
            document = documents_service.get_document(identifier)
            if not document:
                raise ValueError("Memory item was not found")
            if "project" in self.sources and "library" not in self.sources:
                project = projects.get_project(self.project_id) if self.project_id else None
                if not project or identifier not in (project.get("document_ids") or []):
                    raise ValueError("Document is outside this company's JARVIS Project")
            text = document.get("content") or ""
            return text[:MAX_READ] + ("...[truncated]" if len(text) > MAX_READ else "")
        if kind == "project" and "project" in self.sources and identifier == self.project_id:
            project = projects.get_project(identifier)
            if not project:
                raise ValueError("Memory item was not found")
            return (project.get("instructions") or "")[:MAX_READ]
        raise ValueError("That memory source is not enabled for this company")
