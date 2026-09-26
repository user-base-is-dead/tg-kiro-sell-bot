from __future__ import annotations

from app.database.models.catalog import ProductStatus

STATUS_EMOJI: dict[ProductStatus, str] = {
    ProductStatus.IN_STOCK: "🟢",
    ProductStatus.LOW_STOCK: "🟡",
    ProductStatus.ON_HOLD: "⏳",
    ProductStatus.OUT_OF_STOCK: "🔴",
    ProductStatus.COMING_SOON: "🔵",
    ProductStatus.DISABLED: "⚫",
}

STATUS_LABEL: dict[ProductStatus, str] = {
    ProductStatus.IN_STOCK: "In stock",
    ProductStatus.LOW_STOCK: "Low stock",
    ProductStatus.ON_HOLD: "Temporarily unavailable",
    ProductStatus.OUT_OF_STOCK: "Sold out",
    ProductStatus.COMING_SOON: "Coming soon",
    ProductStatus.DISABLED: "Unavailable",
}
