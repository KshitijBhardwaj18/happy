"""Happy's incident ledger: recall similar incidents, remember outcomes and preferences.

Two backends behind one `Ledger` interface:

* `FileLedger` -- a single JSON document on disk (`Settings.ledger_dir/ledger.json`).
* `MemoryLedger` -- the same JSON document persisted as an AgentCore Memory
  event (latest event wins on load), plus one-sentence natural-language facts
  written as separate events so AgentCore's long-term strategies can extract
  them into `/users/{actorId}/facts` and `/users/{actorId}/preferences`.

`get_ledger()` picks the backend from `Settings.has_memory`.
"""
from __future__ import annotations

import json
import os
import tempfile
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from config import Settings, load_settings
from fingerprint import Fingerprint

_SIMILARITY_THRESHOLD = 0.6

_LEDGER_SESSION_ID = "ledger"
_FACTS_SESSION_ID = "ledger-facts"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _jaccard(a: list[str], b: list[str]) -> float:
    set_a, set_b = set(a), set(b)
    if not set_a or not set_b:
        return 0.0
    union = len(set_a | set_b)
    if union == 0:
        return 0.0
    return len(set_a & set_b) / union


class Outcome(BaseModel):
    action: dict[str, Any]
    succeeded: bool
    minutes_to_recover: float
    at: datetime = Field(default_factory=_utcnow)


class IncidentRecord(BaseModel):
    id: str
    fingerprint: Fingerprint
    count: int = 1
    first_seen: datetime = Field(default_factory=_utcnow)
    last_seen: datetime = Field(default_factory=_utcnow)
    summary: str = ""
    root_cause: str = ""
    outcomes: list[Outcome] = Field(default_factory=list)


class RecallResult(BaseModel):
    records: list[IncidentRecord] = Field(default_factory=list)
    related_memories: list[str] = Field(default_factory=list)


class LedgerDocument(BaseModel):
    """The single JSON document both backends persist."""

    records: dict[str, IncidentRecord] = Field(default_factory=dict)
    preferences: list[str] = Field(default_factory=list)


class Ledger(ABC):
    """Common interface for the incident ledger, backed by file or memory."""

    @abstractmethod
    def recall_similar(self, fp: Fingerprint) -> RecallResult: ...

    @abstractmethod
    def remember_incident(self, fp: Fingerprint, summary: str, root_cause: str) -> str: ...

    @abstractmethod
    def record_outcome(
        self, record_id: str, action: dict[str, Any], succeeded: bool, minutes_to_recover: float
    ) -> None: ...

    @abstractmethod
    def remember_preference(self, text: str) -> None: ...

    @abstractmethod
    def preferences(self) -> list[str]: ...

    @abstractmethod
    def recent_incidents(self, days: int = 7) -> list[IncidentRecord]: ...

    @abstractmethod
    def successful_fixes(self, record_id: str) -> list[dict[str, Any]]: ...

    # -- shared logic on top of the document a backend loads/saves ----------

    @staticmethod
    def _find_similar(document: LedgerDocument, fp: Fingerprint) -> list[IncidentRecord]:
        exact = document.records.get(fp.id)
        if exact is not None:
            return [exact]
        scored: list[tuple[float, IncidentRecord]] = []
        for record in document.records.values():
            overlap = _jaccard(fp.signatures, record.fingerprint.signatures)
            if overlap >= _SIMILARITY_THRESHOLD:
                scored.append((overlap, record))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [record for _, record in scored]

    @staticmethod
    def _apply_remember_incident(
        document: LedgerDocument, fp: Fingerprint, summary: str, root_cause: str
    ) -> str:
        now = _utcnow()
        existing = document.records.get(fp.id)
        if existing is not None:
            existing.count += 1
            existing.last_seen = now
            existing.summary = summary
            existing.root_cause = root_cause
        else:
            document.records[fp.id] = IncidentRecord(
                id=fp.id,
                fingerprint=fp,
                count=1,
                first_seen=now,
                last_seen=now,
                summary=summary,
                root_cause=root_cause,
                outcomes=[],
            )
        return fp.id

    @staticmethod
    def _apply_record_outcome(
        document: LedgerDocument,
        record_id: str,
        action: dict[str, Any],
        succeeded: bool,
        minutes_to_recover: float,
    ) -> None:
        record = document.records.get(record_id)
        if record is None:
            raise KeyError(f"No incident record with id {record_id!r}")
        record.outcomes.append(
            Outcome(action=action, succeeded=succeeded, minutes_to_recover=minutes_to_recover, at=_utcnow())
        )
        record.last_seen = _utcnow()

    @staticmethod
    def _apply_recent_incidents(document: LedgerDocument, days: int) -> list[IncidentRecord]:
        cutoff = _utcnow() - timedelta(days=days)
        records = [record for record in document.records.values() if record.last_seen >= cutoff]
        records.sort(key=lambda record: record.last_seen, reverse=True)
        return records

    @staticmethod
    def _apply_successful_fixes(document: LedgerDocument, record_id: str) -> list[dict[str, Any]]:
        record = document.records.get(record_id)
        if record is None:
            return []

        groups: dict[str, dict[str, Any]] = {}
        for outcome in record.outcomes:
            key = json.dumps(outcome.action, sort_keys=True, default=str)
            group = groups.setdefault(
                key,
                {
                    "action": outcome.action,
                    "success_count": 0,
                    "failure_count": 0,
                    "last_success_at": None,
                },
            )
            if outcome.succeeded:
                group["success_count"] += 1
                if group["last_success_at"] is None or outcome.at > group["last_success_at"]:
                    group["last_success_at"] = outcome.at
            else:
                group["failure_count"] += 1

        def sort_key(group: dict[str, Any]) -> tuple[int, int, float]:
            has_success = group["success_count"] > 0
            last_success_ts = group["last_success_at"].timestamp() if has_success else 0.0
            return (0 if has_success else 1, -group["success_count"], -last_success_ts)

        return sorted(groups.values(), key=sort_key)


class FileLedger(Ledger):
    """JSON-on-disk ledger. One document, written atomically."""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def _load(self) -> LedgerDocument:
        if not self.path.exists():
            return LedgerDocument()
        raw = self.path.read_text(encoding="utf-8")
        if not raw.strip():
            return LedgerDocument()
        return LedgerDocument.model_validate_json(raw)

    def _save(self, document: LedgerDocument) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = document.model_dump_json(indent=2)
        fd, tmp_path = tempfile.mkstemp(dir=str(self.path.parent), prefix=".ledger-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
                tmp_file.write(payload)
            os.replace(tmp_path, self.path)
        except BaseException:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    def recall_similar(self, fp: Fingerprint) -> RecallResult:
        document = self._load()
        return RecallResult(records=self._find_similar(document, fp), related_memories=[])

    def remember_incident(self, fp: Fingerprint, summary: str, root_cause: str) -> str:
        document = self._load()
        record_id = self._apply_remember_incident(document, fp, summary, root_cause)
        self._save(document)
        return record_id

    def record_outcome(
        self, record_id: str, action: dict[str, Any], succeeded: bool, minutes_to_recover: float
    ) -> None:
        document = self._load()
        self._apply_record_outcome(document, record_id, action, succeeded, minutes_to_recover)
        self._save(document)

    def remember_preference(self, text: str) -> None:
        document = self._load()
        document.preferences.append(text)
        self._save(document)

    def preferences(self) -> list[str]:
        return self._load().preferences

    def recent_incidents(self, days: int = 7) -> list[IncidentRecord]:
        return self._apply_recent_incidents(self._load(), days)

    def successful_fixes(self, record_id: str) -> list[dict[str, Any]]:
        return self._apply_successful_fixes(self._load(), record_id)


class MemoryLedger(Ledger):
    """AgentCore Memory-backed ledger.

    The JSON document is persisted as an event in session id "ledger"
    (`extraction_mode="SKIP"` so the raw JSON blob is never fed to the
    long-term memory strategies); the latest such event wins on load. Each
    incident/outcome/preference is also written as a short natural-language
    sentence, as a separate event in session id "ledger-facts", so AgentCore's
    semantic and user-preference strategies can extract it into
    `/users/{actorId}/facts` and `/users/{actorId}/preferences`.
    """

    def __init__(self, memory_id: str, actor_id: str, region: str):
        from bedrock_agentcore.memory import MemoryClient

        self.memory_id = memory_id
        self.actor_id = actor_id
        self.client = MemoryClient(region_name=region)

    # -- document persistence ------------------------------------------------

    def _load(self) -> LedgerDocument:
        events = self.client.list_events(
            memory_id=self.memory_id,
            actor_id=self.actor_id,
            session_id=_LEDGER_SESSION_ID,
            max_results=1000,
            include_payload=True,
        )
        latest_json: str | None = None
        for event in events:
            for message in event.get("payload", []):
                conversational = message.get("conversational") or {}
                text = (conversational.get("content") or {}).get("text")
                if text and text.strip().startswith("{"):
                    latest_json = text
        if latest_json is None:
            return LedgerDocument()
        return LedgerDocument.model_validate_json(latest_json)

    def _save(self, document: LedgerDocument) -> None:
        payload = document.model_dump_json()
        self.client.create_event(
            memory_id=self.memory_id,
            actor_id=self.actor_id,
            session_id=_LEDGER_SESSION_ID,
            messages=[(payload, "ASSISTANT")],
            extraction_mode="SKIP",
        )

    def _remember_fact(self, text: str, role: str = "ASSISTANT") -> None:
        self.client.create_event(
            memory_id=self.memory_id,
            actor_id=self.actor_id,
            session_id=_FACTS_SESSION_ID,
            messages=[(text, role)],
        )

    # -- Ledger interface -----------------------------------------------------

    def recall_similar(self, fp: Fingerprint) -> RecallResult:
        document = self._load()
        records = self._find_similar(document, fp)
        query = " ".join([fp.service, fp.namespace, fp.reason, *fp.signatures])
        related_memories: list[str] = []
        try:
            memories = self.client.retrieve_memories(
                memory_id=self.memory_id,
                namespace=f"/users/{self.actor_id}/facts",
                query=query,
                top_k=5,
            )
        except Exception:
            memories = []
        for memory in memories:
            content = memory.get("content") if isinstance(memory, dict) else None
            text = content.get("text") if isinstance(content, dict) else None
            if text:
                related_memories.append(text)
        return RecallResult(records=records, related_memories=related_memories)

    def remember_incident(self, fp: Fingerprint, summary: str, root_cause: str) -> str:
        document = self._load()
        is_repeat = fp.id in document.records
        record_id = self._apply_remember_incident(document, fp, summary, root_cause)
        self._save(document)
        record = document.records[record_id]
        if is_repeat:
            fact = (
                f"Incident {fp.service}/{fp.reason} in namespace {fp.namespace} has now happened "
                f"{record.count} times. Latest summary: {summary}"
            )
        else:
            fact = (
                f"New incident on {fp.service}/{fp.reason} in namespace {fp.namespace}: {summary} "
                f"(root cause: {root_cause})"
            )
        self._remember_fact(fact)
        return record_id

    def record_outcome(
        self, record_id: str, action: dict[str, Any], succeeded: bool, minutes_to_recover: float
    ) -> None:
        document = self._load()
        self._apply_record_outcome(document, record_id, action, succeeded, minutes_to_recover)
        self._save(document)
        record = document.records.get(record_id)
        service = record.fingerprint.service if record else record_id
        outcome_word = "succeeded" if succeeded else "failed"
        fact = f"Action {action} on {service} {outcome_word}, taking {minutes_to_recover:.1f} minutes to recover."
        self._remember_fact(fact)

    def remember_preference(self, text: str) -> None:
        document = self._load()
        document.preferences.append(text)
        self._save(document)
        self._remember_fact(f"Preference: {text}", role="USER")

    def preferences(self) -> list[str]:
        return self._load().preferences

    def recent_incidents(self, days: int = 7) -> list[IncidentRecord]:
        return self._apply_recent_incidents(self._load(), days)

    def successful_fixes(self, record_id: str) -> list[dict[str, Any]]:
        return self._apply_successful_fixes(self._load(), record_id)


def get_ledger(settings: Settings | None = None) -> Ledger:
    """Return the AgentCore Memory ledger when memory is configured, else a file ledger."""
    settings = settings or load_settings()
    if settings.has_memory:
        return MemoryLedger(memory_id=settings.memory_id, actor_id=settings.actor_id, region=settings.aws_region)
    return FileLedger(Path(settings.ledger_dir) / "ledger.json")
