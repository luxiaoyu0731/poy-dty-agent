from __future__ import annotations

import json
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path

import pytest

from app import user_file_import
from app.settings import settings
from app.user_file_import import process_user_files


def test_empty_user_file_inbox_is_healthy_idle(tmp_path: Path) -> None:
    result = process_user_files(inbox=tmp_path / "user-files", report_dir=tmp_path / "reports", apply=False)

    assert result["status"] == "idle"
    assert result["pending_files"] == 0
    assert result["errors"] == []


def test_public_csv_is_imported_once_and_moved_after_apply(tmp_path: Path) -> None:
    original_db = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "user-files.db"))
    try:
        inbox = tmp_path / "user-files"
        inbox.mkdir()
        csv_path = inbox / "public.csv"
        csv_path.write_text(
            "observed_at,source_id,tier,dataset,series_id,instrument,product,frequency,value,unit,"
            "currency,region,period_start,period_end,quality_flag,source_url,raw_field_name,ingested_by,notes\n"
            "2026-08-30,user_upload,A,test,user_metric,,pta,daily,123.4,CNY/mt,CNY,China,,,,"
            "https://example.test/source,,operator,test row\n",
            encoding="utf-8",
        )

        result = process_user_files(inbox=inbox, report_dir=tmp_path / "reports", apply=True)

        assert result["status"] == "completed"
        assert result["imported_files"] == 1
        assert not csv_path.exists()
        assert len(list((inbox / "processed").glob("*-public.csv"))) == 1
        with closing(sqlite3.connect(tmp_path / "user-files.db")) as connection, connection:
            assert (
                connection.execute("SELECT COUNT(*) FROM market_observations WHERE source_id='user_upload'").fetchone()[
                    0
                ]
                == 1
            )

        replay = inbox / "public.csv"
        replay.write_text(
            csv_path.read_text(encoding="utf-8")
            if csv_path.exists()
            else (
                "observed_at,source_id,tier,dataset,series_id,instrument,product,frequency,value,unit,"
                "currency,region,period_start,period_end,quality_flag,source_url,raw_field_name,ingested_by,notes\n"
                "2026-08-30,user_upload,A,test,user_metric,,pta,daily,123.4,CNY/mt,CNY,China,,,,"
                "https://example.test/source,,operator,test row\n"
            ),
            encoding="utf-8",
        )
        replay_result = process_user_files(inbox=inbox, report_dir=tmp_path / "reports", apply=True)
        assert replay_result["inserted"] == 0
        assert replay_result["updated"] == 0
        assert replay_result["unchanged"] == 1
        with closing(sqlite3.connect(tmp_path / "user-files.db")) as connection, connection:
            assert (
                connection.execute("SELECT COUNT(*) FROM market_observations WHERE source_id='user_upload'").fetchone()[
                    0
                ]
                == 1
            )
    finally:
        object.__setattr__(settings, "sqlite_path", original_db)


def test_pdf_generates_review_report_without_database_write(tmp_path: Path) -> None:
    inbox = tmp_path / "user-files"
    inbox.mkdir()
    pdf = inbox / "note.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")

    result = process_user_files(inbox=inbox, report_dir=tmp_path / "reports", apply=True)

    assert result["status"] == "completed"
    assert result["writes_database"] is False
    assert result["review_files"] == 1
    report = json.loads(next((tmp_path / "reports").glob("*.json")).read_text(encoding="utf-8"))
    assert report["status"] == "review_required"
    assert len(list((inbox / "review").glob("*-note.pdf"))) == 1


def test_unsupported_file_is_rejected_without_stopping_other_files(tmp_path: Path) -> None:
    inbox = tmp_path / "user-files"
    inbox.mkdir()
    (inbox / "archive.zip").write_bytes(b"not a supported input")

    result = process_user_files(inbox=inbox, report_dir=tmp_path / "reports", apply=True)

    assert result["status"] == "degraded"
    assert result["rejected_files"] == 1
    assert "unsupported file type" in str(result["errors"])
    assert len(list((inbox / "rejected").glob("*-archive.zip"))) == 1


def test_xlsx_standard_template_is_parsed_without_external_dependency(tmp_path: Path) -> None:
    inbox = tmp_path / "user-files"
    inbox.mkdir()
    xlsx = inbox / "events.xlsx"
    _write_inline_xlsx(
        xlsx,
        [
            ["event_time", "source_id", "title", "event_type", "affected_products"],
            ["2026-08-30T10:00:00Z", "user_files", "Port delay", "logistics", "pta"],
        ],
    )

    result = process_user_files(inbox=inbox, report_dir=tmp_path / "reports", apply=False)

    assert result["status"] == "completed"
    assert result["files"][0]["kind"] == "events"
    assert result["rows"] == 1


def _write_inline_xlsx(
    path: Path,
    rows: list[list[str]],
    *,
    formula: bool = False,
    extra_names: tuple[str, ...] = (),
) -> None:
    cells = []
    for row_number, row in enumerate(rows, start=1):
        values = "".join(
            f'<c r="{chr(65 + column)}{row_number}" t="inlineStr"><is><t>{value}</t></is></c>'
            for column, value in enumerate(row)
        )
        cells.append(f'<row r="{row_number}">{values}</row>')
    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
        + "".join(cells)
        + "</sheetData></worksheet>"
    )
    if formula:
        sheet = sheet.replace("</sheetData>", '<row r="99"><c r="A99"><f>1+1</f><v>2</v></c></row></sheetData>')
    workbook = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    relationships = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/></Relationships>'
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
        for name in extra_names:
            archive.writestr(name, b"blocked")


@pytest.mark.parametrize(
    ("formula", "extra_names", "expected"),
    [
        (True, (), "formulas are not allowed"),
        (False, ("xl/vbaProject.bin",), "external links or macros are not allowed"),
        (False, ("xl/externalLinks/externalLink1.xml",), "external links or macros are not allowed"),
    ],
)
def test_unsafe_xlsx_features_are_rejected(
    tmp_path: Path,
    formula: bool,
    extra_names: tuple[str, ...],
    expected: str,
) -> None:
    inbox = tmp_path / "user-files"
    inbox.mkdir()
    _write_inline_xlsx(
        inbox / "unsafe.xlsx",
        [["event_time", "source_id", "title"], ["2026-08-30T10:00:00Z", "user_files", "Port delay"]],
        formula=formula,
        extra_names=extra_names,
    )

    result = process_user_files(inbox=inbox, report_dir=tmp_path / "reports", apply=False)

    assert result["status"] == "degraded"
    assert expected in str(result["errors"])


def test_image_is_review_only_and_symlink_is_ignored(tmp_path: Path) -> None:
    inbox = tmp_path / "user-files"
    inbox.mkdir()
    (inbox / "chart.png").write_bytes(b"not-decoded-by-automatic-import")
    target = tmp_path / "outside.csv"
    target.write_text("observed_at,source_id,value\n2026-08-30,x,1\n", encoding="utf-8")
    (inbox / "linked.csv").symlink_to(target)

    result = process_user_files(inbox=inbox, report_dir=tmp_path / "reports", apply=False)

    assert result["pending_files"] == 1
    assert result["review_files"] == 1
    assert result["writes_database"] is False


def test_oversize_file_is_rejected_before_parsing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inbox = tmp_path / "user-files"
    inbox.mkdir()
    (inbox / "large.csv").write_bytes(b"0123456789")
    monkeypatch.setattr(user_file_import, "MAX_FILE_BYTES", 5)

    result = process_user_files(inbox=inbox, report_dir=tmp_path / "reports", apply=False)

    assert "file exceeds 5 byte limit" in str(result["errors"])


@pytest.mark.parametrize(
    "bad_row",
    [
        "not-a-date,user_upload,A,test,bad_metric,,pta,daily,1,CNY/mt,CNY,China,,,,https://x.test,,,,",
        "2026-08-30,,A,test,bad_metric,,pta,daily,1,CNY/mt,CNY,China,,,,https://x.test,,,,",
    ],
)
def test_invalid_second_row_rejects_whole_file_without_writes(
    tmp_path: Path,
    bad_row: str,
) -> None:
    original_db = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "invalid-file.db"))
    header = (
        "observed_at,source_id,tier,dataset,series_id,instrument,product,frequency,value,unit,"
        "currency,region,period_start,period_end,quality_flag,source_url,raw_field_name,ingested_by,notes\n"
    )
    valid = "2026-08-30,user_upload,A,test,good_metric,,pta,daily,1,CNY/mt,CNY,China,,,,https://x.test,,,,\n"
    try:
        with closing(user_file_import.connect()) as _created_connection, _created_connection:
            pass
        inbox = tmp_path / "user-files"
        inbox.mkdir()
        (inbox / "mixed.csv").write_text(header + valid + bad_row + "\n", encoding="utf-8")

        result = process_user_files(inbox=inbox, report_dir=tmp_path / "reports", apply=True)

        assert result["rejected_files"] == 1
        with closing(sqlite3.connect(settings.sqlite_path)) as connection, connection:
            assert connection.execute("SELECT COUNT(*) FROM market_observations").fetchone()[0] == 0
    finally:
        object.__setattr__(settings, "sqlite_path", original_db)


def test_database_failure_rolls_back_every_row_in_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_db = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "rollback.db"))
    original_upsert = user_file_import._upsert_market_observation_with_connection
    calls = 0

    def fail_second(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError("simulated storage failure")
        return original_upsert(*args, **kwargs)

    monkeypatch.setattr(user_file_import, "_upsert_market_observation_with_connection", fail_second)
    try:
        inbox = tmp_path / "user-files"
        inbox.mkdir()
        (inbox / "two.csv").write_text(
            "observed_at,source_id,series_id,product,value\n"
            "2026-08-30,user_upload,one,pta,1\n"
            "2026-08-30,user_upload,two,pta,2\n",
            encoding="utf-8",
        )

        result = process_user_files(inbox=inbox, report_dir=tmp_path / "reports", apply=True)

        assert result["rejected_files"] == 1
        with closing(sqlite3.connect(settings.sqlite_path)) as connection, connection:
            assert connection.execute("SELECT COUNT(*) FROM market_observations").fetchone()[0] == 0
    finally:
        object.__setattr__(settings, "sqlite_path", original_db)


def test_duplicate_valid_file_second_run_writes_zero_rows(tmp_path: Path) -> None:
    """Production-acceptance E: second delivery of an identical file adds 0 rows."""

    original_db = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "user-files-idempotency.db"))
    try:
        inbox = tmp_path / "user-files"
        inbox.mkdir()
        csv_text = (
            "observed_at,source_id,tier,dataset,series_id,instrument,product,frequency,value,unit,"
            "currency,region,period_start,period_end,quality_flag,source_url,raw_field_name,ingested_by,notes\n"
            "2026-08-30,user_upload,A,test,acceptance_metric,,pta,daily,123.4,CNY/mt,CNY,China,,,,"
            "https://example.test/source,,operator,acceptance row\n"
        )

        (inbox / "acceptance.csv").write_text(csv_text, encoding="utf-8")
        first = process_user_files(inbox=inbox, report_dir=tmp_path / "reports-1", apply=True)
        assert first["status"] == "completed"
        assert first["imported_files"] == 1

        # Identical redelivery (new delivery of the same content) must be idempotent.
        (inbox / "acceptance-redelivered.csv").write_text(csv_text, encoding="utf-8")
        second = process_user_files(inbox=inbox, report_dir=tmp_path / "reports-2", apply=True)
        assert second["imported_files"] == 1
        assert second["rows"] == 1
        assert second["inserted"] == 0
        assert second["updated"] == 0
        assert second["unchanged"] == 1

        import sqlite3

        with closing(sqlite3.connect(settings.sqlite_path)) as connection, connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM market_observations WHERE indicator LIKE '%acceptance_metric%'"
            ).fetchone()[0]
        assert count == 1, "identical redelivery must not add a second row"
    finally:
        object.__setattr__(settings, "sqlite_path", original_db)
