import os
import json
import logging
import threading
import time
import asyncio
import csv
import io
import random
import string
from datetime import datetime, timedelta
import requests
import firebase_admin
from firebase_admin import credentials, firestore
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler,
    MessageHandler, filters, ConversationHandler
)
from google_play_scraper import Sort, reviews as play_reviews
from flask import Flask

# --- AI Import Safeguard ---
try:
    import google.generativeai as genai
    AI_AVAILABLE = True
except Exception as e:
    print(f"⚠️ Google AI Library Error (Skipping AI features): {e}")
    AI_AVAILABLE = False
    genai = None

# ==========================================
# 1. কনফিগারেশন এবং সেটআপ
# ==========================================

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

# Gemini AI সেটআপ
model = None
if AI_AVAILABLE and GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel('gemini-1.5-flash')
    except Exception as e:
        logger.error(f"Gemini AI Config Error: {e}")

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
        print(f"❌ Firebase Connection Failed: {e}")

db = firestore.client()

# ==========================================
# 2. গ্লোবাল কনফিগারেশন ও স্টেট
# ==========================================

DEFAULT_CONFIG = {
    "task_price": 20.0,
    "referral_bonus": 5.0,
    "min_withdraw": 50.0,
    "monitored_apps": [],
    "log_channel_id": "",
    "work_start_time": "15:30",
    "work_end_time": "23:00",
    "rules_text": "⚠️ কাজের নিয়ম: ভিডিওতে দেখানো হয়েছে ভিডিওটি দেখে নিন।",
    "schedule_text": "⏰ কাজের সময়: বিকেল 03:30 PM To 11:00 PM।",
    "buttons": {
        "submit": {"text": "💰 কাজ জমা দিন", "show": True},
        "profile": {"text": "👤 প্রোফাইল", "show": True},
        "withdraw": {"text": "📤 উইথড্র", "show": True},
        "refer": {"text": "📢 রেফার", "show": True},
        "schedule": {"text": "📅 সময়সূচী", "show": True}
    },
    "custom_buttons": [] 
}

# Conversation States
(
    T_APP_SELECT, T_REVIEW_NAME, T_EMAIL, T_DEVICE, T_SS,           
    ADD_APP_ID, ADD_APP_NAME, ADD_APP_LIMIT,                        
    WD_METHOD, WD_NUMBER, WD_AMOUNT,                                
    REMOVE_APP_SELECT,                                              
    ADMIN_USER_SEARCH, ADMIN_USER_ACTION, ADMIN_USER_AMOUNT,        
    ADMIN_EDIT_TEXT_KEY, ADMIN_EDIT_TEXT_VAL,                       
    ADMIN_EDIT_BTN_KEY, ADMIN_EDIT_BTN_NAME,                        
    ADMIN_ADD_BTN_NAME, ADMIN_ADD_BTN_LINK,                         
    ADMIN_SET_LOG_CHANNEL,                                          
    ADMIN_ADD_ADMIN_ID, ADMIN_RMV_ADMIN_ID,                         
    ADMIN_SET_START_TIME, ADMIN_SET_END_TIME,                       
    EDIT_APP_SELECT, EDIT_APP_LIMIT_VAL,                            
    REMOVE_CUS_BTN                                                  
) = range(29)

# ==========================================
# 3. হেল্পার ফাংশন
# ==========================================

def get_config():
    try:
        ref = db.collection('settings').document('main_config')
        doc = ref.get()
        if doc.exists:
            data = doc.to_dict()
            for key, val in DEFAULT_CONFIG.items():
                if key not in data:
                    data[key] = val
            return data
        else:
            ref.set(DEFAULT_CONFIG)
            return DEFAULT_CONFIG
    except:
        return DEFAULT_CONFIG

def update_config(data):
    try:
        db.collection('settings').document('main_config').set(data, merge=True)
    except Exception as e:
        logger.error(f"Config Update Error: {e}")

def get_bd_time():
    return datetime.utcnow() + timedelta(hours=6)

def is_working_hour():
    config = get_config()
    start_str = config.get("work_start_time", "15:30")
    end_str = config.get("work_end_time", "23:00")
    try:
        now = get_bd_time().time()
        start = datetime.strptime(start_str, "%H:%M").time()
        end = datetime.strptime(end_str, "%H:%M").time()
        if start < end: return start <= now <= end
        else: return now >= start or now <= end
    except: return True 

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
    if not get_user(user_id):
        try:
            user_data = {
                "id": str(user_id),
                "name": first_name,
                "balance": 0.0,
                "total_tasks": 0,
                "joined_at": datetime.now(),
                "referrer": referrer_id if referrer_id and referrer_id.isdigit() and str(referrer_id) != str(user_id) else None,
                "is_blocked": False,
                "is_admin": str(user_id) == str(OWNER_ID),
                "web_password": "",
                "device_id": ""
            }
            db.collection('users').document(str(user_id)).set(user_data)
        except: pass

async def send_log_message(context, text, reply_markup=None):
    config = get_config()
    chat_id = config.get('log_channel_id')
    target_id = chat_id if chat_id else OWNER_ID
    if target_id:
        try:
            await context.bot.send_message(chat_id=target_id, text=text, reply_markup=reply_markup, parse_mode="Markdown")
        except: pass

def get_ai_summary(text, rating):
    if not model: return "N/A"
    try:
        prompt = f"Review: '{text}' ({rating}/5). Summarize sentiment in Bangla (max 10 words). Start with 'মুড:'"
        response = model.generate_content(prompt)
        return response.text.strip()
    except: return "N/A"

def get_app_task_count(app_id):
    try:
        pending = db.collection('tasks').where('app_id', '==', app_id).where('status', '==', 'pending').stream()
        approved = db.collection('tasks').where('app_id', '==', app_id).where('status', '==', 'approved').stream()
        return len(list(pending)) + len(list(approved))
    except: return 0

# ==========================================
# 4. ইউজার সাইড ফাংশন
# ==========================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    referrer = args[0] if args and args[0].isdigit() else None
    create_user(user.id, user.first_name, referrer)
    
    db_user = get_user(user.id)
    if db_user and db_user.get('is_blocked'):
        await update.message.reply_text("⛔ আপনাকে ব্লক করা হয়েছে।")
        return

    config = get_config()
    btns_conf = config.get('buttons', DEFAULT_CONFIG['buttons'])
    
    welcome_msg = (
        f"আসসালামু আলাইকুম, {user.first_name}! 🌙\n\n"
        f"🗒 **কাজের নিয়মাবলী:**\n{config.get('rules_text', '')}\n\n"
        "🔑 **অ্যাপে লগইন:** অ্যাপে লগইন করার জন্য `/login` কমান্ডটি ব্যবহার করুন।"
    )

    keyboard = []
    row1 = []
    if btns_conf['submit']['show']: row1.append(InlineKeyboardButton(btns_conf['submit']['text'], callback_data="submit_task"))
    if btns_conf['profile']['show']: row1.append(InlineKeyboardButton(btns_conf['profile']['text'], callback_data="my_profile"))
    if row1: keyboard.append(row1)
    
    row2 = []
    if btns_conf['withdraw']['show']: row2.append(InlineKeyboardButton(btns_conf['withdraw']['text'], callback_data="start_withdraw"))
    if btns_conf['refer']['show']: row2.append(InlineKeyboardButton(btns_conf['refer']['text'], callback_data="refer_friend"))
    if row2: keyboard.append(row2)

    row3 = []
    if btns_conf.get('schedule', {}).get('show', True): row3.append(InlineKeyboardButton(btns_conf.get('schedule', {}).get('text', "📅 সময়সূচী"), callback_data="show_schedule"))
    row3.append(InlineKeyboardButton("🔄 রিফ্রেশ", callback_data="back_home"))
    if row3: keyboard.append(row3)

    custom_btns = config.get('custom_buttons', [])
    for btn in custom_btns:
        if btn.get('text') and btn.get('url'):
            keyboard.append([InlineKeyboardButton(btn['text'], url=btn['url'])])

    if is_admin(user.id):
        keyboard.append([InlineKeyboardButton("⚙️ এডমিন প্যানেল", callback_data="admin_panel")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.callback_query:
        try: await update.callback_query.edit_message_text(welcome_msg, reply_markup=reply_markup, parse_mode="Markdown")
        except BadRequest: pass
    else:
        await update.message.reply_text(welcome_msg, reply_markup=reply_markup, parse_mode="Markdown")

# --- SECURE LOGIN HANDLER ---
async def generate_login_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    create_user(user_id, update.effective_user.first_name)
    
    # Generate 6 digit random code
    code = ''.join(random.choices(string.digits, k=6))
    
    try:
        db.collection('users').document(user_id).update({
            "web_password": code,
            "pass_generated_at": datetime.now()
        })
        
        msg = (
            f"🔐 **Web/App Login Code**\n\n"
            f"আপনার লগইন কোড: `{code}`\n\n"
            f"⚠️ অ্যাপে ইউজার আইডি `{user_id}` এবং এই কোডটি দিন। কোডটি কাউকে শেয়ার করবেন না।"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Login Gen Error: {e}")
        await update.message.reply_text("❌ টেকনিক্যাল সমস্যা হয়েছে। আবার চেষ্টা করুন।")

async def common_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    try:
        if query.data == "back_home":
            await start(update, context)
            
        elif query.data == "my_profile":
            user = get_user(query.from_user.id)
            if user:
                msg = f"👤 **প্রোফাইল**\n\n🆔 ID: `{user['id']}`\n💰 ব্যালেন্স: ৳{user['balance']:.2f}\n✅ সম্পন্ন টাস্ক: {user['total_tasks']}"
            else:
                msg = "প্রোফাইল পাওয়া যায়নি।"
            await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="back_home")]]))
            
        elif query.data == "refer_friend":
            config = get_config()
            link = f"https://t.me/{context.bot.username}?start={query.from_user.id}"
            await query.edit_message_text(f"📢 **রেফার লিংক:**\n`{link}`\n\nবোনাস: ৳{config['referral_bonus']}", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="back_home")]]))
        
        elif query.data == "show_schedule":
            config = get_config()
            s_time = datetime.strptime(config.get('work_start_time', '15:30'), "%H:%M").strftime("%I:%M %p")
            e_time = datetime.strptime(config.get('work_end_time', '23:00'), "%H:%M").strftime("%I:%M %p")
            msg = f"📅 **সময়সূচী:**\n{config.get('schedule_text', '')}\n\n🕒 শুরু: `{s_time}`\nশেষ: `{e_time}`"
            await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="back_home")]]))
    except BadRequest: pass

# --- Withdrawal System ---
async def withdraw_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = get_user(query.from_user.id)
    config = get_config()
    
    if user['balance'] < config['min_withdraw']:
        await query.edit_message_text(f"❌ সর্বনিম্ন উইথড্র অ্যামাউন্ট: ৳{config['min_withdraw']:.2f}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="back_home")]]))
        return ConversationHandler.END
        
    await query.edit_message_text("পেমেন্ট মেথড:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Bkash", callback_data="m_bkash"), InlineKeyboardButton("Nagad", callback_data="m_nagad")],[InlineKeyboardButton("❌ বাতিল", callback_data="cancel")]]))
    return WD_METHOD

async def withdraw_method(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel": return await cancel_conv(update, context)
    context.user_data['wd_method'] = "Bkash" if "bkash" in query.data else "Nagad"
    await query.edit_message_text(f"আপনার {context.user_data['wd_method']} নাম্বারটি দিন:")
    return WD_NUMBER

async def withdraw_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['wd_number'] = update.message.text
    await update.message.reply_text("কত টাকা উইথড্র করতে চান?")
    return WD_AMOUNT

async def withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    config = get_config()
    try:
        amount = float(update.message.text)
        if amount < config['min_withdraw'] or amount > user['balance']:
             await update.message.reply_text(f"❌ ইনভ্যালিড অ্যামাউন্ট।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 হোম", callback_data="back_home")]]))
             return ConversationHandler.END

        db.collection('users').document(user_id).update({"balance": firestore.Increment(-amount)})
        wd_ref = db.collection('withdrawals').add({
            "user_id": user_id, "user_name": update.effective_user.first_name,
            "amount": amount, "method": context.user_data['wd_method'],
            "number": context.user_data['wd_number'], "status": "pending", "time": datetime.now()
        })
        
        admin_msg = f"💸 **Withdraw Request**\nUser: `{user_id}`\nAmount: ৳{amount:.2f}\nMethod: {context.user_data['wd_method']} ({context.user_data['wd_number']})"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Approve", callback_data=f"wd_apr_{wd_ref[1].id}_{user_id}"), InlineKeyboardButton("❌ Reject", callback_data=f"wd_rej_{wd_ref[1].id}_{user_id}")]])
        await send_log_message(context, admin_msg, kb)
        await update.message.reply_text("✅ রিকোয়েস্ট সফল হয়েছে!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 হোম", callback_data="back_home")]]))
    except: await update.message.reply_text("❌ সমস্যা হয়েছে।")
    return ConversationHandler.END

async def handle_withdrawal_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id): return
    data = query.data.split('_')
    action, wd_id, user_id = data[1], data[2], data[3]
    
    wd_doc = db.collection('withdrawals').document(wd_id).get()
    if not wd_doc.exists or wd_doc.to_dict()['status'] != 'pending':
        await query.answer("Already processed", show_alert=True)
        return

    amount = wd_doc.to_dict()['amount']
    if action == "apr":
        db.collection('withdrawals').document(wd_id).update({"status": "approved"})
        await query.edit_message_text(f"✅ Approved for `{user_id}` (৳{amount:.2f})", parse_mode="Markdown")
        await context.bot.send_message(chat_id=user_id, text=f"✅ আপনার ৳{amount:.2f} উইথড্র সফল হয়েছে!")
    elif action == "rej":
        db.collection('withdrawals').document(wd_id).update({"status": "rejected"})
        db.collection('users').document(user_id).update({"balance": firestore.Increment(amount)})
        await query.edit_message_text(f"❌ Rejected for `{user_id}`", parse_mode="Markdown")
        await context.bot.send_message(chat_id=user_id, text=f"❌ আপনার ৳{amount:.2f} উইথড্র বাতিল হয়েছে।")

# --- Task Submission System ---
async def start_task_submission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    config = get_config()
    
    if not is_working_hour():
        await query.edit_message_text("⛔ এখন কাজের সময় নয়!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 হোম", callback_data="back_home")]]))
        return ConversationHandler.END

    apps = config.get('monitored_apps', [])
    if not apps:
        await query.edit_message_text("❌ কোনো কাজ নেই।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="back_home")]]))
        return ConversationHandler.END
        
    buttons = []
    for app in apps:
        limit = app.get('limit', 1000)
        count = get_app_task_count(app['id'])
        btn_text = f"📱 {app['name']} ({count}/{limit})"
        if count >= limit: btn_text += " (Full)"
        buttons.append([InlineKeyboardButton(btn_text, callback_data=f"sel_{app['id']}")])
    buttons.append([InlineKeyboardButton("❌ বাতিল", callback_data="cancel")])
    
    await query.edit_message_text("অ্যাপ সিলেক্ট করুন:", reply_markup=InlineKeyboardMarkup(buttons))
    return T_APP_SELECT

async def app_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel": return await cancel_conv(update, context)
    app_id = query.data.split("sel_")[1]
    context.user_data['tid'] = app_id
    await query.edit_message_text("✍️ প্লে-স্টোর রিভিউ নেম:")
    return T_REVIEW_NAME

async def get_review_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['rname'] = update.message.text.strip()
    await update.message.reply_text("আপনার ইমেইল এড্রেস:")
    return T_EMAIL

async def get_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['email'] = update.message.text
    await update.message.reply_text("ডিভাইস নাম:")
    return T_DEVICE

async def get_device(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['dev'] = update.message.text
    await update.message.reply_text("স্ক্রিনশট দিন:")
    return T_SS

async def save_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data
    config = get_config()
    user = update.effective_user
    screenshot_link = ""
    
    if update.message.photo:
        wait_msg = await update.message.reply_text("📤 আপলোড হচ্ছে...")
        try:
            photo = await update.message.photo[-1].get_file()
            img_bytes = io.BytesIO()
            await photo.download_to_memory(img_bytes)
            img_bytes.seek(0)
            if IMGBB_API_KEY:
                files = {'image': img_bytes}
                payload = {'key': IMGBB_API_KEY}
                response = requests.post("https://api.imgbb.com/1/upload", data=payload, files=files)
                if response.json().get('success'): screenshot_link = response.json()['data']['url']
            await wait_msg.delete()
        except: pass
    elif update.message.text: screenshot_link = update.message.text.strip()
    
    if not screenshot_link:
        await update.message.reply_text("❌ স্ক্রিনশট দিতে হবে।")
        return T_SS

    task_ref = db.collection('tasks').add({
        "user_id": str(user.id), "app_id": data['tid'], "review_name": data['rname'],
        "email": data['email'], "device": data['dev'], "screenshot": screenshot_link,
        "status": "pending", "submitted_at": datetime.now(), "price": config['task_price']
    })
    
    log_msg = f"📝 **New Task**\nUser: `{user.id}`\nName: {data['rname']}\nApp: {data['tid']}\n[Proof]({screenshot_link})"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Approve", callback_data=f"t_apr_{task_ref[1].id}_{user.id}"), InlineKeyboardButton("❌ Reject", callback_data=f"t_rej_{task_ref[1].id}_{user.id}")]])
    await send_log_message(context, log_msg, kb)
    await update.message.reply_text("✅ কাজ জমা হয়েছে!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 হোম", callback_data="back_home")]]))
    return ConversationHandler.END

async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: await update.callback_query.edit_message_text("❌ বাতিল।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="back_home")]]))
    except: pass
    return ConversationHandler.END

async def handle_task_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id): return
    data = query.data.split('_')
    action, task_id, user_id = data[1], data[2], data[3]
    
    task_ref = db.collection('tasks').document(task_id)
    t_data = task_ref.get().to_dict()
    if not t_data or t_data['status'] != 'pending':
        await query.answer("Already processed", show_alert=True)
        return

    price = t_data.get('price', 0)
    if action == "apr":
        task_ref.update({"status": "approved", "approved_at": datetime.now()})
        db.collection('users').document(str(user_id)).update({"balance": firestore.Increment(price), "total_tasks": firestore.Increment(1)})
        await query.edit_message_text(f"✅ Task Approved (৳{price:.2f})", parse_mode="Markdown")
        await context.bot.send_message(chat_id=user_id, text=f"🎉 আপনার কাজটি এপ্রুভ হয়েছে! ৳{price:.2f} যোগ হয়েছে।")
    elif action == "rej":
        task_ref.update({"status": "rejected"})
        await query.edit_message_text(f"❌ Task Rejected", parse_mode="Markdown")
        await context.bot.send_message(chat_id=user_id, text="❌ আপনার কাজটি রিজেক্ট করা হয়েছে।")

# ==========================================
# 5. অটোমেশন ও গ্রুপ নোটিফিকেশন
# ==========================================

def run_automation():
    while True:
        try:
            config = get_config()
            apps = config.get('monitored_apps', [])
            log_id = config.get('log_channel_id', OWNER_ID)
            for app in apps:
                try:
                    reviews, _ = play_reviews(app['id'], count=10, sort=Sort.NEWEST)
                    for r in reviews:
                        rid = r['reviewId']
                        if r['at'] < datetime.now() - timedelta(hours=48): continue
                        if not db.collection('seen_reviews').document(rid).get().exists:
                            ai_txt = get_ai_summary(r['content'], r['score'])
                            msg = f"🔔 **Review Found**\nApp: `{app['name']}`\nUser: **{r['userName']}**\n⭐ {r['score']}/5\n💬 {r['content']}\nMood: {ai_txt}"
                            send_telegram_message(msg, chat_id=log_id)
                            db.collection('seen_reviews').document(rid).set({"t": datetime.now()})

                            if r['score'] == 5:
                                p_tasks = db.collection('tasks').where('app_id', '==', app['id']).where('status', '==', 'pending').stream()
                                for t in p_tasks:
                                    td = t.to_dict()
                                    if td['review_name'].lower().strip() == r['userName'].lower().strip():
                                        price = td.get('price', 0)
                                        db.collection('tasks').document(t.id).update({"status": "approved", "approved_at": datetime.now()})
                                        db.collection('users').document(str(td['user_id'])).update({"balance": firestore.Increment(price), "total_tasks": firestore.Increment(1)})
                                        send_telegram_message(f"🤖 **Auto Approved!**\nUser: `{td['user_id']}`", chat_id=log_id)
                                        send_telegram_message(f"🎉 অটোমেটিক এপ্রুভ হয়েছে! ৳{price:.2f} যোগ হয়েছে।", chat_id=td['user_id'])
                                        break
                except: pass
        except: pass
        time.sleep(300)

def send_telegram_message(message, chat_id=None):
    if not chat_id: return
    try: requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}, timeout=10)
    except: pass

# ==========================================
# 6. এডমিন প্যানেল
# ==========================================

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id): return
    kb = [
        [InlineKeyboardButton("👥 Users", callback_data="adm_users"), InlineKeyboardButton("💰 Finance", callback_data="adm_finance")],
        [InlineKeyboardButton("📱 Apps", callback_data="adm_apps"), InlineKeyboardButton("👮 Admins", callback_data="adm_admins")],
        [InlineKeyboardButton("🎨 Settings", callback_data="adm_content"), InlineKeyboardButton("📢 Logs", callback_data="adm_log")],
        [InlineKeyboardButton("📊 Reports", callback_data="adm_reports")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_home")]
    ]
    await query.edit_message_text("⚙️ **Super Admin Panel**", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

# (বাকি এডমিন ফাংশনগুলো আগের মতই থাকবে, জায়গার অভাবে সংক্ষিপ্ত করা হলো। আগের লজিক কাজ করবে)
# ... [এখানে আপনার আগের কোডের এডমিন পার্ট অপরিবর্তিত থাকবে] ...
# শুধুমাত্র ইম্পোর্ট এবং হ্যান্ডলারের পরিবর্তনগুলো নিচে দেওয়া হলো

# ==========================================
# 7. মেইন রানার
# ==========================================

app = Flask(__name__)
@app.route('/')
def home(): return "Bot is Alive & Secure!"

def run_flask(): app.run(host='0.0.0.0', port=PORT)

def main():
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=run_automation, daemon=True).start()

    application = ApplicationBuilder().token(TOKEN).build()
    
    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("login", generate_login_pass)) # NEW: Login Command
    
    # Callbacks
    application.add_handler(CallbackQueryHandler(admin_panel, pattern="^admin_panel$"))
    application.add_handler(CallbackQueryHandler(common_callback, pattern="^(my_profile|refer_friend|back_home|show_schedule)$"))
    application.add_handler(CallbackQueryHandler(handle_withdrawal_action, pattern="^wd_(apr|rej)_"))
    application.add_handler(CallbackQueryHandler(handle_task_action, pattern="^t_(apr|rej)_"))

    # Converstations (Task, Withdraw, Admin etc - Same as before)
    # ... [Conversations Handlers আগের মতই রাখুন] ...
    # এখানে শর্টকাটের জন্য আমি শুধু Task Handler দিচ্ছি, আপনি আগেরগুলো অ্যাড করে নেবেন
    
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(start_task_submission, pattern="^submit_task$")],
        states={
            T_APP_SELECT: [CallbackQueryHandler(app_selected, pattern="^sel_")],
            T_REVIEW_NAME: [MessageHandler(filters.TEXT, get_review_name)],
            T_EMAIL: [MessageHandler(filters.TEXT, get_email)],
            T_DEVICE: [MessageHandler(filters.TEXT, get_device)],
            T_SS: [MessageHandler((filters.TEXT | filters.PHOTO), save_task)]
        },
        fallbacks=[CallbackQueryHandler(cancel_conv, pattern="^cancel")]
    ))
    
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(withdraw_start, pattern="^start_withdraw$")],
        states={
            WD_METHOD: [CallbackQueryHandler(withdraw_method, pattern="^m_(bkash|nagad)$|^cancel$")],
            WD_NUMBER: [MessageHandler(filters.TEXT, withdraw_number)],
            WD_AMOUNT: [MessageHandler(filters.TEXT, withdraw_amount)]
        },
        fallbacks=[CallbackQueryHandler(cancel_conv, pattern="^cancel")]
    ))

    print("🚀 Bot Started with Secure Login...")
    application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
