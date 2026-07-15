import sqlite3

from streamlit.testing.v1 import AppTest


def test_app_bootstraps_without_exceptions():
    app = AppTest.from_file("app.py", default_timeout=20)

    app.run().run()

    assert not app.exception
    assert [title.value for title in app.title] == ["Tender Intelligence Dashboard"]
    assert [(select.label, select.options) for select in app.selectbox] == [
        ("Choose a region", ["Chandigarh", "Lucknow"]),
    ]
    buttons = {button.label: button for button in app.button}
    assert set(buttons) == {"📊 Use Chandigarh sample data", "↻ Reset website"}

    app.selectbox[0].set_value("Lucknow").run()

    assert not app.exception
    buttons = {button.label: button for button in app.button}
    assert set(buttons) == {"📊 Use Lucknow sample data", "↻ Reset website"}

    buttons["↻ Reset website"].click().run()

    buttons = {button.label: button for button in app.button}
    assert buttons["Permanently reset website"].disabled
    assert "Cancel" in buttons

    app.checkbox[0].set_value(True).run()

    reset_button = next(button for button in app.button if button.label == "Permanently reset website")
    assert not reset_button.disabled


def test_reset_confirmation_keeps_the_current_page():
    app = AppTest.from_file("app.py", default_timeout=20)
    app.run().run()
    with sqlite3.connect(app.session_state["session_db_path"]) as connection:
        connection.execute(
            """INSERT INTO tenders(
                   external_key, work_name, estimated_cost, quoted_value, bid_position, source_file,
                   bid_opening_datetime,
                   first_imported_at, last_updated_at
               ) VALUES(
                   'page-test', 'Test tender', 1000000, 900000, 'Not Available',
                   'region_cpwd_lucknow.xls', '2026-07-31 15:30:00', 'now', 'now'
               )"""
        )
    app.session_state["_tender_data_cache"] = None
    app.run()
    assert any(
        "bundled sample data for Lucknow" in info.value
        and "31 July 2026" in info.value
        for info in app.info
    )
    app.radio[0].set_value("Tender Analysis & Bid Estimator").run()
    assert [title.value for title in app.title] == ["Tender Analysis & Bid Estimator"]

    next(button for button in app.button if button.label == "↻ Reset website").click().run()
    assert app.radio[0].value == "Tender Analysis & Bid Estimator"
    assert [title.value for title in app.title] == ["Tender Analysis & Bid Estimator"]

    next(button for button in app.button if button.label == "Cancel").click().run()
    assert app.radio[0].value == "Tender Analysis & Bid Estimator"
    assert [title.value for title in app.title] == ["Tender Analysis & Bid Estimator"]
