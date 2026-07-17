"""Phase 5 MCP wrapper — a thin server over the deterministic engine (Architecture.md §9).

Design (issue #12, settled in its design comment):

* **No business logic here.** Every tool resolves config/db exactly as the CLI does
  (``_resolve_*`` reused from :mod:`pemr.cli`), calls the *same* ``pemr.*`` function the
  CLI dispatches to, and returns the engine's structured (``--json``-shaped) result as-is.
* **In-process, never a subprocess.** The renderers/query layer are plain callables with
  structured args for exactly this reuse (#9); the wrapper imports and calls them.
* **The ``mcp`` SDK is an optional dependency** (``pip install pemr[mcp]``). The tool
  functions below import nothing from ``mcp`` and take a caller-managed ``sqlite3``
  connection, so the wrapper test-suite runs without the SDK installed. Only
  :func:`build_server` / :func:`main` (the stdio entry point) require it, imported lazily.
* **Read/write separation** is declared via MCP ``readOnlyHint`` annotations in
  :func:`build_server`. The read-only tools here never write; the write tools
  (``person_add``, ``ingest``, ``commit_extraction``, and ``review_conflicts`` *with* a
  resolution) are the only ones that mutate.

Run it: ``python -m pemr.mcp_server`` (stdio transport). Config/db resolution is env +
``config.toml`` (``PEMR_DB`` / ``PEMR_CONFIG`` / ``[paths]``) — identical to the CLI with
no flags; see ``AGENTS.md`` for the client-config snippet.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from types import SimpleNamespace
from typing import Any

# Engine modules are underscore-aliased so the public tool named `query` (and any future
# tool sharing a module name) can't shadow the module it delegates to.
from . import cli, db
from . import dedup as _dedup
from . import ingest as _ingest
from . import persons as _persons
from . import query as _query
from . import render as _render


class ToolError(RuntimeError):
    """A friendly, expected tool failure (bad slug, un-migrated DB, validation, …).

    The stdio server maps this to an MCP tool error; tests assert it directly. It is
    deliberately distinct from an unexpected crash so the wrapper never leaks a raw
    traceback for the ordinary "you asked for something that isn't there" cases.
    """


# --------------------------------------------------------------------------- #
# Resolution — reuse the CLI's exact logic (no new path logic, issue §1)
# --------------------------------------------------------------------------- #

# A flagless args stand-in: the CLI resolvers read `.db`/`.config`/`.sources`/
# `.dictionary`; MCP has no per-call flags, so all are None and resolution falls
# through to env vars + config.toml — byte-identical to `pemr <cmd>` with no flags.
_ARGS = SimpleNamespace(db=None, config=None, sources=None, dictionary=None)


def _db_path():
    return cli._resolve_db_path(_ARGS)


def _dictionary() -> dict[str, str]:
    return _dedup.load_dictionary(cli._resolve_dictionary_path(_ARGS))


def _connect() -> sqlite3.Connection:
    return db.connect(_db_path())


def _friendly(exc: Exception) -> ToolError:
    return ToolError(str(exc))


# --------------------------------------------------------------------------- #
# Tools — each mirrors one CLI verb; returns the engine payload as-is.
# Every tool takes an explicit connection so tests seed a scratch DB directly and
# the stdio wrappers (build_server) own connection lifecycle.
# --------------------------------------------------------------------------- #

# --- people (read: person_list/show; write: person_add) --------------------

def person_add(
    conn: sqlite3.Connection,
    *,
    slug: str,
    full_name: str,
    dob: str | None = None,
    sex: str | None = None,
    blood_type: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """[write] Add a person to the roster. Mirrors ``pemr person add``."""
    try:
        person = _persons.add_person(
            conn, slug=slug, full_name=full_name, dob=dob, sex=sex,
            blood_type=blood_type, notes=notes,
        )
    except ValueError as exc:  # SlugExistsError / empty-field
        raise _friendly(exc) from exc
    return asdict(person)


def person_list(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """[read] List people. Mirrors ``pemr person list``."""
    return [asdict(p) for p in _persons.list_people(conn)]


def person_show(conn: sqlite3.Connection, *, slug: str) -> dict[str, Any]:
    """[read] One person by slug. Mirrors ``pemr person show``."""
    person = _persons.get_person(conn, slug)
    if person is None:
        raise ToolError(f"no person with slug '{slug}'")
    return asdict(person)


# --- ingest / extraction (write) -------------------------------------------

def ingest_document(
    conn: sqlite3.Connection,
    *,
    file: str,
    person: str,
    ocr_text: str | None = None,
    doc_date: str | None = None,
    category: str | None = None,
    provider: str | None = None,
    ocr: bool = False,
) -> dict[str, Any]:
    """[write] Ingest a document (hash, blob-store, layer-1 dedup). Mirrors ``pemr ingest``.

    ``ocr_text`` is the agent's own transcription — the ``AGENTS.md`` default path.
    The response's ``ocr_text_populated`` lets the agent self-check the FTS-visibility
    contract without a follow-up read.
    """
    try:
        sources_dir = cli._resolve_sources_dir(_ARGS)
    except SystemExit as exc:  # CLI resolvers exit the process; a server must not
        raise ToolError(str(exc)) from exc
    try:
        result = _ingest.ingest_document(
            conn, file, person_slug=person, sources_dir=sources_dir,
            doc_date=doc_date, category=category, provider=provider,
            ocr=ocr, ocr_text=ocr_text,
        )
    except (db.NotMigratedError, _ingest.IngestError) as exc:
        raise _friendly(exc) from exc
    return {
        "status": result.status,
        "is_duplicate": result.is_duplicate,
        "ocr_text_populated": result.ocr_text_populated,
        "document": asdict(result.document),
    }


def commit_extraction(
    conn: sqlite3.Connection,
    *,
    document_id: int,
    records: dict[str, Any],
) -> dict[str, Any]:
    """[write] Validate + dedup + commit extracted rows for a document. Mirrors
    ``pemr commit-extraction``. ``records`` maps record-type -> list of row dicts;
    per the ``AGENTS.md`` naming rule, analyte names must be canonical dictionary tokens.
    """
    try:
        summary = _dedup.commit_extraction(conn, document_id, records, _dictionary())
    except (db.NotMigratedError, _dedup.ValidationError) as exc:
        raise _friendly(exc) from exc
    return {
        "counts": summary.counts,
        "new": summary.new,
        "duplicate": summary.duplicate,
        "conflict": summary.conflict,
    }


# --- conflicts (read to list; write only with a signed-off resolution) ------

def review_conflicts(
    conn: sqlite3.Connection,
    *,
    resolve: int | None = None,
    keep: str = "existing",
    signoff: str | None = None,
    note: str | None = None,
    all: bool = False,
) -> Any:
    """[read to list / write to resolve] List or resolve staged dedup conflicts.

    Listing is free. **Resolution requires explicit human sign-off** (``AGENTS.md``
    conflict discipline): ``signoff`` must quote the human's instruction verbatim, or the
    write is refused. The sign-off text is threaded into the stored resolution note so the
    record shows who authorized it.
    """
    if resolve is not None:
        if not (signoff and signoff.strip()):
            raise ToolError(
                "conflict resolution requires human sign-off: pass `signoff` quoting the "
                "human's explicit instruction. A standing/general instruction is not "
                "sign-off (AGENTS.md conflict discipline)."
            )
        merged_note = f"signoff: {signoff.strip()}" + (f" — {note}" if note else "")
        try:
            _dedup.resolve_conflict(conn, resolve, keep=keep, note=merged_note)
        except (db.NotMigratedError, ValueError) as exc:
            raise _friendly(exc) from exc
        return {"resolved": resolve, "keep": keep, "signoff": signoff.strip()}

    try:
        rows = _dedup.list_conflicts(conn, status=None if all else "open")
    except db.NotMigratedError as exc:
        raise _friendly(exc) from exc
    return [dict(r) for r in rows]


# --- structured reads -------------------------------------------------------

def query(
    conn: sqlite3.Connection,
    *,
    kind: str,
    person: str,
    test: str | None = None,
    since: str | None = None,
    active: bool = False,
) -> list[dict[str, Any]]:
    """[read] Structured reads over the record tables. ``kind`` is ``labs`` | ``meds`` |
    ``timeline`` (mirrors ``pemr query <kind>`` rather than fanning out to three tools).
    """
    try:
        if kind == "labs":
            rows = _query.query_labs(conn, person, test=test, since=since, dictionary=_dictionary())
        elif kind == "meds":
            rows = _query.query_meds(conn, person, active=active)
        elif kind == "timeline":
            rows = _query.query_timeline(conn, person, since=since)
        else:
            raise ToolError(f"unknown query kind '{kind}' (known: labs, meds, timeline)")
    except (db.NotMigratedError, _query.PersonNotFoundError) as exc:
        raise _friendly(exc) from exc
    return [{k: v for k, v in r.items() if k != "dedup_key"} for r in rows]


def find(conn: sqlite3.Connection, *, person: str, query_text: str) -> list[dict[str, Any]]:
    """[read] Full-text search over OCR text + record fields. Mirrors ``pemr find``."""
    try:
        return _query.find(conn, person, query_text)
    except (db.NotMigratedError, _query.PersonNotFoundError) as exc:
        raise _friendly(exc) from exc


def trends(conn: sqlite3.Connection, *, person: str, test: str) -> dict[str, Any]:
    """[read] min/max/latest/slope for one analyte over time. Mirrors ``pemr trends``."""
    try:
        return _query.trends(conn, person, test, dictionary=_dictionary())
    except (db.NotMigratedError, _query.PersonNotFoundError) as exc:
        raise _friendly(exc) from exc


# --- renderers (read-only Markdown) ----------------------------------------

def render_summary(conn: sqlite3.Connection, *, person: str) -> dict[str, str]:
    """[read] Master summary for a person -> Markdown. Mirrors ``pemr render summary``."""
    try:
        markdown = _render.render_summary(conn, person, dictionary=_dictionary())
    except (db.NotMigratedError, _query.PersonNotFoundError) as exc:
        raise _friendly(exc) from exc
    return {"markdown": markdown}


def render_brief(conn: sqlite3.Connection, *, appointment: int) -> dict[str, str]:
    """[read] Walk-in brief for one appointment -> Markdown. Mirrors ``pemr render brief``.

    The Markdown carries a placeholder "Medication Interaction Review" section; per
    ``AGENTS.md`` the agent fills it from general knowledge under the mandated
    verify-with-a-pharmacist framing, and must never claim safety or give dosing advice.
    """
    try:
        markdown = _render.render_brief(conn, appointment, dictionary=_dictionary())
    except (db.NotMigratedError, _render.AppointmentNotFoundError) as exc:
        raise _friendly(exc) from exc
    return {"markdown": markdown}


def render_journal(
    conn: sqlite3.Connection, *, person: str, since: str | None = None
) -> dict[str, str]:
    """[read] Narrative chronology for a person -> Markdown. Mirrors ``pemr render journal``."""
    try:
        markdown = _render.render_journal(conn, person, since=since)
    except (db.NotMigratedError, _query.PersonNotFoundError) as exc:
        raise _friendly(exc) from exc
    return {"markdown": markdown}


# The exposed tool surface, in one place. AGENTS.md's contract-lint asserts it
# references every name here; build_server registers exactly these.
READ_ONLY_TOOLS = (
    "person_list", "person_show", "query", "find", "trends",
    "render_summary", "render_brief", "render_journal",
)
WRITE_TOOLS = ("person_add", "ingest", "commit_extraction", "review_conflicts")
TOOL_NAMES = READ_ONLY_TOOLS + WRITE_TOOLS


# --------------------------------------------------------------------------- #
# stdio server — the only part that needs the `mcp` SDK (imported lazily).
# --------------------------------------------------------------------------- #

def build_server():  # pragma: no cover - exercised only with the mcp SDK installed
    """Construct the FastMCP server, registering every tool with the right read/write
    annotation. Each registered handler opens a connection, delegates to the plain tool
    function above, and closes it — the tool functions stay connection-injected for tests.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ModuleNotFoundError as exc:  # friendly nudge, not a traceback
        raise SystemExit(
            "error: the MCP server needs the optional `mcp` SDK — install with "
            "`pip install pemr[mcp]` (or `pip install mcp`)."
        ) from exc

    server = FastMCP("pemr")

    def _run(fn, /, **kwargs):
        conn = _connect()
        try:
            return fn(conn, **kwargs)
        finally:
            conn.close()

    ro = {"readOnlyHint": True}
    rw = {"readOnlyHint": False}

    # Tools are registered with explicit `name=` so the wire surface equals TOOL_NAMES —
    # FastMCP otherwise registers under the function name (`*_tool`), and an agent
    # following AGENTS.md (which documents the CLI-mirroring names) would call a
    # nonexistent tool. The `*_tool` function names stay distinct from the plain
    # connection-injected tool functions above they delegate to.

    # -- read-only --
    @server.tool(name="person_list", annotations=ro)
    def person_list_tool() -> list[dict]:
        return _run(person_list)

    @server.tool(name="person_show", annotations=ro)
    def person_show_tool(slug: str) -> dict:
        return _run(person_show, slug=slug)

    @server.tool(name="query", annotations=ro)
    def query_tool(kind: str, person: str, test: str | None = None,
                   since: str | None = None, active: bool = False) -> list[dict]:
        return _run(query, kind=kind, person=person, test=test, since=since, active=active)

    @server.tool(name="find", annotations=ro)
    def find_tool(person: str, query_text: str) -> list[dict]:
        return _run(find, person=person, query_text=query_text)

    @server.tool(name="trends", annotations=ro)
    def trends_tool(person: str, test: str) -> dict:
        return _run(trends, person=person, test=test)

    @server.tool(name="render_summary", annotations=ro)
    def render_summary_tool(person: str) -> dict:
        return _run(render_summary, person=person)

    @server.tool(name="render_brief", annotations=ro)
    def render_brief_tool(appointment: int) -> dict:
        return _run(render_brief, appointment=appointment)

    @server.tool(name="render_journal", annotations=ro)
    def render_journal_tool(person: str, since: str | None = None) -> dict:
        return _run(render_journal, person=person, since=since)

    # -- write --
    @server.tool(name="person_add", annotations=rw)
    def person_add_tool(slug: str, full_name: str, dob: str | None = None,
                        sex: str | None = None, blood_type: str | None = None,
                        notes: str | None = None) -> dict:
        return _run(person_add, slug=slug, full_name=full_name, dob=dob, sex=sex,
                    blood_type=blood_type, notes=notes)

    @server.tool(name="ingest", annotations=rw)
    def ingest_tool(file: str, person: str, ocr_text: str | None = None,
                    doc_date: str | None = None, category: str | None = None,
                    provider: str | None = None, ocr: bool = False) -> dict:
        return _run(ingest_document, file=file, person=person, ocr_text=ocr_text,
                    doc_date=doc_date, category=category, provider=provider, ocr=ocr)

    @server.tool(name="commit_extraction", annotations=rw)
    def commit_extraction_tool(document_id: int, records: dict) -> dict:
        return _run(commit_extraction, document_id=document_id, records=records)

    @server.tool(name="review_conflicts", annotations=rw)
    def review_conflicts_tool(resolve: int | None = None, keep: str = "existing",
                              signoff: str | None = None, note: str | None = None,
                              all: bool = False) -> object:
        return _run(review_conflicts, resolve=resolve, keep=keep, signoff=signoff,
                    note=note, all=all)

    return server


def main() -> None:  # pragma: no cover - stdio entry point
    build_server().run()


if __name__ == "__main__":  # pragma: no cover
    main()
