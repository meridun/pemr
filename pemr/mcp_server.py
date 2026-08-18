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
  (``person_add``, ``person_edit``, ``ingest``, ``commit_extraction``,
  ``document_set_text``, and ``review_conflicts`` *with* a resolution) are the only ones
  that mutate.

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
from . import __version__, cli, db
from . import curation as _curation
from . import dedup as _dedup
from . import documents as _documents
from . import ingest as _ingest
from . import persons as _persons
from . import query as _query
from . import render as _render
from . import tombstones as _tombstones


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


def _routine_procedures() -> tuple[str, ...]:
    """The summary's render-only routine-procedure list (issue #166), from the same file
    :func:`_dictionary` reads -- so both front doors narrow identically."""
    return _dedup.load_routine_procedures(cli._resolve_dictionary_path(_ARGS))


def _connect() -> sqlite3.Connection:
    """Same missing-database gate the CLI applies, raised as a tool error (issue #55).

    Both front doors must give the same answer: neither may silently create an empty
    archive over a database that has gone missing. The message is shared verbatim with
    ``cli._connect_db``; only the exception type differs (a server must not SystemExit).
    """
    path = _db_path()
    if not db.database_exists(path):
        raise ToolError(cli._no_database_message(path))
    return db.connect(path)


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


def person_edit(
    conn: sqlite3.Connection,
    *,
    slug: str,
    full_name: str | None = None,
    dob: str | None = None,
    sex: str | None = None,
    blood_type: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """[write] Edit a person's fields (partial update). Mirrors ``pemr person edit``.

    Only the fields you pass change; ``slug`` is not editable. Pass an empty string for
    a nullable field (``dob``/``sex``/``blood_type``/``notes``) to clear it to ``NULL``.
    """
    # None means "not provided" (skip); an explicit "" clears — same as the CLI, where
    # an unset flag is None and `--dob ""` clears the column.
    fields = {
        name: value
        for name, value in (
            ("full_name", full_name), ("dob", dob), ("sex", sex),
            ("blood_type", blood_type), ("notes", notes),
        )
        if value is not None
    }
    try:
        person = _persons.update_person(conn, slug, **fields)
    except _persons.PersonNotFoundError as exc:
        raise ToolError(str(exc)) from exc
    except ValueError as exc:  # empty name / no fields / unknown field
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
    force: bool = False,
    study: str | None = None,
    allow_large: bool = False,
) -> dict[str, Any]:
    """[write] Ingest a document (hash, blob-store, layer-1 dedup). Mirrors ``pemr ingest``.

    ``ocr_text`` is the agent's own transcription — the ``AGENTS.md`` default path.
    The response's ``ocr_text_populated`` lets the agent self-check the FTS-visibility
    contract without a follow-up read. ``ocr=true`` is the fallback: it extracts by
    whatever route the file type allows (plaintext/`.docx`/`.xlsx`, CCDA `.xml` and a
    saved `.html`/`.htm` page natively, everything
    else via tesseract), and stores nothing when nothing could be read — `.pdf`, `.rtf`,
    `.msg` and `.doc` have no route at all (tesseract does not accept PDF input), so
    transcribe those yourself.

    A Google Drive pointer stub (``.gsheet``/``.gdoc`` — a ~1 KB JSON link, not the
    document) is refused pre-write; the fix is to export it from Drive first.

    ``study="dicom"`` makes ``file`` a study *directory* — a burned imaging disc — and
    packs its slices into one document (issue #69). ``ocr_text`` then defaults to a
    derived study summary; pass a transcription of the accompanying radiology report
    when you have one. ``allow_large`` waives the study size guard.

    When text is present it is checked against the claimed owner; the verdict comes
    back as ``owner_check`` (``null`` on a duplicate, which is never checked). A
    ``mismatch``/``suspect`` verdict refuses the ingest pre-write — per ``AGENTS.md``
    §3, surface the verdict and its evidence to the human and get an explicit
    go-ahead before retrying with ``force=true``. ``suspect`` (a patient-identity
    header naming nobody on the roster) is scoped by route, and the split is *structured
    vs prose*: it applies to the text you supply, to a tesseract pass, and to a natively
    extracted ``.html``/``.htm`` page (a saved portal page is a printed page). Never to
    the structured native routes
    (``.txt``/``.md``/``.csv``/``.tsv``/``.json``/``.log``/``.docx``/``.xlsx``, CCDA
    ``.xml``), where
    those words are column labels — so pass your transcription as ``ocr_text`` rather
    than saving it to a ``.txt`` and re-reading that. ``mismatch`` holds on every route.

    Content whose hash carries a **tombstone** — a removal a human recorded as permanent
    (issue #80) — comes back as ``status="tombstoned"`` with ``document: null`` and the
    ``tombstone`` row: a structured, non-exceptional "not filed, on purpose". ``force``
    does **not** override it (unlike the owner check, which is a heuristic): a tombstone
    is the human's already-recorded decision, so forcing one is never an agent judgment
    call. Report the skip and its reason and stop; lifting it is a CLI action.
    """
    try:
        sources_dir = cli._resolve_sources_dir(_ARGS)
    except SystemExit as exc:  # CLI resolvers exit the process; a server must not
        raise ToolError(str(exc)) from exc
    try:
        if study:
            result = _ingest.ingest_study_dir(
                conn, file, person_slug=person, sources_dir=sources_dir,
                study=study, allow_large=allow_large, doc_date=doc_date,
                category=category, provider=provider, ocr_text=ocr_text, force=force,
                tombstone_force=False,
            )
        else:
            result = _ingest.ingest_document(
                conn, file, person_slug=person, sources_dir=sources_dir,
                doc_date=doc_date, category=category, provider=provider,
                ocr=ocr, ocr_text=ocr_text, force=force, tombstone_force=False,
            )
    except (db.NotMigratedError, _ingest.IngestError) as exc:
        raise _friendly(exc) from exc
    if result.is_tombstoned and force:
        # `force` overrides the owner check, which is a *heuristic* a human may
        # reasonably ask an agent to override. A tombstone is the opposite: it is the
        # human's already-recorded decision that this content stays out, so overriding
        # it is never an agent judgment call (AGENTS.md §3). Lifting it is a CLI action.
        sha = (result.tombstone or {}).get("sha256", "")
        raise ToolError(
            "this content is tombstoned - a human recorded its removal as intentional "
            f"({_tombstones.describe(result.tombstone or {})}). `force` does not "
            "override a tombstone. Report the skip and its reason to the human; if "
            f"they want it filed, they lift it at the CLI with "
            f"`pemr document tombstone rm {sha}`."
        )
    return {
        "status": result.status,
        "is_duplicate": result.is_duplicate,
        "is_tombstoned": result.is_tombstoned,
        "ocr_text_populated": result.ocr_text_populated,
        "owner_check": (
            asdict(result.owner_check) if result.owner_check is not None else None
        ),
        "tombstone": result.tombstone,
        "document": asdict(result.document) if result.document is not None else None,
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
    except (
        db.NotMigratedError, _dedup.ValidationError, _dedup.DictionaryDriftError
    ) as exc:
        # Drift is a refusal carrying its own remedy (`pemr rekey --apply`), not an
        # internal error - surface it as friendly as a schema violation.
        raise _friendly(exc) from exc
    return {
        "counts": summary.counts,
        "new": summary.new,
        "duplicate": summary.duplicate,
        "enriched": summary.enriched,
        "conflict": summary.conflict,
    }


def document_set_text(
    conn: sqlite3.Connection, *, document_id: int, text: str
) -> dict[str, Any]:
    """[write] Attach a document's text (``ocr_text``) after ingest. Mirrors
    ``pemr document set-text``.

    This is **ingest completion**, not misfiling recovery: ``AGENTS.md`` §3 makes a
    populated ``ocr_text`` an obligation, and ``ingest`` returns early on a layer-1
    content-hash hit — so an agent that missed the text on the first pass cannot fix it by
    re-ingesting. This tool is the remediation. The document becomes visible to ``find``
    immediately (FTS is trigger-maintained).

    Deliberately narrower than the CLI: there is **no ``force``**. Filling an empty
    ``ocr_text`` is an agent action; replacing an existing transcription is a human one at
    the CLI (``pemr document set-text <id> --ocr-text-file <path> --force``), because it
    discards work that is not cheaply re-derived. Refused with an explanatory error when
    the column is already populated.

    The rest of the ``document`` group (``list``/``show``/``edit``/``reassign``/``rm``) is
    CLI-only by design — recovery is a human escape hatch.
    """
    try:
        return _documents.set_document_text(conn, document_id, text)
    # DocumentNotFoundError and OcrTextPresentError are both ValueError subclasses.
    except (db.NotMigratedError, ValueError) as exc:
        raise _friendly(exc) from exc


# --- conflicts (read to list; write only with a signed-off resolution) ------

def review_conflicts(
    conn: sqlite3.Connection,
    *,
    resolve: int | None = None,
    keep: str = "existing",
    signoff: str | None = None,
    note: str | None = None,
    fields: dict[str, str] | None = None,
    all: bool = False,
) -> Any:
    """[read to list / write to resolve] List or resolve staged dedup conflicts.

    Listing is free, and reports ``occurrences`` — how many rows are already stored
    under the conflict's identity. Resolution takes ``keep`` = ``"existing"`` (drop the
    incoming row), ``"incoming"`` (overwrite the stored one), ``"both"`` (admit the
    incoming row *alongside* the stored one as a new occurrence of that identity — for a
    genuine repeat, e.g. two same-day draws on a report that prints no collection times)
    or ``"merge"`` (field level: take what the incoming row states over a stored NULL,
    keep what it leaves unstated, so accepting a refinement doesn't erase the fields the
    document is simply silent about). ``"merge"`` **refuses** when both rows state a
    different value for a field, naming those fields and writing nothing; pass
    ``fields`` = ``{"<field>": "existing"|"incoming"}`` to settle one, and only for a
    field that actually collides.

    Listing is free. **Resolution requires explicit human sign-off** (``AGENTS.md``
    conflict discipline) — ``"both"`` and ``"merge"`` included, and a ``fields`` choice is
    itself a decision the human has to have made: ``signoff`` must quote the human's
    instruction verbatim, or the write is refused. The sign-off text is threaded into the
    stored resolution note so the record shows who authorized it.
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
            result = _dedup.resolve_conflict(
                conn, resolve, keep=keep, note=merged_note, dictionary=_dictionary(),
                fields=fields,
            )
        # ValueError covers ValidationError, raised when a keep-both payload no longer
        # validates as a row.
        except (db.NotMigratedError, ValueError) as exc:
            raise _friendly(exc) from exc
        payload = {"resolved": resolve, "keep": keep, "signoff": signoff.strip()}
        if keep == "both":
            payload |= {
                "record_type": result.record_type,
                "row_id": result.row_id,
                "occurrence": result.occurrence,
                "no_op": result.no_op,
            }
        elif keep == "merge":
            # Field names and sides only, never values - same rule as the stored
            # resolution text (this payload is an audit trail too).
            payload |= {
                "record_type": result.record_type,
                "row_id": result.row_id,
                "taken": sorted(result.gains),
                "preserved": sorted(result.preserved),
                "settled": result.settled,
            }
        return payload

    try:
        rows = _dedup.list_conflicts(conn, status=None if all else "open")
        dictionary = _dictionary()
        return [
            dict(r) | {
                "occurrences": _dedup.conflict_occurrences(conn, r, dictionary)
            }
            for r in rows
        ]
    except db.NotMigratedError as exc:
        raise _friendly(exc) from exc


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

    Every row carries its curation verdict under ``_curation`` when a human recorded one
    (issue #131), and **nothing is suppressed**: this payload is the structured contract,
    the same call the CLI's ``--json`` makes, so it discloses the verdict and lets the
    caller decide. The suppression rule is the human-readable view's alone.
    """
    try:
        verdicts = _curation.load_verdicts(conn)
        if kind == "labs":
            rows = _query.query_labs(conn, person, test=test, since=since, dictionary=_dictionary())
            _curation.annotate_rows(rows, "lab_result", verdicts)
        elif kind == "meds":
            rows = _query.query_meds(conn, person, active=active)
            _curation.annotate_rows(rows, "medication", verdicts)
        elif kind == "timeline":
            rows = _query.query_timeline(
                conn, person, since=since, with_identity=bool(verdicts)
            )
            _curation.annotate_events(rows, verdicts)
            # Resolution scaffolding, not payload — the CLI strips the same three keys.
            rows = [
                {k: v for k, v in e.items() if k not in cli._QUERY_IDENTITY_KEYS}
                for e in rows
            ]
        else:
            raise ToolError(f"unknown query kind '{kind}' (known: labs, meds, timeline)")
    except (db.NotMigratedError, _query.PersonNotFoundError) as exc:
        raise _friendly(exc) from exc
    # Same read contract the CLI's `--json` emits: internal key columns stripped, and
    # attestation provenance carried only when the row actually has it (issue #110).
    return [_dedup.public_row(r) for r in rows]


def find(
    conn: sqlite3.Connection, *, query_text: str, person: str | None = None
) -> list[dict[str, Any]]:
    """[read] Full-text search over OCR text + record fields. Mirrors ``pemr find``.

    Omit ``person`` to search the whole household; each hit carries its owning
    person slug.
    """
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
        markdown = _render.render_summary(
            conn,
            person,
            dictionary=_dictionary(),
            routine_procedures=_routine_procedures(),
        )
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
WRITE_TOOLS = (
    "person_add", "person_edit", "ingest", "commit_extraction", "document_set_text",
    "review_conflicts",
)
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
            "error: the MCP server needs the optional `mcp` SDK - install with "
            "`pip install pemr[mcp]` (or `pip install mcp`)."
        ) from exc

    server = FastMCP("pemr")

    # `serverInfo.version` — what a client UI shows the human. FastMCP takes no
    # `version=` argument, and the low-level server it wraps falls back to the *SDK's*
    # own package version, so an unset version advertises e.g. "pemr 1.28.1" (the `mcp`
    # release) instead of pemr's — wrong, ahead of the real version, and useless for
    # diagnosing a version mismatch (#60). The attribute is read at initialize time, so
    # setting it after construction is equivalent to a constructor argument.
    server._mcp_server.version = __version__

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
    def find_tool(query_text: str, person: str | None = None) -> list[dict]:
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

    @server.tool(name="person_edit", annotations=rw)
    def person_edit_tool(slug: str, full_name: str | None = None,
                         dob: str | None = None, sex: str | None = None,
                         blood_type: str | None = None,
                         notes: str | None = None) -> dict:
        return _run(person_edit, slug=slug, full_name=full_name, dob=dob, sex=sex,
                    blood_type=blood_type, notes=notes)

    @server.tool(name="ingest", annotations=rw)
    def ingest_tool(file: str, person: str, ocr_text: str | None = None,
                    doc_date: str | None = None, category: str | None = None,
                    provider: str | None = None, ocr: bool = False,
                    force: bool = False, study: str | None = None,
                    allow_large: bool = False) -> dict:
        return _run(ingest_document, file=file, person=person, ocr_text=ocr_text,
                    doc_date=doc_date, category=category, provider=provider, ocr=ocr,
                    force=force, study=study, allow_large=allow_large)

    @server.tool(name="commit_extraction", annotations=rw)
    def commit_extraction_tool(document_id: int, records: dict) -> dict:
        return _run(commit_extraction, document_id=document_id, records=records)

    @server.tool(name="document_set_text", annotations=rw)
    def document_set_text_tool(document_id: int, text: str) -> dict:
        return _run(document_set_text, document_id=document_id, text=text)

    @server.tool(name="review_conflicts", annotations=rw)
    def review_conflicts_tool(resolve: int | None = None, keep: str = "existing",
                              signoff: str | None = None, note: str | None = None,
                              fields: dict[str, str] | None = None,
                              all: bool = False) -> object:
        return _run(review_conflicts, resolve=resolve, keep=keep, signoff=signoff,
                    note=note, fields=fields, all=all)

    return server


def main() -> None:  # pragma: no cover - stdio entry point
    build_server().run()


if __name__ == "__main__":  # pragma: no cover
    main()
