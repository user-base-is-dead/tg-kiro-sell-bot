from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.catalog import Category, Product, ProductStatus
from app.database.models.order import Order, OrderStatus
from app.database.models.user import User


@dataclass(frozen=True)
class DashboardStats:
    total_users: int
    total_products: int
    total_categories: int
    total_orders: int
    pending_orders: int
    completed_orders: int
    cancelled_orders: int
    total_revenue_minor: int
    in_stock_products: int
    out_of_stock_products: int
    orders_today: int
    revenue_today_minor: int
    new_users_today: int
    revenue_week_minor: int
    revenue_month_minor: int


async def get_dashboard_stats(session: AsyncSession) -> DashboardStats:
    now = datetime.now(UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=7)
    month_start = today_start - timedelta(days=30)

    total_users = (await session.execute(select(func.count()).select_from(User))).scalar_one()
    total_products = (await session.execute(select(func.count()).select_from(Product))).scalar_one()
    total_categories = (await session.execute(select(func.count()).select_from(Category))).scalar_one()
    total_orders = (await session.execute(select(func.count()).select_from(Order))).scalar_one()

    pending_orders = (
        await session.execute(
            select(func.count()).select_from(Order).where(Order.status.in_([OrderStatus.PENDING, OrderStatus.PROCESSING]))
        )
    ).scalar_one()
    completed_orders = (
        await session.execute(select(func.count()).select_from(Order).where(Order.status == OrderStatus.COMPLETED))
    ).scalar_one()
    cancelled_orders = (
        await session.execute(select(func.count()).select_from(Order).where(Order.status == OrderStatus.CANCELLED))
    ).scalar_one()

    total_revenue = (
        await session.execute(
            select(func.coalesce(func.sum(Order.total_minor), 0)).where(Order.status == OrderStatus.COMPLETED)
        )
    ).scalar_one()

    in_stock = (
        await session.execute(
            select(func.count()).select_from(Product).where(Product.status.in_([ProductStatus.IN_STOCK, ProductStatus.LOW_STOCK]))
        )
    ).scalar_one()
    out_of_stock = (
        await session.execute(select(func.count()).select_from(Product).where(Product.status == ProductStatus.OUT_OF_STOCK))
    ).scalar_one()

    orders_today = (
        await session.execute(select(func.count()).select_from(Order).where(Order.placed_at >= today_start))
    ).scalar_one()
    revenue_today = (
        await session.execute(
            select(func.coalesce(func.sum(Order.total_minor), 0)).where(
                Order.status == OrderStatus.COMPLETED, Order.completed_at >= today_start
            )
        )
    ).scalar_one()
    new_users_today = (
        await session.execute(select(func.count()).select_from(User).where(User.first_seen_at >= today_start))
    ).scalar_one()
    revenue_week = (
        await session.execute(
            select(func.coalesce(func.sum(Order.total_minor), 0)).where(
                Order.status == OrderStatus.COMPLETED, Order.completed_at >= week_start
            )
        )
    ).scalar_one()
    revenue_month = (
        await session.execute(
            select(func.coalesce(func.sum(Order.total_minor), 0)).where(
                Order.status == OrderStatus.COMPLETED, Order.completed_at >= month_start
            )
        )
    ).scalar_one()

    return DashboardStats(
        total_users=total_users,
        total_products=total_products,
        total_categories=total_categories,
        total_orders=total_orders,
        pending_orders=pending_orders,
        completed_orders=completed_orders,
        cancelled_orders=cancelled_orders,
        total_revenue_minor=total_revenue,
        in_stock_products=in_stock,
        out_of_stock_products=out_of_stock,
        orders_today=orders_today,
        revenue_today_minor=revenue_today,
        new_users_today=new_users_today,
        revenue_week_minor=revenue_week,
        revenue_month_minor=revenue_month,
    )
