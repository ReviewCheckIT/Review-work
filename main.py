import os
import json
import logging
import threading
import time
import asyncio
import csv
import io
import random
from datetime import datetime, timedelta
import pytz
import requests
import firebase_admin
from firebase_admin import credentials, firestore
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, TimedOut
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler,
    MessageHandler, filters, ConversationHandler, Application
)
from google_play_scraper import Sort, reviews as play_reviews
from flask import Flask, request, jsonify
import schedule

# ==========================================
# 1. কনফিগারেশন এবং সেটআপ
# ==========================================

# লগিং সেটআপ
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ENV ভেরিয়েবল
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OWNER_ID = os.environ.get("OWNER_ID", "")
FIREBASE_JSON = os.environ.get("FIREBASE_CREDENTIALS", "firebase_key.json")
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', "")
IMGBB_API_KEY = os.environ.get('IMGBB_API_KEY', "")
PORT = int(os.environ.get("PORT", 8080))
WEB_URL = os.environ.get("WEB_URL", "")
WEB_API_TOKEN = os.environ.get("WEB_API_TOKEN", "secret_token_12345")
TIMEZONE = pytz.timezone('Asia/Dhaka')

# AI Import Safeguard
try:
    import google.generativeai as genai
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel('gemini-1.5-flash')
        AI_AVAILABLE = True
    else:
        AI_AVAILABLE = False
except Exception as e:
    logger.warning(f"AI Library skipped: {e}")
    AI_AVAILABLE = False
    model = None

# Firebase কানেকশন
if not firebase_admin._apps:
    try:
        if FIREBASE_JSON.startswith("{"):
            cred_dict = json.loads(FIREBASE_JSON)
            cred = credentials.Certificate(cred_dict)
        else:
            cred = credentials.Certificate(FIREBASE_JSON)
        firebase_admin.initialize_app(cred)
        print("✅ Firebase Connected Successfully!")
    except Exception as e:
        logger.error(f"❌ Firebase Connection Failed: {e}")
        # Local fallback for testing only
        if os.path.exists("firebase_key.json"):
            cred = credentials.Certificate("firebase_key.json")
            firebase_admin.initialize_app(cred)

db = firestore.client()

# ==========================================
# 2. গ্লোবাল কনফিগারেশন ও স্টেট
# ==========================================

# ক্যাশিং সিস্টেম (Performance Optimization)
CONFIG_CACHE = {}
LAST_CONFIG_FETCH = 0
CACHE_TTL = 60  # 60 সেকেন্ড ক্যাশ থাকবে

DEFAULT_CONFIG = {
    "task_price": 5.0,
    "referral_bonus": 2.0,
    "min_withdraw": 50.0,
    "monitored_apps": [],
    "log_channel_id": "",
    "work_start_time": "15:30",
    "work_end_time": "23:00",
    "rules_text": "⚠️ কাজের নিয়ম: অ্যাপ ইন্সটল করুন, ৫ স্টার রেটিং দিন এবং সুন্দর রিভিউ লিখুন।",
    "schedule_text": "⏰ কাজের সময়: প্রতিদিন বিকেল ৩:৩০ থেকে রাত ১১:০০ পর্যন্ত।",
    "buttons": {
        "submit": {"text": "💰 কাজ জমা দিন", "show": True},
        "profile": {"text": "👤 প্রোফাইল", "show": True},
        "withdraw": {"text": "📤 উইথড্র", "show": True},
        "refer": {"text": "📢 রেফার", "show": True},
        "schedule": {"text": "📅 সময়সূচী", "show": True}
    },
    "custom_buttons": [],
    "auto_approve_time": "20:30",
    "check_interval_hours": 24
}

# Conversation States
(
    T_APP_SELECT, T_REVIEW_NAME, T_EMAIL, T_DEVICE, T_SS,           
    ADD_APP_ID, ADD_APP_NAME, ADD_APP_LIMIT,                        
    WD_METHOD, WD_NUMBER, WD_AMOUNT,                                
    REMOVE_APP_SELECT,                                              
    ADMIN_USER_SEARCH, ADMIN_USER_ACTION, ADMIN_USER_AMOUNT,        
    ADMIN_EDIT_TEXT_VAL, ADMIN_EDIT_BTN_NAME,
    ADMIN_ADD_BTN_NAME, ADMIN_ADD_BTN_LINK,                         
    ADMIN_SET_LOG_CHANNEL,                                          
    ADMIN_ADD_ADMIN_ID, ADMIN_RMV_ADMIN_ID,                         
    ADMIN_SET_START_TIME, ADMIN_SET_END_TIME,                       
    EDIT_APP_SELECT, EDIT_APP_LIMIT_VAL,                            
    REMOVE_CUS_BTN                                                  
) = range(27)

# ==========================================
# 3. হেল্পার ফাংশন
# ==========================================

def get_config():
    global CONFIG_CACHE, LAST_CONFIG_FETCH
    current_time = time.time()
    
    if CONFIG_CACHE and (current_time - LAST_CONFIG_FETCH < CACHE_TTL):
        return CONFIG_CACHE

    try:
        ref = db.collection('settings').document('main_config')
        doc = ref.get()
        if doc.exists:
            data = doc.to_dict()
            # Merge with default to ensure all keys exist
            merged_config = DEFAULT_CONFIG.copy()
            merged_config.update(data)
            CONFIG_CACHE = merged_config
        else:
            ref.set(DEFAULT_CONFIG)
            CONFIG_CACHE = DEFAULT_CONFIG
        
        LAST_CONFIG_FETCH = current_time
        return CONFIG_CACHE
    except Exception as e:
        logger.error(f"Config Fetch Error: {e}")
        return DEFAULT_CONFIG

def update_config(data):
    global CONFIG_CACHE
    try:
        db.collection('settings').document('main_config').set(data, merge=True)
        # Invalidate cache
        CONFIG_CACHE = {}
    except Exception as e:
        logger.error(f"Config Update Error: {e}")

def get_bd_time():
    return datetime.now(TIMEZONE)

def is_working_hour():
    config = get_config()
    start_str = config.get("work_start_time", "15:30")
    end_str = config.get("work_end_time", "23:00")

    try:
        now = get_bd_time().time()
        start = datetime.strptime(start_str, "%H:%M").time()
        end = datetime.strptime(end_str, "%H:%M").time()

        if start < end:
            return start <= now <= end
        else:
            # Crosses midnight
            return now >= start or now <= end
    except Exception as e:
        logger.error(f"Time Check Error: {e}")
        return True

def is_admin(user_id):
    if str(user_id) == str(OWNER_ID): return True
    try:
        user = db.collection('users').document(str(user_id)).get()
        return user.exists and user.to_dict().get('is_admin', False)
    except: return False

def get_user(user_id):
    try:
        doc = db.collection('users').document(str(user_id)).get()
        if doc.exists: return doc.to_dict()
    except: pass
    return None

def create_user(user_id, first_name, referrer_id=None):
    user_ref = db.collection('users').document(str(user_id))
    user_doc = user_ref.get()
    
    if not user_doc.exists:
        try:
            web_password = str(random.randint(100000, 999999))
            
            user_data = {
                "id": str(user_id),
                "name": first_name,
                "balance": 0.0,
                "total_tasks": 0,
                "joined_at": datetime.now(),
                "referrer": referrer_id if referrer_id and referrer_id.isdigit() and str(referrer_id) != str(user_id) else None,
                "is_blocked": False,
                "is_admin": str(user_id) == str(OWNER_ID),
                "web_password": web_password,
                "referral_count": 0
            }
            user_ref.set(user_data)
            
            if referrer_id and referrer_id.isdigit() and str(referrer_id) != str(user_id):
                try:
                    # Transactional increment is safer but direct update is faster for this scale
                    db.collection('users').document(str(referrer_id)).update({
                        "referral_count": firestore.Increment(1)
                    })
                except Exception as e:
                    logger.error(f"Referrer update failed: {e}")
            
            return web_password
        except Exception as e:
            logger.error(f"Create user error: {e}")
            return None
    
    return user_doc.to_dict().get('web_password')

async def send_log_message(context, text, reply_markup=None):
    config = get_config()
    chat_id = config.get('log_channel_id')
    target_id = chat_id if chat_id else OWNER_ID
    if target_id:
        try:
            await context.bot.send_message(chat_id=target_id, text=text, reply_markup=reply_markup, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Log Send Error: {e}")

def get_app_task_count(app_id):
    try:
        # Optimization: Create a composite index in Firebase for (app_id, status)
        pending = db.collection('tasks').where('app_id', '==', app_id).where('status', '==', 'pending').count().get()
        approved = db.collection('tasks').where('app_id', '==', app_id).where('status', '==', 'approved').count().get()
        return pending[0][0].value + approved[0][0].value
    except Exception as e:
        logger.error(f"Count Error: {e}")
        return 0

# ==========================================
# 4. বট কমান্ড হ্যান্ডলার (User Side)
# ==========================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    referrer = args[0] if args and args[0].isdigit() else None
    
    # Run DB operation in thread to avoid blocking
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, create_user, user.id, user.first_name, referrer)
    
    # Check block status
    db_user = await loop.run_in_executor(None, get_user, user.id)
    if db_user and db_user.get('is_blocked'):
        await update.message.reply_text("⛔ আপনাকে ব্লক করা হয়েছে।")
        return

    config = get_config()
    btns_conf = config.get('buttons', DEFAULT_CONFIG['buttons'])

    welcome_msg = (
        f"আসসালামু আলাইকুম, {user.first_name}! 🌙\n\n"
        f"🗒 **কাজের নিয়মাবলী:**\n{config.get('rules_text', '')}\n\n"
        "নিচের মেনু থেকে অপশন সিলেক্ট করুন:"
    )

    keyboard = []
    row1, row2, row3 = [], [], []
    
    if btns_conf['submit']['show']: row1.append(InlineKeyboardButton(btns_conf['submit']['text'], callback_data="submit_task"))
    if btns_conf['profile']['show']: row1.append(InlineKeyboardButton(btns_conf['profile']['text'], callback_data="my_profile"))
    if btns_conf['withdraw']['show']: row2.append(InlineKeyboardButton(btns_conf['withdraw']['text'], callback_data="start_withdraw"))
    if btns_conf['refer']['show']: row2.append(InlineKeyboardButton(btns_conf['refer']['text'], callback_data="refer_friend"))
    if btns_conf.get('schedule', {}).get('show', True): row3.append(InlineKeyboardButton(btns_conf.get('schedule', {}).get('text', "📅 সময়সূচী"), callback_data="show_schedule"))
    row3.append(InlineKeyboardButton("🔄 রিফ্রেশ", callback_data="back_home"))

    if row1: keyboard.append(row1)
    if row2: keyboard.append(row2)
    if row3: keyboard.append(row3)

    for btn in config.get('custom_buttons', []):
        if btn.get('text') and btn.get('url'):
            keyboard.append([InlineKeyboardButton(btn['text'], url=btn['url'])])

    if is_admin(user.id):
        keyboard.append([InlineKeyboardButton("⚙️ এডমিন প্যানেল", callback_data="admin_panel")])

    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.message.reply_text(welcome_msg, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await update.message.reply_text(welcome_msg, reply_markup=reply_markup, parse_mode="Markdown")

async def common_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "back_home":
        # Edit message instead of sending new one to keep chat clean
        await start_menu_edit(update, context)
        return

    user_id = query.from_user.id
    loop = asyncio.get_running_loop()
    user = await loop.run_in_executor(None, get_user, user_id)

    if not user:
        await query.message.reply_text("❌ User not found, please type /start")
        return

    if data == "my_profile":
        msg = (
            f"👤 **প্রোফাইল**\n\n"
            f"🆔 ID: `{user['id']}`\n"
            f"👤 নাম: {user.get('name', 'N/A')}\n"
            f"💰 ব্যালেন্স: ৳{user['balance']:.2f}\n"
            f"✅ সম্পন্ন টাস্ক: {user['total_tasks']}\n"
            f"👥 রেফার করেছেন: {user.get('referral_count', 0)} জন\n"
            f"🔑 ওয়েব পাসওয়ার্ড: `{user.get('web_password', 'N/A')}`\n"
            f"🌐 ওয়েব লগইন: {WEB_URL}"
        )
        kb = [
            [InlineKeyboardButton("🔄 পাসওয়ার্ড পরিবর্তন", callback_data="reset_password")],
            [InlineKeyboardButton("🔙 হোম", callback_data="back_home")]
        ]
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

    elif data == "refer_friend":
        config = get_config()
        link = f"https://t.me/{context.bot.username}?start={user_id}"
        msg = (
            f"📢 **রেফার ও আর্ন**\n\n"
            f"🔗 **আপনার লিংক:**\n`{link}`\n\n"
            f"👥 মোট রেফার: {user.get('referral_count', 0)} জন\n"
            f"💰 প্রতি রেফার বোনাস: ৳{config['referral_bonus']:.2f}\n"
            "আপনার বন্ধু এই লিংকে জয়েন করে কাজ করলে আপনি বোনাস পাবেন।"
        )
        kb = [
            [InlineKeyboardButton("🔗 শেয়ার করুন", url=f"https://t.me/share/url?url={link}")],
            [InlineKeyboardButton("🔙 হোম", callback_data="back_home")]
        ]
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

    elif data == "show_schedule":
        config = get_config()
        msg = (
            f"📅 **সময়সূচী**\n\n{config.get('schedule_text', '')}\n\n"
            f"🟢 শুরু: {config.get('work_start_time')}\n"
            f"🔴 শেষ: {config.get('work_end_time')}\n"
            f"🕒 অটো এপ্রুভ: {config.get('auto_approve_time')}"
        )
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 হোম", callback_data="back_home")]]))

    elif data == "reset_password":
        new_pass = str(random.randint(100000, 999999))
        db.collection('users').document(str(user_id)).update({'web_password': new_pass})
        await query.edit_message_text(f"✅ নতুন পাসওয়ার্ড: `{new_pass}`", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 প্রোফাইল", callback_data="my_profile")]]))

async def start_menu_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Re-generates the start menu but edits existing message
    user = update.effective_user
    config = get_config()
    btns_conf = config.get('buttons', DEFAULT_CONFIG['buttons'])
    
    welcome_msg = (
        f"আসসালামু আলাইকুম, {user.first_name}! 🌙\n\n"
        f"🗒 **কাজের নিয়মাবলী:**\n{config.get('rules_text', '')}\n\n"
        "নিচের মেনু থেকে অপশন সিলেক্ট করুন:"
    )
    keyboard = []
    row1, row2, row3 = [], [], []
    if btns_conf['submit']['show']: row1.append(InlineKeyboardButton(btns_conf['submit']['text'], callback_data="submit_task"))
    if btns_conf['profile']['show']: row1.append(InlineKeyboardButton(btns_conf['profile']['text'], callback_data="my_profile"))
    if btns_conf['withdraw']['show']: row2.append(InlineKeyboardButton(btns_conf['withdraw']['text'], callback_data="start_withdraw"))
    if btns_conf['refer']['show']: row2.append(InlineKeyboardButton(btns_conf['refer']['text'], callback_data="refer_friend"))
    row3.append(InlineKeyboardButton("🔄 রিফ্রেশ", callback_data="back_home"))
    
    if row1: keyboard.append(row1)
    if row2: keyboard.append(row2)
    keyboard.append(row3)
    
    if is_admin(user.id):
        keyboard.append([InlineKeyboardButton("⚙️ এডমিন প্যানেল", callback_data="admin_panel")])
        
    try:
        await update.callback_query.edit_message_text(welcome_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    except:
        await update.callback_query.message.reply_text(welcome_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

# ==========================================
# 5. টাস্ক সাবমিশন সিস্টেম (Robust)
# ==========================================

async def task_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not is_working_hour():
        await query.edit_message_text(
            "⛔ **এখন কাজের সময় নয়!**\nসময়সূচী দেখুন।", 
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 হোম", callback_data="back_home")]])
        )
        return ConversationHandler.END

    config = get_config()
    apps = config.get('monitored_apps', [])

    if not apps:
        await query.edit_message_text("❌ বর্তমানে কোনো কাজ নেই।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 হোম", callback_data="back_home")]]))
        return ConversationHandler.END

    buttons = []
    for app in apps:
        count = get_app_task_count(app['id'])
        limit = app.get('limit', 1000)
        btn_txt = f"📱 {app['name']} ({count}/{limit})"
        if count >= limit: btn_txt += " [Full]"
        buttons.append([InlineKeyboardButton(btn_txt, callback_data=f"sel_{app['id']}")])

    buttons.append([InlineKeyboardButton("❌ বাতিল", callback_data="cancel")])
    await query.edit_message_text("কোন অ্যাপে কাজ করবেন?", reply_markup=InlineKeyboardMarkup(buttons))
    return T_APP_SELECT

async def task_app_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel": 
        await cancel_conv(update, context)
        return ConversationHandler.END

    app_id = query.data.split("sel_")[1]
    config = get_config()
    app = next((a for a in config['monitored_apps'] if a['id'] == app_id), None)
    
    if not app:
        await query.edit_message_text("❌ অ্যাপ পাওয়া যায়নি।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 হোম", callback_data="back_home")]]))
        return ConversationHandler.END

    count = get_app_task_count(app_id)
    if count >= app.get('limit', 1000):
        await query.edit_message_text("⛔ এই অ্যাপের লিমিট শেষ!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 হোম", callback_data="back_home")]]))
        return ConversationHandler.END

    context.user_data['task_app_id'] = app_id
    context.user_data['task_app_name'] = app['name']
    
    await query.edit_message_text(
        f"📱 **App:** {app['name']}\n\n✍️ প্লে-স্টোরে যে **নাম (Name)** দিয়ে রিভিউ দিয়েছেন সেটি লিখুন:",
        parse_mode="Markdown"
    )
    return T_REVIEW_NAME

async def task_get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['task_rname'] = update.message.text.strip()
    await update.message.reply_text("📧 আপনার ইমেইল এড্রেস দিন:")
    return T_EMAIL

async def task_get_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['task_email'] = update.message.text.strip()
    await update.message.reply_text("📱 ডিভাইসের নাম/মডেল লিখুন:")
    return T_DEVICE

async def task_get_device(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['task_device'] = update.message.text.strip()
    await update.message.reply_text("🖼️ রিভিউ এর স্ক্রিনশট পাঠান (অথবা লিংক দিন):")
    return T_SS

async def task_submit_final(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    config = get_config()
    data = context.user_data
    
    ss_link = "No Image"
    
    if update.message.photo:
        wait_msg = await update.message.reply_text("📤 আপলোড হচ্ছে...")
        try:
            photo_file = await update.message.photo[-1].get_file()
            img_bytes = io.BytesIO()
            await photo_file.download_to_memory(img_bytes)
            img_bytes.seek(0)
            
            if IMGBB_API_KEY:
                # Async upload via run_in_executor
                def upload_img():
                    return requests.post(
                        "https://api.imgbb.com/1/upload",
                        data={'key': IMGBB_API_KEY},
                        files={'image': img_bytes}
                    ).json()
                
                loop = asyncio.get_running_loop()
                resp = await loop.run_in_executor(None, upload_img)
                
                if resp.get('success'):
                    ss_link = resp['data']['url']
                else:
                    await wait_msg.edit_text("❌ ইমেজ আপলোড ফেইল্ড। আবার চেষ্টা করুন।")
                    return T_SS
            else:
                ss_link = "API Key Missing"
            await wait_msg.delete()
        except Exception as e:
            logger.error(f"Image Upload Error: {e}")
            await wait_msg.edit_text("❌ আপলোড সমস্যা। আবার পাঠান।")
            return T_SS
    elif update.message.text and "http" in update.message.text:
        ss_link = update.message.text.strip()
    else:
        await update.message.reply_text("❌ দয়া করে ছবি পাঠান।")
        return T_SS

    # Save to Firestore
    task_data = {
        "user_id": str(user.id),
        "app_id": data['task_app_id'],
        "review_name": data['task_rname'],
        "email": data['task_email'],
        "device": data['task_device'],
        "screenshot": ss_link,
        "status": "pending",
        "submitted_at": datetime.now(),
        "price": config['task_price'],
        "check_count": 0
    }
    
    loop = asyncio.get_running_loop()
    ref = await loop.run_in_executor(None, lambda: db.collection('tasks').add(task_data))
    task_id = ref[1].id
    
    # Notify Admin
    log_msg = (
        f"📝 **New Task Submitted**\n"
        f"👤 User: `{user.id}`\n"
        f"📱 App: {data['task_app_name']}\n"
        f"✍️ Review Name: `{data['task_rname']}`\n"
        f"🖼 Proof: [Link]({ss_link})"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Approve", callback_data=f"t_apr_{task_id}_{user.id}"),
         InlineKeyboardButton("❌ Reject", callback_data=f"t_rej_{task_id}_{user.id}")]
    ])
    await send_log_message(context, log_msg, kb)
    
    await update.message.reply_text(
        "✅ **সফলভাবে জমা হয়েছে!**\nএডমিন চেক করার পর ব্যালেন্স যোগ হবে।", 
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 হোম", callback_data="back_home")]])
    )
    return ConversationHandler.END

async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update.callback_query:
            await update.callback_query.edit_message_text("❌ বাতিল করা হয়েছে।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 হোম", callback_data="back_home")]]))
        else:
            await update.message.reply_text("❌ বাতিল করা হয়েছে。", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 হোম", callback_data="back_home")]]))
    except: pass
    return ConversationHandler.END

# ==========================================
# 6. উইথড্র সিস্টেম
# ==========================================

async def withdraw_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user = get_user(query.from_user.id)
    config = get_config()
    
    if user['balance'] < config['min_withdraw']:
        await query.edit_message_text(
            f"❌ অপর্যাপ্ত ব্যালেন্স!\nসর্বনিম্ন উইথড্র: ৳{config['min_withdraw']:.2f}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 হোম", callback_data="back_home")]])
        )
        return ConversationHandler.END
    
    await query.edit_message_text(
        "💳 পেমেন্ট মেথড সিলেক্ট করুন:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Bkash", callback_data="wdm_bkash"), InlineKeyboardButton("Nagad", callback_data="wdm_nagad")],
            [InlineKeyboardButton("❌ বাতিল", callback_data="cancel")]
        ])
    )
    return WD_METHOD

async def withdraw_method(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel": return await cancel_conv(update, context)
    
    context.user_data['wd_method'] = "Bkash" if "bkash" in query.data else "Nagad"
    await query.edit_message_text(f"📝 আপনার {context.user_data['wd_method']} নাম্বারটি লিখুন:")
    return WD_NUMBER

async def withdraw_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['wd_number'] = update.message.text.strip()
    await update.message.reply_text("💰 টাকার পরিমাণ লিখুন (Example: 50):")
    return WD_AMOUNT

async def withdraw_final(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip())
        user_id = str(update.effective_user.id)
        config = get_config()
        user = get_user(user_id)
        
        if amount < config['min_withdraw']:
            await update.message.reply_text(f"❌ মিনিমাম উইথড্র ৳{config['min_withdraw']}")
            return ConversationHandler.END
            
        if amount > user['balance']:
            await update.message.reply_text("❌ ব্যালেন্স এর চেয়ে বেশি উইথড্র করা যাবে না।")
            return ConversationHandler.END
            
        # Deduct Balance
        db.collection('users').document(user_id).update({"balance": firestore.Increment(-amount)})
        
        # Create Request
        wd_ref = db.collection('withdrawals').add({
            "user_id": user_id,
            "amount": amount,
            "method": context.user_data['wd_method'],
            "number": context.user_data['wd_number'],
            "status": "pending",
            "time": datetime.now()
        })
        
        # Log
        msg = f"💸 **New Withdraw**\n👤 User: `{user_id}`\n💰 Amount: ৳{amount}\n📱 {context.user_data['wd_method']}: `{context.user_data['wd_number']}`"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Pay", callback_data=f"wd_apr_{wd_ref[1].id}_{user_id}"),
             InlineKeyboardButton("❌ Reject", callback_data=f"wd_rej_{wd_ref[1].id}_{user_id}")]
        ])
        await send_log_message(context, msg, kb)
        
        await update.message.reply_text("✅ উইথড্র রিকোয়েস্ট জমা হয়েছে!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 হোম", callback_data="back_home")]]))
        
    except ValueError:
        await update.message.reply_text("❌ ভুল ইনপুট। শুধু সংখ্যা দিন।")
    
    return ConversationHandler.END

# ==========================================
# 7. এডমিন প্যানেল ও অ্যাকশন হ্যান্ডলার
# ==========================================

async def admin_panel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Access Denied", show_alert=True)
        return
    
    kb = [
        [InlineKeyboardButton("👥 Users", callback_data="adm_users"), InlineKeyboardButton("⚙️ Settings", callback_data="adm_settings")],
        [InlineKeyboardButton("📱 Apps", callback_data="adm_apps"), InlineKeyboardButton("💰 Withdrawals", callback_data="adm_wd")],
        [InlineKeyboardButton("🔙 Exit", callback_data="back_home")]
    ]
    await query.edit_message_text("⚙️ **Admin Panel**", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

async def handle_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id): return

    data = query.data.split('_')
    action = data[1] # apr/rej
    item_id = data[2]
    user_id = data[3]
    
    if data[0] == "t": # Task Action
        task_ref = db.collection('tasks').document(item_id)
        task = task_ref.get().to_dict()
        
        if not task or task['status'] != 'pending':
            await query.edit_message_text("⚠️ Already Processed.")
            return

        if action == "apr":
            task_ref.update({"status": "approved", "approved_at": datetime.now()})
            db.collection('users').document(user_id).update({
                "balance": firestore.Increment(task['price']),
                "total_tasks": firestore.Increment(1)
            })
            
            # Referral Bonus
            u_doc = db.collection('users').document(user_id).get().to_dict()
            if u_doc.get('referrer'):
                ref_bonus = get_config().get('referral_bonus', 0)
                db.collection('users').document(u_doc['referrer']).update({"balance": firestore.Increment(ref_bonus)})

            await context.bot.send_message(user_id, f"🎉 আপনার কাজটি এপ্রুভ হয়েছে! ৳{task['price']} যোগ হয়েছে।")
            await query.edit_message_text(f"✅ Approved Task for User {user_id}")
            
        elif action == "rej":
            task_ref.update({"status": "rejected"})
            await context.bot.send_message(user_id, "❌ আপনার কাজটি রিজেক্ট করা হয়েছে।")
            await query.edit_message_text(f"❌ Rejected Task for User {user_id}")

    elif data[0] == "wd": # Withdrawal Action
        wd_ref = db.collection('withdrawals').document(item_id)
        wd = wd_ref.get().to_dict()
        
        if not wd or wd['status'] != 'pending':
            await query.edit_message_text("⚠️ Already Processed.")
            return

        if action == "apr":
            wd_ref.update({"status": "approved"})
            await context.bot.send_message(user_id, f"✅ আপনার ৳{wd['amount']} উইথড্র সফল হয়েছে!")
            await query.edit_message_text(f"✅ Paid ৳{wd['amount']} to {user_id}")
            
        elif action == "rej":
            wd_ref.update({"status": "rejected"})
            db.collection('users').document(user_id).update({"balance": firestore.Increment(wd['amount'])})
            await context.bot.send_message(user_id, f"❌ আপনার উইথড্র রিজেক্ট হয়েছে। টাকা ফেরত দেওয়া হয়েছে।")
            await query.edit_message_text(f"❌ Rejected WD for {user_id}")

# --- Apps Management ---
async def adm_apps_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    config = get_config()
    apps = config.get('monitored_apps', [])
    
    txt = "📱 **App List:**\n"
    for a in apps:
        txt += f"- {a['name']} (`{a['id']}`)\n"
        
    kb = [
        [InlineKeyboardButton("➕ Add App", callback_data="add_app"), InlineKeyboardButton("➖ Remove App", callback_data="rmv_app")],
        [InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]
    ]
    await query.edit_message_text(txt, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

# Add App Logic
async def add_app_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("📱 App Package ID দিন (Example: com.facebook.katana):")
    return ADD_APP_ID

async def add_app_id_in(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['new_app_id'] = update.message.text.strip()
    await update.message.reply_text("📝 অ্যাপের নাম দিন:")
    return ADD_APP_NAME

async def add_app_name_in(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['new_app_name'] = update.message.text.strip()
    await update.message.reply_text("🔢 টাস্ক লিমিট দিন (Example: 100):")
    return ADD_APP_LIMIT

async def add_app_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        limit = int(update.message.text.strip())
        new_app = {
            "id": context.user_data['new_app_id'],
            "name": context.user_data['new_app_name'],
            "limit": limit
        }
        config = get_config()
        apps = config.get('monitored_apps', [])
        apps.append(new_app)
        update_config({"monitored_apps": apps})
        
        await update.message.reply_text("✅ অ্যাপ যুক্ত হয়েছে!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]))
        return ConversationHandler.END
    except:
        await update.message.reply_text("❌ ভুল লিমিট। আবার চেষ্টা করুন।")
        return ADD_APP_LIMIT

# Remove App Logic
async def rmv_app_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = get_config()
    apps = config.get('monitored_apps', [])
    if not apps:
        await update.callback_query.answer("No Apps!", show_alert=True)
        return ConversationHandler.END
        
    btns = [[InlineKeyboardButton(a['name'], callback_data=f"rma_{i}")] for i, a in enumerate(apps)]
    btns.append([InlineKeyboardButton("Cancel", callback_data="cancel")])
    await update.callback_query.edit_message_text("Select App to Remove:", reply_markup=InlineKeyboardMarkup(btns))
    return REMOVE_APP_SELECT

async def rmv_app_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.data == "cancel": return await cancel_conv(update, context)
    
    idx = int(query.data.split("rma_")[1])
    config = get_config()
    apps = config.get('monitored_apps', [])
    
    if 0 <= idx < len(apps):
        del apps[idx]
        update_config({"monitored_apps": apps})
        await query.edit_message_text("✅ অ্যাপ রিমুভ করা হয়েছে!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]))
    
    return ConversationHandler.END

# ==========================================
# 8. অটোমেশন ও শিডিউলার (Thread Safe)
# ==========================================

def run_background_checks():
    """Background thread to check reviews"""
    logger.info("Automation Thread Started")
    
    while True:
        try:
            # 1. Check 24-hour Pending Tasks
            pending_tasks = db.collection('tasks').where('status', '==', 'pending').stream()
            config = get_config()
            
            for t in pending_tasks:
                task = t.to_dict()
                task_id = t.id
                
                # Check age (24 hours check)
                submitted = task['submitted_at'].replace(tzinfo=None)
                if datetime.now() - submitted > timedelta(hours=24):
                    
                    # Scrape Google Play
                    try:
                        reviews, _ = play_reviews(
                            task['app_id'],
                            count=50, # Check last 50 reviews
                            sort=Sort.NEWEST
                        )
                        
                        found = False
                        for r in reviews:
                            # Name match logic (Case insensitive, stripped)
                            if r['userName'].strip().lower() == task['review_name'].strip().lower():
                                # Check Rating
                                if r['score'] == 5:
                                    # Approve
                                    db.collection('tasks').document(task_id).update({
                                        "status": "approved",
                                        "approved_at": datetime.now(),
                                        "auto_approved": True
                                    })
                                    db.collection('users').document(task['user_id']).update({
                                        "balance": firestore.Increment(task['price']),
                                        "total_tasks": firestore.Increment(1)
                                    })
                                    
                                    # Send Request to Telegram API directly (avoid async loop conflict)
                                    requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={
                                        "chat_id": config.get('log_channel_id', OWNER_ID),
                                        "text": f"🤖 **Auto Approved**\nUser: {task['user_id']}\nApp: {task['app_id']}",
                                        "parse_mode": "Markdown"
                                    })
                                    found = True
                                    break
                        
                        if not found:
                            # Reject after 24 hours if not found
                            db.collection('tasks').document(task_id).update({
                                "status": "rejected",
                                "note": "Review not found after 24h"
                            })
                            
                    except Exception as e:
                        logger.error(f"Scrape Error for {task_id}: {e}")
            
            # Sleep to save resources
            time.sleep(3600) # Check every hour
            
        except Exception as e:
            logger.error(f"Background Loop Error: {e}")
            time.sleep(60)

# ==========================================
# 9. ফ্লাস্ক সার্ভার (Web + Keep Alive)
# ==========================================

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is Running! Status: 200 OK"

@app.route('/keep_alive', methods=['GET'])
def keep_alive():
    return jsonify({"status": "alive", "timestamp": time.time()})

def run_flask():
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)

# ==========================================
# 10. মেইন এক্সিকিউশন
# ==========================================

def main():
    # 1. Start Flask in separate thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # 2. Start Automation in separate thread
    auto_thread = threading.Thread(target=run_background_checks, daemon=True)
    auto_thread.start()

    # 3. Setup Bot
    application = ApplicationBuilder().token(TOKEN).build()

    # Handlers
    application.add_handler(CommandHandler("start", start))
    
    # Task Conversation
    task_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(task_start, pattern="^submit_task$")],
        states={
            T_APP_SELECT: [CallbackQueryHandler(task_app_select, pattern="^sel_")],
            T_REVIEW_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, task_get_name)],
            T_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, task_get_email)],
            T_DEVICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, task_get_device)],
            T_SS: [MessageHandler((filters.PHOTO | filters.TEXT) & ~filters.COMMAND, task_submit_final)]
        },
        fallbacks=[CallbackQueryHandler(cancel_conv, pattern="^cancel$|back_home")]
    )
    application.add_handler(task_conv)

    # Withdraw Conversation
    wd_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(withdraw_start, pattern="^start_withdraw$")],
        states={
            WD_METHOD: [CallbackQueryHandler(withdraw_method, pattern="^wdm_")],
            WD_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_number)],
            WD_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_final)]
        },
        fallbacks=[CallbackQueryHandler(cancel_conv, pattern="^cancel$|back_home")]
    )
    application.add_handler(wd_conv)

    # App Management Conversation
    app_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_app_start, pattern="^add_app$")],
        states={
            ADD_APP_ID: [MessageHandler(filters.TEXT, add_app_id_in)],
            ADD_APP_NAME: [MessageHandler(filters.TEXT, add_app_name_in)],
            ADD_APP_LIMIT: [MessageHandler(filters.TEXT, add_app_save)]
        },
        fallbacks=[CallbackQueryHandler(cancel_conv, pattern="^cancel$|back_home")]
    )
    application.add_handler(app_conv)
    
    # Remove App Conversation
    rm_app_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(rmv_app_start, pattern="^rmv_app$")],
        states={
            REMOVE_APP_SELECT: [CallbackQueryHandler(rmv_app_confirm, pattern="^rma_")]
        },
        fallbacks=[CallbackQueryHandler(cancel_conv, pattern="^cancel$|back_home")]
    )
    application.add_handler(rm_app_conv)

    # General Callbacks
    application.add_handler(CallbackQueryHandler(admin_panel_handler, pattern="^admin_panel$"))
    application.add_handler(CallbackQueryHandler(adm_apps_menu, pattern="^adm_apps$"))
    application.add_handler(CallbackQueryHandler(handle_action, pattern="^(t_|wd_)"))
    application.add_handler(CallbackQueryHandler(common_callback))

    print("🚀 Bot Started Successfully on Render/VPS!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    # Fix for Asyncio Loop issues on some platforms
    import nest_asyncio
    nest_asyncio.apply()
    main()
