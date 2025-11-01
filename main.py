import sqlite3
import time
import os
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
)
import logging
from apscheduler.schedulers.background import BackgroundScheduler
from PIL import Image, ImageDraw, ImageFont                                  
import qrcode
import json
from typing import Dict, List, Tuple
import asyncio
from concurrent.futures import ThreadPoolExecutor
import config

# ----- لاگ -----
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

DB_FILE = "tickets.db"
RESERVE_TIMEOUT = 60
SEAT_SIZE = 40
MARGIN = 20

# تعریف global برای app
app = None

# فقط در حالت توسعه دیتابیس ریست شود
if os.getenv("RESET_DB") == "1" and os.path.exists(DB_FILE):
    os.remove(DB_FILE)
    print("✅ فایل دیتابیس قدیمی حذف شد (حالت توسعه)")

if not os.path.exists("receipts"):
    os.makedirs("receipts")
if not os.path.exists("qrcodes"):
    os.makedirs("qrcodes")
if not os.path.exists("event_posters"):
    os.makedirs("event_posters")

# اجرای کوئری‌ها در ترد جداگانه
executor = ThreadPoolExecutor(max_workers=10)

async def run_in_thread(func, *args):
    """اجرای توابع blocking در ترد جداگانه"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, func, *args)

# ----- وضعیت‌های مختلف -----
admin_price_wait = {}
user_confirmation_wait = {}
admin_add_wait = {}
admin_remove_wait = {}
support_wait = {}
admin_reply_wait = {}
support_pagination = {}

# ----- دیتابیس -----
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # جدول وضعیت کاربران
    c.execute('''
        CREATE TABLE IF NOT EXISTS user_states (
            user_id INTEGER PRIMARY KEY,
            state_type TEXT,
            state_data TEXT,
            created_at INTEGER
        )
    ''')
    
    # جدول صندلی‌ها
    c.execute('''
        CREATE TABLE IF NOT EXISTS seats (
            event_id INTEGER,
            seat_id TEXT,
            row INTEGER,
            col INTEGER,
            status TEXT,
            reserved_by INTEGER,
            reserved_at INTEGER,
            price INTEGER,
            PRIMARY KEY (event_id, seat_id)
        )
    ''')
    
    # جدول پرداخت‌های موفق
    c.execute('''
        CREATE TABLE IF NOT EXISTS successful_payments (
            user_id INTEGER,
            event_id INTEGER,
            seat_id TEXT,
            paid_at INTEGER,
            qr_verified INTEGER DEFAULT 0
        )
    ''')
    
    # جدول ادمین‌ها
    c.execute('''
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            added_by INTEGER,
            added_at INTEGER,
            username TEXT
        )
    ''')
    
    # جدول رویدادها با جزئیات کامل
    c.execute('''
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY,
            title TEXT,
            description TEXT,
            event_date TEXT,
            event_type TEXT,
            poster_path TEXT,
            created_at INTEGER
        )
    ''')
    
    # جدول کاربران
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            joined_at INTEGER,
            last_activity INTEGER
        )
    ''')
    
    # جدول پیام‌های پشتیبانی
    c.execute('''
        CREATE TABLE IF NOT EXISTS support_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            message_text TEXT,
            message_type TEXT,
            created_at INTEGER,
            admin_id INTEGER,
            status TEXT DEFAULT 'pending'
        )
    ''')
    
    conn.commit()
    
    # اضافه کردن ادمین اصلی
    c.execute('INSERT OR IGNORE INTO admins (user_id, added_by, added_at, username) VALUES (?, ?, ?, ?)',
              (config.ADMIN_CHAT_ID, config.ADMIN_CHAT_ID, int(time.time()), 'admin'))
    
    # اضافه کردن رویدادها به جدول events
    for ev in config.EVENTS:
        c.execute('SELECT COUNT(*) FROM events WHERE id=?', (ev["id"],))
        if c.fetchone()[0] == 0:
            c.execute('''
                INSERT INTO events (id, title, description, event_date, event_type, poster_path, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                ev["id"],
                ev["title"],
                ev.get("description", "توضیحاتی برای این اجرا موجود نیست."),
                ev.get("date", "تعیین نشده"),
                ev.get("type", "عمومی"),
                ev.get("poster", ""),
                int(time.time())
            ))
    
    # ایجاد صندلی‌ها
    for ev in config.EVENTS:
        event_id = ev["id"]
        rows = ev["rows"]
        cols = ev["cols"]
        c.execute('SELECT COUNT(*) FROM seats WHERE event_id=?', (event_id,))
        cnt = c.fetchone()[0]
        if cnt == 0:
            for r in range(1, rows+1):
                for co in range(1, cols+1):
                    seat_id = f"R{r}C{co}"
                    price = ev.get("prices", {}).get(r, 100000)
                    c.execute('''
                        INSERT OR IGNORE INTO seats (event_id, seat_id, row, col, status, reserved_by, reserved_at, price)
                        VALUES (?, ?, ?, ?, 'free', NULL, NULL, ?)
                    ''', (event_id, seat_id, r, co, price))
    
    conn.commit()
    conn.close()
    print("✅ دیتابیس با موفقیت ایجاد/بارگذاری شد")

# ----- مدیریت کاربران -----
def save_or_update_user(user_id: int, username: str = "", first_name: str = "", last_name: str = ""):
    """ثبت یا به‌روزرسانی اطلاعات کاربر"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    c.execute('SELECT 1 FROM users WHERE user_id=?', (user_id,))
    exists = c.fetchone() is not None
    
    now = int(time.time())
    
    if exists:
        c.execute('''
            UPDATE users 
            SET username=?, first_name=?, last_name=?, last_activity=?
            WHERE user_id=?
        ''', (username, first_name, last_name, now, user_id))
    else:
        c.execute('''
            INSERT INTO users (user_id, username, first_name, last_name, joined_at, last_activity)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (user_id, username, first_name, last_name, now, now))
    
    conn.commit()
    conn.close()

def get_all_users(limit: int = 100, offset: int = 0) -> List[Tuple]:
    """دریافت لیست تمام کاربران"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        SELECT user_id, username, first_name, last_name, joined_at, last_activity 
        FROM users 
        ORDER BY last_activity DESC 
        LIMIT ? OFFSET ?
    ''', (limit, offset))
    users = c.fetchall()
    conn.close()
    return users

def get_users_count() -> int:
    """تعداد کل کاربران"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM users')
    count = c.fetchone()[0]
    conn.close()
    return count

# ----- مدیریت وضعیت کاربران -----
def save_user_state(user_id: int, state_type: str, state_data: str = ""):
    """ذخیره وضعیت کاربر در دیتابیس"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        INSERT OR REPLACE INTO user_states (user_id, state_type, state_data, created_at)
        VALUES (?, ?, ?, ?)
    ''', (user_id, state_type, state_data, int(time.time())))
    conn.commit()
    conn.close()

def get_user_state(user_id: int):
    """دریافت وضعیت کاربر از دیتابیس"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT state_type, state_data FROM user_states WHERE user_id=?', (user_id,))
    result = c.fetchone()
    conn.close()
    return result if result else (None, None)

def clear_user_state(user_id: int):
    """پاک کردن وضعیت کاربر"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('DELETE FROM user_states WHERE user_id=?', (user_id,))
    conn.commit()
    conn.close()

# ----- مدیریت ادمین‌ها -----
def is_admin(user_id: int) -> bool:
    """بررسی آیا کاربر ادمین است"""
    if user_id == config.ADMIN_CHAT_ID:
        return True
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT 1 FROM admins WHERE user_id=?', (user_id,))
    result = c.fetchone() is not None
    conn.close()
    return result

def add_admin(user_id: int, added_by: int, username: str = "") -> bool:
    """اضافه کردن ادمین جدید"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute('INSERT OR REPLACE INTO admins (user_id, added_by, added_at, username) VALUES (?, ?, ?, ?)',
                  (user_id, added_by, int(time.time()), username))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"خطا در اضافه کردن ادمین: {e}")
        return False
    finally:
        conn.close()

def remove_admin(user_id: int) -> bool:
    """حذف ادمین"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute('DELETE FROM admins WHERE user_id=? AND user_id!=?', (user_id, config.ADMIN_CHAT_ID))
        conn.commit()
        return c.rowcount > 0
    except Exception as e:
        logger.error(f"خطا در حذف ادمین: {e}")
        return False
    finally:
        conn.close()

def get_all_admins() -> List[Tuple]:
    """دریافت لیست تمام ادمین‌ها"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT user_id, username, added_at FROM admins')
    admins = c.fetchall()
    conn.close()
    return admins

# ----- مدیریت پشتیبانی -----
def save_support_message(user_id: int, message_text: str, message_type: str = "text"):
    """ذخیره پیام پشتیبانی"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        INSERT INTO support_messages (user_id, message_text, message_type, created_at)
        VALUES (?, ?, ?, ?)
    ''', (user_id, message_text, message_type, int(time.time())))
    conn.commit()
    conn.close()
    print(f"✅ پیام پشتیبانی از کاربر {user_id} ذخیره شد")

def get_pending_support_messages(limit: int = 10, offset: int = 0) -> List[Tuple]:
    """دریافت پیام‌های پشتیبانی در انتظار"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        SELECT sm.id, sm.user_id, sm.message_text, sm.message_type, sm.created_at, 
               u.username, u.first_name, u.last_name
        FROM support_messages sm
        LEFT JOIN users u ON sm.user_id = u.user_id
        WHERE sm.status = 'pending'
        ORDER BY sm.created_at ASC
        LIMIT ? OFFSET ?
    ''', (limit, offset))
    messages = c.fetchall()
    conn.close()
    print(f"📨 دریافت {len(messages)} پیام پشتیبانی")
    return messages

def get_pending_support_messages_count() -> int:
    """تعداد پیام‌های پشتیبانی در انتظار"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM support_messages WHERE status = "pending"')
    count = c.fetchone()[0]
    conn.close()
    return count

def mark_support_message_handled(message_id: int, admin_id: int):
    """علامت گذاری پیام پشتیبانی به عنوان پاسخ داده شده"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        UPDATE support_messages 
        SET status = 'handled', admin_id = ?
        WHERE id = ?
    ''', (admin_id, message_id))
    conn.commit()
    conn.close()

def delete_support_message(message_id: int):
    """حذف پیام پشتیبانی"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('DELETE FROM support_messages WHERE id = ?', (message_id,))
    conn.commit()
    conn.close()
    print(f"🗑️ پیام پشتیبانی {message_id} حذف شد")

# ----- مدیریت صندلی -----
async def get_seats(event_id):
    """دریافت لیست صندلی‌ها به صورت async"""
    return await run_in_thread(_get_seats_sync, event_id)

def _get_seats_sync(event_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT seat_id, row, col, status, reserved_by, price FROM seats WHERE event_id=? ORDER BY row, col', (event_id,))
    rows = c.fetchall()
    conn.close()
    return rows

async def set_reserved(event_id, seat_id, user_id):
    """رزرو صندلی به صورت اتمیک"""
    return await run_in_thread(_set_reserved_sync, event_id, seat_id, user_id)

def _set_reserved_sync(event_id, seat_id, user_id):
    now = int(time.time())
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    c.execute('''
        UPDATE seats 
        SET status=?, reserved_by=?, reserved_at=? 
        WHERE event_id=? AND seat_id=? AND status='free'
    ''', ('reserved', user_id, now, event_id, seat_id))
    
    success = c.rowcount > 0
    conn.commit()
    conn.close()
    
    if not success:
        return False, "این صندلی در حال حاضر قابل انتخاب نیست."
    return True, None

async def release_seat(event_id, seat_id):
    """آزادسازی صندلی"""
    await run_in_thread(_release_seat_sync, event_id, seat_id)

def _release_seat_sync(event_id, seat_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('UPDATE seats SET status="free", reserved_by=NULL, reserved_at=NULL WHERE event_id=? AND seat_id=?',
              (event_id, seat_id))
    conn.commit()
    conn.close()

async def mark_sold(event_id, seat_id):
    """علامت گذاری صندلی به عنوان فروخته شده"""
    await run_in_thread(_mark_sold_sync, event_id, seat_id)

def _mark_sold_sync(event_id, seat_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('UPDATE seats SET status="sold" WHERE event_id=? AND seat_id=?', (event_id, seat_id))
    conn.commit()
    conn.close()

async def get_reserved_seat_by_user(user_id):
    """دریافت صندلی رزرو شده توسط کاربر"""
    return await run_in_thread(_get_reserved_seat_by_user_sync, user_id)

def _get_reserved_seat_by_user_sync(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT event_id, seat_id FROM seats WHERE reserved_by=? AND status="reserved"', (user_id,))
    r = c.fetchone()
    conn.close()
    return r if r else None

async def record_successful_payment(user_id, event_id, seat_id):
    """ثبت پرداخت موفق"""
    await run_in_thread(_record_successful_payment_sync, user_id, event_id, seat_id)

def _record_successful_payment_sync(user_id, event_id, seat_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('INSERT INTO successful_payments (user_id, event_id, seat_id, paid_at) VALUES (?, ?, ?, ?)',
              (user_id, event_id, seat_id, int(time.time())))
    conn.commit()
    conn.close()

# ----- گزارش‌گیری مالی -----
async def get_financial_report(event_id: int = None) -> Dict:
    """گزارش مالی کامل"""
    return await run_in_thread(_get_financial_report_sync, event_id)

def _get_financial_report_sync(event_id: int = None) -> Dict:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    report = {
        'total_income': 0,
        'total_sold': 0,
        'total_reserved': 0,
        'total_free': 0,
        'event_details': []
    }
    
    if event_id:
        events = [event_id]
    else:
        c.execute('SELECT DISTINCT event_id FROM seats')
        events = [row[0] for row in c.fetchall()]
    
    for ev_id in events:
        c.execute('SELECT title FROM events WHERE id=?', (ev_id,))
        event_title = c.fetchone()
        event_title = event_title[0] if event_title else f"رویداد {ev_id}"
        
        c.execute('SELECT status, COUNT(*), SUM(price) FROM seats WHERE event_id=? AND status="sold"', (ev_id,))
        sold_data = c.fetchone()
        sold_count = sold_data[1] if sold_data else 0
        sold_income = sold_data[2] if sold_data and sold_data[2] else 0
        
        c.execute('SELECT COUNT(*) FROM seats WHERE event_id=? AND status="reserved"', (ev_id,))
        reserved_count = c.fetchone()[0]
        
        c.execute('SELECT COUNT(*) FROM seats WHERE event_id=? AND status="free"', (ev_id,))
        free_count = c.fetchone()[0]
        
        report['event_details'].append({
            'event_id': ev_id,
            'title': event_title,
            'stats': {
                'sold': sold_count,
                'reserved': reserved_count,
                'free': free_count,
                'income': sold_income
            }
        })
        
        report['total_income'] += sold_income
        report['total_sold'] += sold_count
        report['total_reserved'] += reserved_count
        report['total_free'] += free_count
    
    conn.close()
    return report

# ----- آزادسازی خودکار و یادآوری پرداخت -----
def release_expired_seats():
    """آزادسازی صندلی‌های منقضی و ارسال یادآوری پرداخت"""
    now = int(time.time())
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    c.execute('SELECT event_id, seat_id, reserved_by, reserved_at FROM seats WHERE status="reserved"')
    rows = c.fetchall()
    
    for event_id, seat_id, user_id, reserved_at in rows:
        if now - reserved_at > 1800:
            try:
                asyncio.create_task(send_reminder(user_id, seat_id))
            except Exception as e:
                logger.error(f"خطا در ارسال یادآوری: {e}")
        
        if now - reserved_at > 2400:
            c.execute('UPDATE seats SET status="free", reserved_by=NULL, reserved_at=NULL WHERE event_id=? AND seat_id=?',
                      (event_id, seat_id))
            try:
                asyncio.create_task(send_expiration_notice(user_id, seat_id))
            except Exception as e:
                logger.error(f"خطا در ارسال اخطار انقضا: {e}")
    
    conn.commit()
    conn.close()

async def send_reminder(user_id: int, seat_id: str):
    """ارسال یادآوری پرداخت"""
    try:
        await app.bot.send_message(
            chat_id=user_id, 
            text=f"⏰ یادآوری: رزرو صندلی {seat_id} شما در حال انقضا است. لطفاً ظرف ۱۰ دقیقه پرداخت را انجام دهید."
        )
    except Exception as e:
        logger.error(f"خطا در ارسال یادآوری به کاربر {user_id}: {e}")

async def send_expiration_notice(user_id: int, seat_id: str):
    """ارسال اخطار انقضای رزرو"""
    try:
        await app.bot.send_message(
            chat_id=user_id, 
            text=f"❌ رزرو صندلی {seat_id} منقضی شد و آزاد گردید."
        )
    except Exception as e:
        logger.error(f"خطا در ارسال اخطار انقضا به کاربر {user_id}: {e}")

# ----- نقشه صندلی پیشرفته -----
async def generate_seat_map_image(event_id):
    """تولید نقشه صندلی با رنگ‌بندی پیشرفته"""
    return await run_in_thread(_generate_seat_map_image_sync, event_id)

def _generate_seat_map_image_sync(event_id):
    seats = _get_seats_sync(event_id)
    if not seats:
        width = SEAT_SIZE + 2*MARGIN
        height = SEAT_SIZE + 2*MARGIN
        img = Image.new('RGB', (width, height), color=(255,255,255))
        img.save(f"seat_map_{event_id}.png")
        return f"seat_map_{event_id}.png"

    rows = max([r for _, r, _, _, _, _ in seats])
    cols = max([c for _, _, c, _, _, _ in seats])
    width = cols * SEAT_SIZE + 2*MARGIN
    height = rows * SEAT_SIZE + 2*MARGIN + 80
    img = Image.new('RGB', (width, height), color=(240, 240, 240))
    draw = ImageDraw.Draw(img)
    
    try:
        font_paths = [
            "fonts/Vazir.ttf", "fonts/Shabnam.ttf", "fonts/B Nazanin.ttf",
            "arial.ttf", "Arial.ttf"
        ]
        font = None
        for font_path in font_paths:
            try:
                font = ImageFont.truetype(font_path, 10)
                small_font = ImageFont.truetype(font_path, 8)
                tiny_font = ImageFont.truetype(font_path, 7)
                break
            except:
                continue
        if font is None:
            raise Exception("فونتی یافت نشد")
    except:
        font = ImageFont.load_default()
        small_font = ImageFont.load_default()
        tiny_font = ImageFont.load_default()
    
    legend_y = height - 60
    legend_x = MARGIN
    
    legends = [
        ("آزاد", (0, 200, 0)),
        ("رزرو شده", (255, 200, 0)),
        ("فروخته شده", (255, 0, 0)),
        ("وی‌آی‌پی", (0, 100, 255))
    ]
    
    legend_spacing = (width - 2*MARGIN) // len(legends)
    
    for i, (text, color) in enumerate(legends):
        x_pos = legend_x + i * legend_spacing
        draw.rectangle([x_pos, legend_y, x_pos + 15, legend_y + 15], 
                      fill=color, outline=(0,0,0), width=1)
        text_width = draw.textlength(text, font=small_font)
        text_x = x_pos + 20
        draw.text((text_x, legend_y), text, fill=(0,0,0), font=small_font)

    for seat_id, r, c, status, _, price in seats:
        x0 = MARGIN + (c-1)*SEAT_SIZE
        y0 = MARGIN + (r-1)*SEAT_SIZE
        
        if status == 'free':
            if price > 150000:
                color = (0, 100, 255)
            else:
                color = (0, 200, 0)
        elif status == 'reserved':
            color = (255, 200, 0)
        else:
            color = (255, 0, 0)
            
        draw.rectangle([x0, y0, x0+SEAT_SIZE-2, y0+SEAT_SIZE-2], 
                      fill=color, outline=(0,0,0), width=1)
        
        seat_label = f"{r}-{c}"
        text_width = draw.textlength(seat_label, font=small_font)
        text_x = x0 + (SEAT_SIZE - text_width) // 2
        text_y = y0 + (SEAT_SIZE - 10) // 2
        draw.text((text_x, text_y), seat_label, fill=(0,0,0), font=small_font)
        
        if status == 'free':
            price_toman = price
            
            if price_toman >= 1000000:
                million_value = price_toman // 1000000
                price_text = f"{million_value} میلیون"
            elif price_toman >= 1000:
                thousand_value = price_toman // 1000
                price_text = f"{thousand_value} هزار"
            else:
                price_text = f"{price_toman}"
            
            current_font = tiny_font if len(price_text) > 8 else small_font
            price_width = draw.textlength(price_text, font=current_font)
            price_x = x0 + (SEAT_SIZE - price_width) // 2
            price_y = y0 + SEAT_SIZE - 15
            draw.text((price_x, price_y), price_text, fill=(0,0,0), font=current_font)
    
    path = f"seat_map_{event_id}.png"
    img.save(path)
    return path

# ----- رسید گرافیکی زیبا -----
async def generate_beautiful_receipt(user_id: int, event_id: int, seat_id: str, username: str = "") -> str:
    """تولید رسید گرافیکی زیبا"""
    return await run_in_thread(_generate_beautiful_receipt_sync, user_id, event_id, seat_id, username)

def _generate_beautiful_receipt_sync(user_id: int, event_id: int, seat_id: str, username: str = "") -> str:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    c.execute('SELECT title, event_date FROM events WHERE id=?', (event_id,))
    event_info = c.fetchone()
    event_title = event_info[0] if event_info else "نامشخص"
    event_date = event_info[1] if event_info else "نامشخص"
    
    c.execute('SELECT price FROM seats WHERE event_id=? AND seat_id=?', (event_id, seat_id))
    price_result = c.fetchone()
    price = price_result[0] if price_result else 0
    
    conn.close()
    
    width, height = 400, 500
    img = Image.new('RGB', (width, height), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    
    for i in range(80):
        color_ratio = i / 80
        color = (
            int(0 * (1 - color_ratio) + 0 * color_ratio),
            int(100 * (1 - color_ratio) + 150 * color_ratio),
            int(200 * (1 - color_ratio) + 255 * color_ratio)
        )
        draw.line([0, i, width, i], fill=color)
    
    try:
        font_paths = ["fonts/Vazir.ttf", "fonts/Shabnam.ttf", "arial.ttf"]
        title_font = normal_font = small_font = None
        for font_path in font_paths:
            try:
                title_font = ImageFont.truetype(font_path, 24)
                normal_font = ImageFont.truetype(font_path, 16)
                small_font = ImageFont.truetype(font_path, 12)
                break
            except:
                continue
        if title_font is None:
            raise Exception("فونتی یافت نشد")
    except:
        title_font = ImageFont.load_default()
        normal_font = ImageFont.load_default()
        small_font = ImageFont.load_default()
    
    draw.text((width//2, 40), "🎭 رسید پرداخت", fill=(255,255,255), 
              font=title_font, anchor="mm")
    
    y_pos = 120
    infos = [
        ("کاربر:", f"@{username}" if username else f"ID: {user_id}"),
        ("اجرا:", event_title),
        ("تاریخ:", event_date),
        ("صندلی:", seat_id),
        ("مبلغ:", f"{price:,} تومان"),
        ("زمان خرید:", datetime.now().strftime("%Y/%m/%d %H:%M")),
        ("کد رهگیری:", f"TK{user_id:06d}{int(time.time()) % 10000:04d}")
    ]
    
    for label, value in infos:
        draw.text((50, y_pos), label, fill=(0,0,0), font=normal_font)
        draw.text((200, y_pos), value, fill=(100,100,100), font=normal_font)
        y_pos += 40
    
    draw.line([50, y_pos+20, width-50, y_pos+20], fill=(200,200,200), width=2)
    
    draw.text((width//2, height-50), "با تشکر از خرید شما! 🎉", 
              fill=(0,150,0), font=normal_font, anchor="mm")
    
    path = f"receipts/receipt_{user_id}_{seat_id}_{int(time.time())}.png"
    img.save(path)
    return path

# ----- QR Code -----
async def generate_qr_code(event_id: int, seat_id: str, user_id: int) -> str:
    """تولید QR Code برای بلیت"""
    return await run_in_thread(_generate_qr_code_sync, event_id, seat_id, user_id)

def _generate_qr_code_sync(event_id: int, seat_id: str, user_id: int) -> str:
    qr_data = {
        'event_id': event_id,
        'seat_id': seat_id,
        'user_id': user_id,
        'timestamp': int(time.time()),
        'verification': f"VT{event_id:03d}{user_id % 10000:04d}"
    }
    
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(json.dumps(qr_data))
    qr.make(fit=True)
    
    qr_img = qr.make_image(fill_color="black", back_color="white")
    path = f"qrcodes/qr_{event_id}_{seat_id}_{user_id}.png"
    qr_img.save(path)
    
    return path

# ----- دکمه‌های ثابت -----
def get_persistent_keyboard(user_id):
    keyboard = [
        [KeyboardButton("📅 دیدن اجراها")],
        [KeyboardButton("📊 آمار صندلی‌ها"), KeyboardButton("❓ راهنما")],
        [KeyboardButton("📞 ارتباط با پشتیبانی")]
    ]
    if is_admin(user_id):
        keyboard.append([KeyboardButton("🛠 پنل مدیریت")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ----- استارت -----
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    user_id = user.id
    username = user.username or ""
    first_name = user.first_name or ""
    last_name = user.last_name or ""
    
    await run_in_thread(save_or_update_user, user_id, username, first_name, last_name)
    
    welcome_text = (
        "🎭 **سلام! به ربات رزرو بلیت خوش آمدید**\n\n"
        "✨ **امکانات جدید:**\n"
        "• گالری اجراها با پوستر\n"
        "• تأیید نهایی قبل از خرید\n"
        "• رسید گرافیکی زیبا\n"
        "• QR Code برای بلیت\n"
        "• سیستم چند ادمین\n"
        "• پشتیبانی مستقیم\n\n"
        "📝 **نحوه کار:**\n"
        "1) اجرا را انتخاب کنید\n"
        "2) صندلی رزرو کنید\n"
        "3) مبلغ را واریز کنید\n"
        "4) رسید پرداخت را ارسال کنید\n\n"
        "📞 **پشتیبانی:** در صورت هرگونه مشکل از دکمه 'ارتباط با پشتیبانی' استفاده کنید."
    )
    await update.message.reply_text(
        welcome_text, 
        reply_markup=get_persistent_keyboard(user_id),
        parse_mode='Markdown'
    )

# ----- نمایش لیست اجراها -----
async def show_events_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش لیست اجراها با دکمه اینلاین"""
    user_id = update.message.from_user.id
    events = config.EVENTS
    
    if not events:
        await update.message.reply_text("📭 هیچ اجرایی در حال حاضر موجود نیست.")
        return
    
    events_text = "🎭 **لیست اجراهای موجود:**\n\n"
    for i, event in enumerate(events, 1):
        events_text += f"{i}. **{event['title']}**\n"
        events_text += f"   📅 {event.get('date', 'تعیین نشده')}\n"
        events_text += f"   🏷 {event.get('type', 'عمومی')}\n"
        events_text += f"   💺 {event['rows']} ردیف × {event['cols']} صندلی\n\n"
    
    await update.message.reply_text(events_text, parse_mode='Markdown')
    
    keyboard = []
    for event in events:
        keyboard.append([InlineKeyboardButton(
            f"🎭 {event['title']}", 
            callback_data=f"event|{event['id']}"
        )])
    
    await update.message.reply_text(
        "لطفاً اجرای مورد نظر را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ----- نمایش لیست اجراها برای آمار -----
async def show_events_for_statistics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش لیست اجراها برای انتخاب آمار صندلی‌ها"""
    user_id = update.message.from_user.id
    events = config.EVENTS
    
    if not events:
        await update.message.reply_text("📭 هیچ اجرایی در حال حاضر موجود نیست.")
        return
    
    events_text = "📊 **انتخاب اجرا برای مشاهده آمار صندلی‌ها**\n\n"
    for i, event in enumerate(events, 1):
        events_text += f"{i}. **{event['title']}**\n"
        events_text += f"   📅 {event.get('date', 'تعیین نشده')}\n"
        events_text += f"   💺 {event['rows']} ردیف × {event['cols']} صندلی\n\n"
    
    await update.message.reply_text(events_text, parse_mode='Markdown')
    
    keyboard = []
    for event in events:
        keyboard.append([InlineKeyboardButton(
            f"📊 {event['title']}", 
            callback_data=f"stats|{event['id']}"
        )])
    
    await update.message.reply_text(
        "لطفاً اجرای مورد نظر را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ----- پشتیبانی -----
async def handle_support_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """مدیریت درخواست پشتیبانی"""
    user_id = update.message.from_user.id
    user = update.message.from_user
    
    await run_in_thread(save_or_update_user, user_id, user.username or "", 
                       user.first_name or "", user.last_name or "")
    
    support_wait[user_id] = True
    save_user_state(user_id, "support_wait")
    
    await update.message.reply_text(
        "📞 **پشتیبانی**\n\n"
        "لطفاً مشکل یا سوال خود را به صورت کامل توضیح دهید:\n\n"
        "💡 **مثال:**\n"
        "• مشکل در پرداخت\n"
        "• مشکل در انتخاب صندلی\n"
        "• سوال درباره اجرا\n"
        "• گزارش مشکل فنی\n\n"
        "پشتیبان‌ها در اسرع وقت پاسخ خواهند داد.\n\n"
        "❌ برای لغو، از منوی اصلی استفاده کنید.",
        parse_mode='Markdown',
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton("❌ لغو")]], resize_keyboard=True)
    )

async def handle_support_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """مدیریت پیام پشتیبانی کاربر"""
    user_id = update.message.from_user.id
    text = update.message.text
    
    if text == "❌ لغو":
        support_wait.pop(user_id, None)
        clear_user_state(user_id)
        await update.message.reply_text(
            "✅ درخواست پشتیبانی لغو شد.",
            reply_markup=get_persistent_keyboard(user_id)
        )
        return
    
    if user_id not in support_wait:
        return
    
    await run_in_thread(save_support_message, user_id, text, "text")
    
    user = update.message.from_user
    user_info = f"@{user.username}" if user.username else f"{user.first_name or ''} {user.last_name or ''}".strip()
    if not user_info or user_info.strip() == "":
        user_info = f"آیدی: {user_id}"
    
    admin_message = (
        f"📞 **پیام پشتیبانی جدید**\n\n"
        f"👤 **کاربر:** {user_info}\n"
        f"🆔 **آیدی:** `{user_id}`\n"
        f"📝 **پیام:**\n{text}\n\n"
        f"⏰ **زمان:** {datetime.now().strftime('%Y/%m/%d %H:%M')}"
    )
    
    admins = get_all_admins()
    for admin_id, _, _ in admins:
        try:
            keyboard = [
                [InlineKeyboardButton("💬 پاسخ مستقیم به کاربر", callback_data=f"support_reply|{user_id}")],
                [InlineKeyboardButton("📝 مشاهده تاریخچه کاربر", callback_data=f"support_history|{user_id}")],
                [InlineKeyboardButton("✅ حل شده", callback_data=f"support_resolved|{user_id}")]
            ]
            await context.bot.send_message(
                chat_id=admin_id,
                text=admin_message,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"خطا در ارسال پیام پشتیبانی به ادمین {admin_id}: {e}")
    
    await update.message.reply_text(
        "✅ پیام شما برای پشتیبان‌ها ارسال شد.\n\n"
        "به زودی پاسخ خود را دریافت خواهید کرد.",
        reply_markup=get_persistent_keyboard(user_id)
    )
    
    support_wait.pop(user_id, None)
    clear_user_state(user_id)

# ----- نمایش پیام‌های پشتیبانی -----
async def show_support_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش پیام‌های پشتیبانی در انتظار"""
    user_id = update.message.from_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text("❌ دسترسی denied.")
        return
    
    total_messages = await run_in_thread(get_pending_support_messages_count)
    
    if total_messages == 0:
        await update.message.reply_text("✅ هیچ پیام پشتیبانی در انتظار پاسخ نیست.")
        return
    
    # استفاده از page=0 برای نمایش اولین صفحه
    await show_support_messages_page(update, context, page=0)

async def show_support_messages_page(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    """نمایش صفحه‌بندی شده پیام‌های پشتیبانی"""
    user_id = update.effective_user.id
    logger.info(f"Showing support messages page {page} for user {user_id}")
    
    limit = 5
    offset = page * limit
    
    messages = await run_in_thread(get_pending_support_messages, limit, offset)
    total_messages = await run_in_thread(get_pending_support_messages_count)
    total_pages = (total_messages + limit - 1) // limit
    
    if not messages:
        if hasattr(update, 'message'):
            await update.message.reply_text("✅ هیچ پیام پشتیبانی در انتظار پاسخ نیست.")
        else:
            await update.edit_message_text("✅ هیچ پیام پشتیبانی در انتظار پاسخ نیست.")
        return
    
    support_pagination[user_id] = {'page': page, 'total_pages': total_pages}
    
    list_text = f"📞 **پیام‌های پشتیبانی در انتظار**\n\n"
    list_text += f"📊 **تعداد کل پیام‌ها:** {total_messages}\n"
    list_text += f"📄 **صفحه {page + 1} از {total_pages}**\n\n"
    
    keyboard = []
    
    for i, (msg_id, user_id_msg, message_text, message_type, created_at, username, first_name, last_name) in enumerate(messages, 1):
        user_info = f"@{username}" if username else f"{first_name or ''} {last_name or ''}".strip()
        if not user_info:
            user_info = f"آیدی: {user_id_msg}"
        
        message_time = datetime.fromtimestamp(created_at).strftime("%Y/%m/%d %H:%M")
        short_message = message_text[:30] + "..." if len(message_text) > 30 else message_text
        
        list_text += f"{i}. 👤 {user_info}\n"
        list_text += f"   ⏰ {message_time}\n"
        list_text += f"   📝 {short_message}\n\n"
        
        keyboard.append([InlineKeyboardButton(
            f"📩 مشاهده پیام {i} از {user_info}", 
            callback_data=f"view_support_message|{msg_id}"
        )])
    
    pagination_buttons = []
    if page > 0:
        pagination_buttons.append(InlineKeyboardButton("⬅️ صفحه قبلی", callback_data=f"support_page|{page-1}"))
    
    if page < total_pages - 1:
        pagination_buttons.append(InlineKeyboardButton("صفحه بعدی ➡️", callback_data=f"support_page|{page+1}"))
    
    if pagination_buttons:
        keyboard.append(pagination_buttons)
    
    keyboard.append([InlineKeyboardButton("🔄 به‌روزرسانی", callback_data="refresh_support_messages")])
    
    if hasattr(update, 'message'):
        await update.message.reply_text(
            list_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    else:
        await update.edit_message_text(
            list_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )

async def show_support_message_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, message_id: int):
    """نمایش جزئیات یک پیام پشتیبانی"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if not is_admin(user_id):
        return
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        SELECT sm.id, sm.user_id, sm.message_text, sm.message_type, sm.created_at, 
               u.username, u.first_name, u.last_name
        FROM support_messages sm
        LEFT JOIN users u ON sm.user_id = u.user_id
        WHERE sm.id = ?
    ''', (message_id,))
    message_data = c.fetchone()
    conn.close()
    
    if not message_data:
        await query.message.reply_text("❌ پیام مورد نظر یافت نشد.")
        return
    
    msg_id, user_id_msg, message_text, message_type, created_at, username, first_name, last_name = message_data
    
    user_info = f"@{username}" if username else f"{first_name or ''} {last_name or ''}".strip()
    if not user_info:
        user_info = f"آیدی: {user_id_msg}"
    
    message_time = datetime.fromtimestamp(created_at).strftime("%Y/%m/%d %H:%M")
    
    message_display = (
        f"📞 **پیام پشتیبانی**\n\n"
        f"👤 **کاربر:** {user_info}\n"
        f"🆔 **آیدی:** `{user_id_msg}`\n"
        f"⏰ **زمان:** {message_time}\n\n"
        f"📝 **پیام:**\n{message_text}"
    )
    
    keyboard = [
        [InlineKeyboardButton("💬 پاسخ مستقیم", callback_data=f"support_reply|{user_id_msg}")],
        [InlineKeyboardButton("📝 تاریخچه کاربر", callback_data=f"support_history|{user_id_msg}")],
        [InlineKeyboardButton("✅ حل شده و حذف", callback_data=f"support_resolved|{user_id_msg}|{msg_id}")],
        [InlineKeyboardButton("🔙 بازگشت به لیست", callback_data="support_back_to_list")]
    ]
    
    await query.message.reply_text(
        message_display,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

# ----- هندلر اصلی برای تمام پیام‌های متنی -----
async def handle_all_text_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """مدیریت همه پیام‌های متنی به صورت متمرکز"""
    user = update.message.from_user
    user_id = user.id
    text = update.message.text.strip()
    
    logger.info(f"پیام متنی دریافت شد از {user_id}: {text}")
    
    await run_in_thread(save_or_update_user, user_id, user.username or "", 
                       user.first_name or "", user.last_name or "")
    
    state_type, state_data = get_user_state(user_id)
    
    if state_type == "admin_price_wait":
        admin_price_wait[user_id] = eval(state_data)
        await handle_admin_price_input(update, context)
        return
    
    elif state_type == "admin_add_wait":
        admin_add_wait[user_id] = True
        await handle_admin_add_input(update, context)
        return
    
    elif state_type == "admin_remove_wait":
        admin_remove_wait[user_id] = True
        await handle_admin_remove_input(update, context)
        return
    
    elif state_type == "support_wait":
        support_wait[user_id] = True
        await handle_support_message(update, context)
        return
    
    elif state_type == "admin_reply_wait":
        admin_reply_wait[user_id] = int(state_data)
        await handle_admin_reply(update, context)
        return
    
    if user_id in admin_price_wait:
        await handle_admin_price_input(update, context)
        return
    
    if user_id in admin_add_wait:
        await handle_admin_add_input(update, context)
        return
    
    if user_id in admin_remove_wait:
        await handle_admin_remove_input(update, context)
        return
    
    if user_id in support_wait:
        await handle_support_message(update, context)
        return
    
    if user_id in admin_reply_wait:
        await handle_admin_reply(update, context)
        return
    
    if text in ["📅 دیدن اجراها", "📊 آمار صندلی‌ها", "❓ راهنما", "🛠 پنل مدیریت", "📞 ارتباط با پشتیبانی"]:
        await handle_main_buttons(update, context)
        return
    
    if text in ["👥 مدیریت ادمین‌ها", "💰 گزارش مالی", "🎯 مدیریت قیمت صندلی‌ها", "📊 آمار لحظه‌ای", "👤 لیست کاربران", "📞 پیام‌های پشتیبانی", "🔙 بازگشت"]:
        await handle_admin_buttons(update, context)
        return
    
    await handle_main_buttons(update, context)

# ----- هندلر برای دکمه‌های اصلی -----
async def handle_main_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """هندلر اصلی برای دکمه‌های کیبورد ثابت"""
    text = update.message.text
    user_id = update.message.from_user.id
    
    logger.info(f"دکمه فشرده شده: {text} توسط کاربر: {user_id}")
    
    if text == "📅 دیدن اجراها":
        await show_events_list(update, context)
    
    elif text == "📊 آمار صندلی‌ها":
        await show_events_for_statistics(update, context)
    
    elif text == "❓ راهنما":
        help_text = (
            "📖 **راهنمای استفاده:**\n\n"
            "1. 🎭 **انتخاب اجرا:** از منوی اصلی گزینه 'دیدن اجراها' را انتخاب کنید\n"
            "2. 💺 **انتخاب صندلی:** روی اجرا مورد نظر کلیک و صندلی را انتخاب کنید\n"
            "3. ✅ **تأیید نهایی:** اطلاعات را بررسی و تأیید کنید\n"
            "4. 💳 **پرداخت:** مبلغ را به شماره کارت واریز کنید\n"
            "5. 📸 **ارسال رسید:** عکس رسید را ارسال کنید\n"
            "6. 🎫 **دریافت بلیت:** پس از تأیید ادمین، بلیت را دریافت می‌کنید\n\n"
            "🔍 **امکانات جدید:**\n"
            "• گالری با پوستر و توضیحات\n"
            "• تأیید نهایی قبل از خرید\n"
            "• رسید گرافیکی زیبا\n"
            "• QR Code برای ورود\n"
            "• پشتیبانی مستقیم\n\n"
            "📞 **پشتیبانی:** در صورت مشکل از دکمه 'ارتباط با پشتیبانی' استفاده کنید."
        )
        await update.message.reply_text(help_text, parse_mode='Markdown')
    
    elif text == "📞 ارتباط با پشتیبانی":
        await handle_support_request(update, context)
    
    elif text == "🛠 پنل مدیریت" and is_admin(user_id):
        await show_admin_panel(update, context)
    
    else:
        await update.message.reply_text(
            "لطفاً از دکمه‌های زیر استفاده کنید:",
            reply_markup=get_persistent_keyboard(user_id)
        )

# ----- نمایش پنل مدیریت -----
async def show_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش پنل مدیریت"""
    user_id = update.message.from_user.id
    
    keyboard = [
        [KeyboardButton("👥 مدیریت ادمین‌ها"), KeyboardButton("💰 گزارش مالی")],
        [KeyboardButton("🎯 مدیریت قیمت صندلی‌ها")],
        [KeyboardButton("📊 آمار لحظه‌ای")],
        [KeyboardButton("👤 لیست کاربران")],
        [KeyboardButton("📞 پیام‌های پشتیبانی")],
        [KeyboardButton("🔙 بازگشت")]
    ]
    await update.message.reply_text(
        "🛠 **پنل مدیریت**\n\nلطفاً گزینه مورد نظر را انتخاب کنید:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
        parse_mode='Markdown'
    )

# ----- نمایش لیست کاربران -----
async def show_users_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش لیست کاربران برای ادمین"""
    user_id = update.message.from_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text("❌ دسترسی denied.")
        return
    
    users = await run_in_thread(get_all_users, 50)
    total_users = await run_in_thread(get_users_count)
    
    if not users:
        await update.message.reply_text("📭 هیچ کاربری در ربات ثبت نشده است.")
        return
    
    users_text = f"👥 **لیست کاربران ربات**\n\n"
    users_text += f"📊 **تعداد کل کاربران:** {total_users}\n"
    users_text += f"📋 **نمایش:** {len(users)} کاربر آخر\n\n"
    
    for i, (user_id, username, first_name, last_name, joined_at, last_activity) in enumerate(users, 1):
        joined_date = datetime.fromtimestamp(joined_at).strftime("%Y/%m/%d")
        last_active = datetime.fromtimestamp(last_activity).strftime("%Y/%m/%d %H:%M")
        
        full_name = f"{first_name or ''} {last_name or ''}".strip()
        if not full_name:
            full_name = "بدون نام"
        
        user_handle = f"@{username}" if username else "بدون یوزرنیم"
        
        users_text += f"{i}. **{full_name}**\n"
        users_text += f"   👤 {user_handle}\n"
        users_text += f"   🆔 `{user_id}`\n"
        users_text += f"   📅 عضویت: {joined_date}\n"
        users_text += f"   ⏰ آخرین فعالیت: {last_active}\n\n"
        
        if len(users_text) > 3000:
            users_text += "📋 ... و کاربران بیشتر\n"
            break
    
    keyboard = [
        [InlineKeyboardButton("🔄 به‌روزرسانی", callback_data="refresh_users")],
        [InlineKeyboardButton("📊 آمار کامل", callback_data="users_stats")]
    ]
    
    await update.message.reply_text(
        users_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

# ----- نمایش آمار کاربران -----
async def show_users_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش آمار کامل کاربران"""
    user_id = update.message.from_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text("❌ دسترسی denied.")
        return
    
    total_users = await run_in_thread(get_users_count)
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    today_start = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    c.execute('SELECT COUNT(*) FROM users WHERE joined_at >= ?', (today_start,))
    today_users = c.fetchone()[0]
    
    day_ago = int(time.time()) - 86400
    c.execute('SELECT COUNT(*) FROM users WHERE last_activity >= ?', (day_ago,))
    active_users = c.fetchone()[0]
    
    conn.close()
    
    stats_text = (
        "📊 **آمار کامل کاربران**\n\n"
        f"👥 **تعداد کل کاربران:** {total_users}\n"
        f"🆕 **کاربران امروز:** {today_users}\n"
        f"✅ **کاربران فعال (24h):** {active_users}\n"
        f"📈 **نرخ فعالیت:** {(active_users/total_users*100) if total_users > 0 else 0:.1f}%\n\n"
        "💡 **توضیحات:**\n"
        "• کاربران فعال: کاربرانی که در 24 ساعت گذشته فعالیت داشتند\n"
        "• کاربران امروز: کاربرانی که امروز به ربات پیوستند"
    )
    
    keyboard = [
        [InlineKeyboardButton("📋 مشاهده لیست کاربران", callback_data="show_users_list")],
        [InlineKeyboardButton("🔄 به‌روزرسانی", callback_data="refresh_users_stats")]
    ]
    
    await update.message.reply_text(
        stats_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

# ----- هندلر برای منوی مدیریت -----
async def handle_admin_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """هندلر برای دکمه‌های منوی مدیریت"""
    text = update.message.text
    user_id = update.message.from_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text("❌ دسترسی denied.")
        return
    
    if text == "👥 مدیریت ادمین‌ها":
        await manage_admins(update, context)
    
    elif text == "💰 گزارش مالی":
        report = await get_financial_report()
        report_text = (
            "📊 **گزارش مالی کامل**\n\n"
            f"💰 **درآمد کل:** {report['total_income']:,} تومان\n"
            f"🎫 **بلیت‌های فروخته شده:** {report['total_sold']}\n"
            f"⏳ **بلیت‌های رزرو شده:** {report['total_reserved']}\n"
            f"🆓 **صندلی‌های آزاد:** {report['total_free']}\n\n"
            "📈 **جزئیات هر اجرا:**\n"
        )
        
        for event in report['event_details']:
            report_text += f"\n🎭 {event['title']}:\n"
            report_text += f"   💰 {event['stats']['income']:,} تومان - "
            report_text += f"🎫 {event['stats']['sold']} - "
            report_text += f"⏳ {event['stats']['reserved']} - "
            report_text += f"🆓 {event['stats']['free']}\n"
        
        await update.message.reply_text(report_text, parse_mode='Markdown')
    
    elif text == "🎯 مدیریت قیمت صندلی‌ها":
        keyboard = []
        for event in config.EVENTS:
            keyboard.append([InlineKeyboardButton(
                f"💰 {event['title']}", 
                callback_data=f"admin_price_event|{event['id']}"
            )])
        
        await update.message.reply_text(
            "🎯 **مدیریت قیمت صندلی‌ها**\n\nلطفاً اجرای مورد نظر را انتخاب کنید:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    
    elif text == "📊 آمار لحظه‌ای":
        for ev in config.EVENTS:
            path = await generate_seat_map_image(ev["id"])
            with open(path, "rb") as photo:
                await context.bot.send_photo(
                    chat_id=user_id, 
                    photo=photo, 
                    caption=f"📊 نقشه صندلی {ev['title']}"
                )
    
    elif text == "👤 لیست کاربران":
        await show_users_list(update, context)
    
    elif text == "📞 پیام‌های پشتیبانی":
        await show_support_messages(update, context)
    
    elif text == "🔙 بازگشت":
        await update.message.reply_text(
            "بازگشت به منوی اصلی:",
            reply_markup=get_persistent_keyboard(user_id)
        )

# ----- مدیریت ادمین‌ها -----
async def manage_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """مدیریت ادمین‌ها"""
    user_id = update.message.from_user.id
    
    admins = get_all_admins()
    admin_list = "👥 **لیست ادمین‌ها:**\n\n"
    for admin_id, username, added_at in admins:
        date_str = datetime.fromtimestamp(added_at).strftime("%Y/%m/%d")
        admin_list += f"• @{username or 'بدون نام'} (ID: `{admin_id}`) - {date_str}\n"
    
    keyboard = [
        [InlineKeyboardButton("➕ افزودن ادمین", callback_data="admin_add")],
        [InlineKeyboardButton("➖ حذف ادمین", callback_data="admin_remove")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="admin_back")]
    ]
    
    await update.message.reply_text(
        admin_list,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

# ----- توابع مدیریت ادمین -----
async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """مدیریت callback های ادمین"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if not is_admin(user_id):
        await query.message.reply_text("❌ دسترسی denied.")
        return
        
    data = query.data
    
    if data == "admin_add":
        admin_add_wait[user_id] = True
        save_user_state(user_id, "admin_add_wait")
        await query.message.reply_text(
            "👤 لطفاً آیدی عددی کاربر مورد نظر را برای افزودن به ادمین‌ها ارسال کنید:\n\n"
            "⚠️ توجه: کاربر باید قبلاً با ربات استارت کرده باشد."
        )
    
    elif data == "admin_remove":
        admin_remove_wait[user_id] = True
        save_user_state(user_id, "admin_remove_wait")
        admins = get_all_admins()
        admin_list = "👥 **لیست ادمین‌ها برای حذف:**\n\n"
        for admin_id, username, _ in admins:
            if admin_id != config.ADMIN_CHAT_ID:
                admin_list += f"• @{username or 'بدون نام'} (ID: `{admin_id}`)\n"
        
        await query.message.reply_text(
            f"{admin_list}\nلطفاً آیدی عددی ادمین مورد نظر را برای حذف ارسال کنید:",
            parse_mode='Markdown'
        )
    
    elif data == "admin_back":
        await show_admin_panel_from_callback(update, context)
    
    elif data.startswith("admin_price_event|"):
        event_id = int(data.split("|")[1])
        await show_seat_selection_for_price(update, context, event_id)

    elif data.startswith("admin_price_seat|"):
        parts = data.split("|")
        event_id = int(parts[1])
        seat_id = parts[2]
        
        admin_price_wait[user_id] = (event_id, seat_id)
        save_user_state(user_id, "admin_price_wait", str((event_id, seat_id)))
        await query.message.reply_text(
            f"💵 لطفاً قیمت جدید برای صندلی {seat_id} را (فقط عدد) ارسال کنید:\n\n"
            "مثال: 150000"
        )

    elif data.startswith("support_page|"):
        page = int(data.split("|")[1])
        await show_support_messages_page(update, context, page)
    
    elif data.startswith("view_support_message|"):
        message_id = int(data.split("|")[1])
        await show_support_message_detail(update, context, message_id)
    
    elif data == "support_back_to_list":
        await show_support_messages_page(update, context, page=0)
    
    elif data in ["refresh_support_messages", "support_back"]:
        await show_support_messages_page(update, context, page=0)
    
    elif data.startswith("support_reply|"):
        target_user_id = int(data.split("|")[1])
        
        admin_reply_wait[user_id] = target_user_id
        save_user_state(user_id, "admin_reply_wait", str(target_user_id))
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT username, first_name, last_name FROM users WHERE user_id=?', (target_user_id,))
        user_info = c.fetchone()
        conn.close()
        
        username = user_info[0] if user_info else ""
        first_name = user_info[1] if user_info else ""
        last_name = user_info[2] if user_info else ""
        
        user_display = f"@{username}" if username else f"{first_name} {last_name}".strip()
        if not user_display:
            user_display = f"آیدی: {target_user_id}"
        
        await query.message.reply_text(
            f"💬 **پاسخ به کاربر**\n\n"
            f"👤 کاربر: {user_display}\n"
            f"🆔 آیدی: `{target_user_id}`\n\n"
            f"لطفاً پاسخ خود را ارسال کنید:\n\n"
            f"📝 می‌توانید متن، عکس یا فایل ارسال کنید.\n"
            f"❌ برای لغو از منوی مدیریت استفاده کنید.",
            parse_mode='Markdown'
        )
    
    elif data.startswith("support_history|"):
        target_user_id = int(data.split("|")[1])
        await show_user_support_history(update, context, target_user_id)
    
    elif data.startswith("support_resolved|"):
        parts = data.split("|")
        target_user_id = int(parts[1])
        
        if len(parts) > 2:
            message_id = int(parts[2])
            await run_in_thread(delete_support_message, message_id)
            await query.message.edit_text("✅ پیام پشتیبانی حذف شد.")
        else:
            try:
                await context.bot.send_message(
                    chat_id=target_user_id,
                    text="✅ **پیام از پشتیبانی:**\n\nمشکل شما توسط پشتیبان به عنوان حل شده علامت گذاری شد.\n\nدر صورت نیاز بیشتر می‌توانید مجدداً با پشتیبانی تماس بگیرید.",
                    parse_mode='Markdown'
                )
            except Exception as e:
                logger.error(f"خطا در ارسال پیام به کاربر {target_user_id}: {e}")
            
            await query.message.reply_text("✅ مشکل کاربر به عنوان حل شده علامت گذاری شد.")

async def show_admin_panel_from_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش پنل مدیریت از طریق callback"""
    query = update.callback_query
    user_id = query.from_user.id
    
    keyboard = [
        [KeyboardButton("👥 مدیریت ادمین‌ها"), KeyboardButton("💰 گزارش مالی")],
        [KeyboardButton("🎯 مدیریت قیمت صندلی‌ها")],
        [KeyboardButton("📊 آمار لحظه‌ای")],
        [KeyboardButton("👤 لیست کاربران")],
        [KeyboardButton("📞 پیام‌های پشتیبانی")],
        [KeyboardButton("🔙 بازگشت")]
    ]
    
    await query.message.reply_text(
        "🛠 **پنل مدیریت**\n\nلطفاً گزینه مورد نظر را انتخاب کنید:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
        parse_mode='Markdown'
    )

async def show_user_support_history(update: Update, context: ContextTypes.DEFAULT_TYPE, target_user_id: int):
    """نمایش تاریخچه پیام‌های پشتیبانی کاربر"""
    query = update.callback_query
    user_id = query.from_user.id
    
    if not is_admin(user_id):
        return
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    c.execute('SELECT username, first_name, last_name FROM users WHERE user_id=?', (target_user_id,))
    user_info = c.fetchone()
    
    c.execute('''
        SELECT message_text, created_at, status 
        FROM support_messages 
        WHERE user_id=? 
        ORDER BY created_at DESC 
        LIMIT 10
    ''', (target_user_id,))
    messages = c.fetchall()
    
    conn.close()
    
    username = user_info[0] if user_info else ""
    first_name = user_info[1] if user_info else ""
    last_name = user_info[2] if user_info else ""
    
    user_display = f"@{username}" if username else f"{first_name} {last_name}".strip()
    if not user_display:
        user_display = f"آیدی: {target_user_id}"
    
    history_text = f"📋 **تاریخچه پشتیبانی کاربر**\n\n"
    history_text += f"👤 **کاربر:** {user_display}\n"
    history_text += f"🆔 **آیدی:** `{target_user_id}`\n\n"
    
    if not messages:
        history_text += "📭 هیچ پیام پشتیبانی از این کاربر ثبت نشده است."
    else:
        history_text += "📝 **آخرین پیام‌ها:**\n\n"
        for i, (message_text, created_at, status) in enumerate(messages, 1):
            time_str = datetime.fromtimestamp(created_at).strftime("%Y/%m/%d %H:%M")
            status_icon = "✅" if status == 'handled' else "⏳"
            history_text += f"{i}. {status_icon} **{time_str}**\n"
            if len(message_text) > 100:
                history_text += f"   {message_text[:100]}...\n\n"
            else:
                history_text += f"   {message_text}\n\n"
    
    keyboard = [
        [InlineKeyboardButton("💬 پاسخ به کاربر", callback_data=f"support_reply|{target_user_id}")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="support_back")]
    ]
    
    await query.message.reply_text(
        history_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

# ----- هندلرهای ورودی ادمین -----
async def handle_admin_add_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """مدیریت ورود آیدی برای افزودن ادمین"""
    user_id = update.message.from_user.id
    text = update.message.text.strip()
    
    try:
        new_admin_id = int(text)
        
        try:
            user = await context.bot.get_chat(new_admin_id)
            username = user.username or f"user_{new_admin_id}"
            
            if add_admin(new_admin_id, user_id, username):
                await update.message.reply_text(
                    f"✅ کاربر @{username} (آیدی: `{new_admin_id}`) با موفقیت به ادمین‌ها اضافه شد."
                )
                admin_add_wait.pop(user_id, None)
                clear_user_state(user_id)
                await show_admin_panel(update, context)
            else:
                await update.message.reply_text("❌ خطا در اضافه کردن ادمین.")
        except Exception as e:
            await update.message.reply_text(
                f"❌ کاربر یافت نشد! مطمئن شوید کاربر قبلاً با ربات استارت کرده است.\nخطا: {e}"
            )
            
    except ValueError:
        await update.message.reply_text("❌ لطفاً یک آیدی عددی معتبر وارد کنید.")

async def handle_admin_remove_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """مدیریت ورود آیدی برای حذف ادمین"""
    user_id = update.message.from_user.id
    text = update.message.text.strip()
    
    try:
        remove_admin_id = int(text)
        if remove_admin_id == config.ADMIN_CHAT_ID:
            await update.message.reply_text("❌ نمی‌توانید ادمین اصلی را حذف کنید.")
        elif remove_admin(remove_admin_id):
            await update.message.reply_text(f"✅ ادمین با آیدی `{remove_admin_id}` حذف شد.")
            admin_remove_wait.pop(user_id, None)
            clear_user_state(user_id)
            await show_admin_panel(update, context)
        else:
            await update.message.reply_text("❌ خطا در حذف ادمین یا کاربر یافت نشد.")
    except ValueError:
        await update.message.reply_text("❌ لطفاً یک آیدی عددی معتبر وارد کنید.")

async def handle_admin_price_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """مدیریت ورود قیمت توسط ادمین"""
    user_id = update.message.from_user.id
    text = update.message.text.strip()
    
    if user_id not in admin_price_wait:
        return
    
    event_id, seat_id = admin_price_wait[user_id]
    
    price_text = ''.join(filter(str.isdigit, text))
    
    if not price_text:
        await update.message.reply_text("❌ لطفاً یک عدد معتبر وارد کنید (مثال: 150000).")
        return
    
    try:
        price = int(price_text)
        if price <= 0:
            await update.message.reply_text("❌ قیمت باید بزرگتر از صفر باشد.")
            return
            
        if price > 10000000:
            await update.message.reply_text("❌ قیمت وارد شده بسیار بزرگ است.")
            return
            
    except ValueError:
        await update.message.reply_text("❌ لطفاً یک عدد معتبر وارد کنید (مثال: 150000).")
        return
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE seats SET price=? WHERE event_id=? AND seat_id=?", (price, event_id, seat_id))
    conn.commit()
    conn.close()
    
    del admin_price_wait[user_id]
    clear_user_state(user_id)
    
    event = next((ev for ev in config.EVENTS if ev["id"] == event_id), None)
    event_name = event['title'] if event else f"رویداد {event_id}"
    
    await update.message.reply_text(
        f"✅ قیمت صندلی {seat_id} در اجرای **{event_name}** به {price:,} تومان تغییر کرد.",
        parse_mode='Markdown'
    )
    
    await show_admin_panel(update, context)

async def show_seat_selection_for_price(update: Update, context: ContextTypes.DEFAULT_TYPE, event_id: int):
    """نمایش صندلی‌ها برای تغییر قیمت"""
    query = update.callback_query
    user_id = query.from_user.id
    
    seats = await get_seats(event_id)
    
    event = next((ev for ev in config.EVENTS if ev["id"] == event_id), None)
    if not event:
        await query.message.reply_text("❌ رویداد یافت نشد.")
        return
    
    keyboard = []
    current_row = []
    
    for seat_id, r, c, status, _, price in seats:
        seat_label = f"{r}-{c}"
        current_row.append(InlineKeyboardButton(
            seat_label, 
            callback_data=f"admin_price_seat|{event_id}|{seat_id}"
        ))
        
        if len(current_row) >= 5:
            keyboard.append(current_row)
            current_row = []
    
    if current_row:
        keyboard.append(current_row)
    
    keyboard.append([InlineKeyboardButton("🔙 بازگشت", callback_data="admin_back")])
    
    await query.message.reply_text(
        f"💵 **مدیریت قیمت - {event['title']}**\n\n"
        "لطفاً صندلی مورد نظر برای تغییر قیمت را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

# ----- هندلر پاسخ ادمین -----
async def handle_admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """مدیریت پاسخ ادمین به کاربر"""
    user_id = update.message.from_user.id
    
    if user_id not in admin_reply_wait:
        return
    
    target_user_id = admin_reply_wait[user_id]
    
    try:
        if update.message.text:
            await context.bot.send_message(
                chat_id=target_user_id,
                text=f"💬 **پاسخ پشتیبانی:**\n\n{update.message.text}\n\n📞 در صورت نیاز بیشتر با پشتیبانی تماس بگیرید.",
                parse_mode='Markdown'
            )
            await update.message.reply_text("✅ پاسخ متنی شما برای کاربر ارسال شد.")
        
        elif update.message.photo:
            file = await update.message.photo[-1].get_file()
            await context.bot.send_photo(
                chat_id=target_user_id,
                photo=file.file_id,
                caption="📸 **پاسخ پشتیبانی**\n\nدر صورت نیاز بیشتر با پشتیبانی تماس بگیرید.",
                parse_mode='Markdown'
            )
            await update.message.reply_text("✅ عکس شما برای کاربر ارسال شد.")
        
        elif update.message.document:
            file = await update.message.document.get_file()
            file_name = update.message.document.file_name or "فایل"
            await context.bot.send_document(
                chat_id=target_user_id,
                document=file.file_id,
                caption=f"📎 **پاسخ پشتیبانی - {file_name}**\n\nدر صورت نیاز بیشتر با پشتیبانی تماس بگیرید.",
                parse_mode='Markdown'
            )
            await update.message.reply_text("✅ فایل شما برای کاربر ارسال شد.")
        
        else:
            await update.message.reply_text("❌ این نوع پیام پشتیبانی نمی‌شود.")
            return
    
    except Exception as e:
        error_msg = f"❌ خطا در ارسال پاسخ: {str(e)}"
        await update.message.reply_text(error_msg)
        logger.error(f"خطا در ارسال پاسخ ادمین به کاربر {target_user_id}: {e}")
    
    admin_reply_wait.pop(user_id, None)
    clear_user_state(user_id)
    
    await show_admin_panel(update, context)

# ----- Callback Router اصلی -----
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    logger.info(f"Callback received: {data} from user: {user_id}")

    try:
        # هندلرهای پشتیبانی - اولویت اول
        if data.startswith("support_"):
            await handle_admin_callback(update, context)
            return
            
        # هندلرهای ادمین
        elif data.startswith("admin_"):
            await handle_admin_callback(update, context)
            return

        elif data.startswith("stats|"):
            event_id = int(data.split("|")[1])
            path = await generate_seat_map_image(event_id)
            
            event = next((ev for ev in config.EVENTS if ev["id"] == event_id), None)
            if not event:
                await context.bot.send_message(chat_id=user_id, text="❌ رویداد یافت نشد.")
                return
            
            seats = await get_seats(event_id)
            total_seats = len(seats)
            free_seats = len([s for s in seats if s[3] == 'free'])
            reserved_seats = len([s for s in seats if s[3] == 'reserved'])
            sold_seats = len([s for s in seats if s[3] == 'sold'])
            
            stats_text = (
                f"📊 **آمار صندلی‌ها - {event['title']}**\n\n"
                f"🎫 **کل صندلی‌ها:** {total_seats}\n"
                f"🟢 **آزاد:** {free_seats}\n"
                f"🟡 **رزرو شده:** {reserved_seats}\n"
                f"🔴 **فروخته شده:** {sold_seats}\n"
                f"📈 **پرشدگی:** {((sold_seats + reserved_seats) / total_seats * 100):.1f}%"
            )
            
            try:
                with open(path, "rb") as photo:
                    await context.bot.send_photo(
                        chat_id=user_id,
                        photo=photo,
                        caption=stats_text,
                        parse_mode='Markdown'
                    )
            except Exception as e:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"📊 **آمار صندلی‌ها - {event['title']}**\n\n{stats_text}",
                    parse_mode='Markdown'
                )

        elif data.startswith("map|"):
            event_id = int(data.split("|")[1])
            path = await generate_seat_map_image(event_id)
            with open(path, "rb") as photo:
                await context.bot.send_photo(
                    chat_id=user_id, 
                    photo=photo, 
                    caption="نقشه صندلی: 🟩آزاد 🟨رزرو شده 🟥فروخته شده 🟦وی‌آی‌پی"
                )

        elif data.startswith("event|"):
            event_id = int(data.split("|")[1])
            path = await generate_seat_map_image(event_id)
            seats = await get_seats(event_id)
            
            event = next((ev for ev in config.EVENTS if ev["id"] == event_id), None)
            if not event:
                await context.bot.send_message(chat_id=user_id, text="❌ رویداد یافت نشد.")
                return
            
            keyboard = []
            current_row = []
            
            for seat_id, r, c, status, _, price in seats:
                seat_label = f"{r}-{c}"
                
                if status == 'free':
                    btn = InlineKeyboardButton(seat_label, callback_data=f"seat|{event_id}|{seat_id}")
                elif status == 'reserved':
                    btn = InlineKeyboardButton(f"⏳{seat_label}", callback_data="disabled")
                else:
                    btn = InlineKeyboardButton(f"❌{seat_label}", callback_data="disabled")
                
                current_row.append(btn)
                
                if len(current_row) >= 5:
                    keyboard.append(current_row)
                    current_row = []
            
            if current_row:
                keyboard.append(current_row)
            
            keyboard.append([InlineKeyboardButton("🔙 بازگشت به لیست اجراها", callback_data="back_to_events")])
            
            with open(path, "rb") as photo:
                await context.bot.send_photo(
                    chat_id=user_id,
                    photo=photo,
                    caption=f"💺 **انتخاب صندلی - {event['title']}**\n\n"
                           "🟩 آزاد 🟨 رزرو شده 🟥 فروخته شده\n\n"
                           "لطفاً صندلی مورد نظر را انتخاب کنید:",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='Markdown'
                )

        elif data.startswith("seat|"):
            parts = data.split("|")
            event_id = int(parts[1])
            seat_id = parts[2]
            
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute('SELECT status FROM seats WHERE event_id=? AND seat_id=?', (event_id, seat_id))
            seat_status = c.fetchone()
            conn.close()
            
            if not seat_status or seat_status[0] != 'free':
                await context.bot.send_message(chat_id=user_id, text="❌ این صندلی در دسترس نیست.")
                return
                
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            
            c.execute('SELECT title, event_date FROM events WHERE id=?', (event_id,))
            event_info = c.fetchone()
            event_title = event_info[0] if event_info else "نامشخص"
            
            c.execute('SELECT price FROM seats WHERE event_id=? AND seat_id=?', (event_id, seat_id))
            price_info = c.fetchone()
            price = price_info[0] if price_info else 0
            
            conn.close()
            
            confirmation_text = (
                "🎯 **تأیید نهایی رزرو**\n\n"
                f"🎭 **اجرا:** {event_title}\n"
                f"💺 **صندلی:** {seat_id}\n"
                f"💰 **قیمت:** {price:,} تومان\n"
                f"👤 **کاربر:** {user_id}\n\n"
                "⚠️ **توجه:** این رزرو به مدت ۳۰ دقیقه معتبر است.\n"
                "پس از پرداخت، رسید خود را ارسال کنید."
            )
            
            keyboard = [
                [InlineKeyboardButton("✅ تأیید و رزرو", callback_data=f"confirm|{event_id}|{seat_id}")],
                [InlineKeyboardButton("❌ انصراف", callback_data=f"cancel|{event_id}")]
            ]
            
            await context.bot.send_message(
                chat_id=user_id,
                text=confirmation_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )

        elif data.startswith("confirm|"):
            parts = data.split("|")
            event_id = int(parts[1])
            seat_id = parts[2]
            
            ok, err = await set_reserved(event_id, seat_id, user_id)
            if not ok:
                await context.bot.send_message(chat_id=user_id, text=err)
                return
                
            seats = await get_seats(event_id)
            price = next(p for s, r, c, st, uid, p in seats if s==seat_id)
            
            msg_user = (
                f"✅ **صندلی {seat_id} برای شما رزرو شد!**\n\n"
                f"💰 **مبلغ قابل پرداخت:** {price:,} تومان\n"
                f"💳 **شماره کارت:** `{config.BANK_CARD}`\n"
                f"⏰ **زمان باقی‌مانده:** ۳۰ دقیقه\n\n"
                "📸 لطفاً پس از پرداخت، عکس رسید را ارسال کنید."
            )
            
            await context.bot.send_message(
                chat_id=user_id, 
                text=msg_user, 
                parse_mode='Markdown'
            )
            
            admin_msg = (
                f"🔔 **رزرو جدید**\n\n"
                f"👤 کاربر: @{query.from_user.username or user_id}\n"
                f"🎭 اجرا: {event_id}\n"
                f"💺 صندلی: {seat_id}\n"
                f"💰 مبلغ: {price:,} تومان"
            )
            
            admins = get_all_admins()
            for admin_id, _, _ in admins:
                try:
                    await context.bot.send_message(chat_id=admin_id, text=admin_msg, parse_mode='Markdown')
                except:
                    pass

        elif data.startswith("cancel|"):
            event_id = int(data.split("|")[1])
            await context.bot.send_message(
                chat_id=user_id,
                text="❌ رزرو لغو شد. می‌توانید اجرای دیگری را انتخاب کنید."
            )

        elif data == "back_to_events":
            await show_events_list(update, context)

        elif data in ["refresh_users", "show_users_list"]:
            await show_users_list(update, context)

        elif data in ["users_stats", "refresh_users_stats"]:
            await show_users_stats(update, context)

        else:
            logger.warning(f"Unknown callback data: {data}")
            
    except Exception as e:
        logger.error(f"Error in callback router: {e}")
        await context.bot.send_message(
            chat_id=user_id,
            text="❌ خطایی در پردازش درخواست شما رخ داده است."
        )

# ----- هندلر پرداخت -----
async def handle_payment_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    seat_info = await get_reserved_seat_by_user(user_id)
    if not seat_info:
        await update.message.reply_text("ابتدا صندلی رزرو کنید.")
        return
    event_id, seat_id = seat_info
    if update.message.photo:
        username = update.message.from_user.username or user_id
        file = await update.message.photo[-1].get_file()
        path = f"receipts/{username}_{seat_id}_{int(time.time())}.jpg"
        await file.download_to_drive(path)
        
        receipt_path = await generate_beautiful_receipt(user_id, event_id, seat_id, username)
        qr_path = await generate_qr_code(event_id, seat_id, user_id)
        
        await update.message.reply_text("✅ رسید دریافت شد و برای ادمین ارسال شد.")
        
        with open(receipt_path, "rb") as photo:
            await context.bot.send_photo(
                chat_id=user_id,
                photo=photo,
                caption="🎫 **رسید پرداخت شما**\n\nاین رسید را تا زمان تأیید نهایی نگه دارید.",
                parse_mode='Markdown'
            )
        
        admin_caption = (
            f"📸 **رسید پرداخت جدید**\n\n"
            f"👤 کاربر: @{username}\n"
            f"🎭 اجرا: {event_id}\n"
            f"💺 صندلی: {seat_id}\n"
            f"🆔 آیدی: {user_id}"
        )
        
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ تایید پرداخت", callback_data=f"admin_approve|{event_id}|{seat_id}|{user_id}"),
            InlineKeyboardButton("❌ رد پرداخت", callback_data=f"admin_reject|{event_id}|{seat_id}|{user_id}")
        ]])
        
        admins = get_all_admins()
        for admin_id, _, _ in admins:
            try:
                with open(path, "rb") as photo:
                    await context.bot.send_photo(
                        chat_id=admin_id,
                        photo=photo,
                        caption=admin_caption,
                        reply_markup=kb,
                        parse_mode='Markdown'
                    )
            except Exception as e:
                logger.error(f"خطا در ارسال رسید به ادمین {admin_id}: {e}")
    else:
        await update.message.reply_text("لطفاً عکس رسید را ارسال کنید.")

# ----- هندلر تایید پرداخت توسط ادمین -----
async def handle_admin_approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """مدیریت تایید/رد پرداخت توسط ادمین"""
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    
    if not is_admin(user_id):
        await query.message.reply_text("❌ دسترسی denied.")
        return
        
    logger.info(f"ادمین {user_id} دکمه {data} را زد")
    
    if data.startswith("admin_approve|") or data.startswith("admin_reject|"):
        parts = data.split("|")
        action = "approve" if data.startswith("admin_approve") else "reject"
        event_id = int(parts[1])
        seat_id = parts[2]
        customer_user_id = int(parts[3])
        
        logger.info(f"پردازش {action} برای صندلی {seat_id} کاربر {customer_user_id}")
        
        if action == "approve":
            await mark_sold(event_id, seat_id)
            await record_successful_payment(customer_user_id, event_id, seat_id)
            
            qr_path = await generate_qr_code(event_id, seat_id, customer_user_id)
            receipt_path = await generate_beautiful_receipt(customer_user_id, event_id, seat_id)
            
            try:
                await context.bot.send_message(
                    chat_id=customer_user_id,
                    text="🎉 **پرداخت شما تأیید شد!**\n\nبلیت شما آماده است.",
                    parse_mode='Markdown'
                )
                
                with open(receipt_path, "rb") as photo:
                    await context.bot.send_photo(
                        chat_id=customer_user_id,
                        photo=photo,
                        caption="🎫 **بلیت شما**\n\nاین بلیت را هنگام ورود نشان دهید.",
                        parse_mode='Markdown'
                    )
                
                with open(qr_path, "rb") as photo:
                    await context.bot.send_photo(
                        chat_id=customer_user_id,
                        photo=photo,
                        caption="📱 **QR Code بلیت**\n\nاین کد برای ورود اسکن خواهد شد.",
                        parse_mode='Markdown'
                    )
                logger.info(f"پیام تایید به کاربر {customer_user_id} ارسال شد")
            except Exception as e:
                logger.error(f"خطا در ارسال پیام به کاربر: {e}")
            
            try:
                await query.message.edit_reply_markup(reply_markup=None)
                await query.message.edit_caption(
                    caption=f"✅ **پرداخت تایید شد!**\n\n"
                           f"👤 کاربر: {customer_user_id}\n"
                           f"🎭 اجرا: {event_id}\n"
                           f"💺 صندلی: {seat_id}\n"
                           f"🕒 زمان: {datetime.now().strftime('%Y/%m/%d %H:%M')}",
                    parse_mode='Markdown'
                )
                logger.info("پیام ادمین آپدیت شد")
            except Exception as e:
                logger.error(f"خطا در آپدیت پیام ادمین: {e}")
                
        else:
            await release_seat(event_id, seat_id)
            
            try:
                await context.bot.send_message(
                    chat_id=customer_user_id,
                    text="❌ پرداخت شما رد شد و صندلی آزاد گردید.\nلطفاً با پشتیبانی تماس بگیرید."
                )
                logger.info(f"پیام رد به کاربر {customer_user_id} ارسال شد")
            except Exception as e:
                logger.error(f"خطا در ارسال پیام به کاربر: {e}")
            
            try:
                await query.message.edit_reply_markup(reply_markup=None)
                await query.message.edit_caption(
                    caption=f"❌ **پرداخت رد شد!**\n\n"
                           f"👤 کاربر: {customer_user_id}\n"
                           f"🎭 اجرا: {event_id}\n"
                           f"💺 صندلی: {seat_id}\n"
                           f"🕒 زمان: {datetime.now().strftime('%Y/%m/%d %H:%M')}",
                    parse_mode='Markdown'
                )
                logger.info("پیام ادمین آپدیت شد")
            except Exception as e:
                logger.error(f"خطا در آپدیت پیام ادمین: {e}")

# ----- راه‌اندازی -----
def main():
    global app
    init_db()
    app = ApplicationBuilder().token(config.BOT_TOKEN).build()

    scheduler = BackgroundScheduler()
    scheduler.add_job(release_expired_seats, 'interval', seconds=30)
    scheduler.start()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", start))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_all_text_messages))

    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handle_payment_receipt))

    app.add_handler(CallbackQueryHandler(callback_router))

    app.add_handler(CallbackQueryHandler(handle_admin_approval_callback, pattern="^admin_(approve|reject)\|"))

    print("🤖 Bot started with complete support system...")
    app.run_polling()

if __name__ == "__main__":
    main()
    
