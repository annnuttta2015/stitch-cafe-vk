"""
SQLite database module.

Provides functions for users, orders and game statistics.
"""
import json
from contextlib import asynccontextmanager
from typing import Any, Protocol, cast, runtime_checkable

import aiosqlite
from loguru import logger

from data.levels import LEVELS, MAX_LEVEL, ORDERS_PER_LEVEL

DB_PATH = "data/cafe.db"


@runtime_checkable
class _UserLike(Protocol):
    """Protocol for objects with id and first_name (e.g. Telegram User)."""
    id: int
    first_name: str | None


class _FakeUser:
    """Minimal user-like object for ensure_user in fetch_user."""
    __slots__ = ("id", "first_name")

    def __init__(self, user_id: int, first_name: str | None = None) -> None:
        self.id = user_id
        self.first_name = first_name


@asynccontextmanager
async def get_db():
    """
    Context manager for database access.

    Creates a connection, runs migrations and closes it on exit.

    Yields:
        aiosqlite.Connection: Database connection

    Raises:
        aiosqlite.Error: On connection errors
    """
    db = None
    try:
        db = await aiosqlite.connect(DB_PATH)
        db.row_factory = aiosqlite.Row
        await migrate(db)
        yield db
    except aiosqlite.Error as e:
        logger.error(f"Database error: {e}")
        raise
    finally:
        if db:
            await db.close()


async def migrate(db: aiosqlite.Connection) -> None:
    """
    Run database migrations.

    Creates users table if missing and adds new columns when needed.

    Args:
        db: Database connection

    Raises:
        aiosqlite.Error: On SQL execution errors
    """
    try:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_name TEXT,
                level INTEGER DEFAULT 0,
                total_orders INTEGER DEFAULT 0,
                total_crosses INTEGER DEFAULT 0,
                has_student_done INTEGER DEFAULT 0,
                has_dirty_plate_done INTEGER DEFAULT 0,
                has_critic_done INTEGER DEFAULT 0,
                has_second_chef_done INTEGER DEFAULT 0,
                next_order_half INTEGER DEFAULT 0,
                last_order_json TEXT,
                active_order_json TEXT
            );
            """
        )
        # Migration: add new columns if missing
        migrations = [
            "ALTER TABLE users ADD COLUMN total_crosses INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN has_dirty_plate_done INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN has_critic_done INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN has_second_chef_done INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN next_order_half INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN last_order_json TEXT",
        ]
        for migration in migrations:
            try:
                await db.execute(migration)
            except aiosqlite.OperationalError:
                # Column already exists
                pass
        await db.commit()
    except aiosqlite.Error as e:
        logger.error(f"Migration error: {e}")
        raise


async def ensure_user(db: aiosqlite.Connection, from_user: _UserLike) -> None:
    """
    Create user in database if not exists.

    Args:
        db: Database connection
        from_user: Telegram user object (id, first_name)

    Raises:
        aiosqlite.Error: On SQL execution error
    """
    try:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, first_name) VALUES (?, ?)",
            (from_user.id, from_user.first_name or "Guest"),
        )
        await db.commit()
    except aiosqlite.Error as e:
        logger.error(f"Error creating user {from_user.id}: {e}")
        raise


async def fetch_user(db: aiosqlite.Connection, user_id: int, first_name: str) -> dict[str, Any]:
    """
    Fetch user data from database.

    Creates the user automatically if not found.

    Args:
        db: Database connection
        user_id: Telegram user ID
        first_name: User first name

    Returns:
        User data dict

    Raises:
        aiosqlite.Error: On SQL execution error
    """
    try:
        await ensure_user(db, _FakeUser(user_id=user_id, first_name=first_name))
        cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        if not row:
            raise ValueError(f"User {user_id} not found after create")
        return dict(row)
    except aiosqlite.Error as e:
        logger.error(f"Error fetching user {user_id}: {e}")
        raise


async def save_active_order(
    db: aiosqlite.Connection, user_id: int, dishes: list[tuple[str, int]], tag: str | None
) -> None:
    """
    Save user's active order to database.

    Args:
        db: Database connection
        user_id: Telegram user ID
        dishes: List of (dish_name, crosses) tuples
        tag: Special order tag ("critic", "student", "dirty_plate", None)

    Raises:
        aiosqlite.Error: On SQL execution error
        TypeError: On serialization error (non-JSON-serializable value)
    """
    try:
        payload = {"dishes": dishes, "tag": tag}
        await db.execute(
            "UPDATE users SET active_order_json=? WHERE user_id=?",
            (json.dumps(payload, ensure_ascii=False), user_id),
        )
        await db.commit()
    except (aiosqlite.Error, TypeError) as e:
        logger.error(f"Error saving active order for user {user_id}: {e}")
        raise


async def get_active_order(db: aiosqlite.Connection, user_id: int) -> dict[str, Any] | None:
    """
    Get user's active order from database.

    Args:
        db: Database connection
        user_id: Telegram user ID

    Returns:
        Order dict {"dishes": [...], "tag": ...} or None

    Raises:
        aiosqlite.Error: On SQL execution error
        json.JSONDecodeError: On deserialization error
    """
    try:
        cur = await db.execute("SELECT active_order_json FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        if not row or not row["active_order_json"]:
            return None
        payload = json.loads(row["active_order_json"])
        return {
            "dishes": payload.get("dishes", []),
            "tag": payload.get("tag"),
        }
    except (aiosqlite.Error, json.JSONDecodeError) as e:
        logger.error(f"Error getting active order for user {user_id}: {e}")
        raise


async def clear_active_order(db: aiosqlite.Connection, user_id: int) -> None:
    """
    Clear user's active order.

    Args:
        db: Database connection
        user_id: Telegram user ID

    Raises:
        aiosqlite.Error: On SQL execution error
    """
    try:
        await db.execute("UPDATE users SET active_order_json=NULL WHERE user_id=?", (user_id,))
        await db.commit()
    except aiosqlite.Error as e:
        logger.error(f"Error clearing active order for user {user_id}: {e}")
        raise


async def save_last_order(
    db: aiosqlite.Connection,
    user_id: int,
    dishes: list[tuple[str, int]],
    order_crosses: int,
    tag: str | None = None,
) -> None:
    """
    Save last completed order (for "dirty plate" event).

    Args:
        db: Database connection
        user_id: Telegram user ID
        dishes: List of (dish_name, crosses) tuples
        order_crosses: Total crosses in order
        tag: Special order tag (optional)

    Raises:
        aiosqlite.Error: On SQL execution error
        TypeError: On serialization error
    """
    try:
        payload = {"dishes": dishes, "crosses": order_crosses, "tag": tag}
        await db.execute(
            "UPDATE users SET last_order_json=? WHERE user_id=?",
            (json.dumps(payload, ensure_ascii=False), user_id),
        )
        await db.commit()
    except (aiosqlite.Error, TypeError) as e:
        logger.error(f"Error saving last order for user {user_id}: {e}")
        raise


async def get_last_order(db: aiosqlite.Connection, user_id: int) -> dict[str, Any] | None:
    """
    Get user's last completed order.

    Args:
        db: Database connection
        user_id: Telegram user ID

    Returns:
        Last order dict or None

    Raises:
        aiosqlite.Error: On SQL execution error
        json.JSONDecodeError: On deserialization error
    """
    try:
        cur = await db.execute("SELECT last_order_json FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        if not row or not row["last_order_json"]:
            return None
        return cast(dict[str, Any], json.loads(row["last_order_json"]))
    except (aiosqlite.Error, json.JSONDecodeError) as e:
        logger.error(f"Error getting last order for user {user_id}: {e}")
        raise


async def finish_order_and_level(
    db: aiosqlite.Connection, user_id: int, tag: str | None, order_crosses: int
) -> tuple[int, bool, str, int]:
    """
    Complete order and update user statistics.

    Increments order count, updates level if needed, saves special-event flags and last order.

    Args:
        db: Database connection
        user_id: Telegram user ID
        tag: Special order tag ("critic", "student", "dirty_plate", "second_chef", None)
        order_crosses: Crosses in the completed order

    Returns:
        Tuple (total_orders, level_changed, title, total_crosses)

    Raises:
        aiosqlite.Error: On SQL execution error
        json.JSONDecodeError: On deserialization error
    """
    try:
        cur = await db.execute(
            """SELECT total_orders, total_crosses, level, has_student_done,
                      has_dirty_plate_done, has_critic_done, has_second_chef_done,
                      active_order_json
               FROM users WHERE user_id=?""",
            (user_id,),
        )
        row = await cur.fetchone()
        if not row:
            raise ValueError(f"User {user_id} not found")

        total = (row["total_orders"] or 0) + 1
        total_crosses = (row["total_crosses"] or 0) + order_crosses
        level = row["level"] or 0
        has_student_done = row["has_student_done"] or 0
        has_dirty_plate_done = row["has_dirty_plate_done"] or 0
        has_critic_done = row["has_critic_done"] or 0
        has_second_chef_done = row["has_second_chef_done"] or 0

        # Update special event flags
        if tag == "critic":
            has_critic_done = 1
        elif tag == "student":
            has_student_done = 1
        elif tag == "dirty_plate":
            has_dirty_plate_done = 1
        elif tag == "second_chef":
            has_second_chef_done = 1

        # Save last order for dirty plate event
        active_order_data = row["active_order_json"]
        if active_order_data:
            try:
                payload = json.loads(active_order_data)
                dishes = payload.get("dishes", [])
                if dishes:
                    await save_last_order(db, user_id, dishes, order_crosses, tag)
            except (json.JSONDecodeError, Exception) as e:
                logger.warning(f"Failed to save last order for {user_id}: {e}")

        prev_level = level
        if total % ORDERS_PER_LEVEL == 0 and level < MAX_LEVEL:
            level += 1

        await db.execute(
            """UPDATE users SET total_orders=?, total_crosses=?, level=?,
                      has_student_done=?, has_dirty_plate_done=?, has_critic_done=?,
                      has_second_chef_done=?, active_order_json=NULL
               WHERE user_id=?""",
            (
                total,
                total_crosses,
                level,
                has_student_done,
                has_dirty_plate_done,
                has_critic_done,
                has_second_chef_done,
                user_id,
            ),
        )
        await db.commit()

        level_changed = level != prev_level
        title = LEVELS.get(level, f"Level {level}")

        return total, level_changed, title, total_crosses
    except (aiosqlite.Error, json.JSONDecodeError, ValueError) as e:
        logger.error(f"Error finishing order for user {user_id}: {e}")
        raise
