import sqlite3
import re
import hashlib
import os
import json
import stripe
import random
import shutil

from typing import List
from dotenv import load_dotenv
from openai import OpenAI
from PIL import Image
import pillow_heif

from fastapi import FastAPI, Form, Request, File, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

pillow_heif.register_heif_opener()

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PUBLIC_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

DB_NAME = "store.db"

UPLOAD_DIR = "static/uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

stripe.api_key = STRIPE_SECRET_KEY

PREMIUM_PRICE = 20

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
def db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def add_column_if_missing(cur, table, column, definition):
    cur.execute(f"PRAGMA table_info({table})")
    columns = [row["name"] for row in cur.fetchall()]
    if column not in columns:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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
        published INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS store_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        store_id INTEGER,
        user_id INTEGER,
        name TEXT,
        description TEXT,
        price REAL,
        stock INTEGER,
        image_url TEXT,
        image_urls TEXT DEFAULT '[]',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER,
        sender_type TEXT,
        sender_user_id INTEGER DEFAULT 0,
        sender_email TEXT DEFAULT '',
        message TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    add_column_if_missing(cur, "users", "password", "TEXT")
    add_column_if_missing(cur, "users", "store_name", "TEXT DEFAULT 'My Store'")
    add_column_if_missing(cur, "users", "stripe_account_id", "TEXT DEFAULT ''")
    add_column_if_missing(cur, "users", "stripe_onboarding_complete", "INTEGER DEFAULT 0")
    add_column_if_missing(cur, "users", "ai_uses", "INTEGER DEFAULT 0")

    add_column_if_missing(cur, "products", "slug", "TEXT")
    add_column_if_missing(cur, "products", "theme", "TEXT DEFAULT 'blue'")
    add_column_if_missing(cur, "products", "views", "INTEGER DEFAULT 0")
    add_column_if_missing(cur, "products", "tagline", "TEXT DEFAULT ''")
    add_column_if_missing(cur, "products", "cta", "TEXT DEFAULT 'Buy Now'")
    add_column_if_missing(cur, "products", "source", "TEXT DEFAULT 'manual'")
    add_column_if_missing(cur, "products", "ai_design", "TEXT DEFAULT '{}'")
    add_column_if_missing(cur, "products", "published", "INTEGER DEFAULT 0")

    add_column_if_missing(cur, "store_items", "image_urls", "TEXT DEFAULT '[]'")

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
def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()


def verify_password(password, hashed_password):
    if not hashed_password:
        return False
    return hash_password(password) == hashed_password


def slugify(text):
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    return text or "store"


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

    while True:
        if product_id:
            cur.execute("SELECT id FROM products WHERE slug = ? AND id != ?", (slug, product_id))
        else:
            cur.execute("SELECT id FROM products WHERE slug = ?", (slug,))

        exists = cur.fetchone()

        if not exists:
            conn.close()
            return slug

        slug = f"{base_slug}-{i}"
        i += 1


def get_current_user(request: Request):
    email = request.cookies.get("LaunchFlow_user")

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
    conn.close()

    if not user:
        return None

    if not verify_password(password, user["password"]):
        return None

    return user


def layout(content, title="LaunchFlow"):
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>{title}</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <link rel="stylesheet" href="/static/style.css">
    </head>

    <body>
        {content}

        <footer class="site-footer">
            <a href="/terms">Terms</a>
            <a href="/privacy">Privacy</a>
            <a href="/refunds">Refunds</a>
        </footer>

        <button id="chat-launcher" class="chat-launcher" type="button">
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

            async function refreshUnreadCount() {{
                const res = await fetch("/chat/unread-count");
                const data = await res.json();

                if (data.ok && data.unread > 0) {{
                    notificationDot.classList.remove("hidden");
                    notificationDot.textContent = data.unread;
                }} else {{
                    notificationDot.classList.add("hidden");
                    notificationDot.textContent = "0";
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
                                <input
                                    type="text"
                                    id="chat-input"
                                    placeholder="Type a message..."
                                    autocomplete="off"
                                    required
                                >
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

                if (!text || !currentConversationId || !messages) {{
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
                        conversation_id: currentConversationId,
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
        <a href="/manage-subscription" class="premium-pill premium-active">
            Premium
        </a>
        """
    else:
        premium_button = """
        <a href="/upgrade" class="upgrade-pill">
            Upgrade
        </a>
        """

    stripe_ready = bool(user["stripe_account_id"] and user["stripe_onboarding_complete"])

    stripe_badge = """
    <a href="/settings" class="nav-status ready">
        Ready to sell
    </a>
    """ if stripe_ready else """
    <a href="/settings" class="nav-status warning">
        Setup payments
    </a>
    """

    return f"""
    <nav class="top-nav">
        <div class="nav-left">
            <a class="brand" href="/dashboard">LaunchFlow</a>
        </div>

        <div class="nav-links">
            <a href="/dashboard">Dashboard</a>
            <a href="/discover">Discover</a>
            <a href="/ai-builder">AI Builder</a>
            <a href="/viral-products">Viral Products</a>
            <a href="/analytics">Analytics</a>
            <a href="/orders">Orders</a>
            <a href="/settings">Settings</a>

            {stripe_badge}
            {premium_button}

            <a href="/logout">Log out</a>
        </div>
    </nav>
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
garage, luxury, streetwear, beauty, tech, editorial

Rules for template_type:
- cars, car accessories, detailing, rustic, garage, tools, outdoor gear = garage
- shampoo, skincare, haircare, beauty, feminine, wellness, clean beauty = beauty
- gaming, streetwear, hype, culture, clothing drops, bold urban = streetwear
- tech, futuristic, software, gadgets, AI, electronics = tech
- luxury, elegant, premium, jewelry, watches, refined = luxury
- books, education, learning, journals, courses, knowledge, content = editorial

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
store_mood

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

        if any(word in text for word in ["shampoo", "hair", "skincare", "skin", "beauty", "makeup", "feminine", "wellness", "soap"]):
            return "beauty"

        if any(word in text for word in ["gaming", "streetwear", "clothing", "hype", "drop", "urban", "culture"]):
            return "streetwear"

        if any(word in text for word in ["tech", "ai", "software", "gadget", "electronics", "future", "futuristic"]):
            return "tech"

        if any(word in text for word in ["luxury", "premium", "elegant", "jewelry", "watch", "watches", "designer"]):
            return "luxury"

        if any(word in text for word in ["book", "books", "learning", "education", "course", "journal", "knowledge", "study"]):
            return "editorial"

        return "editorial"

    def safe_choice(value, allowed, fallback):
        value = str(value or "").strip().lower()
        return value if value in allowed else fallback

    try:
        response = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {
                    "role": "system",
                    "content": "Return only clean valid JSON. No markdown. No explanation."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            temperature=1.0
        )

        content = response.choices[0].message.content.strip()
        data = json.loads(content)

        store_name = data.get("store_name", "").strip() or "Generated Store"

        allowed_templates = ["garage", "luxury", "streetwear", "beauty", "tech", "editorial"]
        template_type = safe_choice(
            data.get("template_type"),
            allowed_templates,
            choose_template_fallback(f"{idea} {audience_text} {vibe_text}")
        )

        allowed_themes = ["blue", "purple", "green", "orange", "dark"]
        theme = safe_choice(data.get("theme"), allowed_themes, "dark")

        if theme not in allowed_themes:
            if template_type == "garage":
                theme = "orange"
            elif template_type == "beauty":
                theme = "purple"
            elif template_type == "tech":
                theme = "blue"
            elif template_type in ["luxury", "streetwear"]:
                theme = "dark"
            else:
                theme = "blue"

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
            "store_mood": data.get("store_mood", "Premium, polished, and conversion-focused")
        }

    except Exception as e:
        print("AI generation error:", e)

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
            "homepage_copy": "This store has a clear brand direction and is ready for real products.",
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
            "store_mood": "Premium, polished, and conversion-focused"
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
    response.set_cookie("LaunchFlow_user", user["email"], max_age=60 * 60 * 24 * 30)
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
def login(email: str = Form(...), password: str = Form(...)):
    user = login_user(email, password)

    if not user:
        return RedirectResponse("/login?error=invalid", status_code=303)

    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie("LaunchFlow_user", user["email"], max_age=60 * 60 * 24 * 30)
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
                <a href="/s/{p["slug"]}">View Store</a>
                <a href="/stores/{p["slug"]}/add-product">Add Product</a>
                <a href="/edit/{p["id"]}">Edit</a>
            </div>

            <div class="store-footer">
                <span class="copy-link">
                    /s/{p["slug"]}
                </span>

                <a
                    class="danger-link"
                    href="/delete/{p["id"]}"
                    onclick="return confirm('Delete this store?')"
                >
                    Delete
                </a>
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
    """, title="Dashboard")


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

                <input type="hidden" name="price" value="0">
                <input type="hidden" name="stock" value="0">
                <input type="hidden" name="image_url" value="">
                <input type="hidden" name="cta" value="Add Product">
                <input type="hidden" name="source" value="manual">

                <button type="submit">Create Store</button>
            </form>
        </div>
    </div>
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
    viral_product_name: str = Form(""),
    viral_product_description: str = Form(""),
    viral_product_price: str = Form(""),
    viral_product_stock: str = Form("10"),
    viral_product_image_url: str = Form("")
):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

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

    cur.execute("""
    INSERT INTO products (
        user_id, name, description, price, stock, image_url, slug, theme,
        views, tagline, cta, source, ai_design, published
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, 0)
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
        ai_design
    ))

    store_id = cur.lastrowid

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
            <p>The AI will generate your store name, tagline, theme, direction, and starter layout.</p>

            <form id="ai-builder-form" action="/ai-generate" method="post">
                <label>What are you selling?</label>
                <textarea name="product_idea" required placeholder="Example: shampoo"></textarea>

                <label>Who is it for?</label>
                <input name="audience" placeholder="Example: people who want clean hair">

                <label>What vibe should the store have?</label>
                <input name="vibe" placeholder="Example: Japanese, clean, premium">

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
            "Building starter categories...",
            "Writing homepage copy...",
            "Finalizing your store draft..."
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
    vibe: str = Form("")
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
            img = Image.open(image.file)

            if img.mode != "RGB":
                img = img.convert("RGB")

            safe_name = f"{random.randint(100000, 999999)}-{safe_base}.jpg"
            file_path = os.path.join(UPLOAD_DIR, safe_name)

            img.save(file_path, "JPEG", quality=95)

            uploaded_paths.append(f"/static/uploads/{safe_name}")

        except Exception as e:
            print("IMAGE SAVE ERROR:", e)
            continue

    main_image = uploaded_paths[0] if uploaded_paths else ""

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
        store["id"],
        user["id"],
        name.strip(),
        description.strip(),
        clean_price(price),
        clean_stock(stock),
        main_image,
        json.dumps(uploaded_paths)
    ))

    conn.commit()
    conn.close()

    return RedirectResponse(f"/s/{slug}#products", status_code=303)




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

    if user["is_pro"] == 0 and product_count >= 5:
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
            img = Image.open(image.file)

            if img.mode != "RGB":
                img = img.convert("RGB")

            safe_name = f"{random.randint(100000, 999999)}-{safe_base}.jpg"
            file_path = os.path.join(UPLOAD_DIR, safe_name)

            img.save(file_path, "JPEG", quality=95)

            uploaded_paths.append(f"/static/uploads/{safe_name}")

        except Exception as e:
            print("IMAGE SAVE ERROR:", e)
            continue

    print("UPLOADED PATHS:", uploaded_paths)

    main_image = uploaded_paths[0] if uploaded_paths else ""

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
        store["id"],
        user["id"],
        name,
        description,
        clean_price(price),
        clean_stock(stock),
        main_image,
        json.dumps(uploaded_paths)
    ))

    conn.commit()
    conn.close()

    return RedirectResponse(f"/s/{slug}#products", status_code=303)

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
    """, title="Upgrade")

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
    ORDER BY created_at DESC
    """, (p["id"],))

    store_items = cur.fetchall()
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

    badge_html = ""

    for badge in trust_badges[:3]:
        badge_html += f"""
        <div>
            <strong>{badge}</strong>
            <span>Built with LaunchFlow</span>
        </div>
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
                """

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

    dashboard_link = f'<a href="/dashboard">Dashboard</a>' if is_owner else ""
    add_product_link = f'<a href="/stores/{p["slug"]}/add-product">Add Product</a>' if is_owner else ""

    hero_action = ""

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
            onclick="openSellerChat('{p["user_id"]}', `{p["name"]}`, '{p["id"]}')"
        >
            Message Seller
        </button>
        """

    return layout(f"""
    <div
        class="public-store theme-{p["theme"]} ai-store ai-design-{design_style} ai-hero-{hero_layout} ai-bg-{background_style} ai-font-{font_style} ai-button-{button_style}"
        style="--ai-accent:{accent_color}; --ai-secondary:{secondary_color};"
    >

        <nav class="public-store-nav">
            <strong>{p["name"]}</strong>

            <div>
                {dashboard_link}
                <a href="#products">Products</a>
                {add_product_link}
            </div>
        </nav>

        <section class="storefront-hero ai-section-{section_style}">
            <div class="storefront-hero-content">
                <p class="eyebrow">Storefront</p>

                <h1>{ai_design.get("hero_headline", p["name"])}</h1>

                <h2>{p["tagline"] or ai_design.get("hero_subheadline", "")}</h2>

                <p>{p["description"]}</p>

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
                </div>
            </div>
        </section>

        <div class="container narrow">
            {owner_controls}
        </div>

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
    """, title=p["name"])


@app.post("/publish-store/{store_id}")
def publish_store(store_id: int, request: Request):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    conn = db()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE products
        SET published = 1
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

    cur.execute(
        "SELECT * FROM store_items WHERE id = ?",
        (item_id,)
    )

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

    cur.execute(
        "SELECT * FROM products WHERE id = ?",
        (item["store_id"],)
    )

    store = cur.fetchone()

    cur.execute(
        "SELECT * FROM users WHERE id = ?",
        (item["user_id"],)
    )

    seller = cur.fetchone()

    conn.close()

    try:
        image_list = json.loads(item["image_urls"] or "[]")
    except Exception:
        image_list = []

    if not image_list and item["image_url"]:
        image_list = [item["image_url"]]

    main_image = (
        image_list[0]
        if image_list
        else "https://images.unsplash.com/photo-1523275335684-37898b6baf30?auto=format&fit=crop&w=900&q=80"
    )

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

    is_owner = bool(
        user and user["id"] == item["user_id"]
    )

    owner_buttons = ""

    if is_owner:
        owner_buttons = f"""
        <div class="product-owner-actions">

            <a
                href="/product/{item["id"]}/edit"
                class="button"
            >
                Edit Product
            </a>

            <form
                action="/product/{item["id"]}/delete"
                method="post"
                onsubmit="return confirm('Delete this product?')"
            >

                <button
                    type="submit"
                    class="delete-product-btn"
                >
                    Delete Product
                </button>

            </form>

        </div>
        """

    stock_text = (
        "Sold out"
        if item["stock"] <= 0
        else f'{item["stock"]} left in stock'
    )

    buy_disabled = (
        "disabled"
        if item["stock"] <= 0
        else ""
    )

    message_button = ""

    if not is_owner:
        message_button = f"""
        <button
            type="button"
            class="button ghost"
            onclick="openSellerChat('{seller["id"]}', `{store["name"]}`)"
        >
            Message Seller
        </button>
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

                <p class="eyebrow">
                    Product
                </p>

                <h1>
                    {item["name"]}
                </h1>

                <p class="product-detail-description">
                    {item["description"]}
                </p>

                <div class="product-detail-meta">

                    <div>
                        <span>Price</span>
                        <strong>
                            ${money(item["price"])}
                        </strong>
                    </div>

                    <div>
                        <span>Availability</span>
                        <strong>
                            {stock_text}
                        </strong>
                    </div>

                </div>

                <div class="product-seller-box">

                    <div>
                        <strong>
                            Sold by {store["name"]}
                        </strong>

                        <p class="muted">
                            Secure checkout through LaunchFlow
                        </p>
                    </div>

                    {message_button}

                </div>

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

                </form>

                <div class="product-detail-note">

                    <strong>
                        Secure checkout
                    </strong>

                    <p>
                        Payments are processed securely through LaunchFlow checkout.
                    </p>

                </div>

                {owner_buttons}

            </div>

        </div>

    </div>
    """

    return layout(
        html,
        title=item["name"]
    )


@app.get("/product/{item_id}/edit", response_class=HTMLResponse)
def edit_product_page(request: Request, item_id: int):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    conn = db()
    cur = conn.cursor()

    cur.execute("""
    SELECT
        store_items.*,
        products.slug as store_slug,
        products.name as store_name
    FROM store_items
    JOIN products ON store_items.store_id = products.id
    WHERE store_items.id = ?
    AND store_items.user_id = ?
    """, (item_id, user["id"]))

    item = cur.fetchone()
    conn.close()

    if not item:
        return RedirectResponse("/dashboard", status_code=303)

    return layout(f"""
    <div class="container narrow">
        {top_nav(user)}

        <a class="back" href="/s/{item["store_slug"]}#products">
            ← Back to Store
        </a>

        <div class="panel">
            <p class="eyebrow">Edit product</p>
            <h1>Edit {item["name"]}</h1>

            <form
                action="/product/{item["id"]}/edit"
                method="post"
                enctype="multipart/form-data"
            >
                <label>Product Name</label>
                <input name="name" value="{item["name"]}" required>

                <label>Product Description</label>
                <textarea name="description" required>{item["description"]}</textarea>

                <label>Price</label>
                <input name="price" class="money-input" value="${money(item["price"])}" required>

                <label>Stock</label>
                <input name="stock" value="{item["stock"]}" required>

                <label>Replace Product Photos</label>

                <label class="upload-box" for="edit-product-images">
                    <input
                        id="edit-product-images"
                        name="images"
                        type="file"
                        accept="image/*"
                        multiple
                        style="display:none;"
                    >

                    <strong>📸 Add Product Photos</strong>

                    <p>
                        Click this box to upload new photos.<br>
                        Upload one or multiple images. Max 10 photos.
                    </p>
                </label>

                <p class="muted">
                    If you upload new photos, the first one becomes the main image.
                </p>

                <button type="submit">
                    Save Changes
                </button>
            </form>
        </div>
    </div>
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

    cur.execute("""
    SELECT store_items.*, products.slug as store_slug
    FROM store_items
    JOIN products ON store_items.store_id = products.id
    WHERE store_items.id = ? AND store_items.user_id = ?
    """, (item_id, user["id"]))

    item = cur.fetchone()

    if not item:
        conn.close()
        return RedirectResponse("/dashboard", status_code=303)

    uploaded_paths = []

    for image in images[:10]:
        if not image or not image.filename:
            continue

        file_ext = os.path.splitext(image.filename)[1].lower()

        if file_ext not in [".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif"]:
            continue

        safe_base = slugify(os.path.splitext(image.filename)[0]) or "product-image"

        try:
            img = Image.open(image.file)

            if img.mode != "RGB":
                img = img.convert("RGB")

            safe_name = f"{random.randint(100000, 999999)}-{safe_base}.jpg"
            file_path = os.path.join(UPLOAD_DIR, safe_name)

            img.save(file_path, "JPEG", quality=95)

            uploaded_paths.append(f"/static/uploads/{safe_name}")

        except Exception as e:
            print("IMAGE UPDATE ERROR:", e)
            continue

    if uploaded_paths:
        main_image = uploaded_paths[0]
        image_urls = json.dumps(uploaded_paths)

        cur.execute("""
        UPDATE store_items
        SET name = ?,
            description = ?,
            price = ?,
            stock = ?,
            image_url = ?,
            image_urls = ?
        WHERE id = ? AND user_id = ?
        """, (
            name,
            description,
            clean_price(price),
            clean_stock(stock),
            main_image,
            image_urls,
            item_id,
            user["id"]
        ))

    else:
        cur.execute("""
        UPDATE store_items
        SET name = ?,
            description = ?,
            price = ?,
            stock = ?
        WHERE id = ? AND user_id = ?
        """, (
            name,
            description,
            clean_price(price),
            clean_stock(stock),
            item_id,
            user["id"]
        ))

    conn.commit()
    store_slug = item["store_slug"]
    conn.close()

    return RedirectResponse(f"/s/{store_slug}#products", status_code=303)


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

    cur.execute("""
    SELECT store_items.*, products.slug as store_slug
    FROM store_items
    JOIN products ON store_items.store_id = products.id
    WHERE store_items.id = ? AND store_items.user_id = ?
    """, (item_id, user["id"]))

    item = cur.fetchone()

    if not item:
        conn.close()
        return RedirectResponse("/dashboard", status_code=303)

    uploaded_paths = []

    for image in images[:10]:
        if not image or not image.filename:
            continue

        file_ext = os.path.splitext(image.filename)[1].lower()

        if file_ext not in [".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif"]:
            continue

        safe_base = slugify(os.path.splitext(image.filename)[0]) or "product-image"

        try:
            img = Image.open(image.file)

            if img.mode != "RGB":
                img = img.convert("RGB")

            safe_name = f"{random.randint(100000, 999999)}-{safe_base}.jpg"
            file_path = os.path.join(UPLOAD_DIR, safe_name)

            img.save(file_path, "JPEG", quality=95)

            uploaded_paths.append(f"/static/uploads/{safe_name}")

        except Exception as e:
            print("IMAGE UPDATE ERROR:", e)
            continue

    if uploaded_paths:
        main_image = uploaded_paths[0]
        image_urls = json.dumps(uploaded_paths)

        cur.execute("""
        UPDATE store_items
        SET name = ?,
            description = ?,
            price = ?,
            stock = ?,
            image_url = ?,
            image_urls = ?
        WHERE id = ? AND user_id = ?
        """, (
            name,
            description,
            clean_price(price),
            clean_stock(stock),
            main_image,
            image_urls,
            item_id,
            user["id"]
        ))

    else:
        cur.execute("""
        UPDATE store_items
        SET name = ?,
            description = ?,
            price = ?,
            stock = ?
        WHERE id = ? AND user_id = ?
        """, (
            name,
            description,
            clean_price(price),
            clean_stock(stock),
            item_id,
            user["id"]
        ))

    conn.commit()
    store_slug = item["store_slug"]
    conn.close()

    return RedirectResponse(f"/s/{store_slug}#products", status_code=303)


@app.post("/checkout-item/{item_id}")
def checkout_item(
    item_id: int,
    customer_email: str = Form(...),
    customer_name: str = Form(""),
    quantity: int = Form(1),
    shipping_name: str = Form(""),
    shipping_address_line1: str = Form(""),
    shipping_address_line2: str = Form(""),
    shipping_city: str = Form(""),
    shipping_state: str = Form(""),
    shipping_postal_code: str = Form(""),
    shipping_country: str = Form("US"),
    buyer_message: str = Form("")
):

    quantity = max(1, int(quantity))

    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT * FROM store_items WHERE id = ?",
        (item_id,)
    )

    item = cur.fetchone()

    if not item:
        conn.close()
        return RedirectResponse("/", status_code=303)

    if item["stock"] <= 0:
        conn.close()
        return RedirectResponse(f"/product/{item_id}", status_code=303)

    if quantity > item["stock"]:
        conn.close()

        return layout(f"""
        <div class="container narrow center">
            <div class="panel">
                <h1>Not enough stock</h1>
                <p>Only {item["stock"]} left in stock.</p>
                <a class="button" href="/product/{item_id}">Back to product</a>
            </div>
        </div>
        """)

    cur.execute(
        "SELECT * FROM users WHERE id = ?",
        (item["user_id"],)
    )

    seller = cur.fetchone()

    if (
        not seller
        or not seller["stripe_account_id"]
        or not seller["stripe_onboarding_complete"]
    ):
        conn.close()

        return layout("""
        <div class="container narrow center">
            <div class="panel">
                <h1>Seller payments not ready</h1>
                <p>This seller has not finished Stripe setup yet.</p>
                <a class="button" href="/">Back home</a>
            </div>
        </div>
        """)

    amount_cents = int(float(item["price"]) * 100)
    total_cents = amount_cents * quantity
    platform_fee = int(total_cents * 0.10)

    conn.close()

    base_url = os.getenv("BASE_URL", BASE_URL)

    checkout_session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        mode="payment",
        customer_email=customer_email,

        line_items=[
            {
                "price_data": {
                    "currency": "usd",
                    "product_data": {
                        "name": item["name"],
                        "description": item["description"],
                    },
                    "unit_amount": amount_cents,
                },
                "quantity": quantity,
            }
        ],

        payment_intent_data={
            "application_fee_amount": platform_fee,
            "transfer_data": {
                "destination": seller["stripe_account_id"],
            },
        },

        metadata={
            "store_item_id": str(item["id"]),
            "store_id": str(item["store_id"]),
            "seller_id": str(item["user_id"]),

            "customer_email": customer_email,
            "customer_name": customer_name,

            "quantity": str(quantity),

            "shipping_name": shipping_name,
            "shipping_address_line1": shipping_address_line1,
            "shipping_address_line2": shipping_address_line2,
            "shipping_city": shipping_city,
            "shipping_state": shipping_state,
            "shipping_postal_code": shipping_postal_code,
            "shipping_country": shipping_country,

            "buyer_message": buyer_message,
        },

        success_url=f"{base_url}/success-item/{item_id}?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{base_url}/product/{item_id}",
    )

    return RedirectResponse(
        checkout_session.url,
        status_code=303
    )


@app.get("/success-item/{item_id}", response_class=HTMLResponse)
def success_item(item_id: int, session_id: str = ""):

    if not session_id:
        return RedirectResponse(
            f"/product/{item_id}",
            status_code=303
        )

    try:
        session = stripe.checkout.Session.retrieve(session_id)

        if (
            session.status != "complete"
            or session.payment_status != "paid"
        ):
            return RedirectResponse(
                f"/product/{item_id}",
                status_code=303
            )

    except Exception as e:
        print("Product checkout success error:", e)

        return RedirectResponse(
            f"/product/{item_id}",
            status_code=303
        )

    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT * FROM store_items WHERE id = ?",
        (item_id,)
    )

    item = cur.fetchone()

    if not item:
        conn.close()

        return layout("""
        <div class="container">
            <h1>Product not found</h1>
        </div>
        """)

    cur.execute(
        "SELECT * FROM orders WHERE stripe_session_id = ?",
        (session_id,)
    )

    existing_order = cur.fetchone()

    order_id = None

    if existing_order:
        order_id = existing_order["id"]

    else:
        quantity = int(
            session.metadata.get("quantity", "1")
        )

        if item["stock"] < quantity:
            conn.close()

            return layout("""
            <div class="container narrow center">
                <div class="panel">
                    <h1>Stock issue</h1>
                    <p>
                        This order was paid, but there is not enough stock left.
                    </p>
                    <a class="button" href="/">Back home</a>
                </div>
            </div>
            """)

        customer_email = session.metadata.get("customer_email", "")
        customer_name = session.metadata.get("customer_name", "")

        total_amount = float(item["price"]) * quantity

        cur.execute("""
        INSERT INTO orders (
            product_id,
            store_item_id,
            amount,

            customer_email,
            customer_name,

            quantity,

            shipping_name,
            shipping_address_line1,
            shipping_address_line2,
            shipping_city,
            shipping_state,
            shipping_postal_code,
            shipping_country,

            stripe_session_id,

            payment_status,

            shipping_status,

            fulfillment_status,

            buyer_message
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            item["store_id"],
            item["id"],

            total_amount,

            customer_email,
            customer_name,

            quantity,

            session.metadata.get("shipping_name", ""),
            session.metadata.get("shipping_address_line1", ""),
            session.metadata.get("shipping_address_line2", ""),
            session.metadata.get("shipping_city", ""),
            session.metadata.get("shipping_state", ""),
            session.metadata.get("shipping_postal_code", ""),
            session.metadata.get("shipping_country", ""),

            session_id,

            "paid",

            "Not shipped yet",

            "New order",

            session.metadata.get("buyer_message", "")
        ))

        order_id = cur.lastrowid

        cur.execute(
            """
            UPDATE store_items
            SET stock = stock - ?
            WHERE id = ?
            AND stock >= ?
            """,
            (quantity, item_id, quantity)
        )

        conn.commit()

    conn.close()

    return layout(f"""
    <div class="container narrow center">
        <div class="panel success-panel">
            <h1>Payment successful 🎉</h1>

            <p>Your order has been confirmed.</p>

            <p class="muted">
                Order ID:
                <strong>{order_id}</strong>
            </p>

            <div class="success-actions">
                <a class="button" href="/track-order/{order_id}">
                    Track your order
                </a>

                <a class="button ghost" href="/">
                    Back home
                </a>
            </div>
        </div>
    </div>
    """)


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

            <form action="/update/{p["id"]}" method="post">
                <label>Store name</label>
                <input name="name" value="{p["name"]}" required>

                <label>Custom URL</label>
                <input name="slug" value="{p["slug"]}" required>

                <label>Tagline</label>
                <input name="tagline" value="{p["tagline"] or ""}">

                <label>Description</label>
                <textarea name="description" required>{p["description"]}</textarea>

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
    cta: str = Form("Add Product"),
    theme: str = Form("blue")
):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    final_slug = unique_slug(slugify(slug), product_id=product_id)

    conn = db()
    cur = conn.cursor()

    cur.execute("""
    UPDATE products
    SET name = ?, slug = ?, tagline = ?, description = ?, cta = ?, theme = ?
    WHERE id = ? AND user_id = ?
    """, (name, final_slug, tagline, description, cta, theme, product_id, user["id"]))

    conn.commit()
    conn.close()

    return RedirectResponse(f"/s/{final_slug}", status_code=303)


@app.get("/delete/{product_id}")
def delete_product(request: Request, product_id: int):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    conn = db()
    cur = conn.cursor()

    cur.execute("DELETE FROM orders WHERE product_id = ?", (product_id,))
    cur.execute("DELETE FROM store_items WHERE store_id = ?", (product_id,))
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

    conversion_rate = 0
    if views and views > 0:
        conversion_rate = round((orders_count / views) * 100, 1)

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
            <p>Add products and start sharing your stores.</p>
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

        <a class="back" href="/dashboard">
            ← Dashboard
        </a>

        <section class="hero analytics-hero">
            <p class="eyebrow">Analytics</p>

            <h1>Your store performance.</h1>

            <p>
                Track revenue, views, conversion rate, top products,
                and customer activity.
            </p>
        </section>

        <section class="stats modern-stats">
            <div>
                <h3>${money(revenue)}</h3>
                <p>Total Revenue</p>
            </div>

            <div>
                <h3>{orders_count}</h3>
                <p>Total Orders</p>
            </div>

            <div>
                <h3>{views}</h3>
                <p>Total Views</p>
            </div>

            <div>
                <h3>{conversion_rate}%</h3>
                <p>Conversion Rate</p>
            </div>
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
                    <p class="eyebrow">Activity</p>
                    <h2>Recent Orders</h2>
                </div>
            </div>

            {recent_rows}
        </div>
    </div>
    """, title="Analytics")


@app.get("/orders", response_class=HTMLResponse)
def orders(request: Request):

    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    conn = db()
    cur = conn.cursor()

    cur.execute("""

    SELECT
        orders.*,
        products.name as store_name,
        store_items.name as item_name

    FROM orders

    JOIN products
    ON orders.product_id = products.id

    LEFT JOIN store_items
    ON orders.store_item_id = store_items.id

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

        shipping_status = (
            o["shipping_status"]
            or "Not shipped yet"
        )

        fulfillment_status = (
            o["fulfillment_status"]
            or "New order"
        )

        buyer_message = (
            o["buyer_message"]
            or ""
        )

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
            <a
                class="button small"
                href="/track-order/{o["id"]}"
            >
                Track Shipping
            </a>
            """

        rows += f"""
        <div class="order-row advanced-order-row">

            <div class="advanced-order-top">

                <div>

                    <strong>
                        {item_name}
                    </strong>

                    <p class="muted">
                        Customer: {o["customer_email"]}
                    </p>

                    <p class="muted">
                        Ordered: {o["created_at"]}
                    </p>

                </div>

                <div class="order-price-box">

                    <strong>
                        ${money(o["amount"])}
                    </strong>

                    <p class="muted">
                        Qty: {o["quantity"]}
                    </p>

                    <p class="muted">
                        Payment: {o["payment_status"]}
                    </p>

                </div>

            </div>

            <div class="order-grid">

                <div class="order-box">

                    <h3>
                        Shipping Address
                    </h3>

                    <p>
                        {shipping_address}
                    </p>

                </div>

                <div class="order-box">

                    <h3>
                        Buyer Message
                    </h3>

                    <p>
                        {buyer_message or "No buyer message."}
                    </p>

                </div>

            </div>

            <form
                action="/orders/{o["id"]}/shipping"
                method="post"
                class="advanced-order-form"
            >

                <div class="form-grid">

                    <div>

                        <label>
                            Carrier
                        </label>

                        <input
                            name="shipping_carrier"
                            value="{shipping_carrier}"
                            placeholder="USPS, UPS, FedEx"
                        >

                    </div>

                    <div>

                        <label>
                            Tracking Number
                        </label>

                        <input
                            name="tracking_number"
                            value="{tracking_number}"
                            placeholder="Tracking number"
                        >

                    </div>

                </div>

                <div class="form-grid">

                    <div>

                        <label>
                            Shipping Status
                        </label>

                        <select name="shipping_status">

                            <option
                                value="Not shipped yet"
                                {"selected" if shipping_status == "Not shipped yet" else ""}
                            >
                                Not shipped yet
                            </option>

                            <option
                                value="Processing"
                                {"selected" if shipping_status == "Processing" else ""}
                            >
                                Processing
                            </option>

                            <option
                                value="Shipped"
                                {"selected" if shipping_status == "Shipped" else ""}
                            >
                                Shipped
                            </option>

                            <option
                                value="Delivered"
                                {"selected" if shipping_status == "Delivered" else ""}
                            >
                                Delivered
                            </option>

                        </select>

                    </div>

                    <div>

                        <label>
                            Fulfillment Status
                        </label>

                        <select name="fulfillment_status">

                            <option
                                value="New order"
                                {"selected" if fulfillment_status == "New order" else ""}
                            >
                                New order
                            </option>

                            <option
                                value="Packing"
                                {"selected" if fulfillment_status == "Packing" else ""}
                            >
                                Packing
                            </option>

                            <option
                                value="Ready to ship"
                                {"selected" if fulfillment_status == "Ready to ship" else ""}
                            >
                                Ready to ship
                            </option>

                            <option
                                value="Completed"
                                {"selected" if fulfillment_status == "Completed" else ""}
                            >
                                Completed
                            </option>

                        </select>

                    </div>

                </div>

                <div class="order-actions">

                    <button type="submit">
                        Save Order Updates
                    </button>

                    {track_link}

                </div>

            </form>

        </div>
        """

    if not rows:

        rows = """
        <p class='muted'>
            No orders yet.
        </p>
        """

    return layout(f"""
    <div class="container">

        {top_nav(user)}

        <a class="back" href="/dashboard">
            ← Dashboard
        </a>

        <section class="hero">

            <p class="eyebrow">
                Fulfillment
            </p>

            <h1>
                Orders
            </h1>

            <p>
                Manage customer purchases, shipping, fulfillment, and tracking.
            </p>

        </section>

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
        "/orders",
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

    JOIN products
    ON orders.product_id = products.id

    LEFT JOIN store_items
    ON orders.store_item_id = store_items.id

    WHERE orders.id = ?

    """, (order_id,))

    order = cur.fetchone()

    conn.close()

    if not order:

        return layout("""
        <div class="container narrow center">

            <div class="panel">

                <h1>
                    Order not found
                </h1>

                <a class="button" href="/">
                    Back home
                </a>

            </div>

        </div>
        """)

    tracking_number = (
        order["tracking_number"]
        or ""
    )

    shipping_carrier = (
        order["shipping_carrier"]
        or "Not added yet"
    )

    shipping_status = (
        order["shipping_status"]
        or "Not shipped yet"
    )

    fulfillment_status = (
        order["fulfillment_status"]
        or "New order"
    )

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
        <a
            class="button"
            href="{tracking_link}"
            target="_blank"
        >
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

                    <strong>
                        {order["item_name"] or order["store_name"]}
                    </strong>

                </div>

                <div class="tracking-box">

                    <span>Shipping Status</span>

                    <strong>
                        {shipping_status}
                    </strong>

                </div>

                <div class="tracking-box">

                    <span>Fulfillment</span>

                    <strong>
                        {fulfillment_status}
                    </strong>

                </div>

                <div class="tracking-box">

                    <span>Carrier</span>

                    <strong>
                        {shipping_carrier}
                    </strong>

                </div>

            </div>

            <div class="tracking-number-box">

                <span>
                    Tracking Number
                </span>

                <strong>
                    {tracking_number or "Not added yet"}
                </strong>

            </div>

            <div class="tracking-actions">

                {track_button}

                <a class="button ghost" href="/">
                    Back home
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

    stripe_ready = bool(
        user["stripe_account_id"] and
        user["stripe_onboarding_complete"]
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
        print("CONNECT BUTTON CLICKED")
        print("User ID:", user["id"])

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

        base_url = os.getenv("BASE_URL", "http://127.0.0.1:8000")

        account_link = stripe.AccountLink.create(
            account=stripe_account_id,
            refresh_url=f"{base_url}/stripe-connect-refresh",
            return_url=f"{base_url}/stripe-connect-return",
            type="account_onboarding",
        )

        conn.close()
        return RedirectResponse(account_link.url, status_code=303)

    except Exception as e:
        print("Stripe Connect error:", e)
        conn.close()
        return RedirectResponse("/settings", status_code=303)


@app.get("/stripe-connect-refresh")
def stripe_connect_refresh(request: Request):
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

        onboarding_complete = (
            account.details_submitted
            and account.charges_enabled
            and account.payouts_enabled
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

        print("CONNECTED:", onboarding_complete)
        print("ACCOUNT:", stripe_account_id)

    except Exception as e:

        print("STRIPE CONNECT RETURN ERROR")
        print(str(e))

    conn.close()

    return RedirectResponse("/settings", status_code=303)


@app.get("/manage-subscription", response_class=HTMLResponse)
def manage_subscription(request: Request):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    if not user["is_pro"]:
        return RedirectResponse("/upgrade", status_code=303)

    return layout(f"""
    <div class="container narrow center">
        {top_nav(user)}

        <div class="panel upgrade-panel">
            <p class="eyebrow">Subscription</p>

            <h1>Premium Active</h1>

            <p>
                Manage your Premium subscription through Stripe.
            </p>

            <div class="price">$20<span>/month</span></div>

            <br>

            <form action="/stripe-billing-portal" method="post">
                <button type="submit">
                    Manage or Cancel Subscription
                </button>
            </form>

            <br>

            <a class="subtle-link" href="/dashboard">
                Back to Dashboard
            </a>
        </div>
    </div>
    """)


@app.post("/stripe-billing-portal")
def stripe_billing_portal(request: Request):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    try:
        customers = stripe.Customer.list(
            email=user["email"],
            limit=1
        )

        if not customers.data:
            return RedirectResponse("/upgrade", status_code=303)

        customer = customers.data[0]

        base_url = os.getenv("BASE_URL", "http://127.0.0.1:8000")

        portal_session = stripe.billing_portal.Session.create(
            customer=customer.id,
            return_url=f"{base_url}/dashboard"
        )

        return RedirectResponse(portal_session.url, status_code=303)

    except Exception as e:
        print("Stripe billing portal error:", e)
        return RedirectResponse("/dashboard", status_code=303)


@app.get("/discover", response_class=HTMLResponse)
def discover(request: Request, q: str = ""):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login", status_code=303)

    conn = db()
    cur = conn.cursor()

    if q.strip():
        search = f"%{q.strip()}%"

        cur.execute("""
        SELECT
            products.*,
            users.store_name as seller_name
        FROM products
        JOIN users ON products.user_id = users.id
        WHERE products.published = 1
        AND (
            products.name LIKE ?
            OR products.description LIKE ?
            OR products.tagline LIKE ?
            OR users.store_name LIKE ?
            OR users.email LIKE ?
        )
        ORDER BY products.views DESC, products.id DESC
        """, (search, search, search, search, search))
    else:
        cur.execute("""
        SELECT
            products.*,
            users.store_name as seller_name
        FROM products
        JOIN users ON products.user_id = users.id
        WHERE products.published = 1
        ORDER BY products.views DESC, products.id DESC
        """)

    stores = cur.fetchall()
    conn.close()

    cards = ""

    for p in stores:
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

        cards += f"""
        <div class="product-card">
            <div class="product-info">
                <div class="card-top">
                    <span class="tag">{p["theme"]}</span>
                    <span>{p["views"] or 0} views</span>
                </div>

                <h3>{p["name"]}</h3>

                <p>{p["tagline"] or "No tagline yet"}</p>

                <p class="muted">
                    Seller: {p["seller_name"] or "LaunchFlow Seller"}
                </p>

                <div class="actions">
                    <a href="/s/{p["slug"]}">
                        View Store
                    </a>

                    {message_button}
                </div>
            </div>
        </div>
        """

    if not cards:
        cards = """
        <div class="empty">
            <h2>No stores found</h2>
            <p>Try another search.</p>
        </div>
        """

    return layout(f"""
    <div class="container">
        {top_nav(user)}

        <section class="hero">
            <p class="eyebrow">Marketplace</p>
            <h1>Discover stores</h1>
            <p>
                Browse published stores, see who runs them, and message sellers directly.
            </p>

            <form method="get" action="/discover" class="search-form">
                <input
                    type="text"
                    name="q"
                    value="{q}"
                    placeholder="Search stores, sellers, or creators..."
                >

                <button type="submit">
                    Search
                </button>
            </form>
        </section>

        <section class="grid">
            {cards}
        </section>
    </div>
    """, title="Discover")


@app.get("/track", response_class=HTMLResponse)
def track_lookup_page():
    return layout("""
    <div class="container narrow center">
        <div class="panel">
            <p class="eyebrow">Order Tracking</p>
            <h1>Track your order</h1>
            <p class="muted">Enter your order ID and email to view your shipping status.</p>

            <form action="/track" method="post">
                <label>Order ID</label>
                <input name="order_id" placeholder="Example: 12">

                <label>Email used at checkout</label>
                <input name="customer_email" type="email" placeholder="you@example.com">

                <button type="submit">Find Order</button>
            </form>
        </div>
    </div>
    """)


@app.post("/track")
def track_lookup(order_id: int = Form(...), customer_email: str = Form(...)):
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    SELECT *
    FROM orders
    WHERE id = ?
    AND LOWER(customer_email) = LOWER(?)
    """, (order_id, customer_email.strip()))

    order = cur.fetchone()
    conn.close()

    if not order:
        return layout("""
        <div class="container narrow center">
            <div class="panel">
                <h1>Order not found</h1>
                <p>Please check your order ID and email.</p>
                <a class="button" href="/track">Try again</a>
            </div>
        </div>
        """)

    return RedirectResponse(f"/track-order/{order_id}", status_code=303)

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

