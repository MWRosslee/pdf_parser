# pylint: disable=no-name-in-module
import os
from unittest.mock import patch
from uuid import uuid4

import pandas as pd
import pytest
from pandas.testing import assert_frame_equal
from streamlit.proto.Common_pb2 import FileURLs
from streamlit.runtime.uploaded_file_manager import UploadedFile, UploadedFileRec

from webapp.app import process_files
from webapp.helpers import (
    CURRENCY_OPTIONS,
    FORMAT_OPTIONS,
    build_export_df,
    build_transactions_from_mapping,
    create_df,
    format_export_value,
    parse_money,
)


def create_uploaded_file(file_name):
    with open(f"tests/fixtures/{file_name}", "rb") as f:
        raw_file = f.read()

    file_id = str(uuid4())

    record = UploadedFileRec(file_id=file_id, name=file_name, type="application/pdf", data=raw_file)
    upload_url = f"/_stcore/upload_file/{uuid4()}/{file_id}"
    file_urls = FileURLs(upload_url=upload_url, delete_url=upload_url)

    return UploadedFile(record, file_urls)


@pytest.fixture()
def uploaded_file():
    return create_uploaded_file("example_statement.pdf")


@pytest.fixture()
def protected_file():
    return create_uploaded_file("protected_example_statement.pdf")


def test_app(uploaded_file):
    with patch("webapp.app.get_files") as get_files:
        get_files.return_value = [uploaded_file]
        df = create_df(process_files(get_files.return_value))

    expected_df = pd.read_csv("tests/fixtures/example_statement.csv")

    df["date"] = pd.to_datetime(df["date"])
    df = df[["description", "amount", "date", "bank"]]
    expected_df["date"] = pd.to_datetime(expected_df["date"])
    assert_frame_equal(df, expected_df, check_dtype=False)


def test_unlock_protected(protected_file):
    os.environ["PDF_PASSWORDS"] = '["foobar123"]'
    with patch("webapp.app.get_files") as get_files:
        get_files.return_value = [protected_file]
        df = create_df(process_files(get_files.return_value))

    expected_df = pd.read_csv("tests/fixtures/example_statement.csv")

    df["date"] = pd.to_datetime(df["date"])
    df = df[["description", "amount", "date", "bank"]]
    expected_df["date"] = pd.to_datetime(expected_df["date"])

    assert_frame_equal(df, expected_df, check_dtype=False)


def test_parse_money():
    assert parse_money("14,030.00 *") == 14030.0
    assert parse_money("R 1,414.50") == 1414.5
    assert parse_money("6,000.00 -") == -6000.0
    assert parse_money("(123.45)") == -123.45
    assert parse_money("*") == 0.0


def test_build_transactions_from_mapped_ledger_columns():
    preview_df = pd.DataFrame(
        [
            {
                "Date": "17/02/2026",
                "Description": "Opening balance",
                "Fees": "",
                "Debits": "",
                "Credits": "",
                "Balance": "68,914.10",
            },
            {
                "Date": "17/02/2026",
                "Description": "A.P. LLOYD & SON",
                "Fees": "",
                "Debits": "",
                "Credits": "6,000.00",
                "Balance": "74,914.10",
            },
            {
                "Date": "17/02/2026",
                "Description": "Euro steel",
                "Fees": "",
                "Debits": "14,030.00 *",
                "Credits": "",
                "Balance": "60,884.10",
            },
            {
                "Date": "19/02/2026",
                "Description": "Cape Wire",
                "Fees": "",
                "Debits": "1,414.50 *",
                "Credits": "",
                "Balance": "59,466.60",
            },
            {
                "Date": "20/02/2026",
                "Description": "Credit reversal",
                "Fees": "",
                "Debits": "",
                "Credits": "500.00 -",
                "Balance": "58,966.60",
            },
        ],
    )
    mapping = {
        "reference": "",
        "date": "Date",
        "description": "Description",
        "fees": "Fees",
        "debits": "Debits",
        "credits": "Credits",
        "balance": "Balance",
    }

    transactions_df, validation_df = build_transactions_from_mapping(preview_df, mapping, "ZAR (R)")

    assert transactions_df["amount"].tolist() == [6000.0, -14030.0, -1414.5, -500.0]
    assert transactions_df["currency"].tolist() == ["ZAR (R)", "ZAR (R)", "ZAR (R)", "ZAR (R)"]
    assert validation_df["opening_balance"].tolist() == [True, False, False, False, False]
    assert validation_df["balance_valid"].dropna().tolist() == [True, True, False, True]


def test_currency_format_export_value():
    options = {
        "type": "Currency",
        "format": "1234.56",
        "currency": "ZAR (R)",
        "custom_currency": "",
        "include_currency": False,
    }

    assert format_export_value(1234.5, options) == 1234.5

    options["include_currency"] = True
    assert format_export_value(1234.5, options) == "R1234.50"


def test_export_keeps_numeric_values_without_currency_symbols_or_commas():
    df = pd.DataFrame({"amount": [1234.5]})
    settings = {
        "amount": {
            "label": "Amount",
            "type": "Currency",
            "format": "1234.56",
            "currency": "ZAR (R)",
            "custom_currency": "",
            "include_currency": False,
        },
    }

    export_df = build_export_df(df, settings, formatted_values=True)

    assert export_df["Amount"].tolist() == [1234.5]


def test_default_financial_formatting_has_no_currency_or_commas():
    assert list(CURRENCY_OPTIONS)[0] == "No currency"
    assert FORMAT_OPTIONS["Currency"][0] == "1234.56"
    assert all("," not in option for option in FORMAT_OPTIONS["Currency"])
    assert all("," not in option for option in FORMAT_OPTIONS["Number"])
