"""Streaming parser of the Health app's ``export.xml`` (the main file of ``export.zip``).

``ExportParser`` is a push parser: the caller feeds the file's bytes in chunks of any size
(as they arrive from an upload or are read from a zip member) and gets back the records
each chunk completed. It is built on ``xml.parsers.expat`` directly rather than on
``ElementTree.iterparse``: iterparse builds an element tree that the caller has to
``clear()`` (and the root keeps references to every cleared child), and it pulls from a
file object, while an upload arrives as an async stream of chunks; expat's push API
(``Parse(data, False)``) fits that stream as it is. Memory stays constant whatever the
size of the export: only the current element stack and the record being built are held.

Security: an export never declares entities, so any ``<!ENTITY`` declaration is refused
(this blocks billion-laughs expansion before it starts); external entities and the
external DTD subset are never read (parameter entity parsing is off, no external entity is
resolved, a DOCTYPE naming an external DTD is refused); the internal DTD subset of
``<!ELEMENT>``/``<!ATTLIST>`` declarations that real exports carry is accepted. Only the
attributes an element actually specifies are kept (no DTD default values are added).

What becomes a record (``app/apple/records.py``):

* ``<Record>``: kind ``quantity`` (``HKQuantityTypeIdentifier…``), ``category``
  (``HKCategoryTypeIdentifier…``) or ``record`` (any other type). The body is every
  attribute as received (text) plus ``metadata`` (``MetadataEntry`` children, when any),
  ``beats`` (heart rate variability ``InstantaneousBeatsPerMinute``, when any) and
  ``children`` (any other nested element, when any). Export samples are immutable, so the
  key is the body's digest: the same record imported again is the same version.
* ``<Correlation>`` (blood pressure, food): kind ``record`` with the correlation type; its
  body lists the digests of its child records under ``records``, and each child
  ``<Record>`` is also a record of its own.
* ``<Workout>``: kind ``workout`` with ``workoutActivityType`` as type; the body adds
  ``metadata``, ``events`` (``WorkoutEvent``), ``statistics`` (``WorkoutStatistics``),
  ``routes`` (the ``FileReference`` paths of ``WorkoutRoute`` elements) and ``children``
  (every other nested element, ``WorkoutRoute`` and ``WorkoutActivity`` included, kept
  whole).
* ``<ActivitySummary>``: kind ``activity-summary``, keyed by its day (a later export may
  carry a changed summary of the same day).

``<ExportDate>`` sets ``export_date``; ``<Me>`` (date of birth, sex, blood type) and the
elements this parser does not turn into records (``ClinicalRecord``, ``Audiogram``,
``VisionPrescription``, ...) are counted in ``skipped`` by tag name and otherwise ignored.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from xml.parsers import expat

from app.apple.records import (
    ACTIVITY_SUMMARY,
    CATEGORY,
    EXPORT,
    QUANTITY,
    RECORD,
    WORKOUT,
    AppleRecord,
    body_digest,
    device_name,
    identity_key,
    parse_apple_date,
)

ROOT = "HealthData"
# Elements of one record are held until it ends; a record with more nested elements than
# this is not an export record (a crafted file could otherwise grow memory without bound).
MAX_RECORD_ELEMENTS = 1_000_000
_RECORDS = ("Record", "Correlation", "Workout", "ActivitySummary")


class ExportFormatError(ValueError):
    """The bytes are not a well-formed, safe Apple Health export."""


class _Node:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag: str, attrs: dict[str, str]) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[_Node] = []


def _generic(node: _Node) -> dict[str, Any]:
    """An element kept whole: its attributes, its tag and its nested elements."""
    item: dict[str, Any] = {**node.attrs, "tag": node.tag}
    if node.children:
        item["children"] = [_generic(child) for child in node.children]
    return item


def _record_kind(record_type: str) -> str:
    if record_type.startswith("HKQuantityTypeIdentifier"):
        return QUANTITY
    if record_type.startswith("HKCategoryTypeIdentifier"):
        return CATEGORY
    return RECORD


class ExportParser:
    """Push parser over ``export.xml`` bytes (see the module docstring).

    ``feed(data)`` returns the records completed by that chunk, ``close()`` those left at
    the end and checks the document is complete. ``export_date``, ``locale``, ``counts``
    (records per kind) and ``skipped`` (elements not turned into records, by tag) fill in
    while parsing. Malformed or unsafe input raises ``ExportFormatError``.
    """

    def __init__(self) -> None:
        self.export_date: datetime | None = None
        self.locale: str | None = None
        self.counts: dict[str, int] = {}
        self.skipped: dict[str, int] = {}
        self._parser = self._create()
        self._pending: list[AppleRecord] = []
        self._depth = 0
        self._root_seen = False
        # Depth at which an ignored element started (its whole subtree is skipped).
        self._ignore_depth: int | None = None
        # The record being built: its open element stack (outermost first).
        self._stack: list[_Node] = []
        self._elements = 0
        # The open <Correlation> and the digests of its finished child records.
        self._correlation: _Node | None = None
        self._correlation_records: list[str] = []
        self._closed = False

    # -- expat setup ---------------------------------------------------------------------

    def _create(self) -> Any:
        parser = expat.ParserCreate()
        parser.buffer_text = True
        parser.specified_attributes = True
        parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
        parser.StartElementHandler = self._start
        parser.EndElementHandler = self._end
        parser.StartDoctypeDeclHandler = self._doctype
        parser.EntityDeclHandler = self._refuse_entity
        parser.UnparsedEntityDeclHandler = self._refuse_entity
        parser.ExternalEntityRefHandler = self._refuse_external
        parser.SkippedEntityHandler = self._refuse_skipped
        return parser

    @staticmethod
    def _doctype(_name: str, system_id: str | None, public_id: str | None, _internal: bool) -> None:
        if system_id or public_id:
            raise ExportFormatError("export.xml refers to an external DTD; refused")

    @staticmethod
    def _refuse_entity(*_args: object) -> None:
        raise ExportFormatError("export.xml declares an entity; refused (exports declare none)")

    @staticmethod
    def _refuse_external(*_args: object) -> int:
        raise ExportFormatError("export.xml refers to an external entity; refused")

    @staticmethod
    def _refuse_skipped(*_args: object) -> None:
        raise ExportFormatError("export.xml uses an undeclared entity; refused")

    # -- feeding -------------------------------------------------------------------------

    def feed(self, data: bytes) -> list[AppleRecord]:
        """Parse the next chunk; returns the records it completed."""
        if self._closed:
            raise ExportFormatError("export.xml parser is already closed")
        self._parse(data, False)
        return self._take()

    def close(self) -> list[AppleRecord]:
        """End of input: the records still pending; an incomplete document raises."""
        if self._closed:
            return []
        self._closed = True
        self._parse(b"", True)
        if not self._root_seen:
            raise ExportFormatError("export.xml is empty")
        return self._take()

    def _parse(self, data: bytes, final: bool) -> None:
        try:
            self._parser.Parse(data, final)
        except expat.ExpatError as error:
            message = expat.ErrorString(error.code) if error.code else str(error)
            raise ExportFormatError(
                f"export.xml is not well-formed: {message} "
                f"(line {error.lineno}, column {error.offset})"
            ) from None

    def _take(self) -> list[AppleRecord]:
        records, self._pending = self._pending, []
        return records

    # -- element handlers ----------------------------------------------------------------

    def _start(self, tag: str, attrs: dict[str, str]) -> None:
        self._depth += 1
        depth = self._depth
        if depth == 1:
            if tag != ROOT:
                raise ExportFormatError(
                    f"not an Apple Health export: root element is <{tag[:64]}>, not <{ROOT}>"
                )
            self._root_seen = True
            self.locale = attrs.get("locale")
            return
        if self._ignore_depth is not None:
            return
        if self._stack:
            self._elements += 1
            if self._elements > MAX_RECORD_ELEMENTS:
                raise ExportFormatError(
                    f"export.xml record has more than {MAX_RECORD_ELEMENTS} nested elements"
                )
            node = _Node(tag, attrs)
            if not (len(self._stack) == 1 and self._stack[0] is self._correlation):
                self._stack[-1].children.append(node)
            elif tag != "Record":
                self._stack[0].children.append(node)
            # else: a child record of the correlation, a record of its own (not attached).
            self._stack.append(node)
            return
        # A direct child of <HealthData>.
        if tag in _RECORDS:
            self._stack = [_Node(tag, attrs)]
            self._elements = 0
            if tag == "Correlation":
                self._correlation = self._stack[0]
                self._correlation_records = []
            return
        if tag == "ExportDate":
            self.export_date = parse_apple_date(attrs.get("value"))
        self.skipped[tag] = self.skipped.get(tag, 0) + 1
        self._ignore_depth = depth

    def _end(self, tag: str) -> None:
        depth = self._depth
        self._depth -= 1
        if depth == 1:
            return
        if self._ignore_depth is not None:
            if depth == self._ignore_depth:
                self._ignore_depth = None
            return
        if not self._stack:
            return
        node = self._stack.pop()
        if node is self._correlation:
            self._emit_correlation(node)
            self._correlation = None
            self._correlation_records = []
            return
        if self._stack:
            if self._stack[0] is self._correlation and len(self._stack) == 1:
                if node.tag == "Record":
                    record = self._record(node)
                    self._correlation_records.append(record.digest)
                    self._emit(record)
            return  # otherwise a nested element; its record is still open
        if node.tag == "Record":
            self._emit(self._record(node))
        elif node.tag == "Workout":
            self._emit(self._workout(node))
        elif node.tag == "ActivitySummary":
            self._emit(self._activity_summary(node))

    def _emit(self, record: AppleRecord) -> None:
        self.counts[record.kind] = self.counts.get(record.kind, 0) + 1
        self._pending.append(record)

    # -- record shapes -------------------------------------------------------------------

    @staticmethod
    def _metadata_and_rest(
        node: _Node, body: dict[str, Any], known: tuple[str, ...] = ()
    ) -> list[_Node]:
        """Fill ``body["metadata"]`` from ``MetadataEntry`` children; returns the other
        children not named in ``known``, which the caller keeps."""
        metadata: dict[str, str] = {}
        rest: list[_Node] = []
        for child in node.children:
            key = child.attrs.get("key")
            if child.tag == "MetadataEntry" and key is not None and key not in metadata:
                metadata[key] = child.attrs.get("value", "")
            elif child.tag not in known:
                rest.append(child)
        if metadata:
            body["metadata"] = metadata
        return rest

    def _common(
        self, kind: str, record_type: str, body: dict[str, Any], node: _Node
    ) -> AppleRecord:
        digest = body_digest(body)
        attrs = node.attrs
        return AppleRecord(
            channel=EXPORT,
            kind=kind,
            type=record_type,
            key=digest,
            body=body,
            start=parse_apple_date(attrs.get("startDate")),
            end=parse_apple_date(attrs.get("endDate")),
            source_name=attrs.get("sourceName"),
            device=device_name(attrs.get("device")),
            digest=digest,
        )

    def _record(self, node: _Node) -> AppleRecord:
        body: dict[str, Any] = dict(node.attrs)
        beats: list[dict[str, Any]] = []
        others: list[_Node] = []
        for child in self._metadata_and_rest(node, body):
            if child.tag == "HeartRateVariabilityMetadataList":
                for item in child.children:
                    if item.tag == "InstantaneousBeatsPerMinute":
                        beats.append(dict(item.attrs))
                    else:
                        others.append(item)
            else:
                others.append(child)
        if beats:
            body["beats"] = beats
        if others:
            body["children"] = [_generic(child) for child in others]
        record_type = node.attrs.get("type") or node.tag
        return self._common(_record_kind(record_type), record_type, body, node)

    def _emit_correlation(self, node: _Node) -> None:
        body: dict[str, Any] = dict(node.attrs)
        others = self._metadata_and_rest(node, body, known=("Record",))
        body["records"] = list(self._correlation_records)
        if others:
            body["children"] = [_generic(child) for child in others]
        self._emit(self._common(RECORD, node.attrs.get("type") or node.tag, body, node))

    def _workout(self, node: _Node) -> AppleRecord:
        body: dict[str, Any] = dict(node.attrs)
        rest = self._metadata_and_rest(node, body)
        body.setdefault("metadata", {})
        events: list[dict[str, Any]] = []
        statistics: list[dict[str, Any]] = []
        routes: list[str] = []
        others: list[_Node] = []
        for child in rest:
            if child.tag == "WorkoutEvent":
                events.append(_generic(child) if child.children else dict(child.attrs))
            elif child.tag == "WorkoutStatistics":
                statistics.append(_generic(child) if child.children else dict(child.attrs))
            else:
                if child.tag == "WorkoutRoute":
                    routes.extend(
                        item.attrs["path"]
                        for item in child.children
                        if item.tag == "FileReference" and "path" in item.attrs
                    )
                others.append(child)
        body["events"] = events
        body["statistics"] = statistics
        body["routes"] = routes
        if others:
            body["children"] = [_generic(child) for child in others]
        record_type = node.attrs.get("workoutActivityType") or node.tag
        return self._common(WORKOUT, record_type, body, node)

    @staticmethod
    def _activity_summary(node: _Node) -> AppleRecord:
        body: dict[str, Any] = dict(node.attrs)
        if node.children:
            body["children"] = [_generic(child) for child in node.children]
        return AppleRecord(
            channel=EXPORT,
            kind=ACTIVITY_SUMMARY,
            type="ActivitySummary",
            key=identity_key("ActivitySummary", node.attrs.get("dateComponents")),
            body=body,
        )
