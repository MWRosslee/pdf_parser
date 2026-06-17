# pylint: disable=unsubscriptable-object

import re
from io import BytesIO

import pandas as pd
import streamlit as st
from monopoly.banks import BankDetector, banks
from monopoly.generic import GenericBank
from monopoly.pdf import MissingOCRError, PdfDocument, PdfParser
from monopoly.pipeline import Pipeline
from monopoly.statements.base import SafetyCheckError
from pydantic import SecretStr

from webapp.models import ProcessedFile, TransactionMetadata

DEFAULT_COLUMN_ORDER = [
    "date",
    "reference",
    "description",
    "fees",
    "debits",
    "credits",
    "amount",
    "balance",
    "currency",
    "balance_valid",
    "balance_difference",
    "bank",
]
DEFAULT_COLUMN_TYPES = {
    "date": "Date",
    "reference": "Text",
    "description": "Text",
    "fees": "Currency",
    "debits": "Currency",
    "credits": "Currency",
    "amount": "Currency",
    "balance": "Currency",
    "currency": "Text",
    "balance_valid": "Text",
    "balance_difference": "Currency",
    "bank": "Text",
}

TYPE_OPTIONS = ["Text", "Number", "Currency", "Date", "Datetime"]
FORMAT_OPTIONS = {
    "Text": ["Plain text"],
    "Number": ["1234.56", "1234"],
    "Currency": ["1234.56", "1234"],
    "Date": ["YYYY-MM-DD", "DD/MM/YYYY", "MM/DD/YYYY", "Mon DD, YYYY"],
    "Datetime": ["YYYY-MM-DD HH:mm", "DD/MM/YYYY HH:mm", "MM/DD/YYYY HH:mm"],
}
CURRENCY_OPTIONS = {
    "No currency": "",
    "USD ($)": "$",
    "ZAR (R)": "R",
    "EUR (€)": "€",
    "GBP (£)": "£",
    "CAD (C$)": "C$",
    "AUD (A$)": "A$",
    "SGD (S$)": "S$",
    "CHF": "CHF",
    "JPY (¥)": "¥",
    "Custom": "",
}
STREAMLIT_FORMATS = {
    "1,234.56": "%.2f",
    "1234.56": "%.2f",
    "1,234": "%.0f",
    "1234": "%.0f",
    "YYYY-MM-DD": "YYYY-MM-DD",
    "DD/MM/YYYY": "DD/MM/YYYY",
    "MM/DD/YYYY": "MM/DD/YYYY",
    "Mon DD, YYYY": "MMM DD, YYYY",
    "YYYY-MM-DD HH:mm": "YYYY-MM-DD HH:mm",
    "DD/MM/YYYY HH:mm": "DD/MM/YYYY HH:mm",
    "MM/DD/YYYY HH:mm": "MM/DD/YYYY HH:mm",
}
EXPORT_DATE_FORMATS = {
    "YYYY-MM-DD": "%Y-%m-%d",
    "DD/MM/YYYY": "%d/%m/%Y",
    "MM/DD/YYYY": "%m/%d/%Y",
    "Mon DD, YYYY": "%b %d, %Y",
    "YYYY-MM-DD HH:mm": "%Y-%m-%d %H:%M",
    "DD/MM/YYYY HH:mm": "%d/%m/%Y %H:%M",
    "MM/DD/YYYY HH:mm": "%m/%d/%Y %H:%M",
}
TABLE_COLUMN_ALIASES = {
    "reference": ("tran", "transaction", "reference", "ref", "no"),
    "date": ("date",),
    "description": ("description", "details", "narrative", "transaction details"),
    "fees": ("fee", "fees", "charges", "charge"),
    "debits": ("debit", "debits", "withdrawal", "withdrawals", "paid out"),
    "credits": ("credit", "credits", "deposit", "deposits", "paid in"),
    "balance": ("balance", "bal"),
}
TABLE_COLUMN_LABELS = {
    "reference": "Tran list no",
    "date": "Date",
    "description": "Description",
    "fees": "Fees",
    "debits": "Debits",
    "credits": "Credits",
    "balance": "Balance",
}


def build_pipeline(document: PdfDocument, password: str | None = None) -> tuple[Pipeline, PdfParser]:
    analyzer = BankDetector(document)
    bank = analyzer.detect_bank(banks) or GenericBank
    parser = PdfParser(bank, document)
    pipeline = Pipeline(parser, passwords=[SecretStr(password)])
    return pipeline, parser


def parse_bank_statement(document: PdfDocument, password: str | None = None) -> ProcessedFile:
    try:
        pipeline, parser = build_pipeline(document, password)
    except MissingOCRError:
        st.info(f"No text found - {document.name}. Attempting to apply OCR.")
        with st.spinner(f"Adding OCR layer for {document.name}"):
            analyzer = BankDetector(document)
            bank = analyzer.detect_bank(banks) or GenericBank
            # certain PDFs have strange formats that can break the OCR,
            # so they need to be cropped before further processing
            if cropbox := bank.pdf_config.page_bbox:
                for page in document:
                    page.set_cropbox(cropbox)

            document = PdfParser.apply_ocr(document)
            pipeline, parser = build_pipeline(document, password)

    # skip initial safety check, and handle it outside the pipeline
    # so that we can raise a warning and still show transactions
    statement = pipeline.extract(safety_check=False)
    bank_name = parser.bank.__name__

    if statement.config.safety_check:
        try:
            statement.perform_safety_check()
        except SafetyCheckError:
            st.error(
                f"Safety check failed for {document.name}, transactions are incorrect or missing",
                icon="❗",
            )
    if not statement.config.safety_check:
        st.warning(
            f"{bank_name} {statement.config.statement_type} statements have no safety check, "
            "please review your transactions and proceed with caution",
            icon="⚠️",
        )

    if bank_name == "GenericBank":
        st.warning("Unrecognized bank - using generic parser", icon="⚠️")

    metadata = TransactionMetadata(bank_name)
    return ProcessedFile(pipeline.transform(statement), metadata)


def create_df(processed_files: list[ProcessedFile]) -> pd.DataFrame:
    dataframes = []
    for file in processed_files:
        df = pd.DataFrame(file)
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df["bank"] = file.metadata.bank_name

        df = df.drop(columns="polarity")
        dataframes.append(df)

    concat_df = pd.concat(dataframes)
    st.session_state["df"] = concat_df
    return concat_df


def ordered_columns(df: pd.DataFrame) -> list[str]:
    preferred = [column for column in DEFAULT_COLUMN_ORDER if column in df.columns]
    remaining = [column for column in df.columns if column not in preferred]
    return preferred + remaining


def column_label(column: str) -> str:
    return column.replace("_", " ").title()


def coerce_column(series: pd.Series, column_type: str) -> pd.Series:
    if column_type in {"Number", "Currency"}:
        return pd.to_numeric(series, errors="coerce")
    if column_type == "Date":
        return pd.to_datetime(series, errors="coerce").dt.date
    if column_type == "Datetime":
        return pd.to_datetime(series, errors="coerce")
    return series.astype("string").fillna("")


def currency_prefix(options: dict[str, str]) -> str:
    if options.get("type") != "Currency":
        return ""

    currency = options.get("currency", "")
    if currency == "Custom":
        return options.get("custom_currency", "").strip()
    return CURRENCY_OPTIONS.get(currency, "")


def number_format_parts(display_format: str) -> tuple[int, str]:
    decimals = 0 if display_format == "1234" else 2
    return decimals, ""


def format_export_value(value, options: dict[str, str]):
    column_type = options["type"]
    display_format = options["format"]

    if pd.isna(value):
        return ""

    if column_type in {"Number", "Currency"}:
        numeric_value = pd.to_numeric(value, errors="coerce")
        if pd.isna(numeric_value):
            return ""
        decimals, separator = number_format_parts(display_format)
        rounded_value = round(float(numeric_value), decimals)
        if column_type == "Currency" and options.get("include_currency"):
            prefix = currency_prefix(options)
            return f"{prefix}{rounded_value:{separator}.{decimals}f}"
        if decimals:
            return rounded_value
        return int(rounded_value)

    if column_type in {"Date", "Datetime"}:
        date_value = pd.to_datetime(value, errors="coerce")
        if pd.isna(date_value):
            return ""
        return date_value.strftime(EXPORT_DATE_FORMATS[display_format])

    return str(value)


def build_column_config(settings: dict[str, dict[str, str]]):
    config = {}
    for column, options in settings.items():
        label = options["label"]
        column_type = options["type"]
        display_format = options["format"]
        streamlit_format = STREAMLIT_FORMATS.get(display_format)

        if column_type == "Currency":
            config[column] = st.column_config.NumberColumn(
                label,
                format=f"{currency_prefix(options) if options.get('include_currency') else ''}{streamlit_format}",
            )
        elif column_type == "Number":
            config[column] = st.column_config.NumberColumn(label, format=streamlit_format)
        elif column_type == "Date":
            config[column] = st.column_config.DateColumn(label, format=streamlit_format)
        elif column_type == "Datetime":
            config[column] = st.column_config.DatetimeColumn(label, format=streamlit_format)
        else:
            config[column] = st.column_config.TextColumn(label)

    return config


def render_column_controls(df: pd.DataFrame) -> tuple[list[str], dict[str, dict[str, str]]]:
    all_columns = ordered_columns(df)

    with st.expander("Column setup", expanded=True):
        show_currency_symbols = st.toggle(
            "Show currency symbols for financial values",
            value=False,
            help="Leave off for numeric CSV/Excel values. Turn on when you want visible currency symbols in the app and formatted CSV export.",
        )
        selected_columns = st.multiselect(
            "Columns to include",
            options=all_columns,
            default=all_columns,
            format_func=column_label,
        )

        settings = {}
        for column in selected_columns:
            default_type = DEFAULT_COLUMN_TYPES.get(column, "Text")
            type_key = f"column_type_{column}"
            format_key = f"column_format_{column}"
            label_key = f"column_label_{column}"
            currency_key = f"column_currency_{column}"
            custom_currency_key = f"column_custom_currency_{column}"

            st.markdown(f"**{column_label(column)}**")
            label_col, type_col, format_col = st.columns([2, 1, 1])
            label = label_col.text_input(
                "Display name",
                value=st.session_state.get(label_key, column_label(column)),
                key=label_key,
            )
            column_type = type_col.selectbox(
                "Type",
                options=TYPE_OPTIONS,
                index=TYPE_OPTIONS.index(st.session_state.get(type_key, default_type)),
                key=type_key,
            )
            formats = FORMAT_OPTIONS[column_type]
            previous_format = st.session_state.get(format_key, formats[0])
            format_index = formats.index(previous_format) if previous_format in formats else 0
            display_format = format_col.selectbox(
                "Format",
                options=formats,
                index=format_index,
                key=format_key,
            )

            currency = ""
            custom_currency = ""
            if column_type == "Currency":
                currency_col, custom_currency_col = st.columns([1, 3])
                currency_options = list(CURRENCY_OPTIONS)
                default_currency = st.session_state.get(
                    currency_key,
                    st.session_state.get("statement_currency", "No currency"),
                )
                if default_currency not in currency_options:
                    default_currency = "No currency"
                currency = currency_col.selectbox(
                    "Currency",
                    options=currency_options,
                    index=currency_options.index(default_currency),
                    key=currency_key,
                )
                if currency == "Custom":
                    custom_currency = custom_currency_col.text_input(
                        "Custom symbol or code",
                        value=st.session_state.get(custom_currency_key, ""),
                        placeholder="e.g. R, AED, kr",
                        key=custom_currency_key,
                    )

            settings[column] = {
                "label": label or column_label(column),
                "type": column_type,
                "format": display_format,
                "currency": currency,
                "custom_currency": custom_currency,
                "include_currency": show_currency_symbols,
            }

    return selected_columns, settings


def apply_column_settings(df: pd.DataFrame, settings: dict[str, dict[str, str]]) -> pd.DataFrame:
    formatted_df = df.copy()
    for column, options in settings.items():
        formatted_df[column] = coerce_column(formatted_df[column], options["type"])
    return formatted_df


def build_export_df(df: pd.DataFrame, settings: dict[str, dict[str, str]], formatted_values: bool) -> pd.DataFrame:
    export_df = pd.DataFrame()
    for column, options in settings.items():
        label = options["label"]
        if formatted_values:
            export_df[label] = df[column].apply(
                lambda value, column_options=options: format_export_value(
                    value,
                    column_options,
                ),
            )
        else:
            export_df[label] = df[column]
    return export_df


def build_excel_bytes(df: pd.DataFrame, settings: dict[str, dict[str, str]]) -> bytes:
    output = BytesIO()
    export_df = build_export_df(df, settings, formatted_values=False)

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        export_df.to_excel(writer, index=False, sheet_name="Transactions")
        worksheet = writer.sheets["Transactions"]
        for index, (_, options) in enumerate(settings.items(), start=1):
            column_type = options["type"]
            display_format = options["format"]

            if column_type in {"Number", "Currency"}:
                decimals, _ = number_format_parts(display_format)
                number_format = "0"
                if decimals:
                    number_format += ".00"
                if column_type == "Currency" and options.get("include_currency"):
                    prefix = currency_prefix(options)
                    if prefix:
                        number_format = f'"{prefix}"{number_format}'
            elif column_type == "Date":
                number_format = EXPORT_DATE_FORMATS[display_format].replace("%Y", "yyyy").replace("%m", "mm").replace(
                    "%d",
                    "dd",
                ).replace("%b", "mmm")
            elif column_type == "Datetime":
                number_format = EXPORT_DATE_FORMATS[display_format].replace("%Y", "yyyy").replace("%m", "mm").replace(
                    "%d",
                    "dd",
                ).replace("%H", "hh").replace("%M", "mm")
            else:
                number_format = None

            if number_format:
                for cell in worksheet.iter_cols(min_col=index, max_col=index, min_row=2):
                    for row_cell in cell:
                        row_cell.number_format = number_format

            worksheet.column_dimensions[worksheet.cell(row=1, column=index).column_letter].width = max(
                14,
                min(42, len(options["label"]) + 4),
            )

    output.seek(0)
    return output.getvalue()


def normalize_header_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def parse_money(value) -> float:
    if value is None or pd.isna(value):
        return 0.0

    raw_value = str(value).strip()
    if not raw_value or raw_value in {"*", "-"}:
        return 0.0

    is_negative = (raw_value.startswith("(") and raw_value.endswith(")")) or "-" in raw_value
    cleaned = raw_value.replace("*", "")
    cleaned = re.sub(r"[^\d,.]", "", cleaned)
    if not cleaned or cleaned in {"-", ".", "-."}:
        return 0.0

    if cleaned.count(",") == 1 and "." not in cleaned:
        left, right = cleaned.split(",")
        if len(right) == 2 and len(left) <= 3:
            cleaned = f"{left}.{right}"
        else:
            cleaned = cleaned.replace(",", "")
    else:
        cleaned = cleaned.replace(",", "")

    try:
        amount = float(cleaned)
    except ValueError:
        return 0.0

    return -abs(amount) if is_negative else amount


def parse_statement_date(value):
    return pd.to_datetime(value, errors="coerce", dayfirst=True)


def infer_currency_from_columns(columns: list[str]) -> str:
    joined_columns = " ".join(columns).lower()
    if "(r)" in joined_columns or " zar" in joined_columns:
        return "ZAR (R)"
    if "$" in joined_columns or " usd" in joined_columns:
        return "USD ($)"
    if " eur" in joined_columns or "€" in joined_columns:
        return "EUR (€)"
    if " gbp" in joined_columns or "£" in joined_columns:
        return "GBP (£)"
    return "No currency"


def group_words_by_line(words: list[dict]) -> list[list[dict]]:
    rows = []
    current_row = []
    current_y = None

    for word in sorted(words, key=lambda item: (item["page"], item["y0"], item["x0"])):
        starts_new_row = current_y is None or word["page"] != current_row[-1]["page"] or abs(word["y0"] - current_y) > 3
        if starts_new_row:
            if current_row:
                rows.append(sorted(current_row, key=lambda item: item["x0"]))
            current_row = [word]
            current_y = word["y0"]
        else:
            current_row.append(word)

    if current_row:
        rows.append(sorted(current_row, key=lambda item: item["x0"]))

    return rows


def row_text(row: list[dict]) -> str:
    return " ".join(word["text"] for word in row)


def detect_header_columns(row: list[dict]) -> list[dict] | None:
    normalized_row = normalize_header_text(row_text(row))
    if "date" not in normalized_row or "description" not in normalized_row:
        return None
    if not any(term in normalized_row for term in ("debit", "credit", "balance", "fee")):
        return None

    detected = {}
    for word in row:
        normalized_word = normalize_header_text(word["text"])
        for target, aliases in TABLE_COLUMN_ALIASES.items():
            if target in detected:
                continue
            if any(alias == normalized_word or alias in normalized_word for alias in aliases):
                detected[target] = word["x0"]

    if "date" not in detected or "description" not in detected:
        return None

    if "reference" not in detected:
        detected["reference"] = 0

    return [
        {"target": target, "label": TABLE_COLUMN_LABELS[target], "x0": x0}
        for target, x0 in sorted(detected.items(), key=lambda item: item[1])
    ]


def assign_words_to_columns(row: list[dict], columns: list[dict]) -> dict[str, str]:
    values = {column["label"]: [] for column in columns}
    starts = [column["x0"] for column in columns]
    boundaries = [(starts[index] + starts[index + 1]) / 2 for index in range(len(starts) - 1)]

    for word in row:
        center = (word["x0"] + word["x1"]) / 2
        column_index = 0
        while column_index < len(boundaries) and center > boundaries[column_index]:
            column_index += 1
        values[columns[column_index]["label"]].append(word["text"])

    return {column: " ".join(parts).strip() for column, parts in values.items()}


def extract_table_preview(document: PdfDocument) -> pd.DataFrame:
    rows = []

    for page_number, page in enumerate(document, start=1):
        page_words = []
        for word in page.get_text("words"):
            x0, y0, x1, y1, text = word[:5]
            if str(text).strip():
                page_words.append(
                    {
                        "page": page_number,
                        "x0": float(x0),
                        "y0": float(y0),
                        "x1": float(x1),
                        "y1": float(y1),
                        "text": str(text).strip(),
                    },
                )

        header_columns = None
        for row in group_words_by_line(page_words):
            detected_columns = detect_header_columns(row)
            if detected_columns:
                header_columns = detected_columns
                continue

            if not header_columns:
                continue

            values = assign_words_to_columns(row, header_columns)
            if any(values.values()):
                values["source_file"] = document.name
                values["page"] = page_number
                rows.append(values)

    return pd.DataFrame(rows)


def guess_source_column(columns: list[str], target: str) -> str | None:
    aliases = TABLE_COLUMN_ALIASES[target]
    for column in columns:
        normalized_column = normalize_header_text(column)
        if any(alias in normalized_column for alias in aliases):
            return column
    return None


def render_mapping_controls(preview_df: pd.DataFrame) -> dict[str, str]:
    source_columns = [column for column in preview_df.columns if column not in {"source_file", "page"}]
    selectable_columns = [""] + source_columns
    mapping = {}

    with st.expander("Parsing column mapping", expanded=True):
        st.caption("Map the columns from the preview into transaction fields before the final table is built.")
        columns = st.columns(4)
        targets = ["reference", "date", "description", "fees", "debits", "credits", "balance"]
        for index, target in enumerate(targets):
            guessed_column = guess_source_column(source_columns, target)
            default_index = selectable_columns.index(guessed_column) if guessed_column in selectable_columns else 0
            mapping[target] = columns[index % 4].selectbox(
                TABLE_COLUMN_LABELS[target],
                options=selectable_columns,
                index=default_index,
                key=f"mapping_{target}",
            )

    return mapping


def is_opening_balance(description: str, fees: float, debits: float, credits: float) -> bool:
    normalized_description = description.lower()
    return "opening balance" in normalized_description and not any((fees, debits, credits))


def build_transactions_from_mapping(
    preview_df: pd.DataFrame,
    mapping: dict[str, str],
    currency: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    required_columns = [mapping.get("date"), mapping.get("description")]
    if not all(required_columns):
        return pd.DataFrame(), pd.DataFrame()

    validation_rows = []
    transaction_rows = []
    previous_balance = None

    for _, row in preview_df.iterrows():
        description = str(row.get(mapping["description"], "")).strip()
        date_value = row.get(mapping["date"], "")
        fees = parse_money(row.get(mapping["fees"], "")) if mapping.get("fees") else 0.0
        debits = parse_money(row.get(mapping["debits"], "")) if mapping.get("debits") else 0.0
        credits = parse_money(row.get(mapping["credits"], "")) if mapping.get("credits") else 0.0
        balance = parse_money(row.get(mapping["balance"], "")) if mapping.get("balance") else None
        reference = str(row.get(mapping["reference"], "")).strip() if mapping.get("reference") else ""

        if not description and not date_value:
            continue

        amount = credits - abs(debits) - abs(fees)
        opening_balance = is_opening_balance(description, fees, debits, credits)
        expected_balance = previous_balance + amount if previous_balance is not None and balance is not None else None
        balance_difference = balance - expected_balance if expected_balance is not None else None
        balance_valid = abs(balance_difference) <= 0.01 if balance_difference is not None else None

        validation_row = {
            "date": date_value,
            "reference": reference,
            "description": description,
            "fees": fees,
            "debits": debits,
            "credits": credits,
            "amount": amount,
            "balance": balance,
            "expected_balance": expected_balance,
            "balance_difference": balance_difference,
            "balance_valid": balance_valid,
            "opening_balance": opening_balance,
        }
        validation_rows.append(validation_row)

        if balance is not None:
            previous_balance = balance

        if opening_balance or not any((fees, debits, credits)):
            continue

        parsed_date = parse_statement_date(date_value)
        transaction_rows.append(
            {
                "date": parsed_date.date() if not pd.isna(parsed_date) else pd.NaT,
                "reference": reference,
                "description": description,
                "fees": fees,
                "debits": debits,
                "credits": credits,
                "amount": amount,
                "balance": balance,
                "currency": currency,
                "balance_valid": "Yes" if balance_valid is True else "No" if balance_valid is False else "",
                "balance_difference": balance_difference,
                "bank": "MappedTable",
            },
        )

    return pd.DataFrame(transaction_rows), pd.DataFrame(validation_rows)


def render_parsing_preview(documents: list[PdfDocument]) -> pd.DataFrame | None:
    preview_tables = []
    for document in documents:
        preview_df = extract_table_preview(document)
        if not preview_df.empty:
            preview_tables.append(preview_df)

    if not preview_tables:
        st.warning("No ledger-style table was detected. Check the PDF text quality or try another statement.")
        return None

    preview_df = pd.concat(preview_tables, ignore_index=True)
    st.markdown("### Parsing preview")
    st.dataframe(preview_df, use_container_width=True, hide_index=True)

    mapping = render_mapping_controls(preview_df)
    currency_options = list(CURRENCY_OPTIONS)
    currency = st.selectbox(
        "Statement currency",
        options=currency_options,
        index=currency_options.index("No currency"),
        key="statement_currency",
    )

    transactions_df, validation_df = build_transactions_from_mapping(preview_df, mapping, currency)
    if transactions_df.empty:
        st.info("Map at least Date and Description, plus debit, credit, or fee columns to build transactions.")
        return None

    st.markdown("### Balance validation")
    st.dataframe(validation_df, use_container_width=True, hide_index=True)

    invalid_rows = validation_df[validation_df["balance_valid"].eq(False)]
    if not invalid_rows.empty:
        st.warning(f"{len(invalid_rows)} row(s) do not reconcile against the running balance.")
    else:
        st.success("Mapped rows reconcile against the running balance.")

    st.session_state["df"] = transactions_df
    return transactions_df


def summary_currency_prefix(settings: dict[str, dict[str, str]] | None) -> str:
    if not settings or "amount" not in settings:
        return ""
    if not settings["amount"].get("include_currency"):
        return ""
    return currency_prefix(settings["amount"])


def render_summary(df: pd.DataFrame, settings: dict[str, dict[str, str]] | None = None) -> None:
    metric_columns = st.columns(4)
    metric_columns[0].metric("Transactions", f"{len(df):,}")

    if "amount" in df.columns:
        amounts = pd.to_numeric(df["amount"], errors="coerce").fillna(0)
        income = amounts[amounts > 0].sum()
        expenses = amounts[amounts < 0].sum()
        net = amounts.sum()
        prefix = summary_currency_prefix(settings)
        metric_columns[1].metric("Income", f"{prefix}{income:,.2f}")
        metric_columns[2].metric("Expenses", f"{prefix}{abs(expenses):,.2f}")
        metric_columns[3].metric("Net", f"{prefix}{net:,.2f}")
    else:
        metric_columns[1].metric("Columns", f"{len(df.columns):,}")


def render_filters(df: pd.DataFrame) -> pd.DataFrame:
    filtered_df = df.copy()

    with st.expander("Filters", expanded=False):
        search = st.text_input("Search transactions")
        if search:
            search_mask = filtered_df.astype(str).apply(
                lambda column: column.str.contains(search, case=False, na=False),
            ).any(axis=1)
            filtered_df = filtered_df[search_mask]

        if "date" in filtered_df.columns:
            date_values = pd.to_datetime(filtered_df["date"], errors="coerce").dropna()
            if not date_values.empty:
                start_date = date_values.min().date()
                end_date = date_values.max().date()
                selected_range = st.date_input("Date range", value=(start_date, end_date))
                if len(selected_range) == 2:
                    selected_start, selected_end = selected_range
                    date_mask = pd.to_datetime(filtered_df["date"], errors="coerce").dt.date.between(
                        selected_start,
                        selected_end,
                    )
                    filtered_df = filtered_df[date_mask]

        if "amount" in filtered_df.columns:
            amounts = pd.to_numeric(filtered_df["amount"], errors="coerce")
            if amounts.notna().any():
                min_amount = float(amounts.min())
                max_amount = float(amounts.max())
                selected_amount = st.slider(
                    "Amount range",
                    min_value=min_amount,
                    max_value=max_amount,
                    value=(min_amount, max_amount),
                )
                filtered_df = filtered_df[amounts.between(*selected_amount)]

    return filtered_df


def show_df(df: pd.DataFrame) -> None:
    st.markdown("### Review and export")

    selected_columns, settings = render_column_controls(df)
    if not selected_columns:
        st.info("Select at least one column to review transactions.")
        return

    working_df = df[selected_columns].copy()
    working_df = render_filters(working_df)
    working_df = apply_column_settings(working_df, settings)

    render_summary(working_df, settings)

    edited_df = st.data_editor(
        working_df,
        use_container_width=True,
        hide_index=True,
        num_rows="dynamic",
        column_config=build_column_config(settings),
        key="transaction_editor",
    )

    st.session_state["edited_df"] = edited_df

    export_formatted = st.toggle("Export formatted values", value=True)
    csv = build_export_df(edited_df, settings, formatted_values=export_formatted).to_csv(index=False).encode("utf-8")
    excel = build_excel_bytes(edited_df, settings)

    download_col1, download_col2 = st.columns(2)
    download_col1.download_button(
        label="Download CSV",
        data=csv,
        file_name="statement_sensei_transactions.csv",
        mime="text/csv",
        use_container_width=True,
    )
    download_col2.download_button(
        label="Download Excel",
        data=excel,
        file_name="statement_sensei_transactions.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )
