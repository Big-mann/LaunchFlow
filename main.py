import sqlite3
import re
import hashlib
import secrets
import logging
import os
import json
import stripe
import random
import shutil
import uuid
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from jose import JWTError, jwt

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("launchflow")

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    psycopg2 = None
import cloudinary
import cloudinary.uploader

from typing import List
from urllib.parse import quote_plus

import anthropic
from dotenv import load_dotenv
from PIL import Image, ImageOps
import pillow_heif
import bcrypt as _bcrypt

from fastapi import FastAPI, Form, Request, File, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

pillow_heif.register_heif_opener()

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PUBLIC_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

JWT_SECRET = os.getenv("JWT_SECRET") or os.getenv("SECRET_KEY") or secrets.token_hex(32)
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_DAYS = 30

DATABASE_URL = os.getenv("DATABASE_URL", "").replace("postgres://", "postgresql://", 1)

CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME", "")
CLOUDINARY_API_KEY    = os.getenv("CLOUDINARY_API_KEY", "")
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET", "")

if CLOUDINARY_CLOUD_NAME:
    cloudinary.config(
        cloud_name=CLOUDINARY_CLOUD_NAME,
        api_key=CLOUDINARY_API_KEY,
        api_secret=CLOUDINARY_API_SECRET,
        secure=True,
    )

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", "LaunchFlow <noreply@launchflow.store>")

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

DB_NAME = "store.db"

UPLOAD_DIR = "static/uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

stripe.api_key = STRIPE_SECRET_KEY

PREMIUM_PRICE = 20

BASE_URL = os.getenv("BASE_URL", "https://launchflow.store")


def send_email(to: str, subject: str, html: str):
    if not SMTP_USER or not SMTP_PASS or not to:
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM
        msg["To"] = to
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, to, msg.as_string())
    except Exception as e:
        logger.error("Email send error: %s", e)


def send_order_emails(order_id: int, item_name: str, store_name: str,
                      customer_email: str, seller_email: str,
                      amount: float, quantity: int,
                      shipping_name: str, address_line1: str,
                      city: str, state: str, postal: str, country: str):

    track_url = f"{BASE_URL}/track-order/{order_id}"
    address_block = f"{shipping_name}<br>{address_line1}<br>{city}, {state} {postal}<br>{country}"

    buyer_html = f"""
    <div style="font-family:system-ui,sans-serif;max-width:560px;margin:0 auto;background:#0f172a;color:#e2e8f0;padding:32px;border-radius:12px;">
      <h1 style="color:#7c3aed;font-size:28px;margin:0 0 4px;">Order Confirmed</h1>
      <p style="color:#94a3b8;margin:0 0 24px;">Thank you for your purchase!</p>
      <table style="width:100%;border-collapse:collapse;margin-bottom:24px;">
        <tr><td style="padding:10px 0;color:#94a3b8;border-bottom:1px solid #1e293b;">Store</td><td style="padding:10px 0;text-align:right;border-bottom:1px solid #1e293b;">{store_name}</td></tr>
        <tr><td style="padding:10px 0;color:#94a3b8;border-bottom:1px solid #1e293b;">Item</td><td style="padding:10px 0;text-align:right;border-bottom:1px solid #1e293b;">{item_name}</td></tr>
        <tr><td style="padding:10px 0;color:#94a3b8;border-bottom:1px solid #1e293b;">Qty</td><td style="padding:10px 0;text-align:right;border-bottom:1px solid #1e293b;">{quantity}</td></tr>
        <tr><td style="padding:10px 0;color:#94a3b8;">Total</td><td style="padding:10px 0;text-align:right;font-weight:700;font-size:18px;color:#7c3aed;">${amount:.2f}</td></tr>
      </table>
      <p style="color:#94a3b8;margin:0 0 8px;font-size:14px;">Shipping to</p>
      <p style="margin:0 0 24px;font-size:14px;">{address_block}</p>
      <a href="{track_url}" style="display:inline-block;background:#7c3aed;color:#fff;text-decoration:none;padding:12px 24px;border-radius:8px;font-weight:600;">Track Your Order</a>
      <p style="margin:32px 0 0;font-size:12px;color:#475569;">Powered by <a href="{BASE_URL}" style="color:#7c3aed;">LaunchFlow</a></p>
    </div>
    """

    seller_html = f"""
    <div style="font-family:system-ui,sans-serif;max-width:560px;margin:0 auto;background:#0f172a;color:#e2e8f0;padding:32px;border-radius:12px;">
      <h1 style="color:#10b981;font-size:28px;margin:0 0 4px;">New Order!</h1>
      <p style="color:#94a3b8;margin:0 0 24px;">You have a new order in <strong>{store_name}</strong>.</p>
      <table style="width:100%;border-collapse:collapse;margin-bottom:24px;">
        <tr><td style="padding:10px 0;color:#94a3b8;border-bottom:1px solid #1e293b;">Item</td><td style="padding:10px 0;text-align:right;border-bottom:1px solid #1e293b;">{item_name}</td></tr>
        <tr><td style="padding:10px 0;color:#94a3b8;border-bottom:1px solid #1e293b;">Qty</td><td style="padding:10px 0;text-align:right;border-bottom:1px solid #1e293b;">{quantity}</td></tr>
        <tr><td style="padding:10px 0;color:#94a3b8;border-bottom:1px solid #1e293b;">Amount</td><td style="padding:10px 0;text-align:right;font-weight:700;color:#10b981;">${amount:.2f}</td></tr>
        <tr><td style="padding:10px 0;color:#94a3b8;">Buyer</td><td style="padding:10px 0;text-align:right;">{customer_email}</td></tr>
      </table>
      <p style="color:#94a3b8;margin:0 0 8px;font-size:14px;">Ship to</p>
      <p style="margin:0 0 24px;font-size:14px;">{address_block}</p>
      <a href="{BASE_URL}/orders" style="display:inline-block;background:#10b981;color:#fff;text-decoration:none;padding:12px 24px;border-radius:8px;font-weight:600;">Manage Orders</a>
      <p style="margin:32px 0 0;font-size:12px;color:#475569;">Powered by <a href="{BASE_URL}" style="color:#7c3aed;">LaunchFlow</a></p>
    </div>
    """

    send_email(customer_email, f"Your order from {store_name} is confirmed!", buyer_html)
    if seller_email:
        send_email(seller_email, f"New order — {item_name} × {quantity}", seller_html)


VIRAL_PRODUCTS = [
    {
        "name": "Glow Desk Lamp",
        "niche": "Room Decor",
        "angle": "A cozy desk upgrade that makes any setup look premium",
        "price": "29.99",
        "theme": "purple",

        "store_name": "LumaDesk Co.",
        "store_tagline": "Lighting and desk upgrades that elevate your setup",
        "store_description": "LumaDesk Co. creates aesthetic desk and room upgrades for people who want their setup to feel cleaner, more modern, and more premium.",

        "image_url": "https://images.unsplash.com/photo-1505693416388-ac5ce068fe85?q=80&w=1200&auto=format&fit=crop"
    },

    {
        "name": "Posture Support Cushion",
        "niche": "Fitness / Comfort",
        "angle": "A simple comfort product for people who sit all day",
        "price": "39.99",
        "theme": "blue",

        "store_name": "SitWell Studio",
        "store_tagline": "Comfort-focused upgrades for everyday life",
        "store_description": "SitWell Studio focuses on comfort and posture products for people who work, study, game, or sit for long periods throughout the day.",

        "image_url": "https://images.unsplash.com/photo-1519947486511-46149fa0a254?q=80&w=1200&auto=format&fit=crop"
    },

    {
        "name": "Mini Travel Blender",
        "niche": "Health",
        "angle": "Portable smoothies for busy people who want to stay healthy",
        "price": "34.99",
        "theme": "green",

        "store_name": "BlendGo",
        "store_tagline": "Healthy routines built for busy lifestyles",
        "store_description": "BlendGo creates simple health and smoothie products designed for people who want healthier habits without slowing down their schedule.",

        "image_url": "https://images.unsplash.com/photo-1622484212850-eb596d769edc?q=80&w=1200&auto=format&fit=crop"
    },

    {
        "name": "Grip Strength Trainer",
        "niche": "Gym",
        "angle": "A small fitness product that is easy to demonstrate in videos",
        "price": "19.99",
        "theme": "orange",

        "store_name": "GripForge",
        "store_tagline": "Strength tools for athletes and lifters",
        "store_description": "GripForge builds simple strength and recovery tools for lifters, athletes, climbers, and people focused on performance.",

        "image_url": "https://images.unsplash.com/photo-1517836357463-d25dfeac3438?q=80&w=1200&auto=format&fit=crop"
    }
]



# -----------------------------
# DATABASE
# -----------------------------

class _PGCursor:
    """Wraps psycopg2 RealDictCursor — converts ? placeholders to %s automatically."""
    def __init__(self, cur):
        self._cur = cur

    def execute(self, query, params=None):
        self._cur.execute(query.replace("?", "%s"), params)
        return self

    def executemany(self, query, params_list):
        self._cur.executemany(query.replace("?", "%s"), params_list)
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    @property
    def lastrowid(self):
        self._cur.execute("SELECT lastval()")
        row = self._cur.fetchone()
        return row["lastval"] if row else None

    @property
    def rowcount(self):
        return self._cur.rowcount


class _PGConn:
    """Wraps psycopg2 connection to mimic sqlite3 interface."""
    def __init__(self, raw):
        self._raw = raw

    def cursor(self):
        return _PGCursor(self._raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor))

    def commit(self):
        self._raw.commit()

    def close(self):
        self._raw.close()

    def execute(self, query, params=None):
        cur = self.cursor()
        cur.execute(query, params)
        return cur


def db():
    if DATABASE_URL:
        return _PGConn(psycopg2.connect(DATABASE_URL))
    # Local fallback — SQLite
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def add_column_if_missing(cur, table, column, definition):
    if DATABASE_URL:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}")
    else:
        cur.execute(f"PRAGMA table_info({table})")
        columns = [row["name"] for row in cur.fetchall()]
        if column not in columns:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


# -----------------------------
# IMAGE UPLOAD
# -----------------------------

def upload_image(file_path: str) -> str:
    """Upload to Cloudinary when configured, otherwise serve from local static."""
    if CLOUDINARY_CLOUD_NAME:
        try:
            result = cloudinary.uploader.upload(
                file_path,
                folder="launchflow",
                resource_type="image",
            )
            return result["secure_url"]
        except Exception as e:
            logger.error("Cloudinary upload error: %s", e)
    return f"/static/uploads/{os.path.basename(file_path)}"


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        email TEXT UNIQUE,
        password TEXT,
        is_pro INTEGER DEFAULT 0,
        store_name TEXT DEFAULT 'My Store',
        stripe_account_id TEXT DEFAULT '',
        stripe_onboarding_complete INTEGER DEFAULT 0,
        ai_uses INTEGER DEFAULT 0
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS products (
        id SERIAL PRIMARY KEY,
        user_id INTEGER,
        name TEXT,
        description TEXT,
        price REAL,
        stock INTEGER,
        image_url TEXT,
        slug TEXT UNIQUE,
        theme TEXT DEFAULT 'blue',
        views INTEGER DEFAULT 0,
        tagline TEXT DEFAULT '',
        cta TEXT DEFAULT 'Buy Now',
        source TEXT DEFAULT 'manual',
        ai_design TEXT DEFAULT '{}',
        brand_json TEXT DEFAULT '{}',
        store_layout TEXT DEFAULT 'default',
        published INTEGER DEFAULT 0,
        page_title TEXT DEFAULT '',
        page_subtitle TEXT DEFAULT '',
        page_content TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS store_pages (
        id SERIAL PRIMARY KEY,
        store_id INTEGER,
        user_id INTEGER,
        title TEXT,
        slug TEXT,
        content TEXT DEFAULT '',
        page_type TEXT DEFAULT 'custom',
        published INTEGER DEFAULT 1,
        sort_order INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS store_sections (
        id SERIAL PRIMARY KEY,
        store_id INTEGER,
        page_id INTEGER DEFAULT 0,
        section_type TEXT,
        section_title TEXT DEFAULT '',
        section_data TEXT DEFAULT '{}',
        sort_order INTEGER DEFAULT 0,
        visible INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS store_items (
        id SERIAL PRIMARY KEY,
        store_id INTEGER,
        user_id INTEGER,
        name TEXT,
        description TEXT,
        price REAL,
        stock INTEGER,
        image_url TEXT,
        image_urls TEXT DEFAULT '[]',
        slug TEXT DEFAULT '',
        featured INTEGER DEFAULT 0,
        views INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS cart_items (
        id SERIAL PRIMARY KEY,
        session_id TEXT DEFAULT '',
        user_id INTEGER DEFAULT 0,
        store_item_id INTEGER,
        quantity INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS store_likes (
        id SERIAL PRIMARY KEY,
        store_id INTEGER,
        user_id INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS store_followers (
        id SERIAL PRIMARY KEY,
        store_id INTEGER,
        user_id INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS store_themes (
        id SERIAL PRIMARY KEY,
        creator_id INTEGER,
        name TEXT,
        config_json TEXT DEFAULT '{}',
        preview_image TEXT DEFAULT '',
        public INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        id SERIAL PRIMARY KEY,
        product_id INTEGER,
        store_item_id INTEGER,
        amount REAL,
        customer_email TEXT,
        customer_name TEXT DEFAULT '',
        quantity INTEGER DEFAULT 1,
        shipping_name TEXT DEFAULT '',
        shipping_address_line1 TEXT DEFAULT '',
        shipping_address_line2 TEXT DEFAULT '',
        shipping_city TEXT DEFAULT '',
        shipping_state TEXT DEFAULT '',
        shipping_postal_code TEXT DEFAULT '',
        shipping_country TEXT DEFAULT '',
        stripe_session_id TEXT DEFAULT '',
        payment_status TEXT DEFAULT '',
        tracking_number TEXT DEFAULT '',
        shipping_carrier TEXT DEFAULT '',
        shipping_status TEXT DEFAULT 'Not shipped yet',
        fulfillment_status TEXT DEFAULT 'New order',
        buyer_message TEXT DEFAULT '',
        seller_notes TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS conversations (
        id SERIAL PRIMARY KEY,
        buyer_email TEXT,
        seller_id INTEGER,
        store_id INTEGER,
        order_id INTEGER,
        subject TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id SERIAL PRIMARY KEY,
        conversation_id INTEGER,
        sender_type TEXT,
        sender_user_id INTEGER DEFAULT 0,
        sender_email TEXT DEFAULT '',
        message TEXT,
        read_at TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    add_column_if_missing(cur, "users", "password", "TEXT")
    add_column_if_missing(cur, "users", "store_name", "TEXT DEFAULT 'My Store'")
    add_column_if_missing(cur, "users", "stripe_account_id", "TEXT DEFAULT ''")
    add_column_if_missing(cur, "users", "stripe_onboarding_complete", "INTEGER DEFAULT 0")
    add_column_if_missing(cur, "users", "ai_uses", "INTEGER DEFAULT 0")
    add_column_if_missing(cur, "users", "reset_token", "TEXT DEFAULT ''")
    add_column_if_missing(cur, "users", "reset_token_expires", "INTEGER DEFAULT 0")

    add_column_if_missing(cur, "products", "slug", "TEXT")
    add_column_if_missing(cur, "products", "theme", "TEXT DEFAULT 'blue'")
    add_column_if_missing(cur, "products", "views", "INTEGER DEFAULT 0")
    add_column_if_missing(cur, "products", "tagline", "TEXT DEFAULT ''")
    add_column_if_missing(cur, "products", "cta", "TEXT DEFAULT 'Buy Now'")
    add_column_if_missing(cur, "products", "source", "TEXT DEFAULT 'manual'")
    add_column_if_missing(cur, "products", "ai_design", "TEXT DEFAULT '{}'")
    add_column_if_missing(cur, "products", "brand_json", "TEXT DEFAULT '{}'")
    add_column_if_missing(cur, "products", "store_layout", "TEXT DEFAULT 'default'")
    add_column_if_missing(cur, "products", "published", "INTEGER DEFAULT 0")
    add_column_if_missing(cur, "products", "page_title", "TEXT DEFAULT ''")
    add_column_if_missing(cur, "products", "page_subtitle", "TEXT DEFAULT ''")
    add_column_if_missing(cur, "products", "page_content", "TEXT DEFAULT ''")
    add_column_if_missing(cur, "products", "shipping_type", "TEXT DEFAULT 'free'")
    add_column_if_missing(cur, "products", "shipping_rate", "REAL DEFAULT 0")
    add_column_if_missing(cur, "products", "shipping_info", "TEXT DEFAULT ''")

    add_column_if_missing(cur, "store_pages", "store_id", "INTEGER")
    add_column_if_missing(cur, "store_pages", "user_id", "INTEGER")
    add_column_if_missing(cur, "store_pages", "title", "TEXT")
    add_column_if_missing(cur, "store_pages", "slug", "TEXT")
    add_column_if_missing(cur, "store_pages", "content", "TEXT DEFAULT ''")
    add_column_if_missing(cur, "store_pages", "page_type", "TEXT DEFAULT 'custom'")
    add_column_if_missing(cur, "store_pages", "published", "INTEGER DEFAULT 1")
    add_column_if_missing(cur, "store_pages", "sort_order", "INTEGER DEFAULT 0")

    add_column_if_missing(cur, "store_sections", "store_id", "INTEGER")
    add_column_if_missing(cur, "store_sections", "page_id", "INTEGER DEFAULT 0")
    add_column_if_missing(cur, "store_sections", "section_type", "TEXT")
    add_column_if_missing(cur, "store_sections", "section_title", "TEXT DEFAULT ''")
    add_column_if_missing(cur, "store_sections", "section_data", "TEXT DEFAULT '{}'")
    add_column_if_missing(cur, "store_sections", "sort_order", "INTEGER DEFAULT 0")
    add_column_if_missing(cur, "store_sections", "visible", "INTEGER DEFAULT 1")

    add_column_if_missing(cur, "store_items", "image_urls", "TEXT DEFAULT '[]'")
    add_column_if_missing(cur, "store_items", "slug", "TEXT DEFAULT ''")
    add_column_if_missing(cur, "store_items", "featured", "INTEGER DEFAULT 0")
    add_column_if_missing(cur, "store_items", "views", "INTEGER DEFAULT 0")
    add_column_if_missing(cur, "store_items", "sort_order", "INTEGER DEFAULT 0")

    add_column_if_missing(cur, "cart_items", "session_id", "TEXT DEFAULT ''")
    add_column_if_missing(cur, "cart_items", "user_id", "INTEGER DEFAULT 0")
    add_column_if_missing(cur, "cart_items", "store_item_id", "INTEGER")
    add_column_if_missing(cur, "cart_items", "quantity", "INTEGER DEFAULT 1")

    add_column_if_missing(cur, "store_likes", "store_id", "INTEGER")
    add_column_if_missing(cur, "store_likes", "user_id", "INTEGER")

    add_column_if_missing(cur, "store_followers", "store_id", "INTEGER")
    add_column_if_missing(cur, "store_followers", "user_id", "INTEGER")

    add_column_if_missing(cur, "store_themes", "creator_id", "INTEGER")
    add_column_if_missing(cur, "store_themes", "name", "TEXT")
    add_column_if_missing(cur, "store_themes", "config_json", "TEXT DEFAULT '{}'")
    add_column_if_missing(cur, "store_themes", "preview_image", "TEXT DEFAULT ''")
    add_column_if_missing(cur, "store_themes", "public", "INTEGER DEFAULT 1")

    add_column_if_missing(cur, "orders", "store_item_id", "INTEGER")
    add_column_if_missing(cur, "orders", "stripe_session_id", "TEXT DEFAULT ''")
    add_column_if_missing(cur, "orders", "payment_status", "TEXT DEFAULT ''")
    add_column_if_missing(cur, "orders", "tracking_number", "TEXT DEFAULT ''")
    add_column_if_missing(cur, "orders", "shipping_carrier", "TEXT DEFAULT ''")
    add_column_if_missing(cur, "orders", "shipping_status", "TEXT DEFAULT 'Not shipped yet'")
    add_column_if_missing(cur, "orders", "customer_name", "TEXT DEFAULT ''")
    add_column_if_missing(cur, "orders", "quantity", "INTEGER DEFAULT 1")
    add_column_if_missing(cur, "orders", "shipping_name", "TEXT DEFAULT ''")
    add_column_if_missing(cur, "orders", "shipping_address_line1", "TEXT DEFAULT ''")
    add_column_if_missing(cur, "orders", "shipping_address_line2", "TEXT DEFAULT ''")
    add_column_if_missing(cur, "orders", "shipping_city", "TEXT DEFAULT ''")
    add_column_if_missing(cur, "orders", "shipping_state", "TEXT DEFAULT ''")
    add_column_if_missing(cur, "orders", "shipping_postal_code", "TEXT DEFAULT ''")
    add_column_if_missing(cur, "orders", "shipping_country", "TEXT DEFAULT ''")
    add_column_if_missing(cur, "orders", "fulfillment_status", "TEXT DEFAULT 'New order'")
    add_column_if_missing(cur, "orders", "buyer_message", "TEXT DEFAULT ''")
    add_column_if_missing(cur, "orders", "seller_notes", "TEXT DEFAULT ''")

    add_column_if_missing(cur, "messages", "read_at", "TEXT DEFAULT ''")

    add_column_if_missing(cur, "store_items", "cost_price", "REAL DEFAULT 0")
    add_column_if_missing(cur, "store_items", "supplier_url", "TEXT DEFAULT ''")
    add_column_if_missing(cur, "store_items", "supplier_name", "TEXT DEFAULT ''")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS suppliers (
        id SERIAL PRIMARY KEY,
        user_id INTEGER,
        name TEXT,
        url TEXT DEFAULT '',
        contact_email TEXT DEFAULT '',
        notes TEXT DEFAULT '',
        shipping_days INTEGER DEFAULT 7,
        is_active INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS discount_codes (
        id SERIAL PRIMARY KEY,
        user_id INTEGER,
        code TEXT,
        discount_type TEXT DEFAULT 'percentage',
        value REAL DEFAULT 10,
        max_uses INTEGER DEFAULT 0,
        uses_count INTEGER DEFAULT 0,
        expires_at TEXT DEFAULT '',
        is_active INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.executescript("""
    CREATE INDEX IF NOT EXISTS idx_products_user_id ON products(user_id);
    CREATE INDEX IF NOT EXISTS idx_products_slug ON products(slug);
    CREATE INDEX IF NOT EXISTS idx_store_items_store_id ON store_items(store_id);
    CREATE INDEX IF NOT EXISTS idx_orders_product_id ON orders(product_id);
    CREATE INDEX IF NOT EXISTS idx_cart_items_session_id ON cart_items(session_id);
    CREATE INDEX IF NOT EXISTS idx_cart_items_user_id ON cart_items(user_id);
    """)

    conn.commit()
    conn.close()


init_db()


@app.get("/chat/inbox")
def chat_inbox(request: Request):
    user = require_user(request)

    if not user:
        return {"ok": False, "unread": 0, "conversations": []}

    conn = db()
    cur = conn.cursor()

    cur.execute("""
    SELECT
        conversations.id,
        conversations.buyer_email,
        conversations.seller_id,
        conversations.store_id,
        conversations.created_at,
        products.name AS store_name,
        users.store_name AS seller_name,

        (
            SELECT message
            FROM messages
            WHERE messages.conversation_id = conversations.id
            ORDER BY messages.id DESC
            LIMIT 1
        ) AS last_message,

        (
            SELECT created_at
            FROM messages
            WHERE messages.conversation_id = conversations.id
            ORDER BY messages.id DESC
            LIMIT 1
        ) AS last_message_time,

        (
            SELECT COUNT(*)
            FROM messages
            WHERE messages.conversation_id = conversations.id
            AND messages.sender_user_id != ?
            AND COALESCE(messages.read_at, '') = ''
        ) AS unread_count

    FROM conversations
    JOIN products ON conversations.store_id = products.id
    JOIN users ON conversations.seller_id = users.id
    WHERE conversations.seller_id = ?
       OR conversations.buyer_email = ?
    ORDER BY COALESCE(last_message_time, conversations.created_at) DESC
    """, (user["id"], user["id"], user["email"]))

    rows = cur.fetchall()
    conn.close()

    conversations_data = []
    total_unread = 0

    for row in rows:
        unread_count = row["unread_count"] or 0
        total_unread += unread_count

        conversations_data.append({
            "id": row["id"],
            "store_id": row["store_id"],
            "store_name": row["store_name"],
            "seller_name": row["seller_name"],
            "seller_id": row["seller_id"],
            "buyer_email": row["buyer_email"],
            "title": row["store_name"],
            "last_message": row["last_message"] or "No messages yet",
            "last_message_time": row["last_message_time"] or row["created_at"],
            "unread_count": unread_count
        })

    return {
        "ok": True,
        "unread": total_unread,
        "conversations": conversations_data
    }


@app.post("/chat/start")
async def chat_start(request: Request):
    user = require_user(request)

    if not user:
        return {"ok": False, "error": "Not logged in"}

    data = await request.json()

    seller_id = int(data.get("seller_id", 0))
    store_id = int(data.get("store_id", 0))

    if seller_id <= 0 or store_id <= 0:
        return {"ok": False, "error": "Missing seller or store"}

    if seller_id == user["id"]:
        return {"ok": False, "error": "You cannot message yourself"}

    conn = db()
    cur = conn.cursor()

    cur.execute("""
    SELECT *
    FROM products
    WHERE id = ?
    AND user_id = ?
    """, (store_id, seller_id))

    store = cur.fetchone()

    if not store:
        conn.close()
        return {"ok": False, "error": "Store not found"}

    cur.execute("""
    SELECT *
    FROM conversations
    WHERE buyer_email = ?
    AND seller_id = ?
    AND store_id = ?
    """, (user["email"], seller_id, store_id))

    convo = cur.fetchone()

    if convo:
        conversation_id = convo["id"]
    else:
        cur.execute("""
        INSERT INTO conversations (
            buyer_email,
            seller_id,
            store_id,
            subject
        )
        VALUES (?, ?, ?, ?)
        """, (
            user["email"],
            seller_id,
            store_id,
            store["name"]
        ))

        conn.commit()
        conversation_id = cur.lastrowid

    conn.close()

    return {
        "ok": True,
        "conversation_id": conversation_id
    }


@app.get("/chat/messages/{conversation_id}")
def chat_messages(request: Request, conversation_id: int):
    user = require_user(request)

    if not user:
        return {"ok": False, "messages": []}

    conn = db()
    cur = conn.cursor()

    cur.execute("""
    SELECT *
    FROM conversations
    WHERE id = ?
    AND (
        seller_id = ?
        OR buyer_email = ?
    )
    """, (conversation_id, user["id"], user["email"]))

    convo = cur.fetchone()

    if not convo:
        conn.close()
        return {"ok": False, "messages": []}

    cur.execute("""
    UPDATE messages
    SET read_at = CURRENT_TIMESTAMP
    WHERE conversation_id = ?
    AND sender_user_id != ?
    AND COALESCE(read_at, '') = ''
    """, (conversation_id, user["id"]))

    conn.commit()

    cur.execute("""
    SELECT *
    FROM messages
    WHERE conversation_id = ?
    ORDER BY id ASC
    """, (conversation_id,))

    rows = cur.fetchall()
    conn.close()

    messages_data = []

    for row in rows:
        mine = row["sender_user_id"] == user["id"]
        read_at = row["read_at"] if "read_at" in row.keys() else ""

        messages_data.append({
            "id": row["id"],
            "conversation_id": row["conversation_id"],
            "sender_type": row["sender_type"],
            "sender_user_id": row["sender_user_id"],
            "sender_email": row["sender_email"],
            "message": row["message"],
            "created_at": row["created_at"],
            "read_at": read_at,
            "mine": mine,
            "seen": bool(mine and read_at)
        })

    return {
        "ok": True,
        "conversation_id": conversation_id,
        "messages": messages_data
    }


@app.post("/chat/send")
async def chat_send(request: Request):
    user = require_user(request)

    if not user:
        return {"ok": False, "error": "Not logged in"}

    data = await request.json()

    conversation_id = int(data.get("conversation_id", 0))
    message = str(data.get("message", "")).strip()

    if len(message) > 5000:
        return {"ok": False, "error": "Message is too long"}

    if conversation_id <= 0:
        return {"ok": False, "error": "Missing conversation"}

    if not message:
        return {"ok": False, "error": "Empty message"}

    conn = db()
    cur = conn.cursor()

    cur.execute("""
    SELECT *
    FROM conversations
    WHERE id = ?
    AND (
        seller_id = ?
        OR buyer_email = ?
    )
    """, (conversation_id, user["id"], user["email"]))

    convo = cur.fetchone()

    if not convo:
        conn.close()
        return {"ok": False, "error": "Conversation not found"}

    sender_type = "seller" if user["id"] == convo["seller_id"] else "buyer"

    cur.execute("""
    INSERT INTO messages (
        conversation_id,
        sender_type,
        sender_user_id,
        sender_email,
        message
    )
    VALUES (?, ?, ?, ?, ?)
    """, (
        conversation_id,
        sender_type,
        user["id"],
        user["email"],
        message
    ))

    conn.commit()
    message_id = cur.lastrowid
    conn.close()

    return {
        "ok": True,
        "message": {
            "id": message_id,
            "conversation_id": conversation_id,
            "sender_type": sender_type,
            "sender_user_id": user["id"],
            "sender_email": user["email"],
            "message": message,
            "created_at": "Just now",
            "mine": True
        }
    }


@app.get("/chat/unread-count")
def chat_unread_count(request: Request):
    user = require_user(request)

    if not user:
        return {"ok": False, "unread": 0}

    conn = db()
    cur = conn.cursor()

    cur.execute("""
    SELECT COUNT(*) AS count
    FROM messages
    JOIN conversations ON messages.conversation_id = conversations.id
    WHERE (
        conversations.seller_id = ?
        OR conversations.buyer_email = ?
    )
    AND messages.sender_user_id != ?
    AND COALESCE(messages.read_at, '') = ''
    """, (user["id"], user["email"], user["id"]))

    count = cur.fetchone()["count"]
    conn.close()

    return {
        "ok": True,
        "unread": count or 0
    }



# -----------------------------
# HELPERS
# -----------------------------
def hash_password(password: str) -> str:
    return _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")


def _sha256_hash(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def verify_password(password: str, hashed_password: str) -> bool:
    if not hashed_password:
        return False
    # Legacy SHA256 hash is exactly 64 hex chars
    if len(hashed_password) == 64:
        return _sha256_hash(password) == hashed_password
    try:
        return _bcrypt.checkpw(password.encode("utf-8"), hashed_password.encode("utf-8"))
    except Exception:
        return False


def slugify(text):
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    return text or "store"


def _hex_to_rgb(hex_color: str):
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except Exception:
        return 124, 58, 237


def generate_store_css(ai_design: dict) -> str:
    """Return Google Fonts link + a <style> block with fully unique per-store CSS."""
    accent    = ai_design.get("accent_color", "#7c3aed")
    secondary = ai_design.get("secondary_color", "#06b6d4")
    template  = ai_design.get("template_type", "editorial")
    button_style = ai_design.get("button_style", "rounded")
    design_style = ai_design.get("design_style", "glass")

    ar, ag, ab = _hex_to_rgb(accent)
    sr, sg, sb = _hex_to_rgb(secondary)

    # ── Google Fonts per template ────────────────────────────────────
    FONT_MAP = {
        "luxury":     ("'Playfair Display', Georgia, serif",
                       "family=Playfair+Display:ital,wght@0,400;0,700;0,900;1,400"),
        "editorial":  ("'Merriweather', Georgia, serif",
                       "family=Merriweather:wght@300;400;700;900"),
        "streetwear": ("'Bebas Neue', Impact, sans-serif",
                       "family=Bebas+Neue&family=Inter:wght@400;700;900"),
        "tech":       ("'Space Grotesk', system-ui, sans-serif",
                       "family=Space+Grotesk:wght@300;400;500;600;700"),
        "beauty":     ("'Cormorant+Garamond', Georgia, serif",
                       "family=Cormorant+Garamond:ital,wght@0,300;0,400;0,600;1,300;1,400"),
        "garage":     ("'Barlow Condensed', Impact, sans-serif",
                       "family=Barlow+Condensed:wght@400;600;700;800;900"),
        "wellness":   ("'Nunito', system-ui, sans-serif",
                       "family=Nunito:wght@300;400;600;700;800"),
        "food":       ("'Libre Baskerville', Georgia, serif",
                       "family=Libre+Baskerville:ital,wght@0,400;0,700;1,400"),
        "art":        ("'DM Serif Display', Georgia, serif",
                       "family=DM+Serif+Display:ital@0;1"),
        "sports":     ("'Oswald', Impact, sans-serif",
                       "family=Oswald:wght@400;500;600;700"),
    }
    heading_font, gfont_query = FONT_MAP.get(template, ("inherit", ""))
    font_tag = (
        f'<link rel="preconnect" href="https://fonts.googleapis.com">'
        f'<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        f'<link href="https://fonts.googleapis.com/css2?{gfont_query}&display=swap" rel="stylesheet">'
    ) if gfont_query else ""

    # ── Card & button radius per template ────────────────────────────
    card_radius = {
        "streetwear": "4px",  "luxury": "0px",    "beauty": "32px",
        "wellness":   "36px", "art":    "0px",     "sports": "6px",
        "food":       "20px", "tech":   "12px",    "garage": "10px",
    }.get(template, "26px")

    btn_radius = {
        "streetwear": "0px",  "luxury": "0px",    "beauty": "999px",
        "wellness":   "999px","art":    "0px",     "sports": "4px",
        "food":       "10px", "tech":   "8px",     "garage": "6px",
        "pill":       "999px","rounded":"16px",    "sharp":  "6px",
    }.get(button_style if button_style in ("pill","rounded","sharp","glow","luxury") else template, "16px")

    btn_glow = f"box-shadow: 0 0 32px rgba({ar},{ag},{ab},0.55);" if button_style == "glow" else ""

    # ── Per-template backgrounds ─────────────────────────────────────
    if template == "beauty":
        body_bg     = (f"radial-gradient(circle at 80% 10%, rgba({ar},{ag},{ab},0.45), transparent 28%),"
                       f"radial-gradient(circle at 10% 80%, rgba({sr},{sg},{sb},0.35), transparent 32%),"
                       f"linear-gradient(135deg, #4a102a, rgba(249,168,212,0.85))")
        hero_bg     = "rgba(255,255,255,0.22)"; hero_border = "rgba(255,255,255,0.32)"
        sec_bg      = "rgba(255,255,255,0.18)"; sec_border  = "rgba(255,255,255,0.24)"
        img_h = "340px"; text_color = "#fff"; muted = "rgba(255,255,255,0.82)"
    elif template == "garage":
        body_bg     = (f"radial-gradient(circle at top right, rgba({ar},{ag},{ab},0.28), transparent 30%),"
                       f"linear-gradient(135deg, #120804, #3b1a0a)")
        hero_bg     = (f"linear-gradient(135deg, rgba(0,0,0,0.6), rgba({ar},{ag},{ab},0.22)),"
                       f"repeating-linear-gradient(45deg,rgba(255,255,255,0.03) 0 2px,transparent 2px 10px)")
        hero_border = f"rgba({ar},{ag},{ab},0.32)"
        sec_bg      = "rgba(0,0,0,0.30)"; sec_border = f"rgba({ar},{ag},{ab},0.22)"
        img_h = "260px"; text_color = "#fff"; muted = "#fed7aa"
    elif template == "streetwear":
        body_bg     = (f"radial-gradient(circle at top left, rgba({ar},{ag},{ab},0.18), transparent 30%),"
                       f"linear-gradient(135deg, #030303, #27272a)")
        hero_bg     = "linear-gradient(135deg, #080808, #18181b)"; hero_border = "rgba(255,255,255,0.20)"
        sec_bg      = "rgba(255,255,255,0.05)"; sec_border = "rgba(255,255,255,0.10)"
        img_h = "300px"; text_color = "#fff"; muted = "#e5e7eb"
    elif template == "tech":
        body_bg     = (f"radial-gradient(circle at top right, rgba({ar},{ag},{ab},0.34), transparent 35%),"
                       f"radial-gradient(circle at bottom left, rgba({sr},{sg},{sb},0.24), transparent 35%),"
                       f"linear-gradient(135deg, #020617, #0f172a)")
        hero_bg     = f"linear-gradient(135deg,rgba(15,23,42,0.88),rgba(2,6,23,0.95))"
        hero_border = f"rgba({ar},{ag},{ab},0.30)"
        sec_bg      = f"rgba({ar},{ag},{ab},0.08)"; sec_border = f"rgba({ar},{ag},{ab},0.24)"
        img_h = "220px"; text_color = "#fff"; muted = "#cffafe"
    elif template == "luxury":
        body_bg     = (f"radial-gradient(circle at top right, rgba({ar},{ag},{ab},0.22), transparent 34%),"
                       f"linear-gradient(135deg, #050509, #1b1630)")
        hero_bg     = "transparent"; hero_border = f"rgba({ar},{ag},{ab},0.40)"
        sec_bg      = "rgba(255,255,255,0.05)"; sec_border = f"rgba({ar},{ag},{ab},0.22)"
        img_h = "360px"; text_color = "#fff"; muted = "#fef3c7"
    elif template == "wellness":
        body_bg     = (f"radial-gradient(circle at 30% 20%, rgba({ar},{ag},{ab},0.28), transparent 40%),"
                       f"radial-gradient(circle at 70% 80%, rgba({sr},{sg},{sb},0.22), transparent 40%),"
                       f"linear-gradient(160deg, #0d1f1a, #1a2e2a)")
        hero_bg     = "rgba(255,255,255,0.12)"; hero_border = f"rgba({ar},{ag},{ab},0.28)"
        sec_bg      = "rgba(255,255,255,0.08)"; sec_border = f"rgba({ar},{ag},{ab},0.18)"
        img_h = "280px"; text_color = "#fff"; muted = "#d1fae5"
    elif template == "food":
        body_bg     = (f"radial-gradient(circle at top left, rgba({ar},{ag},{ab},0.30), transparent 32%),"
                       f"linear-gradient(150deg, #1a0a00, #2d1200)")
        hero_bg     = f"linear-gradient(135deg,rgba({ar},{ag},{ab},0.18),rgba(0,0,0,0.72))"
        hero_border = f"rgba({ar},{ag},{ab},0.35)"
        sec_bg      = "rgba(255,255,255,0.07)"; sec_border = f"rgba({ar},{ag},{ab},0.20)"
        img_h = "280px"; text_color = "#fff"; muted = "#fde68a"
    elif template == "art":
        body_bg     = f"linear-gradient(170deg, #f5f0eb 0%, #e8ddd0 40%, rgba({ar},{ag},{ab},0.08) 100%)"
        hero_bg     = "rgba(255,255,255,0.65)"; hero_border = f"rgba({ar},{ag},{ab},0.22)"
        sec_bg      = "rgba(255,255,255,0.55)"; sec_border = f"rgba({ar},{ag},{ab},0.16)"
        img_h = "360px"; text_color = "#111"; muted = "#444"
    elif template == "sports":
        body_bg     = (f"radial-gradient(circle at top right, rgba({ar},{ag},{ab},0.40), transparent 35%),"
                       f"linear-gradient(135deg, #050505, #0f0f0f)")
        hero_bg     = f"linear-gradient(135deg,rgba({ar},{ag},{ab},0.18),rgba(0,0,0,0.90))"
        hero_border = f"rgba({ar},{ag},{ab},0.35)"
        sec_bg      = f"rgba({ar},{ag},{ab},0.08)"; sec_border = f"rgba({ar},{ag},{ab},0.22)"
        img_h = "260px"; text_color = "#fff"; muted = "#e5e7eb"
    else:  # editorial / default
        body_bg     = (f"radial-gradient(circle at top left, rgba({ar},{ag},{ab},0.24), transparent 30%),"
                       f"linear-gradient(135deg, #111827, #1e293b)")
        hero_bg     = "rgba(255,255,255,0.10)"; hero_border = "rgba(255,255,255,0.18)"
        sec_bg      = "rgba(255,255,255,0.08)"; sec_border = "rgba(255,255,255,0.14)"
        img_h = "260px"; text_color = "#fff"; muted = "#dbeafe"

    # ── Card backgrounds ─────────────────────────────────────────────
    if template == "luxury":
        card_bg = "transparent"; card_border = f"rgba({ar},{ag},{ab},0.28)"
    elif template in ("tech", "sports"):
        card_bg = f"rgba({ar},{ag},{ab},0.09)"; card_border = f"rgba({ar},{ag},{ab},0.34)"
    elif template == "art":
        card_bg = "rgba(255,255,255,0.82)"; card_border = f"rgba({ar},{ag},{ab},0.20)"
    else:
        card_bg = "rgba(255,255,255,0.10)"; card_border = "rgba(255,255,255,0.16)"

    # ── Template-specific extras ─────────────────────────────────────
    extra = ""
    if template == "streetwear":
        extra = f"""
        .ai-custom-store .storefront-hero-content h1 {{text-transform:uppercase;letter-spacing:-4px;}}
        .ai-custom-store .storefront-product-info h3 {{text-transform:uppercase;letter-spacing:-1px;}}
        .ai-custom-store .storefront-product-card img {{filter:contrast(1.05) saturate(0.88);}}"""
    elif template == "luxury":
        extra = f"""
        .ai-custom-store .storefront-product-card {{border:none !important;border-bottom:1px solid rgba({ar},{ag},{ab},0.28) !important;}}
        .ai-custom-store .storefront-product-card:hover {{transform:none !important;background:rgba({ar},{ag},{ab},0.05) !important;}}
        .ai-custom-store .storefront-hero-content h1 {{font-weight:400;letter-spacing:3px;}}"""
    elif template == "tech":
        extra = f"""
        @keyframes techPulse{{from{{box-shadow:0 0 8px rgba({ar},{ag},{ab},0.22);}}to{{box-shadow:0 0 32px rgba({ar},{ag},{ab},0.60);}}}}
        .ai-custom-store .storefront-product-card{{animation:techPulse 2.8s ease-in-out infinite alternate !important;}}
        .ai-custom-store .storefront-hero-content h1 {{letter-spacing:-3px;}}"""
    elif template == "beauty":
        extra = f"""
        .ai-custom-store .storefront-hero-content h1 {{font-weight:300;letter-spacing:2px;}}
        .ai-custom-store .storefront-product-card:hover img {{transform:scale(1.07);}}"""
    elif template == "wellness":
        extra = f"""
        .ai-custom-store .storefront-hero-content h1 {{font-weight:300;letter-spacing:-1px;}}"""
    elif template == "art":
        extra = f"""
        .ai-custom-store .storefront-product-card h3,.ai-custom-store .storefront-product-info p,.ai-custom-store .storefront-product-info strong {{color:#111 !important;}}
        .ai-custom-store .public-store-nav strong,.ai-custom-store .storefront-hero-content h1,.ai-custom-store .storefront-hero-content h2 {{color:#111 !important;}}
        .ai-custom-store .storefront-hero-content h1 {{font-style:italic;font-weight:400;}}
        .ai-custom-store .tag {{color:{accent} !important;background:rgba({ar},{ag},{ab},0.10) !important;}}"""
    elif template == "food":
        extra = f"""
        .ai-custom-store .storefront-hero-content h1 {{font-style:italic;letter-spacing:-1px;}}
        .ai-custom-store .storefront-product-card img {{filter:saturate(1.18) contrast(1.04);}}"""
    elif template in ("sports", "garage"):
        extra = f"""
        .ai-custom-store .storefront-hero-content h1 {{text-transform:uppercase;letter-spacing:-3px;font-weight:900;}}
        .ai-custom-store .storefront-product-info h3 {{text-transform:uppercase;}}"""

    return f"""{font_tag}
<style>
.ai-custom-store {{
    min-height:100vh;
    background:{body_bg};
    background-size:220% 220%;
    color:{text_color};
    animation:lf-drift 16s ease infinite;
}}
@keyframes lf-drift {{
    0%  {{background-position:0% 0%;}}
    33% {{background-position:60% 40%;}}
    66% {{background-position:40% 80%;}}
    100%{{background-position:0% 0%;}}
}}
.ai-custom-store .storefront-hero {{background:{hero_bg} !important;border:1px solid {hero_border} !important;}}
.ai-custom-store .storefront-product-card {{background:{card_bg} !important;border:1px solid {card_border} !important;border-radius:{card_radius} !important;}}
.ai-custom-store .storefront-product-card img {{height:{img_h} !important;}}
.ai-custom-store .public-store-section,.ai-custom-store .ai-brand-section {{background:{sec_bg} !important;border:1px solid {sec_border} !important;}}
.ai-custom-store .storefront-stats div,.ai-custom-store .ai-trust-row div {{background:rgba({ar},{ag},{ab},0.14) !important;border:1px solid rgba({ar},{ag},{ab},0.24) !important;}}
.ai-custom-store .button:not(.ghost) {{background:{accent} !important;color:#fff !important;border-radius:{btn_radius} !important;{btn_glow}}}
.ai-custom-store .button.ghost {{border-radius:{btn_radius} !important;border-color:rgba({ar},{ag},{ab},0.45) !important;}}
.ai-custom-store h1,.ai-custom-store h2,.ai-custom-store h3 {{font-family:{heading_font};}}
.ai-custom-store p,.ai-custom-store span:not(.cart-count-badge):not(.chat-mini-badge) {{color:{muted};}}
.ai-custom-store .eyebrow {{color:{accent} !important;opacity:1;}}
.ai-custom-store .store-brand-badge {{
    display:inline-block;padding:6px 18px;border-radius:999px;
    background:rgba({ar},{ag},{ab},0.18);border:1px solid rgba({ar},{ag},{ab},0.38);
    color:{accent};font-size:12px;font-weight:900;letter-spacing:1.8px;
    text-transform:uppercase;margin-bottom:20px;
}}
.ai-custom-store .tag {{background:rgba({ar},{ag},{ab},0.22) !important;color:#fff !important;}}
.ai-custom-store .storefront-product-card:hover {{box-shadow:0 24px 70px rgba({ar},{ag},{ab},0.32) !important;transform:translateY(-6px);}}
.ai-custom-store .public-store-nav a {{border-color:rgba({ar},{ag},{ab},0.30) !important;}}
.ai-custom-store .public-store-nav a:hover {{background:rgba({ar},{ag},{ab},0.22) !important;}}
{extra}
</style>"""


def money(value):
    return f"{float(value):.2f}"


def clean_price(value):
    value = str(value).replace("$", "").replace(",", "").strip()
    if not value:
        return 0.0
    return float(value)


def clean_stock(value):
    value = str(value).strip()
    if not value:
        return 0
    return int(value)


def unique_slug(base_slug, product_id=None):
    conn = db()
    cur = conn.cursor()
    slug = base_slug
    i = 2
    try:
        while True:
            if product_id:
                cur.execute("SELECT id FROM products WHERE slug = ? AND id != ?", (slug, product_id))
            else:
                cur.execute("SELECT id FROM products WHERE slug = ?", (slug,))

            if not cur.fetchone():
                return slug

            slug = f"{base_slug}-{i}"
            i += 1
    finally:
        conn.close()


def _make_session_cookie(email: str) -> str:
    expire = datetime.utcnow() + timedelta(days=JWT_EXPIRE_DAYS)
    return jwt.encode({"sub": email, "exp": expire}, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _verify_session_cookie(token: str) -> str | None:
    if not token:
        return None
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload.get("sub")
    except JWTError:
        return None


def get_current_user(request: Request):
    token = request.cookies.get("LaunchFlow_user")
    email = _verify_session_cookie(token)

    if not email:
        return None

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE email = ?", (email,))
    user = cur.fetchone()
    conn.close()

    return user


def require_user(request: Request):
    return get_current_user(request)


def create_user(email, password):
    email = email.lower().strip()

    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM users WHERE email = ?", (email,))
    existing = cur.fetchone()

    if existing:
        conn.close()
        return None

    hashed = hash_password(password)

    cur.execute(
        "INSERT INTO users (email, password, is_pro, store_name) VALUES (?, ?, ?, ?)",
        (email, hashed, 0, "My Store")
    )

    conn.commit()
    cur.execute("SELECT * FROM users WHERE email = ?", (email,))
    user = cur.fetchone()
    conn.close()

    return user


def login_user(email, password):
    email = email.lower().strip()

    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM users WHERE email = ?", (email,))
    user = cur.fetchone()

    if not user:
        conn.close()
        return None

    if not verify_password(password, user["password"]):
        conn.close()
        return None

    # Migrate legacy SHA256 hash to bcrypt on first login
    if user["password"] and len(user["password"]) == 64:
        new_hash = hash_password(password)
        cur.execute("UPDATE users SET password = ? WHERE id = ?", (new_hash, user["id"]))
        conn.commit()

    conn.close()
    return user


def layout(content, title="LaunchFlow", description="Build and launch your online store in minutes.", og_image=""):
    og_img_tag = f'<meta property="og:image" content="{og_image}">' if og_image else ""
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>{title}</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <meta name="description" content="{description}">
        <meta property="og:title" content="{title}">
        <meta property="og:description" content="{description}">
        <meta property="og:type" content="website">
        {og_img_tag}
        <meta name="twitter:card" content="summary_large_image">
        <meta name="twitter:title" content="{title}">
        <meta name="twitter:description" content="{description}">
        <link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
        <link rel="stylesheet" href="/static/style.css">
    </head>

    <body>
        {content}

        <footer class="site-footer">
            <a href="/terms">Terms</a>
            <a href="/privacy">Privacy</a>
            <a href="/refunds">Refunds</a>
        </footer>

        <button id="chat-launcher" class="chat-launcher" type="button" style="display:none">
            💬
            <span id="chat-notification-count" class="chat-notification-dot hidden">0</span>
        </button>

        <div id="chat-window" class="chat-window">
            <div class="chat-header">
                <div>
                    <strong id="chat-title">LaunchFlow Messages</strong>
                    <p id="chat-subtitle">Inbox</p>
                </div>
                <div class="chat-header-actions">
                    <button type="button" onclick="expandChatWindow()">⛶</button>
                    <button type="button" onclick="closeChatWindow()">✕</button>
                </div>
            </div>

            <div class="chat-main" id="chat-main">
                <div class="chat-empty-state">
                    <h3>Your messages</h3>
                    <p>Message sellers directly through LaunchFlow.</p>
                </div>
            </div>
        </div>

        <script>
            (function() {{
                const params = new URLSearchParams(window.location.search);
                const msg = params.get("msg");
                if (msg) {{
                    const toast = document.createElement("div");
                    toast.textContent = decodeURIComponent(msg);
                    toast.style.cssText = "position:fixed;top:16px;left:50%;transform:translateX(-50%);background:#22c55e;color:#fff;padding:10px 20px;border-radius:8px;font-size:14px;font-weight:600;z-index:9999;box-shadow:0 4px 16px rgba(0,0,0,.25);";
                    document.body.appendChild(toast);
                    setTimeout(() => toast.remove(), 3500);
                    const url = new URL(window.location);
                    url.searchParams.delete("msg");
                    window.history.replaceState({{}}, "", url);
                }}
            }})();
            const chatLauncher = document.getElementById("chat-launcher");
            const chatWindow = document.getElementById("chat-window");
            const chatMain = document.getElementById("chat-main");
            const notificationDot = document.getElementById("chat-notification-count");

            let currentConversationId = null;
            let currentInbox = [];
            let chatSearchTimer = null;

            document.querySelectorAll(".money-input").forEach(input => {{
                input.addEventListener("input", () => {{
                    let value = input.value.replace(/[^0-9.]/g, "");
                    input.value = value ? "$" + value : "";
                }});
            }});

            async function shareLaunchFlowLink(url, title) {{
                try {{
                    if (navigator.share) {{
                        await navigator.share({{
                            title: title || "LaunchFlow",
                            text: "Check this out on LaunchFlow",
                            url: url
                        }});
                        return;
                    }}
                }} catch (err) {{
                    console.log("Share failed, copying instead.");
                }}

                try {{
                    await navigator.clipboard.writeText(url);
                    alert("Link copied!");
                }} catch (err) {{
                    prompt("Copy this link:", url);
                }}
            }}

            async function copyLaunchFlowLink(url) {{
                try {{
                    await navigator.clipboard.writeText(url);
                    alert("Link copied!");
                }} catch (err) {{
                    prompt("Copy this link:", url);
                }}
            }}

            async function refreshUnreadCount() {{
                const res = await fetch("/chat/unread-count");
                const data = await res.json();

                if (data.ok) {{
                    chatLauncher.style.display = "";
                    if (data.unread > 0) {{
                        notificationDot.classList.remove("hidden");
                        notificationDot.textContent = data.unread;
                    }} else {{
                        notificationDot.classList.add("hidden");
                        notificationDot.textContent = "0";
                    }}
                }} else {{
                    chatLauncher.style.display = "none";
                }}
            }}

            async function renderInbox(searchTerm = "") {{
                currentConversationId = null;

                document.getElementById("chat-title").textContent = "LaunchFlow Messages";
                document.getElementById("chat-subtitle").textContent = "Inbox";

                const res = await fetch("/chat/inbox");
                const data = await res.json();

                if (!data.ok) {{
                    chatMain.innerHTML = `
                        <div class="chat-empty-state">
                            <h3>Please log in</h3>
                            <p>You need an account to use messages.</p>
                        </div>
                    `;
                    return;
                }}

                currentInbox = data.conversations || [];

                let filtered = currentInbox.filter(c => {{
                    const term = searchTerm.toLowerCase();
                    return (
                        String(c.store_name || "").toLowerCase().includes(term) ||
                        String(c.seller_name || "").toLowerCase().includes(term) ||
                        String(c.buyer_email || "").toLowerCase().includes(term)
                    );
                }});

                let buttons = "";

                if (filtered.length === 0) {{
                    buttons = `
                        <div class="chat-empty-state">
                            <h3>No conversations yet</h3>
                            <p>Open a store and press Message Seller to start one.</p>
                        </div>
                    `;
                }}

                filtered.forEach(c => {{
                    const unreadClass = c.unread_count > 0 ? "unread" : "";
                    const badge = c.unread_count > 0 ? `<span class="chat-mini-badge">${{c.unread_count}}</span>` : "";

                    buttons += `
                        <button
                            type="button"
                            class="chat-conversation-item ${{unreadClass}}"
                            onclick="openExistingConversation(${{c.id}})"
                        >
                            <strong>${{c.store_name}} ${{badge}}</strong>
                            <span>${{c.last_message || "No messages yet"}}</span>
                        </button>
                    `;
                }});

                chatMain.innerHTML = `
                    <div class="chat-layout">
                        <div class="chat-sidebar">
                            <input
                                type="text"
                                placeholder="Search conversations..."
                                class="chat-search"
                                id="chat-search"
                                value="${{searchTerm}}"
                            >

                            <div class="chat-conversation-list">
                                ${{buttons}}
                            </div>
                        </div>

                        <div class="chat-active-view">
                            <div class="chat-empty-state">
                                <h3>Select a conversation</h3>
                                <p>Your message history will appear here.</p>
                            </div>
                        </div>
                    </div>
                `;

                const searchInput = document.getElementById("chat-search");

                searchInput.addEventListener("input", function() {{
                    clearTimeout(chatSearchTimer);

                    const value = this.value;

                    chatSearchTimer = setTimeout(() => {{
                        renderInbox(value);
                    }}, 150);
                }});

                refreshUnreadCount();
            }}

            async function openExistingConversation(conversationId) {{
                currentConversationId = conversationId;

                const convo = currentInbox.find(c => c.id === conversationId);

                if (convo) {{
                    document.getElementById("chat-title").textContent = convo.store_name;
                    document.getElementById("chat-subtitle").textContent = "Conversation";
                }}

                const res = await fetch(`/chat/messages/${{conversationId}}`);
                const data = await res.json();

                if (!data.ok) {{
                    renderInbox();
                    return;
                }}

                let messagesHtml = "";

                data.messages.forEach(m => {{
                    messagesHtml += `
                        <div class="chat-message ${{m.mine ? "buyer" : "seller"}}">
                            ${{m.message}}
                        </div>
                    `;
                }});

                chatMain.innerHTML = `
                    <div class="chat-layout single-chat-layout">
                        <div class="chat-active-view">
                            <button type="button" class="chat-conversation-item chat-back-button" onclick="renderInbox()">
                                <strong>← Back to inbox</strong>
                                <span>View all message history</span>
                            </button>

                            <div class="chat-messages" id="chat-messages">
                                ${{messagesHtml}}
                            </div>

                            <form class="chat-input-row" onsubmit="return sendChatMessage(event)">
                                <textarea
                                    id="chat-input"
                                    placeholder="Type a message..."
                                    autocomplete="off"
                                    required
                                    rows="2"
                                ></textarea>

                                <button type="submit">Send</button>
                            </form>
                        </div>
                    </div>
                `;

                const messages = document.getElementById("chat-messages");
                messages.scrollTop = messages.scrollHeight;

                refreshUnreadCount();
            }}

            async function sendChatMessage(event) {{
                event.preventDefault();

                const input = document.getElementById("chat-input");
                const messages = document.getElementById("chat-messages");
                const text = input.value.trim();
                const activeConversationId = currentConversationId;

                if (!text || !activeConversationId || !messages) {{
                    return false;
                }}

                input.value = "";

                messages.innerHTML += `
                    <div class="chat-message buyer">
                        ${{text}}
                    </div>
                `;

                messages.scrollTop = messages.scrollHeight;

                const res = await fetch("/chat/send", {{
                    method: "POST",
                    headers: {{
                        "Content-Type": "application/json"
                    }},
                    body: JSON.stringify({{
                        conversation_id: activeConversationId,
                        message: text
                    }})
                }});

                const data = await res.json();

                if (!data.ok) {{
                    messages.innerHTML += `
                        <div class="chat-message seller">
                            Message failed to send.
                        </div>
                    `;

                    messages.scrollTop = messages.scrollHeight;
                }}

                if (data.ok) {{
                    const inboxRes = await fetch("/chat/inbox");
                    const inboxData = await inboxRes.json();

                    if (inboxData.ok) {{
                        currentInbox = inboxData.conversations || [];
                    }}
                }}

                refreshUnreadCount();

                return false;
            }}

            async function openSellerChat(sellerId, storeName, storeId) {{
                const res = await fetch("/chat/start", {{
                    method: "POST",
                    headers: {{
                        "Content-Type": "application/json"
                    }},
                    body: JSON.stringify({{
                        seller_id: sellerId,
                        store_id: storeId
                    }})
                }});

                const data = await res.json();

                if (!data.ok) {{
                    alert(data.error || "Could not start chat.");
                    return;
                }}

                chatWindow.classList.add("open");

                await renderInbox();
                await openExistingConversation(data.conversation_id);
            }}

            chatLauncher.addEventListener("click", () => {{
                if (chatWindow.classList.contains("open")) {{
                    chatWindow.classList.remove("open");
                }} else {{
                    chatWindow.classList.add("open");
                    renderInbox();
                }}
            }});

            function closeChatWindow() {{
                chatWindow.classList.remove("open");
                chatWindow.classList.remove("expanded");
            }}

            function expandChatWindow() {{
                chatWindow.classList.toggle("expanded");
            }}

            refreshUnreadCount();

            setInterval(async () => {{
                await refreshUnreadCount();

                if (
                    chatWindow.classList.contains("open") &&
                    currentConversationId &&
                    document.activeElement?.id !== "chat-input"
                ) {{
                    await openExistingConversation(currentConversationId);
                }}

                if (
                    chatWindow.classList.contains("open") &&
                    !currentConversationId
                ) {{
                    await renderInbox();
                }}
            }}, 3000);
        </script>
    </body>
    </html>
    """



def top_nav(user):

    if user["is_pro"] == 1:
        premium_button = """
        <a href="/settings" class="premium-pill premium-active">
            Premium
        </a>
        """
    else:
        premium_button = """
        <a href="/upgrade" class="upgrade-pill">
            Upgrade
        </a>
        """

    stripe_ready = bool(
        user["stripe_account_id"]
        and user["stripe_onboarding_complete"]
    )

    stripe_badge = """
    <a href="/settings" class="nav-status ready">
        Ready to sell
    </a>
    """ if stripe_ready else """
    <a href="/settings" class="nav-status warning">
        Setup payments
    </a>
    """

    conn = db()
    cur = conn.cursor()

    cur.execute("""
    SELECT COUNT(*) as count
    FROM cart_items
    WHERE user_id = ?
    """, (user["id"],))

    cart_count = cur.fetchone()["count"]

    conn.close()

    cart_badge = ""

    if cart_count > 0:
        cart_badge = f"""
        <span class="cart-count-badge">
            {cart_count}
        </span>
        """

    return f"""
    <nav class="top-nav">

        <div class="nav-left">
            <a class="brand" href="/dashboard">
                LaunchFlow
            </a>
        </div>

        <button class="hamburger-btn" id="hamburger-btn" type="button" aria-label="Menu" onclick="document.getElementById('nav-links-mobile').classList.toggle('open')">
            &#9776;
        </button>

        <div class="nav-links" id="nav-links-mobile">
            <a href="/dashboard">Dashboard</a>

            <a href="/discover">
                Discover
            </a>

            <a href="/ai-builder">
                AI Builder
            </a>

            <a href="/viral-products">
                Viral Products
            </a>

            <a href="/analytics">
                Analytics
            </a>

            <a href="/orders">
                Orders
            </a>

            <a href="/ai-product-copy">
                AI Copy
            </a>

            <a href="/profit-calculator">
                Profit Calc
            </a>

            <a href="/suppliers">
                Suppliers
            </a>

            <a href="/discounts">
                Discounts
            </a>

            <a href="/cart" class="cart-nav-link">
                Cart
                {cart_badge}
            </a>

            <a href="/settings">
                Settings
            </a>

            {stripe_badge}

            {premium_button}

            <a href="/logout">
                Log out
            </a>
        </div>
    </nav>
    <style>
    .hamburger-btn {{
        display: none;
        background: none;
        border: none;
        color: inherit;
        font-size: 22px;
        cursor: pointer;
        padding: 4px 8px;
        margin-left: auto;
    }}
    @media (max-width: 768px) {{
        .hamburger-btn {{ display: block; }}
        .top-nav {{ flex-wrap: wrap; position: relative; }}
        .nav-links {{
            display: none;
            flex-direction: column;
            width: 100%;
            padding: 8px 0;
            gap: 4px;
        }}
        .nav-links.open {{ display: flex; }}
        .nav-links a, .nav-links .upgrade-pill, .nav-links .premium-active, .nav-links .nav-status {{
            padding: 8px 16px;
            border-radius: 6px;
        }}
    }}
    </style>
    """


# -----------------------------
# AI LOGIC
# -----------------------------
def demo_ai_generate(product_idea, audience="", vibe=""):
    idea = product_idea.strip()
    audience_text = audience.strip() or "online shoppers"
    vibe_text = vibe.strip() or "premium, clean, modern, high-converting"

    prompt = f"""
You are an elite ecommerce website generator and brand designer.

The user wants a REAL full ecommerce store concept with a unique AI-generated storefront style.

Store idea: {idea}
Target audience: {audience_text}
Desired vibe: {vibe_text}

Choose template_type carefully.

template_type must be one of:
garage, luxury, streetwear, beauty, tech, editorial, wellness, food, art, sports

Rules for template_type:
- cars, car accessories, detailing, rustic, garage, tools, outdoor gear = garage
- shampoo, skincare, haircare, beauty, feminine, clean beauty, cosmetics = beauty
- gaming, streetwear, hype, culture, clothing drops, bold urban = streetwear
- tech, futuristic, software, gadgets, AI, electronics = tech
- luxury, elegant, premium, jewelry, watches, refined = luxury
- books, education, learning, journals, courses, knowledge, content = editorial
- yoga, meditation, supplements, wellness, mindfulness, spa, holistic, health = wellness
- coffee, food, restaurant, bakery, culinary, snacks, spices, meal = food
- painting, art, gallery, prints, handmade, crafts, illustration, creative = art
- fitness, gym, sports, athletic, training, workout, running, gear = sports

Important:
- Do NOT use minimal as a template_type.
- Do NOT return generic store names.
- Do NOT sell actual cars unless the user clearly asks for vehicles.
- If the idea is broad, niche it down into a real ecommerce store.
- Make the store feel like a real brand with a specific direction.
- The store is a draft. The user will add real products later.

Return ONLY valid JSON with these exact keys:
store_name,
slug,
tagline,
hero_headline,
hero_subheadline,
brand_vibe,
theme,
primary_category,
featured_sections,
product_categories,
homepage_copy,
cta,
template_type,
design_style,
hero_layout,
card_style,
button_style,
background_style,
accent_color,
secondary_color,
section_style,
font_style,
trust_badges,
store_mood,
default_pages,
ai_sections

Rules:
- theme must be one of: blue, purple, green, orange, dark
- design_style must be one of: glass, bold, soft, editorial, luxury, futuristic, rugged
- hero_layout must be one of: centered, split, billboard, editorial, stacked
- card_style must be one of: glass, soft, sharp, luxury, bordered, shadow
- button_style must be one of: pill, rounded, sharp, luxury, glow
- background_style must be one of: gradient, radial, dark, soft, luxury, clean
- section_style must be one of: cards, split, grid, magazine, stacked
- font_style must be one of: modern, luxury, bold, editorial, clean
- accent_color must be a hex color
- secondary_color must be a hex color
- featured_sections must be a list of 3 specific homepage section names
- product_categories must be a list of 4 specific product category names
- trust_badges must be a list of 3 short trust/brand badges
- default_pages must be a list of 3 pages with title, slug, page_type, and content
- default_pages should include About, FAQ, and Contact
- ai_sections must be a list of 4 sections with type and data
- ai_sections should include hero, featured-products, benefits, and faq
- homepage_copy should be 2-3 persuasive sentences
- cta should be short
- no prices
- no stock
- no markdown
"""

    def choose_template_fallback(text):
        text = text.lower()

        if any(word in text for word in ["car", "cars", "garage", "detailing", "detail", "rustic", "truck", "auto", "vehicle", "tools"]):
            return "garage"

        if any(word in text for word in ["shampoo", "hair", "skincare", "skin", "beauty", "makeup", "feminine", "cosmetic", "soap"]):
            return "beauty"

        if any(word in text for word in ["gaming", "streetwear", "clothing", "hype", "drop", "urban", "culture"]):
            return "streetwear"

        if any(word in text for word in ["tech", "ai", "software", "gadget", "electronics", "future", "futuristic"]):
            return "tech"

        if any(word in text for word in ["luxury", "premium", "elegant", "jewelry", "watch", "watches", "designer"]):
            return "luxury"

        if any(word in text for word in ["book", "books", "learning", "education", "course", "journal", "knowledge", "study"]):
            return "editorial"

        if any(word in text for word in ["yoga", "meditation", "supplement", "wellness", "mindfulness", "spa", "holistic", "health"]):
            return "wellness"

        if any(word in text for word in ["coffee", "food", "restaurant", "bakery", "culinary", "snack", "spice", "meal", "cafe"]):
            return "food"

        if any(word in text for word in ["painting", "art", "gallery", "print", "handmade", "craft", "illustration", "creative"]):
            return "art"

        if any(word in text for word in ["fitness", "gym", "sport", "athletic", "training", "workout", "running", "gear"]):
            return "sports"

        return "editorial"

    def safe_choice(value, allowed, fallback):
        value = str(value or "").strip().lower()
        return value if value in allowed else fallback

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            system="Return only clean valid JSON. No markdown. No explanation. No code fences.",
            messages=[
                {
                    "role": "user",
                    "content": prompt
                }
            ]
        )

        content = response.content[0].text.strip()
        # Strip any accidental markdown fences
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        data = json.loads(content)

        store_name = data.get("store_name", "").strip() or "Generated Store"

        allowed_templates = ["garage", "luxury", "streetwear", "beauty", "tech", "editorial", "wellness", "food", "art", "sports"]
        template_type = safe_choice(
            data.get("template_type"),
            allowed_templates,
            choose_template_fallback(f"{idea} {audience_text} {vibe_text}")
        )

        allowed_themes = ["blue", "purple", "green", "orange", "dark"]
        theme = safe_choice(data.get("theme"), allowed_themes, "dark")

        default_pages = data.get("default_pages", [
            {
                "title": "About",
                "slug": "about",
                "page_type": "about",
                "content": data.get("homepage_copy", "This store was built with LaunchFlow AI.")
            },
            {
                "title": "FAQ",
                "slug": "faq",
                "page_type": "faq",
                "content": "Common questions about this brand, products, shipping, and customer support."
            },
            {
                "title": "Contact",
                "slug": "contact",
                "page_type": "contact",
                "content": "Contact this store through LaunchFlow messages for support or product questions."
            }
        ])

        ai_sections = data.get("ai_sections", [
            {
                "type": "hero",
                "data": {
                    "headline": data.get("hero_headline", f"Welcome to {store_name}"),
                    "subheadline": data.get("hero_subheadline", ""),
                    "description": data.get("homepage_copy", "")
                }
            },
            {
                "type": "featured-products",
                "data": {
                    "title": "Featured Products"
                }
            },
            {
                "type": "benefits",
                "data": {
                    "title": "Why Customers Choose Us",
                    "items": data.get("trust_badges", ["Fast setup", "Premium storefront", "Ready to sell"])
                }
            },
            {
                "type": "faq",
                "data": {
                    "title": "Frequently Asked Questions"
                }
            }
        ])

        return {
            "store_name": store_name,
            "slug": slugify(data.get("slug", store_name)),
            "tagline": data.get("tagline", "A branded storefront ready for your products."),
            "hero_headline": data.get("hero_headline", f"Welcome to {store_name}"),
            "hero_subheadline": data.get("hero_subheadline", "A polished store foundation ready for real products."),
            "brand_vibe": data.get("brand_vibe", vibe_text),
            "theme": theme,
            "primary_category": data.get("primary_category", "Online Store"),
            "featured_sections": data.get("featured_sections", ["Featured Drops", "Customer Favorites", "New Arrivals"]),
            "product_categories": data.get("product_categories", ["Main Collection", "Starter Products", "Premium Picks", "New Arrivals"]),
            "homepage_copy": data.get("homepage_copy", "This store has a clear brand direction and is ready for real products."),
            "cta": data.get("cta", "Add Product"),
            "template_type": template_type,

            "design_style": safe_choice(data.get("design_style"), ["glass", "bold", "soft", "editorial", "luxury", "futuristic", "rugged"], "glass"),
            "hero_layout": safe_choice(data.get("hero_layout"), ["centered", "split", "billboard", "editorial", "stacked"], "split"),
            "card_style": safe_choice(data.get("card_style"), ["glass", "soft", "sharp", "luxury", "bordered", "shadow"], "glass"),
            "button_style": safe_choice(data.get("button_style"), ["pill", "rounded", "sharp", "luxury", "glow"], "rounded"),
            "background_style": safe_choice(data.get("background_style"), ["gradient", "radial", "dark", "soft", "luxury", "clean"], "gradient"),
            "section_style": safe_choice(data.get("section_style"), ["cards", "split", "grid", "magazine", "stacked"], "cards"),
            "font_style": safe_choice(data.get("font_style"), ["modern", "luxury", "bold", "editorial", "clean"], "modern"),

            "accent_color": data.get("accent_color", "#7c3aed"),
            "secondary_color": data.get("secondary_color", "#06b6d4"),
            "trust_badges": data.get("trust_badges", ["Fast setup", "Premium storefront", "Ready to sell"]),
            "store_mood": data.get("store_mood", "Premium, polished, and conversion-focused"),
            "default_pages": default_pages,
            "ai_sections": ai_sections
        }

    except Exception as e:
        logger.error("AI generation error: %s", e)

        template_type = choose_template_fallback(f"{idea} {audience_text} {vibe_text}")

        if template_type == "garage":
            store_name = "IronTrail Garage"
            theme = "orange"
            accent = "#f97316"
            secondary = "#7c2d12"
            design_style = "rugged"
        elif template_type == "beauty":
            store_name = "Shizuku Botanicals"
            theme = "purple"
            accent = "#ec4899"
            secondary = "#f9a8d4"
            design_style = "soft"
        elif template_type == "streetwear":
            store_name = "Signal Drop"
            theme = "dark"
            accent = "#ffffff"
            secondary = "#71717a"
            design_style = "bold"
        elif template_type == "tech":
            store_name = "NovaCircuit"
            theme = "blue"
            accent = "#06b6d4"
            secondary = "#2563eb"
            design_style = "futuristic"
        elif template_type == "luxury":
            store_name = "Aurelle House"
            theme = "dark"
            accent = "#d4af37"
            secondary = "#111827"
            design_style = "luxury"
        elif template_type == "wellness":
            store_name = "Solara Wellness"
            theme = "green"
            accent = "#10b981"
            secondary = "#34d399"
            design_style = "soft"
        elif template_type == "food":
            store_name = "Golden Grind Co."
            theme = "orange"
            accent = "#f59e0b"
            secondary = "#92400e"
            design_style = "bold"
        elif template_type == "art":
            store_name = "Brushstroke Studio"
            theme = "purple"
            accent = "#8b5cf6"
            secondary = "#ede9fe"
            design_style = "editorial"
        elif template_type == "sports":
            store_name = "Apex Athletic"
            theme = "dark"
            accent = "#ef4444"
            secondary = "#1f2937"
            design_style = "bold"
        else:
            store_name = "The Learning Vault"
            theme = "blue"
            accent = "#2563eb"
            secondary = "#60a5fa"
            design_style = "editorial"

        return {
            "store_name": store_name,
            "slug": slugify(store_name),
            "tagline": "A branded storefront ready for your products.",
            "hero_headline": f"Welcome to {store_name}",
            "hero_subheadline": "A polished store foundation ready for real products.",
            "brand_vibe": vibe_text,
            "theme": theme,
            "primary_category": "Online Store",
            "featured_sections": ["Featured Drops", "Customer Favorites", "New Arrivals"],
            "product_categories": ["Main Collection", "Starter Products", "Premium Picks", "New Arrivals"],
            "homepage_copy": "This store has a clear brand direction and is ready for real products, pages, and brand customization.",
            "cta": "Add Product",
            "template_type": template_type,

            "design_style": design_style,
            "hero_layout": "split",
            "card_style": "glass",
            "button_style": "rounded",
            "background_style": "gradient",
            "section_style": "cards",
            "font_style": "modern",
            "accent_color": accent,
            "secondary_color": secondary,
            "trust_badges": ["Fast setup", "Premium storefront", "Ready to sell"],
            "store_mood": "Premium, polished, and conversion-focused",

            "default_pages": [
                {
                    "title": "About",
                    "slug": "about",
                    "page_type": "about",
                    "content": f"{store_name} is a LaunchFlow-generated brand built around {idea}."
                },
                {
                    "title": "FAQ",
                    "slug": "faq",
                    "page_type": "faq",
                    "content": "Common questions about products, shipping, and support will appear here."
                },
                {
                    "title": "Contact",
                    "slug": "contact",
                    "page_type": "contact",
                    "content": "Message this seller through LaunchFlow for questions or support."
                }
            ],

            "ai_sections": [
                {
                    "type": "hero",
                    "data": {
                        "headline": f"Welcome to {store_name}",
                        "subheadline": "A polished store foundation ready for real products.",
                        "description": "This AI-generated store is ready for products, pages, and brand customization."
                    }
                },
                {
                    "type": "featured-products",
                    "data": {
                        "title": "Featured Products"
                    }
                },
                {
                    "type": "benefits",
                    "data": {
                        "title": "Why Customers Choose Us",
                        "items": ["Fast setup", "Premium storefront", "Ready to sell"]
                    }
                },
                {
                    "type": "faq",
                    "data": {
                        "title": "Frequently Asked Questions"
                    }
                }
            ]
        }


# -----------------------------
# LANDING / AUTH
# -----------------------------
@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    user = get_current_user(request)

    if user:
        return RedirectResponse("/dashboard", status_code=303)

    return layout("""
    <div class="landing">
        <nav class="landing-nav">
            <a class="brand" href="/">LaunchFlow</a>
            <div class="nav-links landing-buttons">
                <a href="/login">Log in</a>
                <a href="/signup">Start Basic</a>
            </div>
        </nav>

        <section class="landing-hero">
            <p class="eyebrow">AI store builder</p>
            <h1>Turn product ideas into store drafts in minutes.</h1>
            <p>Build branded store drafts, add real products, test ideas, and track views and orders.</p>

            <div class="hero-actions">
                <a class="button light" href="/signup">Start Basic</a>
                <a class="button ghost" href="/login">Log In</a>
            </div>
        </section>

        <section class="landing-grid">
            <div>
                <h3>1. Describe</h3>
                <p>Tell the AI what kind of store you want.</p>
            </div>
            <div>
                <h3>2. Generate</h3>
                <p>Get a branded store draft with layout, sections, and direction.</p>
            </div>
            <div>
                <h3>3. Add Products</h3>
                <p>Add your real products after the store draft is ready.</p>
            </div>
        </section>
    </div>
    """)


@app.get("/signup", response_class=HTMLResponse)
def signup_page(error: str = ""):
    error_box = ""

    if error == "passwords":
        error_box = '<div class="error-box">Passwords do not match.</div>'
    elif error == "exists":
        error_box = '<div class="error-box">That email already has an account. Try logging in instead.</div>'

    return layout(f"""
    <div class="auth-page">
        <div class="auth-card">
            <p class="eyebrow">Create account</p>
            <h1>Start Basic</h1>
            <p>Create your account and start building your first store.</p>

            {error_box}

            <form action="/signup" method="post">
                <label>Email</label>
                <input name="email" type="email" required placeholder="you@example.com">

                <label>Password</label>
                <input id="signup-password" name="password" type="password" required placeholder="Create password">

                <label>Confirm Password</label>
                <input id="signup-confirm-password" name="confirm_password" type="password" required placeholder="Confirm password">

                <label class="show-password-row">
                    <input id="signup-show-passwords" type="checkbox">
                    <span>Show passwords</span>
                </label>

                <button type="submit">Create Account</button>
            </form>

            <p class="auth-switch">Already have an account? <a href="/login">Log in</a></p>
        </div>
    </div>

    <script>
        const signupToggle = document.getElementById("signup-show-passwords");
        const signupPassword = document.getElementById("signup-password");
        const signupConfirmPassword = document.getElementById("signup-confirm-password");

        signupToggle.addEventListener("change", function () {{
            const inputType = this.checked ? "text" : "password";
            signupPassword.type = inputType;
            signupConfirmPassword.type = inputType;
        }});
    </script>
    """)


@app.post("/signup")
def signup(email: str = Form(...), password: str = Form(...), confirm_password: str = Form(...)):
    if password != confirm_password:
        return RedirectResponse("/signup?error=passwords", status_code=303)

    user = create_user(email, password)

    if not user:
        return RedirectResponse("/signup?error=exists", status_code=303)

    conn = db()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE users
        SET is_pro = 0,
            ai_uses = 0
        WHERE id = ?
        """,
        (user["id"],)
    )

    conn.commit()
    conn.close()

    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie("LaunchFlow_user", _make_session_cookie(user["email"]), max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax")
    return response


@app.get("/login", response_class=HTMLResponse)
def login_page(error: str = ""):
    error_box = ""

    if error == "invalid":
        error_box = '<div class="error-box">Wrong email or password. Try again.</div>'

    return layout(f"""
    <div class="auth-page">
        <div class="auth-card">
            <p class="eyebrow">Welcome back</p>
            <h1>Log in</h1>
            <p>Enter your email and password.</p>

            {error_box}

            <form action="/login" method="post">
                <label>Email</label>
                <input name="email" type="email" required placeholder="you@example.com">

                <label>Password</label>
                <input id="login-password" name="password" type="password" required placeholder="Enter password">

                <label class="show-password-row">
                    <input id="login-show-password" type="checkbox">
                    <span>Show password</span>
                </label>

                <button type="submit">Log In</button>
            </form>

            <p class="auth-switch">New here? <a href="/signup">Start Free</a></p>
            <p class="auth-switch"><a href="/forgot-password">Forgot password?</a></p>
        </div>
    </div>

    <script>
        const loginToggle = document.getElementById("login-show-password");
        const loginPassword = document.getElementById("login-password");

        loginToggle.addEventListener("change", function () {{
            loginPassword.type = this.checked ? "text" : "password";
        }});
    </script>
    """)


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    user = login_user(email, password)

    if not user:
        return RedirectResponse("/login?error=invalid", status_code=303)

    # Merge any guest cart items into the logged-in user's account
    session_id = request.cookies.get("launchflow_cart_id", "")
    if session_id:
        conn = db()
        cur = conn.cursor()
        cur.execute(
            "UPDATE cart_items SET user_id = ? WHERE session_id = ? AND user_id = 0",
            (user["id"], session_id)
        )
        conn.commit()
        conn.close()

    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie("LaunchFlow_user", _make_session_cookie(user["email"]), max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax")
    return response


@app.get("/logout")
def logout():
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("LaunchFlow_user")
    return response


# -----------------------------
# DASHBOARD
# -----------------------------
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT * FROM products WHERE user_id = ? ORDER BY id DESC",
        (user["id"],)
    )
    stores = cur.fetchall()

    cur.execute("""
    SELECT COUNT(*) as count
    FROM orders
    JOIN products ON orders.product_id = products.id
    WHERE products.user_id = ?
    """, (user["id"],))
    order_count = cur.fetchone()["count"]

    cur.execute("""
    SELECT COALESCE(SUM(orders.amount), 0) as revenue
    FROM orders
    JOIN products ON orders.product_id = products.id
    WHERE products.user_id = ?
    """, (user["id"],))
    revenue = cur.fetchone()["revenue"]

    cur.execute("""
    SELECT COALESCE(SUM(views), 0) as views
    FROM products
    WHERE user_id = ?
    """, (user["id"],))
    views = cur.fetchone()["views"]

    cur.execute("""
    SELECT COUNT(*) as count
    FROM cart_items
    WHERE user_id = ?
    """, (user["id"],))
    cart_count = cur.fetchone()["count"]

    conn.close()

    cards = ""

    for p in stores:
        status = "Published" if p["published"] else "Draft"

        theme_label = {
            "blue": "Blue",
            "purple": "Purple",
            "green": "Green",
            "orange": "Orange",
            "dark": "Dark"
        }.get(p["theme"], "Theme")

        cards += f"""
        <div class="store-card">
            <div class="store-card-top">
                <span class="tag">{status}</span>
                <span class="store-theme">{theme_label}</span>
            </div>

            <div class="store-card-main">
                <h3>{p["name"]}</h3>

                <p>
                    {p["tagline"] or "Store draft"}
                </p>
            </div>

            <div class="store-metrics">
                <div>
                    <strong>{p["views"] or 0}</strong>
                    <span>Views</span>
                </div>

                <div>
                    <strong>{p["published"]}</strong>
                    <span>Live</span>
                </div>
            </div>

            <div class="store-actions">
                <a class="button small ghost" href="/s/{p["slug"]}">View Store</a>
                <a class="button small ghost" href="/stores/{p["slug"]}/add-product">Add Product</a>
                <a class="button small ghost" href="/stores/{p["slug"]}/pages">Manage Pages</a>
                <a class="button small ghost" href="/edit/{p["id"]}">Edit</a>

                <form method="post" action="/publish-store/{p["id"]}" style="display:inline">
                    <button type="submit" class="button small ghost">
                        {"Unpublish" if p["published"] else "Publish"}
                    </button>
                </form>

                <button
                    type="button"
                    class="button small ghost"
                    onclick="shareLaunchFlowLink(window.location.origin + '/s/{p["slug"]}', '{p["name"]}')"
                >
                    Share
                </button>
            </div>

            <div class="store-footer">
                <button
                    class="copy-link"
                    style="background:none;border:none;cursor:pointer;padding:0;text-align:left;font-size:13px;color:rgba(255,255,255,0.7);margin-top:0;"
                    onclick="var b=this;navigator.clipboard.writeText(window.location.origin+'/s/{p["slug"]}').then(function(){{b.textContent='Copied!'}});setTimeout(function(){{b.textContent='/{p["slug"]}'}},1500)"
                    title="Click to copy store URL"
                >
                    /{p["slug"]}
                </button>

                <form method="post" action="/delete/{p["id"]}" style="display:inline"
                      onsubmit="return confirm('Delete this store? This cannot be undone.')">
                    <button type="submit" class="danger-link" style="background:none;border:none;cursor:pointer;padding:0;">
                        Delete
                    </button>
                </form>
            </div>
        </div>
        """

    if not cards:
        cards = """
        <div class="empty-state">
            <h2>No stores yet</h2>

            <p>
                Create your first LaunchFlow store and start adding products.
            </p>

            <div class="hero-actions">
                <a class="button" href="/viral-products">
                    Start with Viral Products
                </a>

                <a class="button ghost" href="/new">
                    Create Manually
                </a>
            </div>
        </div>
        """

    stripe_ready = bool(
        user["stripe_account_id"] and
        user["stripe_onboarding_complete"]
    )

    setup_banner = ""

    if not stripe_ready:
        setup_banner = """
        <div class="setup-banner">
            <div>
                <strong>Finish your payment setup</strong>

                <p>
                    Connect Stripe so customers can actually buy your products.
                </p>
            </div>

            <a class="button small" href="/settings">
                Connect Stripe
            </a>
        </div>
        """

    return layout(f"""
    <div class="container">
        {top_nav(user)}

        {setup_banner}

        <section class="hero dashboard-hero">
            <div class="hero-copy">
                <p class="eyebrow">LaunchFlow dashboard</p>

                <h1>
                    Build, publish, and scale product stores.
                </h1>

                <p>
                    Generate stores, add products, connect payments,
                    and track performance from one place.
                </p>

                <div class="hero-actions">
                    <a class="button light" href="/viral-products">
                        Viral Products
                    </a>

                    <a class="button ghost" href="/ai-builder">
                        AI Builder
                    </a>

                    <a class="button ghost" href="/cart">
                        Cart
                    </a>
                </div>
            </div>
        </section>

        <section class="stats modern-stats">
            <div>
                <h3>{len(stores)}</h3>
                <p>Total Stores</p>
            </div>

            <div>
                <h3>{views}</h3>
                <p>Total Views</p>
            </div>

            <div>
                <h3>{order_count}</h3>
                <p>Total Orders</p>
            </div>

            <div>
                <h3>${money(revenue)}</h3>
                <p>Total Revenue</p>
            </div>

            <div>
                <h3>{cart_count}</h3>
                <p>Cart Items</p>
            </div>
        </section>

        <div class="section-header">
            <div>
                <p class="eyebrow">Workspace</p>
                <h2>Your Stores</h2>
            </div>

            <a class="button small" href="/new">
                + New Store
            </a>
        </div>

        <section class="store-grid">
            {cards}
        </section>
    </div>
    """, title="Dashboard — LaunchFlow")


# -----------------------------
# CREATE MANUAL STORE
# -----------------------------
@app.get("/new", response_class=HTMLResponse)
def new_product(request: Request):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    return layout(f"""
    <div class="container narrow">
        {top_nav(user)}
        <a class="back" href="/dashboard">← Dashboard</a>

        <div class="panel">
            <p class="eyebrow">New store</p>
            <h1>Create store</h1>
            <p>Fill this out and your public store goes live instantly.</p>

            <form action="/create" method="post">
                <label>Store name</label>
                <input name="name" required placeholder="Example: Fitness Fuel Co.">

                <label>Custom URL</label>
                <input name="slug" placeholder="fitness-fuel-co">

                <label>Tagline</label>
                <input name="tagline" placeholder="Example: Simple products for better training.">

                <label>Description</label>
                <textarea name="description" required placeholder="Explain what the store is about."></textarea>

                <label>Theme</label>
                <select name="theme">
                    <option value="blue">Blue Glow</option>
                    <option value="purple">Purple Creator</option>
                    <option value="green">Money Green</option>
                    <option value="orange">Launch Orange</option>
                    <option value="dark">Clean Dark</option>
                </select>

                <label>Shipping</label>
                <select name="shipping_type">
                    <option value="free">Free Shipping</option>
                    <option value="flat">Flat Rate</option>
                    <option value="contact">Contact for Shipping</option>
                    <option value="pickup">Local Pickup Only</option>
                </select>

                <input
                    name="shipping_rate"
                    type="number"
                    min="0"
                    step="0.01"
                    placeholder="Flat rate amount (e.g. 5.99)"
                    style="display:none"
                    id="shipping-rate-input"
                >

                <input
                    name="shipping_info"
                    placeholder="Shipping note (e.g. '3-5 business days, US only')"
                >

                <input type="hidden" name="price" value="0">
                <input type="hidden" name="stock" value="0">
                <input type="hidden" name="image_url" value="">
                <input type="hidden" name="cta" value="Add Product">
                <input type="hidden" name="source" value="manual">

                <button type="submit">Create Store</button>
            </form>
        </div>
    </div>
    <script>
    document.querySelector('[name="shipping_type"]').addEventListener('change', function() {{
        document.getElementById('shipping-rate-input').style.display =
            this.value === 'flat' ? 'block' : 'none';
    }});
    </script>
    """)


@app.post("/create")
def create_store(
    request: Request,
    name: str = Form(...),
    slug: str = Form(""),
    tagline: str = Form(""),
    description: str = Form(""),
    price: str = Form(""),
    stock: str = Form(""),
    image_url: str = Form(""),
    cta: str = Form("Add Product"),
    theme: str = Form("blue"),
    source: str = Form("manual"),
    ai_design: str = Form("{}"),
    shipping_type: str = Form("free"),
    shipping_rate: str = Form("0"),
    shipping_info: str = Form(""),
    viral_product_name: str = Form(""),
    viral_product_description: str = Form(""),
    viral_product_price: str = Form(""),
    viral_product_stock: str = Form("10"),
    # NOTE: length truncation applied below after auth check
    viral_product_image_url: str = Form("")
):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    name = name.strip()[:100]
    description = description.strip()[:2000]
    tagline = tagline.strip()[:200]

    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) as count FROM products WHERE user_id = ?", (user["id"],))
    store_count = cur.fetchone()["count"]

    if user["is_pro"] == 0 and store_count >= 1:
        conn.close()
        return RedirectResponse("/upgrade?reason=store_limit", status_code=303)

    if user["is_pro"] == 0 and source == "ai":
        ai_uses = user["ai_uses"] if "ai_uses" in user.keys() else 0

        if ai_uses >= 1:
            conn.close()
            return RedirectResponse("/upgrade?reason=ai_limit", status_code=303)

    base_slug = slugify(slug or name) or "store"
    final_slug = base_slug
    counter = 2

    while True:
        cur.execute("SELECT id FROM products WHERE slug = ?", (final_slug,))
        if not cur.fetchone():
            break

        final_slug = f"{base_slug}-{counter}"
        counter += 1

    default_brand_json = json.dumps({
        "voice": "modern",
        "style": "clean",
        "primary_color": "#7c3aed",
        "secondary_color": "#06b6d4",
        "layout": "hero-products",
        "sections": [
            "hero",
            "featured-products",
            "benefits",
            "faq"
        ]
    })

    if source == "ai":
        try:
            parsed_design = json.loads(ai_design or "{}")
            ai_design = json.dumps(parsed_design)
        except Exception:
            ai_design = "{}"

        final_price = 0
        final_stock = 0
        final_image_url = ""
        final_cta = cta or "Add Product"

    else:
        ai_design = "{}"
        final_price = clean_price(price)
        final_stock = clean_stock(stock)
        final_image_url = image_url
        final_cta = cta or "Buy Now"

    # Sanitize shipping params — they may be FieldInfo objects when called programmatically
    shipping_type_str  = shipping_type  if isinstance(shipping_type,  str) else "free"
    shipping_info_str  = shipping_info  if isinstance(shipping_info,  str) else ""
    shipping_rate_str  = shipping_rate  if isinstance(shipping_rate,  str) else "0"

    final_shipping_rate = 0.0
    try:
        final_shipping_rate = float(shipping_rate_str or 0)
    except Exception:
        pass

    allowed_shipping = ["free", "flat", "contact", "pickup"]
    final_shipping_type = shipping_type_str if shipping_type_str in allowed_shipping else "free"

    cur.execute("""
    INSERT INTO products (
        user_id,
        name,
        description,
        price,
        stock,
        image_url,
        slug,
        theme,
        views,
        tagline,
        cta,
        source,
        ai_design,
        brand_json,
        store_layout,
        published,
        shipping_type,
        shipping_rate,
        shipping_info
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
    """, (
        user["id"],
        name,
        description,
        final_price,
        final_stock,
        final_image_url,
        final_slug,
        theme,
        tagline,
        final_cta,
        source,
        ai_design,
        default_brand_json,
        "default",
        final_shipping_type,
        final_shipping_rate,
        shipping_info_str
    ))

    store_id = cur.lastrowid

    default_pages = [
        ("About", "about", "about"),
        ("FAQ", "faq", "faq"),
        ("Contact", "contact", "contact")
    ]

    for index, (title, page_slug, page_type) in enumerate(default_pages):
        cur.execute("""
        INSERT INTO store_pages (
            store_id,
            user_id,
            title,
            slug,
            page_type,
            content,
            published,
            sort_order
        )
        VALUES (?, ?, ?, ?, ?, ?, 1, ?)
        """, (
            store_id,
            user["id"],
            title,
            page_slug,
            page_type,
            "",
            index
        ))

    default_sections = [
        {
            "type": "hero",
            "data": {
                "headline": name,
                "subheadline": tagline,
                "description": description
            }
        },
        {
            "type": "featured-products",
            "data": {
                "title": "Featured Products"
            }
        },
        {
            "type": "benefits",
            "data": {
                "title": "Why Choose Us"
            }
        },
        {
            "type": "faq",
            "data": {
                "title": "Frequently Asked Questions"
            }
        }
    ]

    for index, section in enumerate(default_sections):
        cur.execute("""
        INSERT INTO store_sections (
            store_id,
            section_type,
            section_data,
            sort_order,
            visible
        )
        VALUES (?, ?, ?, ?, 1)
        """, (
            store_id,
            section["type"],
            json.dumps(section["data"]),
            index
        ))

    if source == "viral":
        first_product_name = viral_product_name or name
        first_product_description = viral_product_description or tagline or description
        first_product_price = clean_price(viral_product_price or price or "0")
        first_product_stock = clean_stock(viral_product_stock or "10")
        first_product_image = viral_product_image_url or ""

        first_product_images = []
        if first_product_image:
            first_product_images = [first_product_image]

        cur.execute("""
        INSERT INTO store_items (
            store_id,
            user_id,
            name,
            description,
            price,
            stock,
            image_url,
            image_urls
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            store_id,
            user["id"],
            first_product_name,
            first_product_description,
            first_product_price,
            first_product_stock,
            first_product_image,
            json.dumps(first_product_images)
        ))

    if user["is_pro"] == 0 and source == "ai":
        cur.execute(
            "UPDATE users SET ai_uses = COALESCE(ai_uses, 0) + 1 WHERE id = ?",
            (user["id"],)
        )

    conn.commit()
    conn.close()

    if source == "ai":
        return RedirectResponse(f"/stores/{final_slug}/add-product", status_code=303)

    return RedirectResponse(f"/s/{final_slug}", status_code=303)



@app.get("/discover", response_class=HTMLResponse)
def discover(request: Request, q: str = "", type: str = "all"):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    q_clean = q.strip()
    q_url = quote_plus(q_clean)
    search = f"%{q_clean}%"

    conn = db()
    cur = conn.cursor()

    if q_clean:
        cur.execute("""
        SELECT
            store_items.*,
            store_items.id AS item_id,
            products.name AS store_name,
            products.slug AS store_slug,
            products.theme AS store_theme,
            users.store_name AS seller_name
        FROM store_items
        JOIN products ON store_items.store_id = products.id
        JOIN users ON store_items.user_id = users.id
        WHERE products.published = 1
        AND (
            store_items.name LIKE ?
            OR store_items.description LIKE ?
            OR products.name LIKE ?
            OR users.store_name LIKE ?
        )
        ORDER BY store_items.created_at DESC
        """, (search, search, search, search))
    else:
        cur.execute("""
        SELECT
            store_items.*,
            store_items.id AS item_id,
            products.name AS store_name,
            products.slug AS store_slug,
            products.theme AS store_theme,
            users.store_name AS seller_name
        FROM store_items
        JOIN products ON store_items.store_id = products.id
        JOIN users ON store_items.user_id = users.id
        WHERE products.published = 1
        ORDER BY store_items.created_at DESC
        LIMIT 24
        """)

    product_results = cur.fetchall()

    if q_clean:
        cur.execute("""
        SELECT
            products.*,
            users.store_name AS seller_name,
            COALESCE(SUM(orders.quantity), 0) AS total_sold
        FROM products
        JOIN users ON products.user_id = users.id
        LEFT JOIN orders ON orders.product_id = products.id
        WHERE products.published = 1
        AND (
            products.name LIKE ?
            OR products.description LIKE ?
            OR products.tagline LIKE ?
            OR users.store_name LIKE ?
        )
        GROUP BY products.id
        ORDER BY total_sold DESC, products.views DESC, products.id DESC
        """, (search, search, search, search))
    else:
        cur.execute("""
        SELECT
            products.*,
            users.store_name AS seller_name,
            COALESCE(SUM(orders.quantity), 0) AS total_sold
        FROM products
        JOIN users ON products.user_id = users.id
        LEFT JOIN orders ON orders.product_id = products.id
        WHERE products.published = 1
        GROUP BY products.id
        ORDER BY total_sold DESC, products.views DESC, products.id DESC
        LIMIT 18
        """)

    store_results = cur.fetchall()
    conn.close()

    product_cards = ""

    for item in product_results:
        product_cards += f"""
        <div class="product-card">
            <div class="product-info">
                <div class="card-top">
                    <span class="tag">{item["stock"]} in stock</span>
                    <span>${money(item["price"])}</span>
                </div>

                <h3>{item["name"]}</h3>

                <p>{item["description"] or "No description yet."}</p>

                <p class="muted">
                    Store: {item["store_name"] or "LaunchFlow Store"}
                </p>

                <div class="actions">
                    <a class="button small ghost" href="/product/{item["item_id"]}">
                        View Product
                    </a>

                    <a class="button small ghost" href="/s/{item["store_slug"]}">
                        View Store
                    </a>

                    <button
                        type="button"
                        class="button small ghost"
                        onclick="shareLaunchFlowLink(window.location.origin + '/product/{item["item_id"]}', `{item["name"]}`)"
                    >
                        Share
                    </button>
                </div>
            </div>
        </div>
        """

    store_cards = ""

    for p in store_results:
        is_owner = bool(user and user["id"] == p["user_id"])

        message_button = ""

        if not is_owner:
            message_button = f"""
            <button
                type="button"
                class="button small ghost"
                onclick="openSellerChat('{p["user_id"]}', `{p["name"]}`, '{p["id"]}')"
            >
                Message Seller
            </button>
            """

        store_cards += f"""
        <div class="product-card">
            <div class="product-info">
                <div class="card-top">
                    <span class="tag">{p["theme"]}</span>
                    <span>{p["total_sold"] or 0} sold</span>
                </div>

                <h3>{p["name"]}</h3>

                <p>{p["tagline"] or "No tagline yet."}</p>

                <p class="muted">
                    Seller: {p["seller_name"] or "LaunchFlow Seller"}
                </p>

                <div class="actions">
                    <a class="button small ghost" href="/s/{p["slug"]}">
                        View Store
                    </a>

                    {message_button}

                    <button
                        type="button"
                        class="button small ghost"
                        onclick="shareLaunchFlowLink(window.location.origin + '/s/{p["slug"]}', `{p["name"]}`)"
                    >
                        Share
                    </button>
                </div>
            </div>
        </div>
        """

    if not product_cards:
        product_cards = """
        <div class="empty">
            <h2>No products found</h2>
            <p>Try searching for something else.</p>
        </div>
        """

    if not store_cards:
        store_cards = """
        <div class="empty">
            <h2>No stores found</h2>
            <p>Try searching for another store or seller.</p>
        </div>
        """

    products_active = "active" if type == "products" else ""
    stores_active = "active" if type == "stores" else ""
    all_active = "active" if type == "all" else ""

    show_products = type in ["all", "products"]
    show_stores = type in ["all", "stores"]

    return layout(f"""
    <div class="container">
        {top_nav(user)}

        <section class="hero">
            <p class="eyebrow">Marketplace</p>
            <h1>Discover products and stores</h1>
            <p>
                Search products, explore sellers, and find top-performing stores on LaunchFlow.
            </p>

            <form method="get" action="/discover" class="search-form">
                <input
                    type="text"
                    name="q"
                    value="{q_clean}"
                    placeholder="Search products, stores, or sellers..."
                >

                <button type="submit">
                    Search
                </button>
            </form>

            <div class="discover-tabs">
                <a class="button ghost {all_active}" href="/discover?q={q_url}&type=all">
                    All
                </a>

                <a class="button ghost {products_active}" href="/discover?q={q_url}&type=products">
                    Products
                </a>

                <a class="button ghost {stores_active}" href="/discover?q={q_url}&type=stores">
                    Stores
                </a>
            </div>
        </section>

        {f'''
        <section>
            <div class="section-header compact">
                <div>
                    <p class="eyebrow">Products</p>
                    <h2>{'Product results' if q_clean else 'Latest products'}</h2>
                </div>
            </div>

            <div class="grid">
                {product_cards}
            </div>
        </section>
        ''' if show_products else ''}

        {f'''
        <section>
            <div class="section-header compact">
                <div>
                    <p class="eyebrow">Stores</p>
                    <h2>{'Store results' if q_clean else 'Top selling stores'}</h2>
                </div>
            </div>

            <div class="grid">
                {store_cards}
            </div>
        </section>
        ''' if show_stores else ''}
    </div>
    """, title="Discover Stores — LaunchFlow")



# -----------------------------
# AI BUILDER
# -----------------------------
@app.get("/ai-builder", response_class=HTMLResponse)
def ai_builder(request: Request):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    return layout(f"""
    <div class="container narrow">
        {top_nav(user)}
        <a class="back" href="/dashboard">← Dashboard</a>

        <div class="panel">
            <p class="eyebrow">AI store designer</p>
            <h1>Describe what you want to sell</h1>
            <p>The AI will generate your store name, branding, pages, sections, layout direction, and starter storefront.</p>

            <form id="ai-builder-form" action="/ai-generate" method="post">
                <label>What are you selling?</label>
                <textarea name="product_idea" required placeholder="Example: shampoo"></textarea>

                <label>Who is it for?</label>
                <input name="audience" placeholder="Example: people who want clean hair">

                <label>What vibe should the store have?</label>
                <input name="vibe" placeholder="Example: luxury, futuristic, viral TikTok, clean, premium">

                <label>What kind of store should AI build?</label>
                <input name="store_goal" placeholder="Example: AI dropshipping store, fitness brand, skincare landing page">

                <button type="submit">Generate Store with AI ✨</button>
            </form>
        </div>

        <div class="ai-loading" id="aiLoading">
            <div class="loading-card">
                <h2>Building your store ✨</h2>
                <p class="loading-step" id="loadingText">Generating store direction...</p>

                <div class="progress-bar">
                    <div></div>
                </div>

                <p class="tiny">Designing layout · Choosing theme · Writing brand copy</p>
            </div>
        </div>
    </div>

    <script>
        const aiForm = document.getElementById("ai-builder-form");
        const aiLoading = document.getElementById("aiLoading");
        const loadingText = document.getElementById("loadingText");

        const loadingSteps = [
            "Generating store direction...",
            "Choosing visual style...",
            "Creating store pages...",
            "Writing homepage sections...",
            "Finalizing your AI storefront..."
        ];

        aiForm.addEventListener("submit", function(event) {{
            event.preventDefault();
            aiLoading.classList.add("active");

            let step = 0;

            const stepTimer = setInterval(function() {{
                step += 1;
                if (step < loadingSteps.length) {{
                    loadingText.textContent = loadingSteps[step];
                }}
            }}, 900);

            setTimeout(function() {{
                clearInterval(stepTimer);
                aiForm.submit();
            }}, 5000);
        }});
    </script>
    """)


def make_unique_store_identity(name):
    clean_name = (name or "AI Store").strip() or "AI Store"
    base_name = clean_name
    base_slug = slugify(base_name) or "ai-store"

    conn = db()
    cur = conn.cursor()

    final_name = base_name
    final_slug = base_slug
    counter = 2

    while True:
        cur.execute(
            "SELECT id FROM products WHERE LOWER(name) = LOWER(?) OR slug = ?",
            (final_name, final_slug)
        )

        existing = cur.fetchone()

        if not existing:
            break

        final_name = f"{base_name} {counter}"
        final_slug = f"{base_slug}-{counter}"
        counter += 1

    conn.close()

    return final_name, final_slug


@app.post("/ai-generate")
def ai_generate(
    request: Request,
    product_idea: str = Form(...),
    audience: str = Form(""),
    vibe: str = Form(""),
    store_goal: str = Form("")
):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) as count FROM products WHERE user_id = ?", (user["id"],))
    store_count = cur.fetchone()["count"]

    if not user["is_pro"] and store_count >= 1:
        conn.close()
        return RedirectResponse("/upgrade?reason=store_limit", status_code=303)

    ai_uses = user["ai_uses"] if "ai_uses" in user.keys() else 0

    if not user["is_pro"] and ai_uses >= 1:
        conn.close()
        return RedirectResponse("/upgrade?reason=ai_limit", status_code=303)

    conn.close()

    ai_data = demo_ai_generate(product_idea, audience, vibe)

    ai_data["store_goal"] = store_goal
    ai_data["ai_sections"] = [
        {
            "type": "hero",
            "headline": ai_data.get("hero_headline", ""),
            "subheadline": ai_data.get("hero_subheadline", ""),
            "description": ai_data.get("homepage_copy", "")
        },
        {
            "type": "benefits",
            "title": "Why customers want this",
            "items": [
                "Designed for the target audience",
                "Built around a clear brand angle",
                "Optimized for a simple product launch"
            ]
        },
        {
            "type": "faq",
            "title": "Frequently Asked Questions",
            "items": [
                {
                    "question": "What is this store about?",
                    "answer": ai_data.get("homepage_copy", "")
                },
                {
                    "question": "Who is this for?",
                    "answer": audience or "Customers interested in this product."
                }
            ]
        }
    ]

    generated_name = ai_data.get("store_name", product_idea.title() + " Store")
    final_name, final_slug = make_unique_store_identity(generated_name)

    ai_data["store_name"] = final_name
    ai_data["slug"] = final_slug

    ai_design = json.dumps(ai_data)

    return create_store(
        request=request,
        name=final_name,
        slug=final_slug,
        tagline=ai_data.get("tagline", ""),
        description=ai_data.get("homepage_copy", ai_data.get("hero_subheadline", "")),
        price="0",
        stock="0",
        image_url="",
        cta=ai_data.get("cta", "Add Product"),
        theme=ai_data.get("theme", "blue"),
        source="ai",
        ai_design=ai_design
    )


@app.get("/ai-generate")
def ai_generate_get():
    return RedirectResponse("/ai-builder", status_code=303)



@app.post("/stores/{slug}/add-product")
def save_store_product(
    request: Request,
    slug: str,
    name: str = Form(...),
    description: str = Form(""),
    price: str = Form("0"),
    stock: str = Form("0"),
    images: List[UploadFile] = File(default=[])
):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    name = name.strip()[:200]
    description = description.strip()[:2000]

    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT * FROM products WHERE slug = ? AND user_id = ?",
        (slug, user["id"])
    )
    store = cur.fetchone()

    if not store:
        conn.close()
        return RedirectResponse("/dashboard", status_code=303)

    cur.execute(
        "SELECT COUNT(*) as count FROM store_items WHERE store_id = ? AND user_id = ?",
        (store["id"], user["id"])
    )
    product_count = cur.fetchone()["count"]

    if not user["is_pro"] and product_count >= 5:
        conn.close()
        return RedirectResponse("/upgrade?reason=product_limit", status_code=303)

    uploaded_paths = []

    for image in images[:10]:
        if not image or not image.filename:
            continue

        file_ext = os.path.splitext(image.filename)[1].lower()

        if file_ext not in [".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif"]:
            continue

        safe_base = slugify(os.path.splitext(image.filename)[0]) or "product-image"

        try:
            image.file.seek(0)
            img = Image.open(image.file)
            img = ImageOps.exif_transpose(img)

            if img.mode != "RGB":
                img = img.convert("RGB")

            safe_name = f"{random.randint(100000, 999999)}-{safe_base}.jpg"
            file_path = os.path.join(UPLOAD_DIR, safe_name)

            img.save(file_path, "JPEG", quality=95)
            uploaded_paths.append(upload_image(file_path))

        except Exception:
            continue

    main_image = uploaded_paths[0] if uploaded_paths else ""
    product_slug = slugify(name) or f"product-{random.randint(1000, 9999)}"

    cur.execute("""
    INSERT INTO store_items (
        store_id,
        user_id,
        name,
        description,
        price,
        stock,
        image_url,
        image_urls,
        slug,
        featured,
        views
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
    """, (
        store["id"],
        user["id"],
        name.strip(),
        description.strip(),
        clean_price(price),
        clean_stock(stock),
        main_image,
        json.dumps(uploaded_paths),
        product_slug
    ))

    conn.commit()
    conn.close()

    return RedirectResponse(f"/s/{slug}?msg=Product+added!#products", status_code=303)




# -----------------------------
# VIRAL PRODUCTS
# -----------------------------
def get_viral_products():
    extra_products = [
        {
            "niche": "Tech Accessories",
            "name": "Magnetic Cable Organizer",
            "angle": "A clean desk accessory that makes setups look organized and premium",
            "price": "14.99",
            "theme": "blue",
            "store_name": "SetupFlow Essentials",
            "store_tagline": "Clean desk upgrades for organized setups",
            "store_description": "SetupFlow Essentials helps people upgrade their desk, gaming, and work setups with simple products that make everyday spaces look cleaner and more premium."
        },
        {
            "niche": "Beauty",
            "name": "Mini Skincare Fridge",
            "angle": "A cute beauty product for people who want their routine to feel luxury",
            "price": "49.99",
            "theme": "purple",
            "store_name": "GlowRoutine Co.",
            "store_tagline": "Beauty routine upgrades that feel luxury",
            "store_description": "GlowRoutine Co. is built for people who want their skincare and beauty routine to feel cleaner, prettier, and more premium."
        },
        {
            "niche": "Home Fitness",
            "name": "Doorway Stretch Strap",
            "angle": "A simple recovery product for people who stretch, lift, or play sports",
            "price": "24.99",
            "theme": "green",
            "store_name": "FlexDaily Gear",
            "store_tagline": "Simple recovery tools for active people",
            "store_description": "FlexDaily Gear helps athletes, lifters, and active people recover better with simple fitness and mobility products."
        },
    ]

    return (VIRAL_PRODUCTS + extra_products)[:6]


@app.get("/viral-products", response_class=HTMLResponse)
def viral_products(request: Request):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    selected_products = get_viral_products()

    cards = ""

    for p in selected_products:
        cards += f"""
        <div class="viral-card viral-product-card">
            <div class="viral-card-inner">
                <span class="tag viral-tag">{p["niche"]}</span>

                <div>
                    <h2>{p["name"]}</h2>
                    <p>{p["angle"]}</p>
                </div>

                <div class="viral-price-row">
                    <span>Suggested price</span>
                    <strong>${p["price"]}</strong>
                </div>

                <a class="button small viral-generate-btn" href="/viral-generate/{slugify(p["name"])}">
                    Turn Into Store
                </a>
            </div>
        </div>
        """

    return layout(f"""
    <div class="container">
        {top_nav(user)}

        <section class="hero">
            <p class="eyebrow">Viral product engine</p>
            <h1>Find products people already want.</h1>
            <p>Pick a product idea, create a branded store around it, and LaunchFlow will add the starter product automatically.</p>
        </section>

        <section class="viral-grid">
            {cards}
        </section>
    </div>
    """, title="Viral Products")


@app.get("/viral-generate/{viral_slug}", response_class=HTMLResponse)
def viral_generate(request: Request, viral_slug: str):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    selected = None

    for item in get_viral_products():
        if slugify(item["name"]) == viral_slug:
            selected = item
            break

    if not selected:
        return RedirectResponse("/viral-products", status_code=303)

    store_name = selected.get("store_name") or f"{selected['name']} Store"
    store_tagline = selected.get("store_tagline") or selected["angle"]
    store_description = selected.get(
        "store_description",
        f"{store_name} is a focused ecommerce store built around the {selected['niche']} niche."
    )

    product_name = selected["name"]
    product_description = selected["angle"]
    product_price = selected["price"]
    product_image_url = selected.get("image_url", "")

    return layout(f"""
    <div class="container narrow">
        {top_nav(user)}

        <a class="back" href="/viral-products">
            ← Viral Products
        </a>

        <div class="panel">
            <p class="eyebrow">Store draft + starter product</p>

            <h1>{store_name}</h1>

            <p>
                LaunchFlow will create this branded store and automatically add
                <strong>{product_name}</strong> as the first product.
            </p>

            <form action="/create" method="post">
                <input type="hidden" name="source" value="viral">

                <input type="hidden" name="viral_product_name" value="{product_name}">
                <input type="hidden" name="viral_product_description" value="{product_description}">
                <input type="hidden" name="viral_product_price" value="{product_price}">
                <input type="hidden" name="viral_product_stock" value="10">
                <input type="hidden" name="viral_product_image_url" value="{product_image_url}">

                <label>Store name</label>
                <input name="name" value="{store_name}" required>

                <label>Custom URL</label>
                <input name="slug" value="{slugify(store_name)}">

                <label>Store tagline</label>
                <input name="tagline" value="{store_tagline}">

                <label>Store description</label>
                <textarea name="description" required>{store_description}</textarea>

                <label>Starter product</label>
                <input value="{product_name}" disabled>

                <label>Starter product price</label>
                <input value="${product_price}" disabled>

                <label>Theme</label>
                <select name="theme">
                    <option value="blue" {"selected" if selected["theme"] == "blue" else ""}>Blue Glow</option>
                    <option value="purple" {"selected" if selected["theme"] == "purple" else ""}>Purple Creator</option>
                    <option value="green" {"selected" if selected["theme"] == "green" else ""}>Money Green</option>
                    <option value="orange" {"selected" if selected["theme"] == "orange" else ""}>Launch Orange</option>
                    <option value="dark" {"selected" if selected["theme"] == "dark" else ""}>Clean Dark</option>
                </select>

                <input type="hidden" name="price" value="0">
                <input type="hidden" name="stock" value="0">
                <input type="hidden" name="image_url" value="">
                <input type="hidden" name="cta" value="Add Product">

                <button type="submit">
                    Create Store + Starter Product
                </button>
            </form>
        </div>
    </div>
    """, title=store_name)


# -----------------------------
# ADD STORE PRODUCTS
# -----------------------------
@app.get("/stores/{slug}/add-product", response_class=HTMLResponse)
def add_store_product_page(request: Request, slug: str):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT * FROM products WHERE slug = ? AND user_id = ?",
        (slug, user["id"])
    )
    store = cur.fetchone()
    conn.close()

    if not store:
        return layout("<div class='container'><h1>Store not found</h1></div>")

    return layout(f"""
    <div class="container narrow">
        {top_nav(user)}

        <a class="back" href="/s/{store["slug"]}">← Store Preview</a>

        <div class="panel">
            <p class="eyebrow">Next step</p>
            <h1>Add a product</h1>
            <p>This will connect real products to <strong>{store["name"]}</strong>.</p>

            <form action="/stores/{store["slug"]}/add-product" method="post" enctype="multipart/form-data">
                <label>Product Name</label>
                <input name="name" placeholder="Example: Leather Seat Organizer" required>

                <label>Product Description</label>
                <textarea name="description" placeholder="Describe what this product does and why customers want it" required></textarea>

                <label>Price</label>
                <input name="price" class="money-input" placeholder="$24.99" required>

                <label>Stock</label>
                <input name="stock" class="stock-input" placeholder="50" required>

                <label>Product Photos</label>

                <div class="upload-box" onclick="document.getElementById('product-images').click()">
                    <strong>Click to add product photos</strong>
                    <p>Add photos one at a time or all at once. Max 10 photos.</p>
                </div>

                <input
                    id="product-images"
                    name="images"
                    type="file"
                    accept="image/*"
                    multiple
                    style="display:none;"
                >

                <div id="image-preview-grid" class="image-preview-grid"></div>

                <p class="muted">First photo becomes the main product image.</p>

                <button type="submit">Save Product</button>
            </form>
        </div>
    </div>

    <script>
        const input = document.getElementById("product-images");
        const previewGrid = document.getElementById("image-preview-grid");

        let selectedFiles = [];

        function refreshInputFiles() {{
            const dataTransfer = new DataTransfer();

            selectedFiles.slice(0, 10).forEach(file => {{
                dataTransfer.items.add(file);
            }});

            input.files = dataTransfer.files;
        }}

        function renderPreviews() {{
            previewGrid.innerHTML = "";

            selectedFiles.slice(0, 10).forEach((file, index) => {{
                const wrap = document.createElement("div");
                wrap.className = "image-preview-wrap";

                const img = document.createElement("img");
                img.className = "image-preview";

                const removeBtn = document.createElement("button");
                removeBtn.type = "button";
                removeBtn.className = "image-remove-btn";
                removeBtn.textContent = "×";

                removeBtn.addEventListener("click", () => {{
                    selectedFiles.splice(index, 1);
                    refreshInputFiles();
                    renderPreviews();
                }});

                const reader = new FileReader();

                reader.onload = (e) => {{
                    img.src = e.target.result;
                }};

                reader.readAsDataURL(file);

                wrap.appendChild(img);
                wrap.appendChild(removeBtn);
                previewGrid.appendChild(wrap);
            }});
        }}

        input.addEventListener("change", () => {{
            const newFiles = Array.from(input.files);

            newFiles.forEach(file => {{
                if (selectedFiles.length < 10) {{
                    selectedFiles.push(file);
                }}
            }});

            refreshInputFiles();
            renderPreviews();
        }});
    </script>
    """, title=f"Add Product - {store['name']}")




@app.get("/stores/{slug}/pages", response_class=HTMLResponse)
def manage_store_pages(request: Request, slug: str):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT * FROM products WHERE slug = ? AND user_id = ?",
        (slug, user["id"])
    )
    store = cur.fetchone()

    if not store:
        conn.close()
        return RedirectResponse("/dashboard", status_code=303)

    cur.execute("""
    SELECT *
    FROM store_pages
    WHERE store_id = ?
    ORDER BY sort_order ASC, id ASC
    """, (store["id"],))

    pages = cur.fetchall()
    conn.close()

    rows = ""

    for page in pages:
        rows += f"""
        <div class="page-manager-row">
            <div>
                <strong>{page["title"]}</strong>
                <p class="muted">/s/{store["slug"]}/pages/{page["slug"]}</p>
            </div>

            <a class="button small ghost" href="/stores/{store["slug"]}/pages/{page["id"]}/edit">
                Edit
            </a>
        </div>
        """

    if not rows:
        rows = "<p class='muted'>No pages yet.</p>"

    return layout(f"""
    <div class="container narrow">
        {top_nav(user)}

        <a class="back" href="/s/{store["slug"]}">
            ← Store Preview
        </a>

        <div class="panel">
            <p class="eyebrow">Store Pages</p>
            <h1>Manage pages</h1>
            <p class="muted">Add About, FAQ, Contact, and custom pages to your storefront.</p>

            <a class="button ghost" href="/stores/{store["slug"]}/sections">
                Manage Homepage Sections
            </a>

            <form action="/stores/{store["slug"]}/pages" method="post">
                <label>Page title</label>
                <input name="title" placeholder="Example: Shipping Info" required>

                <label>Page content</label>
                <textarea name="content" placeholder="Write your page content..." required></textarea>

                <button type="submit">Add Page</button>
            </form>
        </div>

        <div class="panel">
            <h2>Existing pages</h2>
            {rows}
        </div>
    </div>
    """, title="Manage Store Pages")


@app.post("/stores/{slug}/pages")
def create_store_page(
    request: Request,
    slug: str,
    title: str = Form(...),
    content: str = Form("")
):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    clean_title = title.strip()
    page_slug = slugify(clean_title) or "page"

    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT * FROM products WHERE slug = ? AND user_id = ?",
        (slug, user["id"])
    )
    store = cur.fetchone()

    if not store:
        conn.close()
        return RedirectResponse("/dashboard", status_code=303)

    base_slug = page_slug
    counter = 2

    while True:
        cur.execute(
            "SELECT id FROM store_pages WHERE store_id = ? AND slug = ?",
            (store["id"], page_slug)
        )

        if not cur.fetchone():
            break

        page_slug = f"{base_slug}-{counter}"
        counter += 1

    cur.execute("""
    INSERT INTO store_pages (
        store_id,
        user_id,
        title,
        slug,
        content,
        page_type,
        published,
        sort_order
    )
    VALUES (?, ?, ?, ?, ?, 'custom', 1, 99)
    """, (
        store["id"],
        user["id"],
        clean_title,
        page_slug,
        content.strip()
    ))

    conn.commit()
    conn.close()

    return RedirectResponse(f"/stores/{slug}/pages", status_code=303)


@app.get("/stores/{slug}/pages/{page_id}/edit", response_class=HTMLResponse)
def edit_store_page(request: Request, slug: str, page_id: int):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT * FROM products WHERE slug = ? AND user_id = ?",
        (slug, user["id"])
    )
    store = cur.fetchone()

    if not store:
        conn.close()
        return RedirectResponse("/dashboard", status_code=303)

    cur.execute(
        "SELECT * FROM store_pages WHERE id = ? AND store_id = ?",
        (page_id, store["id"])
    )
    page = cur.fetchone()
    conn.close()

    if not page:
        return RedirectResponse(f"/stores/{slug}/pages", status_code=303)

    return layout(f"""
    <div class="container narrow">
        {top_nav(user)}

        <a class="back" href="/stores/{slug}/pages">
            ← Manage Pages
        </a>

        <div class="panel">
            <p class="eyebrow">Edit Page</p>
            <h1>{page["title"]}</h1>

            <form action="/stores/{slug}/pages/{page_id}/edit" method="post">
                <label>Page title</label>
                <input name="title" value="{page["title"]}" required>

                <label>Page content</label>
                <textarea name="content" required>{page["content"] or ""}</textarea>

                <button type="submit">Save Page</button>
            </form>
        </div>
    </div>
    """, title="Edit Store Page")


@app.post("/stores/{slug}/pages/{page_id}/edit")
def save_store_page(
    request: Request,
    slug: str,
    page_id: int,
    title: str = Form(...),
    content: str = Form("")
):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT * FROM products WHERE slug = ? AND user_id = ?",
        (slug, user["id"])
    )
    store = cur.fetchone()

    if not store:
        conn.close()
        return RedirectResponse("/dashboard", status_code=303)

    cur.execute("""
    UPDATE store_pages
    SET title = ?,
        content = ?
    WHERE id = ?
    AND store_id = ?
    """, (
        title.strip(),
        content.strip(),
        page_id,
        store["id"]
    ))

    conn.commit()
    conn.close()

    return RedirectResponse(f"/stores/{slug}/pages", status_code=303)


@app.get("/s/{slug}/pages/{page_slug}", response_class=HTMLResponse)
def public_store_page(request: Request, slug: str, page_slug: str):
    user = require_user(request)

    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM products WHERE slug = ?", (slug,))
    store = cur.fetchone()

    if not store:
        conn.close()
        return layout("<div class='container'><h1>Store not found</h1></div>")

    is_owner = bool(user and user["id"] == store["user_id"])

    if not is_owner and not store["published"]:
        conn.close()
        return RedirectResponse("/", status_code=303)

    cur.execute("""
    SELECT *
    FROM store_pages
    WHERE store_id = ?
    AND slug = ?
    AND published = 1
    """, (store["id"], page_slug))

    page = cur.fetchone()

    cur.execute("""
    SELECT *
    FROM store_pages
    WHERE store_id = ?
    AND published = 1
    ORDER BY sort_order ASC, id ASC
    """, (store["id"],))

    pages = cur.fetchall()
    conn.close()

    if not page:
        return layout("<div class='container'><h1>Page not found</h1></div>")

    page_links = ""

    for nav_page in pages:
        page_links += f"""
        <a href="/s/{store["slug"]}/pages/{nav_page["slug"]}">
            {nav_page["title"]}
        </a>
        """

    dashboard_link = '<a href="/dashboard">Dashboard</a>' if user else ""

    return layout(f"""
    <div class="public-store theme-{store["theme"]}">
        <nav class="public-store-nav">
            <strong>{store["name"]}</strong>

            <div class="public-store-nav-actions">
                {dashboard_link}
                <a href="/s/{store["slug"]}">Products</a>
                {page_links}
            </div>
        </nav>

        <div class="container narrow">
            <div class="panel">
                <p class="eyebrow">Store Page</p>
                <h1>{page["title"]}</h1>
                <p style="white-space: pre-wrap;">{page["content"]}</p>
            </div>
        </div>
    </div>
    """, title=f"{page['title']} - {store['name']}")

@app.get("/stores/{slug}/sections", response_class=HTMLResponse)
def manage_store_sections(request: Request, slug: str):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT * FROM products WHERE slug = ? AND user_id = ?",
        (slug, user["id"])
    )
    store = cur.fetchone()

    if not store:
        conn.close()
        return RedirectResponse("/dashboard", status_code=303)

    cur.execute("""
    SELECT *
    FROM store_sections
    WHERE store_id = ?
    ORDER BY sort_order ASC, id ASC
    """, (store["id"],))

    sections = cur.fetchall()
    conn.close()

    rows = ""

    for section in sections:
        rows += f"""
        <div class="page-manager-row">
            <div>
                <strong>{section["section_type"].replace("-", " ").title()}</strong>
                <p class="muted">Sort order: {section["sort_order"]} · {"Visible" if section["visible"] else "Hidden"}</p>
            </div>

            <div class="actions">
                <form action="/stores/{store["slug"]}/sections/{section["id"]}/move-up" method="post">
                    <button type="submit" class="button small ghost">
                        ↑
                    </button>
                </form>

                <form action="/stores/{store["slug"]}/sections/{section["id"]}/move-down" method="post">
                    <button type="submit" class="button small ghost">
                        ↓
                    </button>
                </form>

                <a class="button small ghost" href="/stores/{store["slug"]}/sections/{section["id"]}/edit">
                    Edit
                </a>

                <form action="/stores/{store["slug"]}/sections/{section["id"]}/toggle" method="post">
                    <button type="submit" class="button small ghost">
                        {"Hide" if section["visible"] else "Show"}
                    </button>
                </form>
            </div>
        </div>
        """

    if not rows:
        rows = "<p class='muted'>No homepage sections yet.</p>"

    return layout(f"""
    <div class="container narrow">
        {top_nav(user)}

        <a class="back" href="/stores/{store["slug"]}/pages">
            ← Manage Pages
        </a>

        <div class="panel">
            <p class="eyebrow">Homepage Sections</p>
            <h1>Manage sections</h1>
            <p class="muted">Edit AI-generated homepage sections like hero, benefits, FAQ, and trust blocks.</p>

            <div class="hero-actions">
                <a class="button ghost" href="/s/{store["slug"]}">
                    Preview Store
                </a>

                <a class="button ghost" href="/stores/{store["slug"]}/pages">
                    Manage Pages
                </a>
            </div>
        </div>

        <div class="panel">
            {rows}
        </div>
    </div>
    """, title="Manage Sections")


@app.get("/stores/{slug}/sections/{section_id}/edit", response_class=HTMLResponse)
def edit_store_section(request: Request, slug: str, section_id: int):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT * FROM products WHERE slug = ? AND user_id = ?",
        (slug, user["id"])
    )
    store = cur.fetchone()

    if not store:
        conn.close()
        return RedirectResponse("/dashboard", status_code=303)

    cur.execute(
        "SELECT * FROM store_sections WHERE id = ? AND store_id = ?",
        (section_id, store["id"])
    )
    section = cur.fetchone()
    conn.close()

    if not section:
        return RedirectResponse(f"/stores/{slug}/sections", status_code=303)

    try:
        data = json.loads(section["section_data"] or "{}")
    except Exception:
        data = {}

    return layout(f"""
    <div class="container narrow">
        {top_nav(user)}

        <a class="back" href="/stores/{slug}/sections">
            ← Manage Sections
        </a>

        <div class="panel">
            <p class="eyebrow">Edit Section</p>
            <h1>{section["section_type"].replace("-", " ").title()}</h1>

            <form action="/stores/{slug}/sections/{section_id}/edit" method="post">
                <label>Section title</label>
                <input name="section_title" value="{data.get("title", section["section_type"].replace("-", " ").title())}">

                <label>Headline</label>
                <input name="headline" value="{data.get("headline", "")}">

                <label>Subheadline</label>
                <input name="subheadline" value="{data.get("subheadline", "")}">

                <label>Description</label>
                <textarea name="description">{data.get("description", "")}</textarea>

                <label>Items</label>
                <textarea name="items" placeholder="One item per line">{chr(10).join(data.get("items", [])) if isinstance(data.get("items", []), list) else ""}</textarea>

                <button type="submit">Save Section</button>
            </form>
        </div>
    </div>
    """, title="Edit Section")


@app.post("/stores/{slug}/sections/{section_id}/edit")
def save_store_section(
    request: Request,
    slug: str,
    section_id: int,
    section_title: str = Form(""),
    headline: str = Form(""),
    subheadline: str = Form(""),
    description: str = Form(""),
    items: str = Form("")
):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    item_list = [
        line.strip()
        for line in items.splitlines()
        if line.strip()
    ]

    section_data = {
        "title": section_title.strip(),
        "headline": headline.strip(),
        "subheadline": subheadline.strip(),
        "description": description.strip(),
        "items": item_list
    }

    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT * FROM products WHERE slug = ? AND user_id = ?",
        (slug, user["id"])
    )
    store = cur.fetchone()

    if not store:
        conn.close()
        return RedirectResponse("/dashboard", status_code=303)

    cur.execute("""
    UPDATE store_sections
    SET section_data = ?
    WHERE id = ?
    AND store_id = ?
    """, (
        json.dumps(section_data),
        section_id,
        store["id"]
    ))

    conn.commit()
    conn.close()

    return RedirectResponse(f"/stores/{slug}/sections", status_code=303)


@app.post("/stores/{slug}/sections/{section_id}/toggle")
def toggle_store_section(request: Request, slug: str, section_id: int):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT * FROM products WHERE slug = ? AND user_id = ?",
        (slug, user["id"])
    )
    store = cur.fetchone()

    if not store:
        conn.close()
        return RedirectResponse("/dashboard", status_code=303)

    cur.execute("""
    UPDATE store_sections
    SET visible = CASE WHEN visible = 1 THEN 0 ELSE 1 END
    WHERE id = ?
    AND store_id = ?
    """, (
        section_id,
        store["id"]
    ))

    conn.commit()
    conn.close()

    return RedirectResponse(f"/stores/{slug}/sections", status_code=303)


@app.post("/stores/{slug}/sections/{section_id}/move-up")
def move_section_up(request: Request, slug: str, section_id: int):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT * FROM products WHERE slug = ? AND user_id = ?",
        (slug, user["id"])
    )
    store = cur.fetchone()

    if not store:
        conn.close()
        return RedirectResponse("/dashboard", status_code=303)

    cur.execute(
        "SELECT * FROM store_sections WHERE id = ? AND store_id = ?",
        (section_id, store["id"])
    )
    section = cur.fetchone()

    if section:
        current_order = section["sort_order"] or 0
        cur.execute(
            "SELECT * FROM store_sections WHERE store_id = ? AND sort_order < ? ORDER BY sort_order DESC LIMIT 1",
            (store["id"], current_order)
        )
        above = cur.fetchone()
        if above:
            cur.execute(
                "UPDATE store_sections SET sort_order = ? WHERE id = ? AND store_id = ?",
                (above["sort_order"], section_id, store["id"])
            )
            cur.execute(
                "UPDATE store_sections SET sort_order = ? WHERE id = ? AND store_id = ?",
                (current_order, above["id"], store["id"])
            )

    conn.commit()
    conn.close()

    return RedirectResponse(f"/stores/{slug}/sections", status_code=303)


@app.post("/stores/{slug}/sections/{section_id}/move-down")
def move_section_down(request: Request, slug: str, section_id: int):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT * FROM products WHERE slug = ? AND user_id = ?",
        (slug, user["id"])
    )
    store = cur.fetchone()

    if not store:
        conn.close()
        return RedirectResponse("/dashboard", status_code=303)

    cur.execute(
        "SELECT * FROM store_sections WHERE id = ? AND store_id = ?",
        (section_id, store["id"])
    )
    section = cur.fetchone()

    if section:
        current_order = section["sort_order"] or 0
        cur.execute(
            "SELECT * FROM store_sections WHERE store_id = ? AND sort_order > ? ORDER BY sort_order ASC LIMIT 1",
            (store["id"], current_order)
        )
        below = cur.fetchone()
        if below:
            cur.execute(
                "UPDATE store_sections SET sort_order = ? WHERE id = ? AND store_id = ?",
                (below["sort_order"], section_id, store["id"])
            )
            cur.execute(
                "UPDATE store_sections SET sort_order = ? WHERE id = ? AND store_id = ?",
                (current_order, below["id"], store["id"])
            )

    conn.commit()
    conn.close()

    return RedirectResponse(f"/stores/{slug}/sections", status_code=303)


@app.post("/stores/{slug}/products/{item_id}/move-up")
def move_product_up(request: Request, slug: str, item_id: int):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM products WHERE slug = ? AND user_id = ?", (slug, user["id"]))
    store = cur.fetchone()
    if not store:
        conn.close()
        return RedirectResponse("/dashboard", status_code=303)
    cur.execute("SELECT * FROM store_items WHERE id = ? AND store_id = ?", (item_id, store["id"]))
    item = cur.fetchone()
    if item:
        current_order = item["sort_order"] or 0
        cur.execute(
            "SELECT * FROM store_items WHERE store_id = ? AND sort_order < ? ORDER BY sort_order DESC LIMIT 1",
            (store["id"], current_order)
        )
        above = cur.fetchone()
        if above:
            cur.execute("UPDATE store_items SET sort_order = ? WHERE id = ?", (above["sort_order"], item_id))
            cur.execute("UPDATE store_items SET sort_order = ? WHERE id = ?", (current_order, above["id"]))
    conn.commit()
    conn.close()
    return RedirectResponse(f"/s/{slug}", status_code=303)


@app.post("/stores/{slug}/products/{item_id}/move-down")
def move_product_down(request: Request, slug: str, item_id: int):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM products WHERE slug = ? AND user_id = ?", (slug, user["id"]))
    store = cur.fetchone()
    if not store:
        conn.close()
        return RedirectResponse("/dashboard", status_code=303)
    cur.execute("SELECT * FROM store_items WHERE id = ? AND store_id = ?", (item_id, store["id"]))
    item = cur.fetchone()
    if item:
        current_order = item["sort_order"] or 0
        cur.execute(
            "SELECT * FROM store_items WHERE store_id = ? AND sort_order > ? ORDER BY sort_order ASC LIMIT 1",
            (store["id"], current_order)
        )
        below = cur.fetchone()
        if below:
            cur.execute("UPDATE store_items SET sort_order = ? WHERE id = ?", (below["sort_order"], item_id))
            cur.execute("UPDATE store_items SET sort_order = ? WHERE id = ?", (current_order, below["id"]))
    conn.commit()
    conn.close()
    return RedirectResponse(f"/s/{slug}", status_code=303)


@app.get("/upgrade", response_class=HTMLResponse)
def upgrade_page(request: Request, reason: str = ""):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    if user["is_pro"]:
        return RedirectResponse("/settings", status_code=303)

    reason_text = ""

    if reason == "store_limit":
        reason_text = "You reached the free store limit. Upgrade to create more stores."
    elif reason == "ai_limit":
        reason_text = "You used your free AI generation. Upgrade for more AI-powered stores."

    return layout(f"""
    <div class="container narrow">
        {top_nav(user)}

        <section class="upgrade-hero">
            <p class="eyebrow">Premium</p>

            <h1>Unlock the full LaunchFlow system.</h1>

            <p>
                {reason_text or "Upgrade once and get access to more stores, AI tools, premium templates, and advanced selling features."}
            </p>

            <div class="premium-feature-card">
                <h2>Premium includes</h2>

                <div class="premium-feature-list">
                    <div class="premium-feature-item">More stores</div>
                    <div class="premium-feature-item">AI store generation</div>
                    <div class="premium-feature-item">Premium templates</div>
                    <div class="premium-feature-item">Advanced store tools</div>
                    <div class="premium-feature-item">Better customization</div>
                    <div class="premium-feature-item">Seller growth features</div>
                    <div class="premium-feature-item">Priority improvements</div>
                </div>

                <form action="/create-checkout-session" method="post">
                    <button type="submit">Upgrade to Premium</button>
                </form>

                <a class="button ghost" href="/dashboard">
                    Back to Dashboard
                </a>
            </div>
        </section>
    </div>
    """, title="Go Premium — LaunchFlow")

# -----------------------------
# PUBLIC STORE
# -----------------------------
@app.get("/s/{slug}", response_class=HTMLResponse)
def public_store(request: Request, slug: str):
    user = require_user(request)

    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM products WHERE slug = ?", (slug,))
    p = cur.fetchone()

    if not p:
        conn.close()
        return layout("""
        <div class="container">
            <div class="empty-state">
                <h1>Store not found</h1>
                <p>This storefront does not exist.</p>
            </div>
        </div>
        """)

    is_owner = bool(user and user["id"] == p["user_id"])

    if not is_owner and not p["published"]:
        conn.close()
        return layout("""
        <div class="container narrow center">
            <div class="panel">
                <h1>This store is not published yet</h1>
                <p>The owner has not made this store public.</p>
                <a class="button" href="/">Back home</a>
            </div>
        </div>
        """)

    cur.execute(
        "UPDATE products SET views = COALESCE(views, 0) + 1 WHERE id = ?",
        (p["id"],)
    )
    conn.commit()

    cur.execute("SELECT * FROM products WHERE id = ?", (p["id"],))
    p = cur.fetchone()

    cur.execute("""
    SELECT *
    FROM store_items
    WHERE store_id = ?
    ORDER BY sort_order ASC, id ASC
    """, (p["id"],))
    store_items = cur.fetchall()

    cur.execute("""
    SELECT *
    FROM store_pages
    WHERE store_id = ?
    AND published = 1
    ORDER BY sort_order ASC, id ASC
    """, (p["id"],))
    store_pages = cur.fetchall()

    cur.execute("""
    SELECT *
    FROM store_sections
    WHERE store_id = ?
    AND visible = 1
    ORDER BY sort_order ASC, id ASC
    """, (p["id"],))
    store_sections = cur.fetchall()

    conn.close()

    ai_design = {}

    try:
        ai_design = json.loads(p["ai_design"] or "{}")
    except Exception:
        ai_design = {}

    design_style = ai_design.get("design_style", "glass")
    hero_layout = ai_design.get("hero_layout", "split")
    card_style = ai_design.get("card_style", "glass")
    button_style = ai_design.get("button_style", "rounded")
    background_style = ai_design.get("background_style", "gradient")
    section_style = ai_design.get("section_style", "cards")
    font_style = ai_design.get("font_style", "modern")
    accent_color = ai_design.get("accent_color", "#7c3aed")
    secondary_color = ai_design.get("secondary_color", "#06b6d4")

    trust_badges = ai_design.get(
        "trust_badges",
        ["Premium storefront", "Ready to sell", "Secure checkout"]
    )

    hero_section_data = {}

    for section in store_sections:
        if section["section_type"] == "hero":
            try:
                hero_section_data = json.loads(section["section_data"] or "{}")
            except Exception:
                hero_section_data = {}
            break

    hero_headline = (
        hero_section_data.get("headline")
        or ai_design.get("hero_headline")
        or p["name"]
    )

    hero_subheadline = (
        hero_section_data.get("subheadline")
        or p["tagline"]
        or ai_design.get("hero_subheadline")
        or ""
    )

    hero_description = (
        hero_section_data.get("description")
        or p["description"]
        or ai_design.get("homepage_copy")
        or ""
    )

    # Shipping badge
    store_shipping_type = p["shipping_type"] if "shipping_type" in p.keys() else "free"
    store_shipping_rate = p["shipping_rate"] if "shipping_rate" in p.keys() else 0
    store_shipping_info = p["shipping_info"] if "shipping_info" in p.keys() else ""

    shipping_label = {
        "free":    "Free Shipping",
        "flat":    f"Shipping: ${money(store_shipping_rate)}",
        "contact": "Contact for Shipping",
        "pickup":  "Local Pickup Only",
    }.get(store_shipping_type or "free", "Free Shipping")

    shipping_note = store_shipping_info or {
        "free":    "On all orders",
        "flat":    "Flat rate on all orders",
        "contact": "Message seller for rates",
        "pickup":  "In-store pickup only",
    }.get(store_shipping_type or "free", "On all orders")

    badge_html = f"""
    <div>
        <strong>{shipping_label}</strong>
        <span>{shipping_note}</span>
    </div>
    """

    for badge in trust_badges[:2]:
        badge_html += f"""
        <div>
            <strong>{badge}</strong>
            <span>Built with LaunchFlow</span>
        </div>
        """

    ai_section_html = ""

    for section in store_sections:
        try:
            section_data = json.loads(section["section_data"] or "{}")
        except Exception:
            section_data = {}

        section_type = section["section_type"]

        if section_type in ["hero", "featured-products"]:
            continue

        if section_type == "benefits":
            items = section_data.get("items", trust_badges)
            cards = ""

            for item in items[:4]:
                cards += f"""
                <div>
                    <strong>{item}</strong>
                    <span>Built for better shopping</span>
                </div>
                """

            ai_section_html += f"""
            <section class="public-store-section ai-brand-section">
                <p class="eyebrow">Why us</p>
                <h2>{section_data.get("title", "Why Customers Choose Us")}</h2>
                <div class="public-store-grid">
                    {cards}
                </div>
            </section>
            """

        elif section_type == "faq":
            ai_section_html += f"""
            <section class="public-store-section ai-brand-section">
                <p class="eyebrow">FAQ</p>
                <h2>{section_data.get("title", "Frequently Asked Questions")}</h2>
                <p>{section_data.get("description", "Questions about products, shipping, and support can be handled through LaunchFlow messages.")}</p>
            </section>
            """

    owner_controls = ""

    if is_owner:
        if p["published"]:
            owner_controls = f"""
            <div class="owner-banner live">
                <div>
                    <strong>Store is live</strong>
                    <p>Customers can currently access this storefront.</p>
                </div>

                <form action="/unpublish-store/{p["id"]}" method="post">
                    <button type="submit">Unpublish Store</button>
                </form>
            </div>
            """
        else:
            owner_controls = f"""
            <div class="owner-banner draft">
                <div>
                    <strong>Draft Store</strong>
                    <p>Only you can currently see this storefront.</p>
                </div>

                <form action="/publish-store/{p["id"]}" method="post">
                    <button type="submit">Publish Store</button>
                </form>
            </div>
            """

    product_html = ""

    if store_items:
        for item in store_items:
            try:
                image_list = json.loads(item["image_urls"] or "[]")
            except Exception:
                image_list = []

            if not image_list and item["image_url"]:
                image_list = [item["image_url"]]

            item_image = (
                image_list[0]
                if image_list
                else "https://images.unsplash.com/photo-1523275335684-37898b6baf30?auto=format&fit=crop&w=900&q=80"
            )

            edit_product_button = ""

            if is_owner:
                edit_product_button = f"""
                <a class="button small ghost" href="/product/{item["id"]}/edit">
                    Edit Product
                </a>
                <form method="post" action="/stores/{p["slug"]}/products/{item["id"]}/move-up" style="display:inline">
                    <button type="submit" class="button small ghost" title="Move Up">↑</button>
                </form>
                <form method="post" action="/stores/{p["slug"]}/products/{item["id"]}/move-down" style="display:inline">
                    <button type="submit" class="button small ghost" title="Move Down">↓</button>
                </form>
                """

            message_button = ""

            if not is_owner:
                message_button = f"""
                <button
                    type="button"
                    class="button small ghost"
                    onclick="openSellerChat('{p["user_id"]}', '{p["name"]}', '{p["id"]}')"
                >
                    Message Seller
                </button>
                """

            product_html += f"""
            <div class="storefront-product-card ai-card-{card_style}">
                <a href="/product/{item["id"]}" class="storefront-image-wrap">
                    <img
                        src="{item_image}"
                        onerror="this.src='https://images.unsplash.com/photo-1523275335684-37898b6baf30?auto=format&fit=crop&w=900&q=80'"
                    >
                </a>

                <div class="storefront-product-info">
                    <div class="storefront-product-top">
                        <span class="tag">{item["stock"]} in stock</span>
                        <strong>${money(item["price"])}</strong>
                    </div>

                    <h3>{item["name"]}</h3>
                    <p>{item["description"]}</p>

                    <div class="storefront-product-actions">
                        <a class="button small" href="/product/{item["id"]}">
                            View Product
                        </a>

                        {message_button}
                        {edit_product_button}

                        <button
                            type="button"
                            class="button small ghost"
                            onclick="shareLaunchFlowLink(window.location.origin + '/product/{item["id"]}', '{item["name"]}')"
                        >
                            Share
                        </button>
                    </div>
                </div>
            </div>
            """

    else:
        product_html = """
        <div class="empty-state">
            <h2>No products yet</h2>
            <p>This store has not added products yet.</p>
        </div>
        """

    dashboard_link = '<a href="/dashboard">Dashboard</a>' if user else ""
    add_product_link = f'<a href="/stores/{p["slug"]}/add-product">Add Product</a>' if is_owner else ""

    page_links = ""

    for page in store_pages:
        page_links += f"""
        <a href="/s/{p["slug"]}/pages/{page["slug"]}">
            {page["title"]}
        </a>
        """

    if is_owner:
        hero_action = f"""
        <a class="button" href="/stores/{p["slug"]}/add-product">
            Add Product
        </a>
        """
    else:
        hero_action = f"""
        <button
            type="button"
            class="button"
            onclick="openSellerChat('{p["user_id"]}', '{p["name"]}', '{p["id"]}')"
        >
            Message Seller
        </button>
        """

    store_css = generate_store_css(ai_design) if ai_design else ""

    return layout(store_css + f"""
    <div class="public-store ai-custom-store" style="--ai-accent:{accent_color}; --ai-secondary:{secondary_color};">


        <nav class="public-store-nav">
            <strong>{p["name"]}</strong>

            <div class="public-store-nav-actions">
                {dashboard_link}
                <a href="#products">Products</a>
                {page_links}
                {add_product_link}
            </div>
        </nav>

        <section class="storefront-hero ai-section-{section_style}">
            <div class="storefront-hero-content">
                {f'<span class="store-brand-badge">{ai_design.get("primary_category") or ai_design.get("brand_vibe") or "Storefront"}</span>' if ai_design else '<p class="eyebrow">Storefront</p>'}

                <h1>{hero_headline}</h1>

                <h2>{hero_subheadline}</h2>

                <p>{hero_description}</p>

                <div class="storefront-stats ai-trust-row">
                    {badge_html}
                </div>

                <div class="storefront-stats">
                    <div>
                        <strong>{len(store_items)}</strong>
                        <span>Products</span>
                    </div>

                    <div>
                        <strong>{p["views"] or 0}</strong>
                        <span>Views</span>
                    </div>

                    <div>
                        <strong>{"Live" if p["published"] else "Draft"}</strong>
                        <span>Status</span>
                    </div>
                </div>

                <div class="storefront-hero-actions">
                    {hero_action}

                    <button
                        type="button"
                        class="button ghost"
                        onclick="shareLaunchFlowLink(window.location.origin + '/s/{p["slug"]}', '{p["name"]}')"
                    >
                        Share Store
                    </button>
                </div>
            </div>
        </section>

        <div class="container narrow">
            {owner_controls}

            {f'''
            <div class="owner-banner draft">
                <div>
                    <strong>Store Pages</strong>
                    <p>Add About, FAQ, Contact, policies, and custom pages.</p>
                </div>

                <a class="button ghost" href="/stores/{p["slug"]}/pages">
                    Manage Pages
                </a>

                <a class="button ghost" href="/stores/{p["slug"]}/sections">
                    Manage Sections
                </a>
            </div>
            ''' if is_owner else ''}
        </div>

        {ai_section_html}

        <section class="public-store-section" id="products">
            <div class="section-header compact">
                <div>
                    <p class="eyebrow">Catalog</p>
                    <h2>Products</h2>
                </div>
            </div>

            <div class="storefront-grid">
                {product_html}
            </div>
        </section>
    </div>
    """,
    title=f"{p['name']} — Shop on LaunchFlow",
    description=p["tagline"] or hero_description or f"Shop {p['name']} on LaunchFlow.",
    og_image=p["image_url"] or ""
    )


@app.post("/publish-store/{store_id}")
def publish_store(store_id: int, request: Request):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM products WHERE id = ? AND user_id = ?", (store_id, user["id"]))
    store = cur.fetchone()

    if not store:
        conn.close()
        return RedirectResponse("/dashboard", status_code=303)

    new_status = 0 if store["published"] else 1
    cur.execute(
        "UPDATE products SET published = ? WHERE id = ? AND user_id = ?",
        (new_status, store_id, user["id"])
    )

    conn.commit()
    conn.close()

    msg = "Store+published!" if new_status else "Store+unpublished."
    return RedirectResponse(f"/dashboard?msg={msg}", status_code=303)


@app.post("/unpublish-store/{store_id}")
def unpublish_store(store_id: int, request: Request):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    conn = db()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE products
        SET published = 0
        WHERE id = ? AND user_id = ?
        """,
        (store_id, user["id"])
    )

    cur.execute("SELECT slug FROM products WHERE id = ? AND user_id = ?", (store_id, user["id"]))
    store = cur.fetchone()

    conn.commit()
    conn.close()

    if not store:
        return RedirectResponse("/dashboard", status_code=303)

    return RedirectResponse(f"/s/{store['slug']}", status_code=303)


@app.get("/product/{item_id}", response_class=HTMLResponse)
def product_detail(request: Request, item_id: int):

    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM store_items WHERE id = ?", (item_id,))
    item = cur.fetchone()

    if not item:
        conn.close()
        return layout("""
        <div class="container">
            <div class="panel">
                <h1>Product not found</h1>
            </div>
        </div>
        """)

    cur.execute("SELECT * FROM products WHERE id = ?", (item["store_id"],))
    store = cur.fetchone()

    cur.execute("SELECT * FROM users WHERE id = ?", (item["user_id"],))
    seller = cur.fetchone()

    conn.close()

    try:
        image_list = json.loads(item["image_urls"] or "[]")
    except Exception:
        image_list = []

    if not image_list and item["image_url"]:
        image_list = [item["image_url"]]

    main_image = image_list[0] if image_list else "https://images.unsplash.com/photo-1523275335684-37898b6baf30?auto=format&fit=crop&w=900&q=80"

    gallery_html = ""

    for img in image_list:
        gallery_html += f"""
        <img
            src="{img}"
            class="product-detail-thumb"
            onclick="document.getElementById('main-product-image').src=this.src"
        >
        """

    user = require_user(request)
    is_owner = bool(user and user["id"] == item["user_id"])

    owner_buttons = ""

    if is_owner:
        owner_buttons = f"""
        <div class="product-owner-actions">
            <a href="/product/{item["id"]}/edit" class="button">Edit Product</a>

            <form
                action="/product/{item["id"]}/delete"
                method="post"
                onsubmit="return confirm('Delete this product?')"
            >
                <button type="submit" class="delete-product-btn">
                    Delete Product
                </button>
            </form>
        </div>
        """

    stock_text = "Sold out" if item["stock"] <= 0 else f'{item["stock"]} left in stock'
    buy_disabled = "disabled" if item["stock"] <= 0 else ""

    message_button = ""

    if not is_owner:
        message_button = f"""
        <button
            type="button"
            class="button ghost"
            onclick="openSellerChat('{seller["id"]}', '{store["name"]}', '{store["id"]}')"
        >
            Message Seller
        </button>
        """

    product_share_url = f"/product/{item['id']}"

    share_buttons = f"""
    <div class="share-row">
        <button
            type="button"
            class="button ghost"
            onclick="shareLaunchFlowLink(window.location.origin + '{product_share_url}', '{item["name"]}')"
        >
            Share Product
        </button>

        <button
            type="button"
            class="button ghost"
            onclick="copyLaunchFlowLink(window.location.origin + '{product_share_url}')"
        >
            Copy Link
        </button>
    </div>
    """

    html = f"""
    <div class="container">

        <a class="back" href="/s/{store["slug"]}">
            ← Back to Store
        </a>

        <div class="product-detail-shell">

            <div class="product-detail-media panel">
                <img
                    id="main-product-image"
                    src="{main_image}"
                    class="product-detail-main-img"
                >

                <div class="product-detail-gallery">
                    {gallery_html}
                </div>
            </div>

            <div class="product-detail-info panel">
                <p class="eyebrow">Product</p>

                <h1>{item["name"]}</h1>

                <p class="product-detail-description">
                    {item["description"]}
                </p>

                <div class="product-detail-meta">
                    <div>
                        <span>Price</span>
                        <strong>${money(item["price"])}</strong>
                    </div>

                    <div>
                        <span>Availability</span>
                        <strong>{stock_text}</strong>
                    </div>
                </div>

                <div class="product-seller-box">
                    <div>
                        <strong>Sold by {store["name"]}</strong>

                        <p class="muted">
                            Secure checkout through LaunchFlow
                        </p>
                    </div>

                    {message_button}
                </div>

                {share_buttons}

                <form
                    action="/checkout-item/{item["id"]}"
                    method="post"
                    class="buy-form product-buy-box"
                >
                    <label>Your email</label>

                    <input
                        name="customer_email"
                        type="email"
                        required
                        placeholder="you@example.com"
                    >

                    <label>Quantity</label>

                    <input
                        type="number"
                        name="quantity"
                        min="1"
                        max="{item["stock"]}"
                        value="1"
                    >

                    <button type="submit" {buy_disabled}>
                        {"Sold Out" if item["stock"] <= 0 else "Buy Now"}
                    </button>

                    <button
                        type="submit"
                        formaction="/cart/add/{item["id"]}"
                        class="button ghost"
                        formnovalidate
                        {buy_disabled}
                    >
                        Add to Cart
                    </button>
                </form>

                <div class="product-detail-note">
                    <strong>Secure checkout</strong>

                    <p>
                        Payments are processed securely through LaunchFlow checkout.
                    </p>
                </div>

                {owner_buttons}
            </div>
        </div>
    </div>
    """

    return layout(html, title=item["name"])
@app.post("/cart/add/{item_id}")
def add_to_cart(
    request: Request,
    item_id: int,
    quantity: int = Form(1)
):
    user = require_user(request)

    quantity = max(1, int(quantity))

    session_id = request.cookies.get("launchflow_cart_id")

    if not session_id:
        session_id = str(uuid.uuid4())

    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM store_items WHERE id = ?", (item_id,))
    item = cur.fetchone()

    if not item:
        conn.close()
        return RedirectResponse("/", status_code=303)

    cur.execute("""
    SELECT *
    FROM cart_items
    WHERE store_item_id = ?
    AND (
        session_id = ?
        OR user_id = ?
    )
    """, (
        item_id,
        session_id,
        user["id"] if user else 0
    ))

    existing = cur.fetchone()

    if existing:
        cur.execute("""
        UPDATE cart_items
        SET quantity = quantity + ?
        WHERE id = ?
        """, (quantity, existing["id"]))
    else:
        cur.execute("""
        INSERT INTO cart_items (
            session_id,
            user_id,
            store_item_id,
            quantity
        )
        VALUES (?, ?, ?, ?)
        """, (
            session_id,
            user["id"] if user else 0,
            item_id,
            quantity
        ))

    conn.commit()
    conn.close()

    response = RedirectResponse("/cart", status_code=303)
    response.set_cookie("launchflow_cart_id", session_id, max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax")

    return response

# -----------------------------
# EDIT / DELETE
# -----------------------------
@app.get("/edit/{product_id}", response_class=HTMLResponse)
def edit(request: Request, product_id: int):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM products WHERE id = ? AND user_id = ?", (product_id, user["id"]))
    p = cur.fetchone()
    conn.close()

    if not p:
        return layout("<div class='container'><h1>Store not found</h1></div>")

    try:
        ai_design_data = json.loads(p["ai_design"] or "{}")
    except Exception:
        ai_design_data = {}

    hero_headline_val = ai_design_data.get("hero_headline", "")
    hero_subheadline_val = ai_design_data.get("hero_subheadline", "")

    themes = ["blue", "purple", "green", "orange", "dark"]
    options = ""

    for t in themes:
        selected = "selected" if p["theme"] == t else ""
        options += f'<option value="{t}" {selected}>{t.title()}</option>'

    return layout(f"""
    <div class="container narrow">
        {top_nav(user)}
        <a class="back" href="/dashboard">← Dashboard</a>

        <div class="panel">
            <p class="eyebrow">Editor</p>
            <h1>Edit store</h1>

            <div class="hero-actions">
                <a class="button ghost" href="/stores/{p["slug"]}/pages">
                    Manage Pages
                </a>

                <a class="button ghost" href="/s/{p["slug"]}">
                    Preview Store
                </a>
            </div>

            <form action="/update/{p["id"]}" method="post">
                <label>Store name</label>
                <input name="name" value="{p["name"]}" required>

                <label>Custom URL</label>
                <input name="slug" value="{p["slug"]}" required>

                <label>Tagline</label>
                <input name="tagline" value="{p["tagline"] or ""}">

                <label>Description</label>
                <textarea name="description" required>{p["description"]}</textarea>

                <label>Hero headline</label>
                <input name="hero_headline" value="{hero_headline_val}" placeholder="e.g. The last bag you'll ever need">

                <label>Hero subheadline</label>
                <input name="hero_subheadline" value="{hero_subheadline_val}" placeholder="e.g. Premium quality at an honest price">

                <label>Button text</label>
                <input name="cta" value="{p["cta"] or "Add Product"}">

                <label>Theme</label>
                <select name="theme">
                    {options}
                </select>

                <button type="submit">Save Changes</button>
            </form>
        </div>
    </div>
    """)



@app.post("/update/{product_id}")
def update(
    request: Request,
    product_id: int,
    name: str = Form(...),
    slug: str = Form(...),
    tagline: str = Form(""),
    description: str = Form(...),
    hero_headline: str = Form(""),
    hero_subheadline: str = Form(""),
    cta: str = Form("Add Product"),
    theme: str = Form("blue")
):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    final_slug = unique_slug(slugify(slug), product_id=product_id)

    conn = db()
    cur = conn.cursor()

    # Merge hero headline/subheadline into the existing ai_design JSON
    cur.execute("SELECT ai_design FROM products WHERE id = ? AND user_id = ?", (product_id, user["id"]))
    row = cur.fetchone()
    try:
        ai_design_data = json.loads(row["ai_design"] or "{}") if row else {}
    except Exception:
        ai_design_data = {}

    ai_design_data["hero_headline"] = hero_headline
    ai_design_data["hero_subheadline"] = hero_subheadline

    cur.execute("""
    UPDATE products
    SET name = ?, slug = ?, tagline = ?, description = ?, cta = ?, theme = ?, ai_design = ?
    WHERE id = ? AND user_id = ?
    """, (name, final_slug, tagline, description, cta, theme, json.dumps(ai_design_data), product_id, user["id"]))

    conn.commit()
    conn.close()

    return RedirectResponse(f"/s/{final_slug}", status_code=303)


@app.post("/delete/{product_id}")
def delete_product(request: Request, product_id: int):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    conn = db()
    cur = conn.cursor()

    cur.execute("DELETE FROM orders WHERE product_id = ?", (product_id,))
    cur.execute("DELETE FROM store_items WHERE store_id = ?", (product_id,))
    cur.execute("DELETE FROM store_pages WHERE store_id = ?", (product_id,))
    cur.execute("DELETE FROM store_sections WHERE store_id = ?", (product_id,))
    cur.execute("DELETE FROM conversations WHERE store_id = ?", (product_id,))
    cur.execute("DELETE FROM products WHERE id = ? AND user_id = ?", (product_id, user["id"]))

    conn.commit()
    conn.close()

    return RedirectResponse("/dashboard", status_code=303)


# -----------------------------
# ANALYTICS / ORDERS / SETTINGS
# -----------------------------
@app.get("/analytics", response_class=HTMLResponse)
def analytics(request: Request):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) as count FROM products WHERE user_id = ?", (user["id"],))
    stores = cur.fetchone()["count"]

    cur.execute("""
    SELECT COUNT(*) as count
    FROM orders
    JOIN products ON orders.product_id = products.id
    WHERE products.user_id = ?
    """, (user["id"],))
    orders_count = cur.fetchone()["count"]

    cur.execute("""
    SELECT COALESCE(SUM(orders.amount), 0) as revenue
    FROM orders
    JOIN products ON orders.product_id = products.id
    WHERE products.user_id = ?
    """, (user["id"],))
    revenue = cur.fetchone()["revenue"]

    cur.execute("SELECT COALESCE(SUM(views), 0) as views FROM products WHERE user_id = ?", (user["id"],))
    views = cur.fetchone()["views"]

    conversion_rate = round((orders_count / views) * 100, 1) if views else 0
    average_order = revenue / orders_count if orders_count else 0

    cur.execute("""
    SELECT
        store_items.name as item_name,
        products.name as store_name,
        products.views as store_views,
        COUNT(orders.id) as sales,
        COALESCE(SUM(orders.amount), 0) as revenue
    FROM store_items
    JOIN products ON store_items.store_id = products.id
    LEFT JOIN orders ON orders.store_item_id = store_items.id
    WHERE store_items.user_id = ?
    GROUP BY store_items.id
    ORDER BY revenue DESC, sales DESC
    LIMIT 10
    """, (user["id"],))
    top_products = cur.fetchall()

    cur.execute("""
    SELECT
        orders.*,
        products.name as store_name,
        store_items.name as item_name
    FROM orders
    JOIN products ON orders.product_id = products.id
    LEFT JOIN store_items ON orders.store_item_id = store_items.id
    WHERE products.user_id = ?
    ORDER BY orders.id DESC
    LIMIT 8
    """, (user["id"],))
    recent_orders = cur.fetchall()

    cur.execute("""
    SELECT
        products.id,
        products.name as store_name,
        products.slug as store_slug,
        products.views as store_views,
        COUNT(orders.id) as orders_count,
        COALESCE(SUM(orders.amount), 0) as revenue
    FROM products
    LEFT JOIN orders ON orders.product_id = products.id
    WHERE products.user_id = ?
    GROUP BY products.id
    ORDER BY revenue DESC, orders_count DESC
    """, (user["id"],))
    per_store = cur.fetchall()

    conn.close()

    product_rows = ""

    for r in top_products:
        product_rows += f"""
        <div class="analytics-row">
            <div>
                <strong>{r["item_name"]}</strong>
                <span>{r["store_name"]}</span>
            </div>
            <span>{r["sales"]} sales</span>
            <span>{r["store_views"] or 0} views</span>
            <strong>${money(r["revenue"])}</strong>
        </div>
        """

    if not product_rows:
        product_rows = """
        <div class="empty-mini">
            <strong>No product analytics yet</strong>
            <p>Add products, pages, and start sharing your storefronts.</p>
        </div>
        """

    recent_rows = ""

    for o in recent_orders:
        recent_rows += f"""
        <div class="analytics-row">
            <div>
                <strong>{o["item_name"] or o["store_name"]}</strong>
                <span>{o["customer_email"]}</span>
            </div>
            <span>{o["payment_status"] or "paid"}</span>
            <span>{o["shipping_status"] or "Not shipped yet"}</span>
            <strong>${money(o["amount"])}</strong>
        </div>
        """

    if not recent_rows:
        recent_rows = """
        <div class="empty-mini">
            <strong>No recent orders yet</strong>
            <p>Orders will appear here after customers buy from your stores.</p>
        </div>
        """

    return layout(f"""
    <div class="container">
        {top_nav(user)}

        <a class="back" href="/dashboard">← Dashboard</a>

        <section class="hero analytics-hero">
            <p class="eyebrow">Analytics</p>
            <h1>Your store performance.</h1>
            <p>
                Track revenue, views, conversion rate, top products,
                storefront activity, and customer operations.
            </p>
        </section>

        <section class="stats modern-stats">
            <div><h3>${money(revenue)}</h3><p>Total Revenue</p></div>
            <div><h3>{orders_count}</h3><p>Total Orders</p></div>
            <div><h3>{views}</h3><p>Total Views</p></div>
            <div><h3>{conversion_rate}%</h3><p>Conversion Rate</p></div>
        </section>

        <section class="analytics-grid">
            <div class="analytics-card">
                <p class="eyebrow">Sales</p>
                <h2>${money(average_order)}</h2>
                <p>Average order value across completed purchases.</p>
            </div>

            <div class="analytics-card">
                <p class="eyebrow">Stores</p>
                <h2>{stores}</h2>
                <p>Total storefronts created inside your LaunchFlow account.</p>
                <p class="tiny muted">Multi-page storefront analytics enabled</p>
            </div>

            <div class="analytics-card">
                <p class="eyebrow">Products</p>
                <h2>{len(top_products)}</h2>
                <p>Products currently being tracked in analytics.</p>
            </div>
        </section>

        <div class="panel analytics-panel">
            <div class="section-header compact">
                <div>
                    <p class="eyebrow">Performance</p>
                    <h2>Top Products</h2>
                </div>
            </div>
            {product_rows}
        </div>

        <div class="panel analytics-panel">
            <div class="section-header compact">
                <div>
                    <p class="eyebrow">Breakdown</p>
                    <h2>Per-Store Analytics</h2>
                </div>
            </div>
            {"".join(f'''
            <div class="analytics-row">
                <div>
                    <strong><a href="/s/{r["store_slug"]}">{r["store_name"]}</a></strong>
                    <span>{r["orders_count"]} orders</span>
                </div>
                <span>{r["store_views"] or 0} views</span>
                <strong>${money(r["revenue"])}</strong>
            </div>
            ''' for r in per_store) or '<div class="empty-mini"><strong>No stores yet</strong></div>'}
        </div>

        <div class="panel analytics-panel">
            <div class="section-header compact">
                <div>
                    <p class="eyebrow">Activity</p>
                    <h2>Recent Orders</h2>
                </div>
            </div>
            {recent_rows}
        </div>
    </div>
    """, title="Analytics")


@app.get("/orders", response_class=HTMLResponse)
def orders(request: Request, view: str = "seller"):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    conn = db()
    cur = conn.cursor()

    if view == "buyer":
        cur.execute("""
        SELECT
            orders.*,
            products.name as store_name,
            products.slug as store_slug,
            store_items.name as item_name
        FROM orders
        JOIN products ON orders.product_id = products.id
        LEFT JOIN store_items ON orders.store_item_id = store_items.id
        WHERE LOWER(orders.customer_email) = LOWER(?)
        ORDER BY orders.id DESC
        """, (user["email"],))
    else:
        cur.execute("""
        SELECT
            orders.*,
            products.name as store_name,
            products.slug as store_slug,
            store_items.name as item_name
        FROM orders
        JOIN products ON orders.product_id = products.id
        LEFT JOIN store_items ON orders.store_item_id = store_items.id
        WHERE products.user_id = ?
        ORDER BY orders.id DESC
        """, (user["id"],))

    orders_data = cur.fetchall()
    conn.close()

    rows = ""

    for o in orders_data:
        item_name = o["item_name"] or o["store_name"]
        tracking_number = o["tracking_number"] or ""
        shipping_carrier = o["shipping_carrier"] or ""
        shipping_status = o["shipping_status"] or "Not shipped yet"
        fulfillment_status = o["fulfillment_status"] or "New order"
        buyer_message = o["buyer_message"] or ""

        shipping_address = f"""
        {o["shipping_name"]}<br>
        {o["shipping_address_line1"]}<br>
        {o["shipping_address_line2"]}<br>
        {o["shipping_city"]}, {o["shipping_state"]} {o["shipping_postal_code"]}<br>
        {o["shipping_country"]}
        """

        track_link = ""

        if tracking_number:
            track_link = f"""
            <a class="button small" href="/track-order/{o["id"]}">
                Track Shipping
            </a>
            """

        seller_controls = ""

        if view != "buyer":
            seller_controls = f"""
            <form action="/orders/{o["id"]}/shipping" method="post" class="advanced-order-form">
                <div class="form-grid">
                    <div>
                        <label>Carrier</label>
                        <input name="shipping_carrier" value="{shipping_carrier}" placeholder="USPS, UPS, FedEx">
                    </div>

                    <div>
                        <label>Tracking Number</label>
                        <input name="tracking_number" value="{tracking_number}" placeholder="Tracking number">
                    </div>
                </div>

                <div class="form-grid">
                    <div>
                        <label>Shipping Status</label>
                        <select name="shipping_status">
                            <option value="Not shipped yet" {"selected" if shipping_status == "Not shipped yet" else ""}>Not shipped yet</option>
                            <option value="Processing" {"selected" if shipping_status == "Processing" else ""}>Processing</option>
                            <option value="Shipped" {"selected" if shipping_status == "Shipped" else ""}>Shipped</option>
                            <option value="Delivered" {"selected" if shipping_status == "Delivered" else ""}>Delivered</option>
                        </select>
                    </div>

                    <div>
                        <label>Fulfillment Status</label>
                        <select name="fulfillment_status">
                            <option value="New order" {"selected" if fulfillment_status == "New order" else ""}>New order</option>
                            <option value="Packing" {"selected" if fulfillment_status == "Packing" else ""}>Packing</option>
                            <option value="Ready to ship" {"selected" if fulfillment_status == "Ready to ship" else ""}>Ready to ship</option>
                            <option value="Completed" {"selected" if fulfillment_status == "Completed" else ""}>Completed</option>
                        </select>
                    </div>
                </div>

                <div class="order-actions">
                    <button type="submit">Save Order Updates</button>
                    {track_link}
                </div>
            </form>
            """
        else:
            seller_controls = f"""
            <div class="order-actions">
                {track_link}
            </div>
            """

        rows += f"""
        <div class="order-row advanced-order-row">
            <div class="advanced-order-top">
                <div>
                    <strong>{item_name}</strong>

                    <p class="muted">
                        {"Store" if view == "buyer" else "Customer"}:
                        {o["store_name"] if view == "buyer" else o["customer_email"]}
                    </p>

                    <a class="button small ghost" href="/s/{o["store_slug"]}">
                        View Store
                    </a>

                    <p class="muted">
                        Ordered: {o["created_at"]}
                    </p>
                </div>

                <div class="order-price-box">
                    <strong>${money(o["amount"])}</strong>
                    <p class="muted">Qty: {o["quantity"]}</p>
                    <p class="muted">Payment: {o["payment_status"]}</p>
                </div>
            </div>

            <div class="order-grid">
                <div class="order-box">
                    <h3>Shipping Address</h3>
                    <p>{shipping_address}</p>
                </div>

                <div class="order-box">
                    <h3>Buyer Message</h3>
                    <p>{buyer_message or "No buyer message."}</p>
                </div>
            </div>

            {seller_controls}
        </div>
        """

    if not rows:
        rows = f"""
        <p class='muted'>
            {'You have not bought anything yet.' if view == 'buyer' else 'No seller orders yet.'}
        </p>
        """

    return layout(f"""
    <div class="container">
        {top_nav(user)}

        <a class="back" href="/dashboard">← Dashboard</a>

        <section class="hero">
            <p class="eyebrow">Fulfillment</p>

            <h1>
                Orders & Fulfillment
            </h1>

            <p>
                {'View things you bought and track shipping.' if view == 'buyer' else 'Manage customer purchases, fulfillment, tracking, storefront activity, and customer operations.'}
            </p>
        </section>

        <div class="order-tabs">
            <a class="button ghost {'active' if view == 'buyer' else ''}" href="/orders?view=buyer">
                Buyer Orders
            </a>

            <a class="button ghost {'active' if view != 'buyer' else ''}" href="/orders?view=seller">
                Seller Orders
            </a>
        </div>

        <div class="orders-wrapper">
            {rows}
        </div>
    </div>
    """, title="Orders")


@app.post("/orders/{order_id}/shipping")
def update_order_shipping(
    request: Request,
    order_id: int,
    shipping_carrier: str = Form(""),
    tracking_number: str = Form(""),
    shipping_status: str = Form("Not shipped yet"),
    fulfillment_status: str = Form("New order")
):

    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    conn = db()
    cur = conn.cursor()

    cur.execute("""
    UPDATE orders
    SET
        shipping_carrier = ?,
        tracking_number = ?,
        shipping_status = ?,
        fulfillment_status = ?
    WHERE id = ?
    AND product_id IN (
        SELECT id
        FROM products
        WHERE user_id = ?
    )
    """, (
        shipping_carrier,
        tracking_number,
        shipping_status,
        fulfillment_status,
        order_id,
        user["id"]
    ))

    conn.commit()
    conn.close()

    return RedirectResponse(
        "/orders?view=seller",
        status_code=303
    )


@app.get("/track-order/{order_id}", response_class=HTMLResponse)
def track_order(order_id: int):

    conn = db()
    cur = conn.cursor()

    cur.execute("""
    SELECT
        orders.*,
        products.name as store_name,
        store_items.name as item_name
    FROM orders
    JOIN products ON orders.product_id = products.id
    LEFT JOIN store_items ON orders.store_item_id = store_items.id
    WHERE orders.id = ?
    """, (order_id,))

    order = cur.fetchone()
    conn.close()

    if not order:
        return layout("""
        <div class="container narrow center">
            <div class="panel">
                <h1>Order not found</h1>
                <a class="button" href="/">Back home</a>
            </div>
        </div>
        """)

    tracking_number = order["tracking_number"] or ""
    shipping_carrier = order["shipping_carrier"] or "Not added yet"
    shipping_status = order["shipping_status"] or "Not shipped yet"
    fulfillment_status = order["fulfillment_status"] or "New order"

    tracking_link = ""

    if tracking_number:
        carrier = shipping_carrier.lower()

        if "usps" in carrier:
            tracking_link = f"https://tools.usps.com/go/TrackConfirmAction?tLabels={tracking_number}"
        elif "ups" in carrier:
            tracking_link = f"https://www.ups.com/track?tracknum={tracking_number}"
        elif "fedex" in carrier:
            tracking_link = f"https://www.fedex.com/fedextrack/?trknbr={tracking_number}"

    track_button = ""

    if tracking_link:
        track_button = f"""
        <a class="button" href="{tracking_link}" target="_blank">
            Track Package
        </a>
        """

    return layout(f"""
    <div class="container narrow center">

        <div class="panel track-panel">

            <p class="eyebrow">
                Shipping
            </p>

            <h1>
                Track your order
            </h1>

            <div class="tracking-grid">

                <div class="tracking-box">
                    <span>Item</span>
                    <strong>{order["item_name"] or order["store_name"]}</strong>
                </div>

                <div class="tracking-box">
                    <span>Shipping Status</span>
                    <strong>{shipping_status}</strong>
                </div>

                <div class="tracking-box">
                    <span>Fulfillment</span>
                    <strong>{fulfillment_status}</strong>
                </div>

                <div class="tracking-box">
                    <span>Carrier</span>
                    <strong>{shipping_carrier}</strong>
                </div>

            </div>

            <div class="tracking-number-box">
                <span>Tracking Number</span>
                <strong>{tracking_number or "Not added yet"}</strong>
            </div>

            <div class="tracking-actions">
                {track_button}

                <a class="button ghost" href="/orders?view=buyer">
                    Back to orders
                </a>
            </div>

        </div>

    </div>
    """, title="Track Order")


@app.post("/messages/start")
def start_message(
    request: Request,
    seller_id: int = Form(...),
    store_id: int = Form(0),
    buyer_email: str = Form(""),
    subject: str = Form("Store message"),
    message: str = Form("")
):
    user = require_user(request)

    sender_email = buyer_email.strip()

    if user:
        sender_email = user["email"]

    if not sender_email:
        return RedirectResponse("/login", status_code=303)

    clean_message = message.strip() or "Hi, I have a question about your store."

    conn = db()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO conversations (
        buyer_email,
        seller_id,
        store_id,
        order_id,
        subject
    )
    VALUES (?, ?, ?, ?, ?)
    """, (
        sender_email,
        seller_id,
        store_id,
        0,
        subject
    ))

    conversation_id = cur.lastrowid

    cur.execute("""
    INSERT INTO messages (
        conversation_id,
        sender_type,
        sender_user_id,
        sender_email,
        message
    )
    VALUES (?, ?, ?, ?, ?)
    """, (
        conversation_id,
        "buyer",
        user["id"] if user else 0,
        sender_email,
        clean_message
    ))

    conn.commit()
    conn.close()

    return RedirectResponse(f"/messages/{conversation_id}", status_code=303)


@app.get("/messages", response_class=HTMLResponse)
def messages_inbox(request: Request):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    conn = db()
    cur = conn.cursor()

    cur.execute("""
    SELECT
        conversations.*,
        products.name as store_name
    FROM conversations
    LEFT JOIN products ON conversations.store_id = products.id
    WHERE conversations.seller_id = ?
       OR conversations.buyer_email = ?
    ORDER BY conversations.id DESC
    """, (
        user["id"],
        user["email"]
    ))

    conversations_data = cur.fetchall()
    conn.close()

    rows = ""

    for c in conversations_data:
        role = "Seller" if c["seller_id"] == user["id"] else "Buyer"

        rows += f"""
        <div class="order-row">
            <div>
                <strong>{c["subject"] or "Conversation"}</strong>
                <p class="muted">Store: {c["store_name"] or "Store message"}</p>
                <p class="muted">Role: {role}</p>
                <p class="muted">Started: {c["created_at"]}</p>
            </div>

            <a class="button small" href="/messages/{c["id"]}">
                Open Chat
            </a>
        </div>
        """

    if not rows:
        rows = """
        <p class="muted">
            No messages yet.
        </p>
        """

    return layout(f"""
    <div class="container">
        {top_nav(user)}

        <a class="back" href="/dashboard">
            ← Dashboard
        </a>

        <section class="hero">
            <p class="eyebrow">Inbox</p>
            <h1>Messages</h1>
            <p>Manage buyer and seller conversations.</p>
        </section>

        <div class="panel">
            {rows}
        </div>
    </div>
    """, title="Messages")


@app.get("/messages/{conversation_id}", response_class=HTMLResponse)
def message_thread(request: Request, conversation_id: int):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    conn = db()
    cur = conn.cursor()

    cur.execute("""
    SELECT
        conversations.*,
        products.name as store_name
    FROM conversations
    LEFT JOIN products ON conversations.store_id = products.id
    WHERE conversations.id = ?
    """, (conversation_id,))

    convo = cur.fetchone()

    if not convo:
        conn.close()
        return RedirectResponse("/messages", status_code=303)

    allowed = (
        convo["seller_id"] == user["id"]
        or convo["buyer_email"] == user["email"]
    )

    if not allowed:
        conn.close()
        return RedirectResponse("/dashboard", status_code=303)

    cur.execute("""
    SELECT *
    FROM messages
    WHERE conversation_id = ?
    ORDER BY id ASC
    """, (conversation_id,))

    message_rows = cur.fetchall()
    conn.close()

    messages_html = ""

    for m in message_rows:
        bubble_class = "buyer"

        if m["sender_type"] == "seller":
            bubble_class = "seller"

        messages_html += f"""
        <div class="chat-message {bubble_class}">
            <p>{m["message"]}</p>
            <span>{m["created_at"]}</span>
        </div>
        """

    if not messages_html:
        messages_html = """
        <p class="muted">No messages yet.</p>
        """

    return layout(f"""
    <div class="container narrow">
        {top_nav(user)}

        <a class="back" href="/messages">
            ← Messages
        </a>

        <div class="panel">
            <p class="eyebrow">Conversation</p>
            <h1>{convo["subject"] or "Chat"}</h1>
            <p class="muted">Store: {convo["store_name"] or "Store message"}</p>

            <div class="full-chat-box">
                {messages_html}
            </div>

            <form action="/messages/{conversation_id}/send" method="post" class="chat-input-row full-chat-input">
                <input
                    name="message"
                    required
                    placeholder="Type your message..."
                >

                <button type="submit">
                    Send
                </button>
            </form>
        </div>
    </div>
    """, title="Chat")


@app.post("/messages/{conversation_id}/send")
def send_message(
    request: Request,
    conversation_id: int,
    message: str = Form(...)
):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    clean_message = message.strip()

    if not clean_message:
        return RedirectResponse(f"/messages/{conversation_id}", status_code=303)

    conn = db()
    cur = conn.cursor()

    cur.execute("""
    SELECT *
    FROM conversations
    WHERE id = ?
    """, (conversation_id,))

    convo = cur.fetchone()

    if not convo:
        conn.close()
        return RedirectResponse("/messages", status_code=303)

    allowed = (
        convo["seller_id"] == user["id"]
        or convo["buyer_email"] == user["email"]
    )

    if not allowed:
        conn.close()
        return RedirectResponse("/dashboard", status_code=303)

    sender_type = "buyer"

    if convo["seller_id"] == user["id"]:
        sender_type = "seller"

    cur.execute("""
    INSERT INTO messages (
        conversation_id,
        sender_type,
        sender_user_id,
        sender_email,
        message
    )
    VALUES (?, ?, ?, ?, ?)
    """, (
        conversation_id,
        sender_type,
        user["id"],
        user["email"],
        clean_message
    ))

    conn.commit()
    conn.close()

    return RedirectResponse(f"/messages/{conversation_id}", status_code=303)

@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    pro_status = "Premium" if user["is_pro"] else "Free Plan"

    stripe_onboarding_complete = user["stripe_onboarding_complete"]

    if user["stripe_account_id"]:
        try:
            account = stripe.Account.retrieve(user["stripe_account_id"])

            requirements_due = []
            currently_due = []

            if hasattr(account, "requirements") and account.requirements:
                requirements_due = account.requirements.past_due or []
                currently_due = account.requirements.currently_due or []

            stripe_onboarding_complete = bool(
                account.details_submitted
                and len(requirements_due) == 0
                and len(currently_due) == 0
            )

            conn = db()
            cur = conn.cursor()

            cur.execute(
                """
                UPDATE users
                SET stripe_onboarding_complete = ?
                WHERE id = ?
                """,
                (1 if stripe_onboarding_complete else 0, user["id"])
            )

            conn.commit()
            conn.close()

        except Exception as e:
            logger.error("Settings Stripe check error: %s", e)

    stripe_ready = bool(
        user["stripe_account_id"] and
        stripe_onboarding_complete
    )

    if stripe_ready:
        connect_status = """
        <div class="settings-status success">
            <strong>Stripe connected</strong>
            <p>Your account is ready to accept payments and receive payouts.</p>
        </div>
        """
        connect_button_text = "Manage Payouts"

    elif user["stripe_account_id"]:
        connect_status = """
        <div class="settings-status warning">
            <strong>Stripe onboarding incomplete</strong>
            <p>Finish setup before customers can buy your products.</p>
        </div>
        """
        connect_button_text = "Complete Setup"

    else:
        connect_status = """
        <div class="settings-status warning">
            <strong>Stripe not connected</strong>
            <p>Connect Stripe so customers can purchase from your stores.</p>
        </div>
        """
        connect_button_text = "Connect Stripe"

    return layout(f"""
    <div class="container narrow">
        {top_nav(user)}

        <a class="back" href="/dashboard">← Dashboard</a>

        <section class="hero settings-hero">
            <p class="eyebrow">Settings</p>
            <h1>Manage your account.</h1>
            <p>
                Update your seller profile, connect payments,
                and manage your LaunchFlow plan.
            </p>
        </section>

        <div class="settings-grid">

            <div class="panel settings-panel">
                <p class="eyebrow">Profile</p>
                <h2>Account Settings</h2>

                <form action="/settings" method="post">
                    <label>Email</label>
                    <input name="email" value="{user["email"]}" disabled>

                    <label>Store owner name</label>
                    <input name="store_name" value="{user["store_name"] or "My Store"}">

                    <button type="submit">Save Settings</button>
                </form>
            </div>

            <div class="panel settings-panel">
                <p class="eyebrow">Payments</p>
                <h2>Stripe Connect</h2>

                {connect_status}

                <form action="/connect-stripe" method="post">
                    <button type="submit">{connect_button_text}</button>
                </form>

                <p class="settings-note">
                    Stripe securely handles checkout, card payments,
                    and seller payouts.
                </p>
            </div>

            <div class="panel settings-panel">
                <p class="eyebrow">Subscription</p>

                <h2>{pro_status}</h2>

                <p class="settings-note">
                    Your plan controls AI features, store limits,
                    premium templates, and advanced tools.
                </p>

                {
                    '''
                    <a class="button" href="/manage-subscription">
                        Manage Subscription
                    </a>
                    '''
                    if user["is_pro"] else
                    '''
                    <a class="button" href="/upgrade">
                        View Plans
                    </a>
                    '''
                }
            </div>

            <div class="panel settings-panel">
                <p class="eyebrow">Security</p>
                <h2>Login Session</h2>

                <p class="settings-note">
                    Logged in as <strong>{user["email"]}</strong>
                </p>

                <a class="button ghost" href="/logout">Log out</a>
            </div>

        </div>
    </div>
    """, title="Settings")


@app.post("/settings")
def save_settings(
    request: Request,
    store_name: str = Form(...)
):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    clean_store_name = store_name.strip() or "My Store"

    conn = db()
    cur = conn.cursor()

    cur.execute(
        "UPDATE users SET store_name = ? WHERE id = ?",
        (clean_store_name, user["id"])
    )

    conn.commit()
    conn.close()

    return RedirectResponse("/settings", status_code=303)


@app.post("/connect-stripe")
def connect_stripe(request: Request):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    conn = db()
    cur = conn.cursor()

    try:
        stripe_account_id = user["stripe_account_id"]

        if stripe_account_id:
            account = stripe.Account.retrieve(stripe_account_id)
        else:
            account = stripe.Account.create(
                type="express",
                country="US",
                email=user["email"],
                capabilities={
                    "card_payments": {"requested": True},
                    "transfers": {"requested": True},
                },
            )

            stripe_account_id = account.id

            cur.execute(
                """
                UPDATE users
                SET stripe_account_id = ?,
                    stripe_onboarding_complete = 0
                WHERE id = ?
                """,
                (stripe_account_id, user["id"])
            )

            conn.commit()

        base_url = os.getenv("BASE_URL", "https://launchflow.store").rstrip("/")

        account_link = stripe.AccountLink.create(
            account=stripe_account_id,
            refresh_url=f"{base_url}/stripe-connect-refresh",
            return_url=f"{base_url}/stripe-connect-return",
            type="account_onboarding",
        )

        conn.close()
        return RedirectResponse(account_link.url, status_code=303)

    except Exception as e:
        logger.error("Stripe Connect error: %s", e)
        conn.close()

        return layout(f"""
        <div class="container narrow center">
            <div class="panel">
                <h1>Stripe Connect Error</h1>
                <p>{str(e)}</p>

                <a class="button" href="/settings">
                    Back to Settings
                </a>
            </div>
        </div>
        """)


@app.get("/stripe-connect-refresh")
def stripe_connect_refresh(request: Request):
    user = require_user(request)

    if not user or not user["stripe_account_id"]:
        return RedirectResponse("/settings", status_code=303)

    try:
        base_url = os.getenv("BASE_URL", "https://launchflow.store").rstrip("/")
        account_link = stripe.AccountLink.create(
            account=user["stripe_account_id"],
            refresh_url=f"{base_url}/stripe-connect-refresh",
            return_url=f"{base_url}/stripe-connect-return",
            type="account_onboarding",
        )
        return RedirectResponse(account_link.url, status_code=303)
    except Exception:
        return RedirectResponse("/settings", status_code=303)


@app.get("/stripe-connect-return")
def stripe_connect_return(request: Request):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    conn = db()
    cur = conn.cursor()

    try:
        cur.execute(
            "SELECT stripe_account_id FROM users WHERE id = ?",
            (user["id"],)
        )

        row = cur.fetchone()

        if not row or not row["stripe_account_id"]:
            conn.close()
            return RedirectResponse("/settings", status_code=303)

        stripe_account_id = row["stripe_account_id"]

        account = stripe.Account.retrieve(stripe_account_id)

        requirements_due = []
        currently_due = []

        if hasattr(account, "requirements") and account.requirements:
            requirements_due = account.requirements.past_due or []
            currently_due = account.requirements.currently_due or []

        requirements_due = []
        currently_due = []
        if hasattr(account, "requirements") and account.requirements:
            requirements_due = account.requirements.past_due or []
            currently_due = account.requirements.currently_due or []

        onboarding_complete = bool(
            account.details_submitted
            and len(requirements_due) == 0
            and len(currently_due) == 0
        )

        cur.execute(
            """
            UPDATE users
            SET stripe_onboarding_complete = ?
            WHERE id = ?
            """,
            (1 if onboarding_complete else 0, user["id"])
        )

        conn.commit()
        conn.close()

        return RedirectResponse("/settings", status_code=303)

    except Exception as e:
        conn.close()
        return RedirectResponse("/settings", status_code=303)



@app.get("/reset-stripe-connect")
def reset_stripe_connect(request: Request):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    conn = db()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE users
        SET stripe_account_id = '',
            stripe_onboarding_complete = 0
        WHERE id = ?
        """,
        (user["id"],)
    )

    conn.commit()
    conn.close()

    return RedirectResponse("/settings", status_code=303)


@app.get("/track", response_class=HTMLResponse)
def track_lookup_page(request: Request):
    return layout(f"""
    <div class="container narrow center">
        <div class="panel">
            <p class="eyebrow">Order Tracking</p>
            <h1>Track your order</h1>
            <p class="muted">
                Enter your order ID and the email used at checkout.
            </p>

            <form action="/track" method="post">
                <label>Order ID</label>
                <input
                    name="order_id"
                    type="number"
                    min="1"
                    placeholder="Example: 12"
                    required
                >

                <label>Email used at checkout</label>
                <input
                    name="customer_email"
                    type="email"
                    placeholder="you@example.com"
                    required
                >

                <button type="submit">Find Order</button>
            </form>
        </div>
    </div>
    """, title="Track Order")


@app.post("/track")
def track_lookup(
    order_id: int = Form(...),
    customer_email: str = Form(...)
):
    clean_email = customer_email.strip().lower()

    conn = db()
    cur = conn.cursor()

    cur.execute("""
    SELECT *
    FROM orders
    WHERE id = ?
    AND LOWER(customer_email) = ?
    """, (order_id, clean_email))

    order = cur.fetchone()
    conn.close()

    if not order:
        return layout("""
        <div class="container narrow center">
            <div class="panel">
                <p class="eyebrow">Order Tracking</p>
                <h1>Order not found</h1>
                <p class="muted">
                    Please check your order ID and email, then try again.
                </p>

                <a class="button" href="/track">
                    Try again
                </a>
            </div>
        </div>
        """, title="Order Not Found")

    return RedirectResponse(
        f"/track-order/{order_id}",
        status_code=303
    )


@app.get("/clear-stripe-account")
def clear_stripe_account(request: Request):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    conn = db()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE users
        SET stripe_account_id = '',
            stripe_onboarding_complete = 0
        WHERE id = ?
        """,
        (user["id"],)
    )

    conn.commit()
    conn.close()

    return RedirectResponse("/settings", status_code=303)

@app.get("/terms", response_class=HTMLResponse)
def terms_page():

    return layout("""
    <div class="container narrow legal-page">

        <a class="back" href="/dashboard">
            ← Back to Dashboard
        </a>

        <div class="panel">

            <h1>Terms of Service</h1>

            <p class="muted">
                Last updated: May 2026
            </p>

            <h2>1. Overview</h2>

            <p>
                LaunchFlow is an ecommerce platform that allows users to create,
                manage, and sell products through customizable online storefronts.
            </p>

            <h2>2. User Responsibilities</h2>

            <p>
                Users are responsible for all products, descriptions, pricing,
                images, and content uploaded to their stores.
            </p>

            <p>
                Users may not sell illegal, counterfeit, fraudulent, harmful,
                or prohibited items through LaunchFlow.
            </p>

            <h2>3. Payments</h2>

            <p>
                Payments are processed securely through Stripe Connect.
                LaunchFlow does not directly store credit card information.
            </p>

            <h2>4. Platform Fees</h2>

            <p>
                LaunchFlow may charge platform fees on transactions processed
                through the platform.
            </p>

            <h2>5. Account Termination</h2>

            <p>
                LaunchFlow reserves the right to suspend or terminate accounts
                that violate these terms or engage in fraudulent activity.
            </p>

            <h2>6. Limitation of Liability</h2>

            <p>
                LaunchFlow is provided "as is" without warranties of any kind.
                We are not liable for losses, damages, disputes, or interruptions
                resulting from platform usage.
            </p>

        </div>

    </div>
    """, title="Terms of Service")


@app.get("/privacy", response_class=HTMLResponse)
def privacy_page():

    return layout("""
    <div class="container narrow legal-page">

        <a class="back" href="/dashboard">
            ← Back to Dashboard
        </a>

        <div class="panel">

            <h1>Privacy Policy</h1>

            <p class="muted">
                Last updated: May 2026
            </p>

            <h2>1. Information We Collect</h2>

            <p>
                LaunchFlow may collect account information, email addresses,
                order information, payment-related metadata, and analytics data.
            </p>

            <h2>2. Payment Information</h2>

            <p>
                Payments are securely processed by Stripe. LaunchFlow does not
                store full payment card information on our servers.
            </p>

            <h2>3. How Information Is Used</h2>

            <p>
                Information may be used to operate the platform, process orders,
                improve services, prevent fraud, and communicate with users.
            </p>

            <h2>4. Data Sharing</h2>

            <p>
                LaunchFlow does not sell personal information. Information may
                be shared with payment processors and service providers required
                to operate the platform.
            </p>

            <h2>5. Security</h2>

            <p>
                We take reasonable steps to protect user information and platform
                security.
            </p>

            <h2>6. Contact</h2>

            <p>
                For privacy-related questions, contact LaunchFlow support.
            </p>

        </div>

    </div>
    """, title="Privacy Policy")


@app.get("/refunds", response_class=HTMLResponse)
def refund_page():

    return layout("""
    <div class="container narrow legal-page">

        <a class="back" href="/dashboard">
            ← Back to Dashboard
        </a>

        <div class="panel">

            <h1>Refund Policy</h1>

            <p class="muted">
                Last updated: May 2026
            </p>

            <h2>1. Seller Responsibility</h2>

            <p>
                Individual sellers on LaunchFlow are responsible for handling
                refunds, returns, exchanges, and customer support for their
                products.
            </p>

            <h2>2. Platform Role</h2>

            <p>
                LaunchFlow provides the ecommerce platform and payment
                infrastructure but is not the direct seller of listed products.
            </p>

            <h2>3. Unauthorized Transactions</h2>

            <p>
                Customers who believe a transaction was unauthorized should
                contact their payment provider immediately.
            </p>

            <h2>4. Disputes</h2>

            <p>
                Refund disputes may be reviewed through Stripe and applicable
                financial institutions.
            </p>

            <h2>5. Contacting Sellers</h2>

            <p>
                Customers should contact the store owner directly regarding
                refund eligibility and return requests.
            </p>

        </div>

    </div>
    """, title="Refund Policy")
@app.get("/cart", response_class=HTMLResponse)
def cart_page(request: Request):
    user = require_user(request)

    session_id = request.cookies.get("launchflow_cart_id", "")

    conn = db()
    cur = conn.cursor()

    cur.execute("""
    SELECT
        cart_items.*,
        store_items.name,
        store_items.price,
        store_items.stock,
        store_items.image_url,
        products.slug as store_slug,
        products.name as store_name
    FROM cart_items
    JOIN store_items ON cart_items.store_item_id = store_items.id
    JOIN products ON store_items.store_id = products.id
    WHERE cart_items.session_id = ?
    OR cart_items.user_id = ?
    ORDER BY cart_items.id DESC
    """, (
        session_id,
        user["id"] if user else 0
    ))

    cart_items_data = cur.fetchall()
    conn.close()

    rows = ""
    total = 0

    for item in cart_items_data:
        line_total = float(item["price"]) * int(item["quantity"])
        total += line_total

        rows += f"""
        <div class="page-manager-row">
            <div>
                <strong>{item["name"]}</strong>
                <p class="muted">{item["store_name"]} · Qty: {item["quantity"]}</p>
            </div>

            <div class="actions">
                <strong>${money(line_total)}</strong>

                <a class="button small ghost" href="/product/{item["store_item_id"]}">
                    View
                </a>

                <form action="/cart/remove/{item["id"]}" method="post">
                    <button class="button small ghost" type="submit">
                        Remove
                    </button>
                </form>
            </div>
        </div>
        """

    if not rows:
        rows = """
        <div class="empty-state">
            <h2>Your cart is empty</h2>
            <p>Add products from stores to see them here.</p>
            <a class="button" href="/discover">Discover Products</a>
        </div>
        """

    return layout(f"""
    <div class="container narrow">
        {top_nav(user) if user else ""}

        <a class="back" href="/discover">
            ← Discover
        </a>

        <div class="panel">
            <p class="eyebrow">Cart</p>
            <h1>Your cart</h1>
            <p class="muted">Review saved products before buying.</p>
        </div>

        <div class="panel">
            {rows}

            <div class="section-header compact">
                <div>
                    <p class="eyebrow">Total</p>
                    <h2>${money(total)}</h2>
                </div>
            </div>
        </div>
    </div>
    """, title="Cart")
@app.post("/cart/remove/{cart_item_id}")
def remove_cart_item(request: Request, cart_item_id: int):
    user = require_user(request)
    session_id = request.cookies.get("launchflow_cart_id", "")

    conn = db()
    cur = conn.cursor()

    cur.execute("""
    DELETE FROM cart_items
    WHERE id = ?
    AND (
        session_id = ?
        OR user_id = ?
    )
    """, (
        cart_item_id,
        session_id,
        user["id"] if user else 0
    ))

    conn.commit()
    conn.close()

    return RedirectResponse("/cart", status_code=303)


# -----------------------------
# PRODUCT EDIT / DELETE
# -----------------------------
@app.get("/product/{item_id}/edit", response_class=HTMLResponse)
def edit_product_page(request: Request, item_id: int):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM store_items WHERE id = ? AND user_id = ?", (item_id, user["id"]))
    item = cur.fetchone()

    if not item:
        conn.close()
        return RedirectResponse("/dashboard", status_code=303)

    cur.execute("SELECT * FROM products WHERE id = ?", (item["store_id"],))
    store = cur.fetchone()
    conn.close()

    try:
        image_list = json.loads(item["image_urls"] or "[]")
    except Exception:
        image_list = []

    if not image_list and item["image_url"]:
        image_list = [item["image_url"]]

    current_images_html = ""
    for img_url in image_list:
        current_images_html += f"""
        <div class="image-preview-wrap">
            <img src="{img_url}" class="image-preview">
        </div>
        """

    return layout(f"""
    <div class="container narrow">
        {top_nav(user)}

        <a class="back" href="/product/{item_id}">← Product</a>

        <div class="panel">
            <p class="eyebrow">Edit Product</p>
            <h1>{item["name"]}</h1>

            <form action="/product/{item_id}/edit" method="post" enctype="multipart/form-data">
                <label>Product Name</label>
                <input name="name" value="{item["name"]}" required>

                <label>Description</label>
                <textarea name="description" required>{item["description"] or ""}</textarea>

                <label>Price</label>
                <input name="price" class="money-input" value="${money(item["price"])}" required>

                <label>Stock</label>
                <input name="stock" type="number" min="0" value="{item["stock"]}" required>

                <label>Current Photos</label>
                <div class="image-preview-grid">
                    {current_images_html or "<p class='muted'>No photos yet.</p>"}
                </div>

                <label>Replace Photos</label>
                <div class="upload-box" onclick="document.getElementById('edit-product-images').click()">
                    <strong>Click to select new photos</strong>
                    <p>Uploading new photos will replace existing ones. Max 10 photos.</p>
                </div>

                <input
                    id="edit-product-images"
                    name="images"
                    type="file"
                    accept="image/*"
                    multiple
                    style="display:none;"
                >

                <div id="edit-image-preview-grid" class="image-preview-grid"></div>

                <button type="submit">Save Changes</button>
            </form>
        </div>
    </div>

    <script>
        const editInput = document.getElementById("edit-product-images");
        const editGrid = document.getElementById("edit-image-preview-grid");

        editInput.addEventListener("change", () => {{
            const files = Array.from(editInput.files).slice(0, 10);
            editGrid.innerHTML = "";
            files.forEach(file => {{
                const wrap = document.createElement("div");
                wrap.className = "image-preview-wrap";
                const img = document.createElement("img");
                img.className = "image-preview";
                const reader = new FileReader();
                reader.onload = e => {{ img.src = e.target.result; }};
                reader.readAsDataURL(file);
                wrap.appendChild(img);
                editGrid.appendChild(wrap);
            }});
        }});
    </script>
    """, title=f"Edit {item['name']}")


@app.post("/product/{item_id}/edit")
def save_product_edit(
    request: Request,
    item_id: int,
    name: str = Form(...),
    description: str = Form(""),
    price: str = Form("0"),
    stock: str = Form("0"),
    images: List[UploadFile] = File(default=[])
):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM store_items WHERE id = ? AND user_id = ?", (item_id, user["id"]))
    item = cur.fetchone()

    if not item:
        conn.close()
        return RedirectResponse("/dashboard", status_code=303)

    uploaded_paths = []
    valid_images = [img for img in images[:10] if img and img.filename]

    for image in valid_images:
        file_ext = os.path.splitext(image.filename)[1].lower()
        if file_ext not in [".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif"]:
            continue

        safe_base = slugify(os.path.splitext(image.filename)[0]) or "product-image"

        try:
            image.file.seek(0)
            img = Image.open(image.file)
            if img.mode != "RGB":
                img = img.convert("RGB")
            safe_name = f"{random.randint(100000, 999999)}-{safe_base}.jpg"
            file_path = os.path.join(UPLOAD_DIR, safe_name)
            img.save(file_path, "JPEG", quality=95)
            uploaded_paths.append(upload_image(file_path))
        except Exception:
            continue

    if not uploaded_paths:
        try:
            uploaded_paths = json.loads(item["image_urls"] or "[]")
        except Exception:
            uploaded_paths = []
        if not uploaded_paths and item["image_url"]:
            uploaded_paths = [item["image_url"]]

    main_image = uploaded_paths[0] if uploaded_paths else ""

    cur.execute("""
    UPDATE store_items
    SET name = ?, description = ?, price = ?, stock = ?, image_url = ?, image_urls = ?
    WHERE id = ? AND user_id = ?
    """, (
        name.strip(),
        description.strip(),
        clean_price(price),
        clean_stock(stock),
        main_image,
        json.dumps(uploaded_paths),
        item_id,
        user["id"]
    ))

    conn.commit()
    conn.close()

    return RedirectResponse(f"/product/{item_id}", status_code=303)


@app.post("/product/{item_id}/delete")
def delete_store_item(request: Request, item_id: int):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM store_items WHERE id = ? AND user_id = ?", (item_id, user["id"]))
    item = cur.fetchone()

    if not item:
        conn.close()
        return RedirectResponse("/dashboard", status_code=303)

    cur.execute("SELECT slug FROM products WHERE id = ?", (item["store_id"],))
    store = cur.fetchone()
    store_slug = store["slug"] if store else None

    cur.execute("DELETE FROM store_items WHERE id = ? AND user_id = ?", (item_id, user["id"]))
    conn.commit()
    conn.close()

    if store_slug:
        return RedirectResponse(f"/s/{store_slug}", status_code=303)
    return RedirectResponse("/dashboard", status_code=303)


# -----------------------------
# STRIPE CHECKOUT
# -----------------------------
@app.post("/checkout-item/{item_id}")
def checkout_item(
    request: Request,
    item_id: int,
    customer_email: str = Form(...),
    quantity: int = Form(1)
):
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM store_items WHERE id = ?", (item_id,))
    item = cur.fetchone()

    if not item:
        conn.close()
        return RedirectResponse("/discover", status_code=303)

    cur.execute("SELECT * FROM products WHERE id = ?", (item["store_id"],))
    store = cur.fetchone()

    if not store:
        conn.close()
        return RedirectResponse("/discover", status_code=303)

    cur.execute("SELECT * FROM users WHERE id = ?", (store["user_id"],))
    seller = cur.fetchone()
    conn.close()

    if item["stock"] <= 0:
        return layout(f"""
        <div class="container narrow center">
            <div class="panel">
                <h1>Sold out</h1>
                <p>This product is no longer available.</p>
                <a class="button" href="/s/{store["slug"]}">Back to Store</a>
            </div>
        </div>
        """)

    if not seller or not seller["stripe_account_id"] or not seller["stripe_onboarding_complete"]:
        return layout(f"""
        <div class="container narrow center">
            <div class="panel">
                <p class="eyebrow">Coming soon</p>
                <h1>Payments not set up yet</h1>
                <p>This store is almost ready — the seller hasn't finished connecting their payment account. Check back soon.</p>
                <a class="button" href="/s/{store["slug"]}">Back to Store</a>
            </div>
        </div>
        """)

    quantity = max(1, min(int(quantity), item["stock"]))
    unit_amount = int(float(item["price"]) * 100)
    app_fee = int(unit_amount * quantity * 0.05)

    base_url = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {
                        "name": item["name"],
                        "description": (item["description"] or "")[:500],
                    },
                    "unit_amount": unit_amount,
                },
                "quantity": quantity,
            }],
            mode="payment",
            customer_email=customer_email.strip(),
            success_url=f"{base_url}/checkout-success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base_url}/checkout-cancel?item_id={item_id}",
            payment_intent_data={
                "transfer_data": {"destination": seller["stripe_account_id"]},
                "application_fee_amount": app_fee,
            },
            shipping_address_collection={"allowed_countries": [
                "US", "CA", "GB", "AU", "NZ", "IE", "DE", "FR", "NL",
                "SE", "NO", "DK", "FI", "AT", "BE", "CH", "ES", "IT",
                "PT", "PL", "JP", "SG", "HK", "MX", "BR",
            ]},
            metadata={
                "item_id": str(item_id),
                "quantity": str(quantity),
                "store_id": str(store["id"]),
                "customer_email": customer_email.strip(),
            }
        )

        return RedirectResponse(session.url, status_code=303)

    except Exception as e:
        logger.error("Checkout error: %s", e)
        return layout(f"""
        <div class="container narrow center">
            <div class="panel">
                <h1>Checkout Error</h1>
                <p>We couldn't start checkout. Please try again.</p>
                <p class="muted" style="font-size:13px;">{str(e)}</p>
                <a class="button" href="/s/{store["slug"]}">Back to Store</a>
            </div>
        </div>
        """)


@app.get("/checkout-success", response_class=HTMLResponse)
def checkout_success(session_id: str = "", request: Request = None):
    if not session_id:
        return RedirectResponse("/", status_code=303)

    try:
        session = stripe.checkout.Session.retrieve(session_id)

        if session.payment_status != "paid":
            return layout("""
            <div class="container narrow center">
                <div class="panel">
                    <h1>Payment Pending</h1>
                    <p>Your payment is still processing. Check back shortly.</p>
                    <a class="button" href="/">Back home</a>
                </div>
            </div>
            """)

        item_id = int(session.metadata.get("item_id", 0))
        quantity = int(session.metadata.get("quantity", 1))
        store_id = int(session.metadata.get("store_id", 0))
        customer_email = session.metadata.get("customer_email", "") or session.customer_email or ""
        amount = float(session.amount_total or 0) / 100

        shipping_name = ""
        address_line1 = ""
        address_city = ""
        address_state = ""
        address_postal = ""
        address_country = ""

        if session.shipping_details:
            try:
                shipping_name = session.shipping_details.name or ""
                if session.shipping_details.address:
                    addr = session.shipping_details.address
                    address_line1 = addr.line1 or ""
                    address_city = addr.city or ""
                    address_state = addr.state or ""
                    address_postal = addr.postal_code or ""
                    address_country = addr.country or ""
            except Exception:
                pass

        conn = db()
        cur = conn.cursor()

        is_new_order = False
        new_order_id = None
        email_item_name = ""
        email_store_name = ""
        email_seller_email = ""

        cur.execute("SELECT id FROM orders WHERE stripe_session_id = ?", (session_id,))
        if not cur.fetchone():
            is_new_order = True
            if item_id:
                cur.execute(
                    "UPDATE store_items SET stock = MAX(0, stock - ?) WHERE id = ?",
                    (quantity, item_id)
                )

            cur.execute("""
            INSERT INTO orders (
                product_id, store_item_id, amount, customer_email,
                quantity, shipping_name, shipping_address_line1,
                shipping_city, shipping_state, shipping_postal_code,
                shipping_country, stripe_session_id, payment_status,
                shipping_status, fulfillment_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'paid', 'Not shipped yet', 'New order')
            """, (
                store_id, item_id, amount, customer_email,
                quantity, shipping_name, address_line1,
                address_city, address_state, address_postal,
                address_country, session_id
            ))

            new_order_id = cur.lastrowid
            conn.commit()

            # Gather data for emails while connection is still open
            cur.execute("SELECT name FROM products WHERE id = ?", (store_id,))
            row = cur.fetchone()
            email_store_name = row["name"] if row else "Your Store"

            if item_id:
                cur.execute("SELECT name FROM store_items WHERE id = ?", (item_id,))
                row = cur.fetchone()
                email_item_name = row["name"] if row else email_store_name
            else:
                email_item_name = email_store_name

            cur.execute("""
                SELECT users.email FROM users
                JOIN products ON users.id = products.user_id
                WHERE products.id = ?
            """, (store_id,))
            row = cur.fetchone()
            email_seller_email = row["email"] if row else ""

        conn.close()

        if is_new_order and new_order_id:
            send_order_emails(
                order_id=new_order_id,
                item_name=email_item_name,
                store_name=email_store_name,
                customer_email=customer_email,
                seller_email=email_seller_email,
                amount=amount,
                quantity=quantity,
                shipping_name=shipping_name,
                address_line1=address_line1,
                city=address_city,
                state=address_state,
                postal=address_postal,
                country=address_country,
            )

        return layout(f"""
        <div class="container narrow center">
            <div class="panel">
                <p class="eyebrow">Order confirmed</p>
                <h1>Payment successful!</h1>
                <p>Thanks for your order, <strong>{customer_email}</strong>.</p>

                <div class="tracking-grid">
                    <div class="tracking-box">
                        <span>Amount paid</span>
                        <strong>${money(amount)}</strong>
                    </div>
                    <div class="tracking-box">
                        <span>Quantity</span>
                        <strong>{quantity}</strong>
                    </div>
                </div>

                <div class="hero-actions">
                    <a class="button" href="/">Continue Shopping</a>
                    <a class="button ghost" href="/track">Track Order</a>
                </div>
            </div>
        </div>
        """, title="Order Confirmed")

    except Exception:
        return layout("""
        <div class="container narrow center">
            <div class="panel">
                <p class="eyebrow">Order confirmed</p>
                <h1>Thank you!</h1>
                <p>Your payment was successful. You'll receive an email confirmation.</p>
                <a class="button" href="/">Back home</a>
            </div>
        </div>
        """, title="Order Confirmed")


@app.get("/checkout-cancel", response_class=HTMLResponse)
def checkout_cancel(item_id: int = 0):
    back_link = f"/product/{item_id}" if item_id else "/discover"

    return layout(f"""
    <div class="container narrow center">
        <div class="panel">
            <p class="eyebrow">Checkout cancelled</p>
            <h1>No charges were made</h1>
            <p>Your checkout was cancelled. You have not been charged.</p>

            <div class="hero-actions">
                <a class="button" href="{back_link}">Back to Product</a>
                <a class="button ghost" href="/discover">Browse Stores</a>
            </div>
        </div>
    </div>
    """, title="Checkout Cancelled")


# -----------------------------
# PREMIUM UPGRADE CHECKOUT
# -----------------------------
@app.post("/create-checkout-session")
def create_checkout_session(request: Request):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    if user["is_pro"]:
        return RedirectResponse("/settings", status_code=303)

    base_url = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {
                        "name": "LaunchFlow Premium",
                        "description": "Unlimited stores, AI features, and premium tools.",
                    },
                    "unit_amount": PREMIUM_PRICE * 100,
                },
                "quantity": 1,
            }],
            mode="payment",
            customer_email=user["email"],
            success_url=f"{base_url}/upgrade-success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base_url}/upgrade",
            metadata={
                "user_id": str(user["id"]),
                "user_email": user["email"],
                "type": "premium_upgrade",
            }
        )

        return RedirectResponse(session.url, status_code=303)

    except Exception:
        return layout("""
        <div class="container narrow center">
            <div class="panel">
                <h1>Checkout Error</h1>
                <p>We couldn't start checkout. Please try again.</p>
                <a class="button" href="/upgrade">Back to Upgrade</a>
            </div>
        </div>
        """)


@app.get("/upgrade-success", response_class=HTMLResponse)
def upgrade_success(session_id: str = "", request: Request = None):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    if session_id:
        try:
            session = stripe.checkout.Session.retrieve(session_id)
            if (
                session.payment_status == "paid"
                and session.metadata.get("type") == "premium_upgrade"
            ):
                conn = db()
                cur = conn.cursor()
                cur.execute("UPDATE users SET is_pro = 1 WHERE id = ?", (user["id"],))
                conn.commit()
                conn.close()
        except Exception:
            pass

    return layout(f"""
    <div class="container narrow center">
        <div class="panel">
            <p class="eyebrow">Upgrade complete</p>
            <h1>Welcome to Premium!</h1>
            <p>Your account has been upgraded. You now have access to all LaunchFlow features.</p>
            <a class="button" href="/dashboard">Go to Dashboard</a>
        </div>
    </div>
    """, title="Premium Activated")


@app.get("/manage-subscription", response_class=HTMLResponse)
def manage_subscription(request: Request):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    if not user["is_pro"]:
        return RedirectResponse("/upgrade", status_code=303)

    return layout(f"""
    <div class="container narrow">
        {top_nav(user)}

        <a class="back" href="/settings">← Settings</a>

        <div class="panel">
            <p class="eyebrow">Subscription</p>
            <h1>Premium Plan</h1>
            <p>You're on the LaunchFlow Premium plan with full access to all features.</p>

            <div class="premium-feature-list">
                <div class="premium-feature-item">Unlimited stores</div>
                <div class="premium-feature-item">AI store generation</div>
                <div class="premium-feature-item">Premium templates</div>
                <div class="premium-feature-item">Advanced store tools</div>
                <div class="premium-feature-item">Better customization</div>
                <div class="premium-feature-item">Seller growth features</div>
            </div>

            <a class="button ghost" href="/settings">Back to Settings</a>
        </div>
    </div>
    """, title="Manage Subscription")


# -----------------------------
# STRIPE WEBHOOK — reliable order creation server-side
# -----------------------------
@app.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    if not STRIPE_WEBHOOK_SECRET:
        return HTMLResponse("Webhook secret not configured", status_code=400)

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        logger.error("Webhook signature error: %s", e)
        return HTMLResponse("Invalid signature", status_code=400)

    if event.get("type") == "checkout.session.completed":
        session = event["data"]["object"]

        if session.get("payment_status") != "paid":
            return HTMLResponse("ok")

        session_id = session.get("id", "")
        meta = session.get("metadata") or {}

        # Premium upgrade — update is_pro and skip order creation
        if meta.get("type") == "premium_upgrade":
            user_id = int(meta.get("user_id", 0))
            if user_id:
                conn = db()
                cur = conn.cursor()
                cur.execute("UPDATE users SET is_pro = 1 WHERE id = ?", (user_id,))
                conn.commit()
                conn.close()
            return HTMLResponse("ok")

        item_id = int(meta.get("item_id", 0))
        store_id = int(meta.get("store_id", 0))
        quantity = int(meta.get("quantity", 1))
        customer_email = meta.get("customer_email", "") or session.get("customer_email") or ""
        amount = float(session.get("amount_total") or 0) / 100

        shipping_name = ""
        address_line1 = ""
        address_city = ""
        address_state = ""
        address_postal = ""
        address_country = ""

        shipping_details = session.get("shipping_details") or {}
        if shipping_details:
            shipping_name = shipping_details.get("name", "")
            addr = shipping_details.get("address") or {}
            address_line1 = addr.get("line1", "")
            address_city = addr.get("city", "")
            address_state = addr.get("state", "")
            address_postal = addr.get("postal_code", "")
            address_country = addr.get("country", "")

        conn = db()
        cur = conn.cursor()

        cur.execute("SELECT id FROM orders WHERE stripe_session_id = ?", (session_id,))
        if cur.fetchone():
            conn.close()
            return HTMLResponse("ok")

        if item_id:
            cur.execute("UPDATE store_items SET stock = MAX(0, stock - ?) WHERE id = ?", (quantity, item_id))

        cur.execute("""
        INSERT INTO orders (
            product_id, store_item_id, amount, customer_email,
            quantity, shipping_name, shipping_address_line1,
            shipping_city, shipping_state, shipping_postal_code,
            shipping_country, stripe_session_id, payment_status,
            shipping_status, fulfillment_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'paid', 'Not shipped yet', 'New order')
        """, (
            store_id, item_id, amount, customer_email,
            quantity, shipping_name, address_line1,
            address_city, address_state, address_postal,
            address_country, session_id
        ))

        new_order_id = cur.lastrowid
        conn.commit()

        cur.execute("SELECT name FROM products WHERE id = ?", (store_id,))
        row = cur.fetchone()
        email_store_name = row["name"] if row else "Your Store"

        if item_id:
            cur.execute("SELECT name FROM store_items WHERE id = ?", (item_id,))
            row = cur.fetchone()
            email_item_name = row["name"] if row else email_store_name
        else:
            email_item_name = email_store_name

        cur.execute("""
            SELECT users.email FROM users
            JOIN products ON users.id = products.user_id
            WHERE products.id = ?
        """, (store_id,))
        row = cur.fetchone()
        email_seller_email = row["email"] if row else ""

        conn.close()

        send_order_emails(
            order_id=new_order_id,
            item_name=email_item_name,
            store_name=email_store_name,
            customer_email=customer_email,
            seller_email=email_seller_email,
            amount=amount,
            quantity=quantity,
            shipping_name=shipping_name,
            address_line1=address_line1,
            city=address_city,
            state=address_state,
            postal=address_postal,
            country=address_country,
        )

    return HTMLResponse("ok")


# -----------------------------
# PASSWORD RESET
# -----------------------------

@app.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_page(sent: str = ""):
    if sent:
        return layout("""
        <div class="auth-page">
            <div class="auth-card">
                <p class="eyebrow">Password reset</p>
                <h1>Check your email</h1>
                <p>If that email exists in our system, a reset link has been sent.</p>
                <a class="button" href="/login">Back to Login</a>
            </div>
        </div>
        """)

    return layout("""
    <div class="auth-page">
        <div class="auth-card">
            <p class="eyebrow">Password reset</p>
            <h1>Forgot password?</h1>
            <p>Enter your email and we'll send you a reset link.</p>

            <form action="/forgot-password" method="post">
                <label>Email</label>
                <input name="email" type="email" required placeholder="you@example.com">
                <button type="submit">Send Reset Link</button>
            </form>

            <p class="auth-switch"><a href="/login">Back to Login</a></p>
        </div>
    </div>
    """)


@app.post("/forgot-password")
def forgot_password_submit(email: str = Form(...)):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE LOWER(email) = LOWER(?)", (email.strip(),))
    user = cur.fetchone()

    if user:
        token = uuid.uuid4().hex
        expires = int(__import__("time").time()) + 3600
        cur.execute(
            "UPDATE users SET reset_token = ?, reset_token_expires = ? WHERE id = ?",
            (token, expires, user["id"])
        )
        conn.commit()

        reset_url = f"{BASE_URL}/reset-password?token={token}"
        html = f"""
        <div style="font-family:system-ui,sans-serif;max-width:560px;margin:0 auto;background:#0f172a;color:#e2e8f0;padding:32px;border-radius:12px;">
          <h1 style="color:#7c3aed;font-size:28px;margin:0 0 8px;">Reset your password</h1>
          <p style="color:#94a3b8;margin:0 0 24px;">Click the button below to set a new password. This link expires in 1 hour.</p>
          <a href="{reset_url}" style="display:inline-block;background:#7c3aed;color:#fff;text-decoration:none;padding:12px 24px;border-radius:8px;font-weight:600;">Reset Password</a>
          <p style="margin:24px 0 0;font-size:12px;color:#475569;">If you didn't request this, ignore this email.</p>
        </div>
        """
        send_email(email.strip(), "Reset your LaunchFlow password", html)

    conn.close()
    return RedirectResponse("/forgot-password?sent=1", status_code=303)


@app.get("/reset-password", response_class=HTMLResponse)
def reset_password_page(token: str = ""):
    if not token:
        return RedirectResponse("/forgot-password", status_code=303)

    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM users WHERE reset_token = ? AND reset_token_expires > ?",
        (token, int(__import__("time").time()))
    )
    user = cur.fetchone()
    conn.close()

    if not user:
        return layout("""
        <div class="auth-page">
            <div class="auth-card">
                <h1>Link expired</h1>
                <p>This reset link is invalid or has expired.</p>
                <a class="button" href="/forgot-password">Request a new one</a>
            </div>
        </div>
        """)

    return layout(f"""
    <div class="auth-page">
        <div class="auth-card">
            <p class="eyebrow">Password reset</p>
            <h1>Set new password</h1>

            <form action="/reset-password" method="post">
                <input type="hidden" name="token" value="{token}">
                <label>New Password</label>
                <input name="password" type="password" required placeholder="At least 8 characters" minlength="8">
                <button type="submit">Set Password</button>
            </form>
        </div>
    </div>
    """)


@app.post("/reset-password")
def reset_password_submit(token: str = Form(""), password: str = Form("")):
    if not token or len(password) < 8:
        return RedirectResponse("/forgot-password", status_code=303)

    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM users WHERE reset_token = ? AND reset_token_expires > ?",
        (token, int(__import__("time").time()))
    )
    user = cur.fetchone()

    if not user:
        conn.close()
        return RedirectResponse("/forgot-password", status_code=303)

    new_hash = hash_password(password)
    cur.execute(
        "UPDATE users SET password = ?, reset_token = '', reset_token_expires = 0 WHERE id = ?",
        (new_hash, user["id"])
    )
    conn.commit()
    conn.close()

    return RedirectResponse("/login", status_code=303)


# -----------------------------
# AI PRODUCT COPY GENERATOR
# -----------------------------
@app.get("/ai-product-copy", response_class=HTMLResponse)
def ai_product_copy_page(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return layout(f"""
    <div class="container narrow">
        {top_nav(user)}
        <a class="back" href="/dashboard">← Dashboard</a>
        <div class="panel">
            <p class="eyebrow">AI Dropshipping Tool</p>
            <h1>AI Product Copy Generator</h1>
            <p>Enter your product and get a full set of ready-to-use copy — description, bullets, Facebook ad, TikTok hook, and email subject.</p>

            <label>Product Name</label>
            <input id="cp-name" placeholder="e.g. Posture Corrector Belt" />

            <label>Product Niche / Angle</label>
            <input id="cp-angle" placeholder="e.g. people who sit at a desk all day and have back pain" />

            <label>Selling Price ($)</label>
            <input id="cp-price" type="number" step="0.01" placeholder="e.g. 29.99" />

            <button onclick="generateCopy(this)" style="margin-top:16px;">Generate Copy ✨</button>

            <div id="cp-status" style="margin-top:12px;font-size:14px;color:#7c3aed;display:none;">Generating...</div>

            <div id="cp-result" style="display:none;margin-top:20px;">
                <div class="cp-section">
                    <div class="cp-label">📦 Product Description</div>
                    <div class="cp-box" id="cp-desc"></div>
                    <button class="copy-btn" onclick="copyBox('cp-desc')">Copy</button>
                </div>
                <div class="cp-section">
                    <div class="cp-label">✅ Bullet Points</div>
                    <div class="cp-box" id="cp-bullets"></div>
                    <button class="copy-btn" onclick="copyBox('cp-bullets')">Copy</button>
                </div>
                <div class="cp-section">
                    <div class="cp-label">📘 Facebook Ad Copy</div>
                    <div class="cp-box" id="cp-fb"></div>
                    <button class="copy-btn" onclick="copyBox('cp-fb')">Copy</button>
                </div>
                <div class="cp-section">
                    <div class="cp-label">🎵 TikTok Hook</div>
                    <div class="cp-box" id="cp-tiktok"></div>
                    <button class="copy-btn" onclick="copyBox('cp-tiktok')">Copy</button>
                </div>
                <div class="cp-section">
                    <div class="cp-label">📧 Email Subject Line</div>
                    <div class="cp-box" id="cp-email"></div>
                    <button class="copy-btn" onclick="copyBox('cp-email')">Copy</button>
                </div>
            </div>
        </div>
    </div>

    <style>
    .cp-section {{ margin-bottom:18px; }}
    .cp-label {{ font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#7c3aed;margin-bottom:6px; }}
    .cp-box {{ background:#0f172a;border:1px solid #1e293b;border-radius:10px;padding:14px;font-size:14px;line-height:1.6;white-space:pre-wrap;color:#e2e8f0;min-height:48px; }}
    .copy-btn {{ margin-top:6px;padding:6px 14px;background:#1e293b;color:#94a3b8;border:1px solid #334155;border-radius:8px;font-size:12px;font-weight:600;cursor:pointer; }}
    .copy-btn:hover {{ background:#334155;color:#e2e8f0; }}
    </style>

    <script>
    async function generateCopy(btn) {{
        const name = document.getElementById('cp-name').value.trim();
        const angle = document.getElementById('cp-angle').value.trim();
        const price = document.getElementById('cp-price').value.trim();
        if (!name) {{ alert('Enter a product name'); return; }}
        btn.disabled = true;
        btn.textContent = 'Generating...';
        document.getElementById('cp-status').style.display = 'block';
        document.getElementById('cp-result').style.display = 'none';
        try {{
            const r = await fetch('/ai-product-copy', {{
                method: 'POST',
                headers: {{'Content-Type':'application/json'}},
                body: JSON.stringify({{name, angle, price}})
            }});
            const d = await r.json();
            if (!d.ok) {{ alert(d.error || 'Error generating copy'); return; }}
            document.getElementById('cp-desc').textContent = d.description;
            document.getElementById('cp-bullets').textContent = d.bullets;
            document.getElementById('cp-fb').textContent = d.facebook_ad;
            document.getElementById('cp-tiktok').textContent = d.tiktok_hook;
            document.getElementById('cp-email').textContent = d.email_subject;
            document.getElementById('cp-result').style.display = 'block';
        }} catch(e) {{ alert('Something went wrong'); }}
        finally {{
            btn.disabled = false;
            btn.textContent = 'Generate Copy ✨';
            document.getElementById('cp-status').style.display = 'none';
        }}
    }}
    function copyBox(id) {{
        const text = document.getElementById(id).textContent;
        navigator.clipboard.writeText(text).then(() => {{
            const btn = document.getElementById(id).nextElementSibling;
            btn.textContent = 'Copied!';
            setTimeout(() => btn.textContent = 'Copy', 2000);
        }});
    }}
    </script>
    """, title="AI Product Copy")


@app.post("/ai-product-copy")
async def ai_product_copy_generate(request: Request):
    user = require_user(request)
    if not user:
        from fastapi.responses import JSONResponse
        return JSONResponse({"ok": False, "error": "Not logged in"}, status_code=401)
    try:
        body = await request.json()
        name = str(body.get("name", "")).strip()[:200]
        angle = str(body.get("angle", "")).strip()[:300]
        price = str(body.get("price", "")).strip()[:20]
        if not name:
            from fastapi.responses import JSONResponse
            return JSONResponse({"ok": False, "error": "Product name required"})

        prompt = f"""You are an expert dropshipping copywriter. Write high-converting copy for this product.

PRODUCT: {name}
ANGLE / TARGET CUSTOMER: {angle or "general consumers"}
SELLING PRICE: ${price or "29.99"}

Write the following sections. Be specific, benefit-focused, and conversion-optimized.

Return a JSON object with these exact keys:
- description: 2-3 sentence product description (benefits-first, no fluff)
- bullets: 5 bullet points starting with ✓ (key benefits and features)
- facebook_ad: A complete Facebook ad copy block (headline, body, CTA). Make it scroll-stopping.
- tiktok_hook: One powerful TikTok/Reels opening hook (first 3 seconds, under 15 words, creates curiosity or pain)
- email_subject: 3 email subject line options that would get high open rates

Return only valid JSON, no markdown."""

        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}]
        )
        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```json?\s*", "", text)
            text = re.sub(r"\s*```$", "", text).strip()
        data = json.loads(text)
        from fastapi.responses import JSONResponse
        return JSONResponse({"ok": True, **data})
    except Exception as e:
        logger.error("AI product copy error: %s", e)
        from fastapi.responses import JSONResponse
        return JSONResponse({"ok": False, "error": "Generation failed — try again"}, status_code=500)


# -----------------------------
# PROFIT MARGIN CALCULATOR
# -----------------------------
@app.get("/profit-calculator", response_class=HTMLResponse)
def profit_calculator(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return layout(f"""
    <div class="container narrow">
        {top_nav(user)}
        <a class="back" href="/dashboard">← Dashboard</a>
        <div class="panel">
            <p class="eyebrow">Dropshipping Tool</p>
            <h1>Profit Margin Calculator</h1>
            <p>Enter your costs and selling price to see real profit numbers including Stripe fees.</p>

            <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:20px;">
                <div>
                    <label>Selling Price ($)</label>
                    <input id="pc-sell" type="number" step="0.01" placeholder="29.99" oninput="calcProfit()" />
                </div>
                <div>
                    <label>Supplier Cost ($)</label>
                    <input id="pc-cost" type="number" step="0.01" placeholder="8.00" oninput="calcProfit()" />
                </div>
                <div>
                    <label>Shipping Cost ($)</label>
                    <input id="pc-ship" type="number" step="0.01" placeholder="3.00" oninput="calcProfit()" />
                </div>
                <div>
                    <label>Ad Spend per Sale ($)</label>
                    <input id="pc-ads" type="number" step="0.01" placeholder="0.00" oninput="calcProfit()" />
                </div>
            </div>

            <div style="margin-top:8px;padding:10px 14px;background:#0f172a;border-radius:10px;border:1px solid #1e293b;">
                <label style="margin:0;font-size:13px;color:#64748b;">Stripe fee (2.9% + $0.30) is auto-included</label>
            </div>

            <div id="pc-result" style="margin-top:24px;display:none;">
                <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:18px;">
                    <div class="stat-box">
                        <div class="stat-val" id="pc-net">$0.00</div>
                        <div class="stat-label">Net Profit</div>
                    </div>
                    <div class="stat-box">
                        <div class="stat-val" id="pc-margin">0%</div>
                        <div class="stat-label">Margin</div>
                    </div>
                    <div class="stat-box">
                        <div class="stat-val" id="pc-roas">0x</div>
                        <div class="stat-label">Break-even ROAS</div>
                    </div>
                </div>
                <div id="pc-breakdown" style="background:#0f172a;border:1px solid #1e293b;border-radius:10px;padding:14px;font-size:14px;line-height:2;color:#94a3b8;"></div>
                <div id="pc-verdict" style="margin-top:12px;padding:12px 16px;border-radius:10px;font-weight:700;font-size:14px;"></div>
            </div>
        </div>
    </div>

    <style>
    .stat-box {{ background:#0f172a;border:1px solid #1e293b;border-radius:12px;padding:16px;text-align:center; }}
    .stat-val {{ font-size:24px;font-weight:800;color:#7c3aed;line-height:1; }}
    .stat-label {{ font-size:12px;color:#64748b;margin-top:4px; }}
    </style>

    <script>
    function calcProfit() {{
        const sell = parseFloat(document.getElementById('pc-sell').value) || 0;
        const cost = parseFloat(document.getElementById('pc-cost').value) || 0;
        const ship = parseFloat(document.getElementById('pc-ship').value) || 0;
        const ads  = parseFloat(document.getElementById('pc-ads').value)  || 0;
        if (sell <= 0) {{ document.getElementById('pc-result').style.display='none'; return; }}
        const stripeFee = sell * 0.029 + 0.30;
        const totalCost = cost + ship + ads + stripeFee;
        const net = sell - totalCost;
        const margin = sell > 0 ? (net / sell) * 100 : 0;
        const roas = ads > 0 ? sell / ads : 0;
        document.getElementById('pc-net').textContent = '$' + net.toFixed(2);
        document.getElementById('pc-margin').textContent = margin.toFixed(1) + '%';
        document.getElementById('pc-roas').textContent = roas > 0 ? roas.toFixed(1) + 'x' : '—';
        document.getElementById('pc-breakdown').innerHTML =
            `Revenue: <strong style="color:#e2e8f0">$${{sell.toFixed(2)}}</strong><br>` +
            `Supplier cost: <strong style="color:#e2e8f0">-$${{cost.toFixed(2)}}</strong><br>` +
            `Shipping: <strong style="color:#e2e8f0">-$${{ship.toFixed(2)}}</strong><br>` +
            `Ad spend: <strong style="color:#e2e8f0">-$${{ads.toFixed(2)}}</strong><br>` +
            `Stripe fee (2.9%+$0.30): <strong style="color:#e2e8f0">-$${{stripeFee.toFixed(2)}}</strong><br>` +
            `<strong style="color:#7c3aed">Net profit: $${{net.toFixed(2)}}</strong>`;
        const verdict = document.getElementById('pc-verdict');
        if (net < 0) {{
            verdict.style.background='#450a0a'; verdict.style.color='#fca5a5';
            verdict.textContent = '⚠️ You would LOSE money on this product at these numbers. Raise the price or find a cheaper supplier.';
        }} else if (margin < 20) {{
            verdict.style.background='#431407'; verdict.style.color='#fdba74';
            verdict.textContent = '⚡ Thin margin. Consider raising price or cutting costs — under 20% is risky once refunds hit.';
        }} else if (margin < 40) {{
            verdict.style.background='#1e3a5f'; verdict.style.color='#93c5fd';
            verdict.textContent = '✅ Decent margin. Viable product — watch your ad spend carefully.';
        }} else {{
            verdict.style.background='#052e16'; verdict.style.color='#86efac';
            verdict.textContent = '🔥 Strong margin! This product can handle ad spend and refunds and still be profitable.';
        }}
        document.getElementById('pc-result').style.display = 'block';
    }}
    </script>
    """, title="Profit Calculator")


# -----------------------------
# SUPPLIER MANAGER
# -----------------------------
@app.get("/suppliers", response_class=HTMLResponse)
def suppliers_page(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM suppliers WHERE user_id = ? ORDER BY id DESC", (user["id"],))
    suppliers = cur.fetchall()
    conn.close()

    rows = ""
    for s in suppliers:
        status_badge = '<span style="color:#10b981;font-size:12px;font-weight:700;">Active</span>' if s["is_active"] else '<span style="color:#64748b;font-size:12px;">Inactive</span>'
        url_link = f'<a href="{s["url"]}" target="_blank" style="color:#7c3aed;font-size:13px;" rel="noopener">View Supplier ↗</a>' if s["url"] else "—"
        rows += f"""
        <tr>
            <td><strong>{s["name"]}</strong></td>
            <td>{url_link}</td>
            <td style="color:#94a3b8;">{s["contact_email"] or "—"}</td>
            <td>{s["shipping_days"]} days</td>
            <td>{status_badge}</td>
            <td style="color:#94a3b8;font-size:13px;max-width:200px;">{(s["notes"] or "")[:80]}</td>
            <td>
                <form method="post" action="/suppliers/{s["id"]}/delete" style="display:inline;">
                    <button type="submit" style="background:none;border:none;color:#ef4444;cursor:pointer;font-size:13px;padding:0;">Delete</button>
                </form>
            </td>
        </tr>"""

    empty = '<tr><td colspan="7" style="text-align:center;color:#475569;padding:32px;">No suppliers yet. Add your first one below.</td></tr>' if not suppliers else ""

    return layout(f"""
    <div class="container">
        {top_nav(user)}
        <a class="back" href="/dashboard">← Dashboard</a>
        <div class="panel">
            <p class="eyebrow">Dropshipping Tool</p>
            <h1>Supplier Manager</h1>
            <p>Keep track of all your suppliers, their URLs, shipping times, and notes in one place.</p>

            <div style="overflow-x:auto;margin-bottom:28px;">
                <table style="width:100%;border-collapse:collapse;font-size:14px;">
                    <thead>
                        <tr style="border-bottom:1px solid #1e293b;color:#64748b;font-size:12px;text-align:left;">
                            <th style="padding:10px 8px;">Name</th>
                            <th style="padding:10px 8px;">Supplier URL</th>
                            <th style="padding:10px 8px;">Contact</th>
                            <th style="padding:10px 8px;">Ship Time</th>
                            <th style="padding:10px 8px;">Status</th>
                            <th style="padding:10px 8px;">Notes</th>
                            <th style="padding:10px 8px;"></th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows}
                        {empty}
                    </tbody>
                </table>
            </div>

            <h2 style="font-size:18px;margin:0 0 14px;">Add Supplier</h2>
            <form method="post" action="/suppliers" style="display:grid;grid-template-columns:1fr 1fr;gap:12px;">
                <div>
                    <label>Supplier Name *</label>
                    <input name="name" required placeholder="e.g. AliExpress Vendor A" />
                </div>
                <div>
                    <label>Supplier URL</label>
                    <input name="url" placeholder="https://aliexpress.com/item/..." />
                </div>
                <div>
                    <label>Contact Email</label>
                    <input name="contact_email" type="email" placeholder="supplier@example.com" />
                </div>
                <div>
                    <label>Avg. Shipping Days</label>
                    <input name="shipping_days" type="number" value="7" min="1" />
                </div>
                <div style="grid-column:span 2;">
                    <label>Notes</label>
                    <textarea name="notes" placeholder="Minimum order, quality notes, communication tips..." style="min-height:80px;"></textarea>
                </div>
                <div style="grid-column:span 2;">
                    <button type="submit">Add Supplier</button>
                </div>
            </form>
        </div>
    </div>
    """, title="Supplier Manager")


@app.post("/suppliers")
def add_supplier(request: Request, name: str = Form(...), url: str = Form(""),
                 contact_email: str = Form(""), shipping_days: int = Form(7),
                 notes: str = Form("")):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO suppliers (user_id, name, url, contact_email, shipping_days, notes) VALUES (?, ?, ?, ?, ?, ?)",
        (user["id"], name.strip(), url.strip(), contact_email.strip(), shipping_days, notes.strip())
    )
    conn.commit()
    conn.close()
    return RedirectResponse("/suppliers", status_code=303)


@app.post("/suppliers/{supplier_id}/delete")
def delete_supplier(supplier_id: int, request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM suppliers WHERE id = ? AND user_id = ?", (supplier_id, user["id"]))
    conn.commit()
    conn.close()
    return RedirectResponse("/suppliers", status_code=303)


# -----------------------------
# DISCOUNT CODES
# -----------------------------
@app.get("/discounts", response_class=HTMLResponse)
def discounts_page(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM discount_codes WHERE user_id = ? ORDER BY id DESC", (user["id"],))
    codes = cur.fetchall()
    conn.close()

    rows = ""
    for c in codes:
        val_str = f"{int(c['value'])}%" if c["discount_type"] == "percentage" else f"${c['value']:.2f} off"
        max_str = str(c["max_uses"]) if c["max_uses"] else "Unlimited"
        status_html = '<span style="color:#10b981;font-weight:700;font-size:12px;">Active</span>' if c["is_active"] else '<span style="color:#64748b;font-size:12px;">Inactive</span>'
        expires = (c["expires_at"] or "")[:10] or "Never"
        rows += f"""
        <tr>
            <td><strong style="font-family:monospace;letter-spacing:.05em;">{c["code"]}</strong></td>
            <td>{val_str}</td>
            <td>{c["uses_count"]} / {max_str}</td>
            <td>{expires}</td>
            <td>{status_html}</td>
            <td style="display:flex;gap:8px;align-items:center;">
                <form method="post" action="/discounts/{c["id"]}/toggle" style="display:inline;">
                    <button type="submit" style="background:none;border:none;color:#7c3aed;cursor:pointer;font-size:13px;padding:0;">
                        {'Deactivate' if c["is_active"] else 'Activate'}
                    </button>
                </form>
                <form method="post" action="/discounts/{c["id"]}/delete" style="display:inline;">
                    <button type="submit" style="background:none;border:none;color:#ef4444;cursor:pointer;font-size:13px;padding:0;">Delete</button>
                </form>
            </td>
        </tr>"""

    empty = '<tr><td colspan="6" style="text-align:center;color:#475569;padding:32px;">No discount codes yet.</td></tr>' if not codes else ""

    return layout(f"""
    <div class="container">
        {top_nav(user)}
        <a class="back" href="/dashboard">← Dashboard</a>
        <div class="panel">
            <p class="eyebrow">Marketing Tool</p>
            <h1>Discount Codes</h1>
            <p>Create percentage or fixed-amount discount codes for your marketing campaigns.</p>

            <div style="overflow-x:auto;margin-bottom:28px;">
                <table style="width:100%;border-collapse:collapse;font-size:14px;">
                    <thead>
                        <tr style="border-bottom:1px solid #1e293b;color:#64748b;font-size:12px;text-align:left;">
                            <th style="padding:10px 8px;">Code</th>
                            <th style="padding:10px 8px;">Discount</th>
                            <th style="padding:10px 8px;">Uses</th>
                            <th style="padding:10px 8px;">Expires</th>
                            <th style="padding:10px 8px;">Status</th>
                            <th style="padding:10px 8px;">Actions</th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows}
                        {empty}
                    </tbody>
                </table>
            </div>

            <h2 style="font-size:18px;margin:0 0 14px;">Create Discount Code</h2>
            <form method="post" action="/discounts" style="display:grid;grid-template-columns:1fr 1fr;gap:12px;">
                <div>
                    <label>Code (leave blank to auto-generate)</label>
                    <input name="code" placeholder="e.g. SAVE20" style="text-transform:uppercase;" />
                </div>
                <div>
                    <label>Discount Type</label>
                    <select name="discount_type">
                        <option value="percentage">Percentage (%) off</option>
                        <option value="fixed">Fixed amount ($) off</option>
                    </select>
                </div>
                <div>
                    <label>Discount Value</label>
                    <input name="value" type="number" step="0.01" required placeholder="e.g. 20 for 20% or $20 off" />
                </div>
                <div>
                    <label>Max Uses (0 = unlimited)</label>
                    <input name="max_uses" type="number" value="0" min="0" />
                </div>
                <div>
                    <label>Expiry Date (optional)</label>
                    <input name="expires_at" type="date" />
                </div>
                <div style="display:flex;align-items:flex-end;">
                    <button type="submit" style="width:100%;">Create Code</button>
                </div>
            </form>
        </div>
    </div>
    """, title="Discount Codes")


@app.post("/discounts")
def create_discount(request: Request, code: str = Form(""), discount_type: str = Form("percentage"),
                    value: float = Form(...), max_uses: int = Form(0), expires_at: str = Form("")):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    final_code = code.strip().upper() or ("SAVE" + str(random.randint(1000, 9999)))
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO discount_codes (user_id, code, discount_type, value, max_uses, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
        (user["id"], final_code, discount_type, value, max_uses, expires_at.strip() or "")
    )
    conn.commit()
    conn.close()
    return RedirectResponse("/discounts", status_code=303)


@app.post("/discounts/{code_id}/toggle")
def toggle_discount(code_id: int, request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT is_active FROM discount_codes WHERE id = ? AND user_id = ?", (code_id, user["id"]))
    row = cur.fetchone()
    if row:
        cur.execute("UPDATE discount_codes SET is_active = ? WHERE id = ? AND user_id = ?",
                    (0 if row["is_active"] else 1, code_id, user["id"]))
        conn.commit()
    conn.close()
    return RedirectResponse("/discounts", status_code=303)


@app.post("/discounts/{code_id}/delete")
def delete_discount(code_id: int, request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM discount_codes WHERE id = ? AND user_id = ?", (code_id, user["id"]))
    conn.commit()
    conn.close()
    return RedirectResponse("/discounts", status_code=303)


# -----------------------------
# CLEAN STORE URLs  /{slug} → /s/{slug}
# Must be last route so it doesn't shadow any real paths
# -----------------------------
@app.get("/{slug}", response_class=HTMLResponse)
def store_shortlink(slug: str, request: Request):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM products WHERE slug = ?", (slug,))
    store = cur.fetchone()
    conn.close()
    if store:
        return RedirectResponse(f"/s/{slug}", status_code=301)
    return HTMLResponse("<h1>Not found</h1>", status_code=404)
