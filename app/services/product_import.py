"""CSV product import. Deliberately free of aiogram imports so the parsing rules can be tested
without standing up a bot — the handler in admin/products.py only downloads the file and reports."""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from app.database.models.catalog import FulfillmentMode
from app.utils.money import parse_to_minor

MAX_ROWS = 5000
MAX_BYTES = 1_000_000
REQUIRED_COLUMNS = frozenset({"name", "price"})

_TRUE = {"yes", "true", "1", "y"}
_FALSE = {"no", "false", "0", "n"}


@dataclass(frozen=True)
class ImportRow:
    line: int
    product_id: int | None
    name: str
    category: str | None
    price_minor: int
    currency: str
    mode: FulfillmentMode
    warranty_days: int
    description: str | None
    delivery_info: str | None
    is_active: bool


@dataclass(frozen=True)
class ParseResult:
    rows: list[ImportRow]
    errors: list[str]


def _blank_to_none(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def parse_csv(text: str, *, default_currency: str = "USD") -> ParseResult:
    """Row errors are collected; header and size problems raise, because those mean the file is
    not what the admin thinks it is and applying half of it would be worse than refusing."""
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ValueError(f"File is larger than {MAX_BYTES // 1000} KB.")

    text = text.lstrip("﻿")  # Excel writes a BOM.

    sample = text[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;")
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    header = {(name or "").strip().lower() for name in (reader.fieldnames or [])}
    missing = REQUIRED_COLUMNS - header
    if missing:
        raise ValueError(f"Missing required column(s): {', '.join(sorted(missing))}.")

    rows: list[ImportRow] = []
    errors: list[str] = []

    for offset, raw in enumerate(reader):
        line = offset + 2  # header is line 1
        if offset >= MAX_ROWS:
            raise ValueError(f"File has more than {MAX_ROWS} rows.")

        cell = {(k or "").strip().lower(): (v or "") for k, v in raw.items()}

        name = cell.get("name", "").strip()
        if not name:
            errors.append(f"Line {line}: name is required.")
            continue
        if len(name) > 128:
            errors.append(f"Line {line}: name is longer than 128 characters.")
            continue

        raw_id = cell.get("id", "").strip()
        product_id: int | None = None
        if raw_id:
            if not raw_id.isdigit():
                errors.append(f'Line {line}: id "{raw_id}" is not a number.')
                continue
            product_id = int(raw_id)

        raw_price = cell.get("price", "").strip()
        try:
            price_minor = parse_to_minor(raw_price)
        except ValueError:
            errors.append(f'Line {line}: price "{raw_price}" is not a number.')
            continue
        if price_minor <= 0:
            errors.append(f'Line {line}: price "{raw_price}" must be greater than zero.')
            continue

        raw_mode = cell.get("mode", "").strip().lower() or "auto"
        if raw_mode not in ("auto", "manual"):
            errors.append(f'Line {line}: mode "{raw_mode}" must be auto or manual.')
            continue

        raw_warranty = cell.get("warranty", "").strip() or "0"
        if not raw_warranty.isdigit():
            errors.append(f'Line {line}: warranty "{raw_warranty}" must be a whole number of days.')
            continue

        raw_active = cell.get("active", "").strip().lower() or "yes"
        if raw_active not in _TRUE | _FALSE:
            errors.append(f'Line {line}: active "{raw_active}" must be yes or no.')
            continue

        rows.append(
            ImportRow(
                line=line,
                product_id=product_id,
                name=name,
                category=_blank_to_none(cell.get("category")),
                price_minor=price_minor,
                currency=(cell.get("currency", "").strip().upper() or default_currency)[:8],
                mode=FulfillmentMode.AUTO if raw_mode == "auto" else FulfillmentMode.MANUAL,
                warranty_days=int(raw_warranty),
                description=_blank_to_none(cell.get("description")),
                delivery_info=_blank_to_none(cell.get("delivery_info")),
                is_active=raw_active in _TRUE,
            )
        )

    return ParseResult(rows=rows, errors=errors)


@dataclass(frozen=True)
class ApplyReport:
    created: int
    updated: int
    errors: list[str]
    categories_created: list[str]


async def apply_rows(session, rows: list[ImportRow]) -> ApplyReport:
    """Match order: explicit id, then unique name, else create. A stale id is an error rather than
    a create — it almost always means a hand-edited or outdated export, and quietly creating a
    duplicate is harder to notice than a reported line."""
    from sqlalchemy import func, select

    from app.database.models.catalog import Category, Product
    from app.services.catalog_service import create_category, create_product

    created = updated = 0
    errors: list[str] = []
    categories_created: list[str] = []
    category_ids: dict[str, int] = {}

    async def _category_id(name: str | None) -> int | None:
        if name is None:
            return None
        key = name.lower()
        if key in category_ids:
            return category_ids[key]
        found = (
            await session.execute(select(Category).where(func.lower(Category.name) == key))
        ).scalars().first()
        if found is None:
            new_id = await create_category(
                session, name=name, emoji=None, description=None, image_file_id=None
            )
            await session.flush()
            categories_created.append(name)
            category_ids[key] = new_id
            return new_id
        category_ids[key] = found.id
        return found.id

    for row in rows:
        target: Product | None = None

        if row.product_id is not None:
            target = await session.get(Product, row.product_id)
            if target is None:
                errors.append(f"Line {row.line}: id {row.product_id} not found.")
                continue
        else:
            matches = (
                await session.execute(
                    select(Product).where(func.lower(Product.name) == row.name.lower())
                )
            ).scalars().all()
            if len(matches) > 1:
                errors.append(
                    f'Line {row.line}: name "{row.name}" matches {len(matches)} existing products.'
                )
                continue
            target = matches[0] if matches else None

        category_id = await _category_id(row.category)

        if target is None:
            await create_product(
                session,
                category_id=category_id,
                name=row.name,
                description=row.description,
                price_minor=row.price_minor,
                currency=row.currency,
                fulfillment_mode=row.mode,
                warranty_days=row.warranty_days,
                delivery_info=row.delivery_info,
                image_file_id=None,
            )
            created += 1
        else:
            target.name = row.name
            target.category_id = category_id
            target.description = row.description
            target.price_minor = row.price_minor
            target.currency = row.currency
            target.fulfillment_mode = row.mode
            target.warranty_days = row.warranty_days
            target.delivery_info = row.delivery_info
            target.is_active = row.is_active
            updated += 1

        await session.flush()

    return ApplyReport(
        created=created, updated=updated, errors=errors, categories_created=categories_created
    )


_EXPORT_COLUMNS = (
    "id", "name", "category", "price", "currency",
    "mode", "warranty", "description", "delivery_info", "active",
)


def to_csv(products, category_names: dict[int, str]) -> str:
    """Emits exactly the import schema, so export → edit → re-upload is a clean round trip."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(_EXPORT_COLUMNS)
    for p in products:
        writer.writerow(
            [
                p.id,
                p.name,
                category_names.get(p.category_id, "") if p.category_id else "",
                f"{p.price_minor / 100:.2f}",
                p.currency,
                p.fulfillment_mode.value.lower(),
                p.warranty_days,
                p.description or "",
                p.delivery_info or "",
                "yes" if p.is_active else "no",
            ]
        )
    return buffer.getvalue()
