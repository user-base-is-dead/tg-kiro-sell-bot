from __future__ import annotations

from app.database.models.catalog import ProductStatus

STATUS_EMOJI: dict[ProductStatus, str] = {
    ProductStatus.IN_STOCK: "🟢",
    ProductStatus.LOW_STOCK: "🟡",
    ProductStatus.OUT_OF_STOCK: "🔴",
    ProductStatus.COMING_SOON: "🔵",
    ProductStatus.DISABLED: "⚫",
}

STATUS_LABEL: dict[ProductStatus, str] = {
    ProductStatus.IN_STOCK: "IN STOCK",
    ProductStatus.LOW_STOCK: "LOW STOCK",
    ProductStatus.OUT_OF_STOCK: "OUT OF STOCK",
    ProductStatus.COMING_SOON: "COMING SOON",
    ProductStatus.DISABLED: "DISABLED",
}
