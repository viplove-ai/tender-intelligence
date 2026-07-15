import pytest

from src.database import connect, initialize_database
from src.importer import commit_import, commit_import_batch, prepare_import, prepare_import_batch, read_report


def test_invalid_xlsx_container_is_rejected():
    with pytest.raises(ValueError, match="valid Excel workbook"):
        read_report(b"not a zip archive", "report.xlsx")


def test_duplicate_tender_id_import(db_path, make_workbook, base_row):
    data = make_workbook([base_row])
    first = prepare_import(data, "first.xlsx", db_path=db_path)
    assert first.counts["new_tenders"] == 1
    commit_import(first, db_path, data)
    second = prepare_import(data, "first.xlsx", db_path=db_path)
    assert second.counts["unchanged_duplicates"] == 1
    commit_import(second, db_path, data)
    with connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tenders").fetchone()[0] == 1
        office = conn.execute("SELECT region,zone_circle,division,subdivision FROM tenders").fetchone()
        assert tuple(office) == ("Delhi", "CE", "EE-A", None)


def test_multi_file_preview_and_commit_is_deduplicated(db_path, make_workbook, base_row):
    first = make_workbook([dict(base_row, **{"Status": "Evaluation in progress", "Awarded Company Name": "", "Quoted Value": 0})])
    second = make_workbook([base_row])
    items = prepare_import_batch([(first, "one.xlsx"), (second, "two.xlsx")], db_path=db_path)
    assert items[0][0].counts["new_tenders"] == 1
    assert items[1][0].counts["updated_tenders"] == 1
    commit_import_batch(items, db_path)
    with connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tenders").fetchone()[0] == 1
        assert conn.execute("SELECT status FROM tenders").fetchone()[0] == "LOA Issued"


def test_status_update_and_blank_not_overwriting(db_path, make_workbook, base_row):
    evaluation = dict(base_row, **{"Status": "Opened - Evaluation in progress", "Awarded Company Name": "", "Quoted Value": 0})
    first_data = make_workbook([evaluation])
    commit_import(prepare_import(first_data, "eval.xlsx", db_path=db_path), db_path)
    awarded = dict(base_row)
    awarded["Name of Work / Subwork / Packages"] = ""
    second_data = make_workbook([awarded])
    preview = prepare_import(second_data, "award.xlsx", db_path=db_path)
    assert preview.counts["updated_tenders"] == 1
    commit_import(preview, db_path)
    with connect(db_path) as conn:
        row = conn.execute("SELECT work_name,status,quoted_value FROM tenders").fetchone()
        assert row["work_name"] == "Construction of office building"
        assert row["status"] == "LOA Issued"
        assert row["quoted_value"] == 9000000


def test_alias_merge(db_path):
    from src.database import merge_contractors, resolve_contractor, transaction
    initialize_database(db_path)
    with transaction(db_path) as conn:
        one = resolve_contractor(conn, "Manoj Kumar")
        same = resolve_contractor(conn, "  MANOJ  KUMAR ")
        two = resolve_contractor(conn, "M. Kumar")
        assert one == same
        merge_contractors(conn, two, one)
    with connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM contractors").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM contractor_aliases").fetchone()[0] == 2


def test_confirmed_delete_by_office_filter(db_path, make_workbook, base_row):
    from src.corrections import delete_tenders_by_filters
    data = make_workbook([base_row])
    commit_import(prepare_import(data, "delete.xlsx", db_path=db_path), db_path)
    assert delete_tenders_by_filters({"division": ["EE-A"]}, db_path) == 1
    with connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tenders").fetchone()[0] == 0
