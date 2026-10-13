# Offline spreadsheet and presentation ingestion

`commontrace ingest ./documents --type docs --dest ./store` accepts
XLSX spreadsheets and PPTX presentations alongside the existing text, PDF and
DOCX formats. Preview uses the same extraction path without writing memory.

XLSX extraction follows workbook sheet order and preserves cell addresses,
types and stored values, including hidden sheets (marked in provenance).
Shared and inline rich text retain meaningful whitespace. Formula cells use
stored cached results; missing results are explicitly marked. Formulas are
never evaluated. Numeric date serials remain numbers: display formatting,
recalculation, macros, images, charts and external data are not extracted.

PPTX extraction follows presentation order and includes slide paragraphs,
table text and speaker notes. Notes page-number, date, header and footer
placeholders are excluded. It does not render slides or OCR images. External
relationships are never fetched. Legacy and macro-enabled Office formats are
reported as unsupported rather than decoded as plain text.

Each chunk retains its package part and sheet/row or slide/paragraph identity.
Document fact submission saves this extraction metadata in the chunk provenance
journal with secret redaction; invalid metadata fails before facts are mutated.
Facts retain the existing short-summary contract. Use conversation ingestion
when the full chunk text is needed in conversation storage.

The parser uses only the Python standard library, reads regular files without
extracting ZIP members to disk, and rejects unsafe or ambiguous archive names.
Accepted archives use single-disk, ordinary ZIP directories with stored or
deflated members; unsupported ZIP64 directory layouts are rejected. Limits
are 64 MiB source bytes, 4 MiB central directory, 4,096 members, 32 MiB aggregate
XML bytes, 200,000 XML elements, depth 128, 1,024 sheets/slides and 8 MiB
extracted characters. Chunks are at most 2,000 characters. DTD/entity
declarations, package-escaping relationships and changes to an opened source
during extraction cause errors. There is no network or model-provider call.

Directory parse/read failures now appear separately from deliberate unsupported
or size skips. Preview reports errors; ingestion retains failed sources for
retry and reports a failed result even when other documents succeeded.
Acknowledged sources still skip on subsequent runs. These contracts are covered
by [Office ingestion tests](../tests/test_office_ingestion.py); they do not
establish fidelity for every Office producer or native multimodal retrieval.

Graphify reports four new static reachability candidates: the package context
manager, its central-directory guard and its XML/relationship methods. They
are exercised by the Office entry points and regression fixtures; static
analysis does not follow this context-manager dispatch. The reviewed audit
baseline increases from 303 to 304 candidates (the previous map had 300),
with 301 Python modules and no missing graph endpoints.
