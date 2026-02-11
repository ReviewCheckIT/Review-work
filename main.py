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

# Gemini AI সেটআপ (অপশনাল)
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
        # চেক করা হচ্ছে এটি JSON ফাইল নাকি সরাসরি JSON স্ট্রিং
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

# Conversation States (Bot side interactions)
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
            # ডিফল্ট ভ্যালু মার্জ করা হচ্ছে যাতে এরর না আসে
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
    """Returns current time in Bangladesh (UTC+6)"""
    return datetime.utcnow() + timedelta(hours=6)

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
        else: # মধ্যরাত ক্রস করলে
            return now >= start or now <= end
    except Exception as e:
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
                "web_password": "",  # For Web Login OTP
                "device_id": ""      # For Device Lock
            }
            db.collection('users').document(str(user_id)).set(user_data)
            
            # রেফার বোনাস সিস্টেম (অপশনাল, শুধু নতুন ইউজারদের জন্য)
            if referrer_id and referrer_id.isdigit() and str(referrer_id) != str(user_id):
                 config = get_config()
                 bonus = config.get('referral_bonus', 0.0)
                 if bonus > 0:
                     db.collection('users').document(str(referrer_id)).update({
                         "balance": firestore.Increment(bonus)
                     })
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
        count = len(list(pending)) + len(list(approved))
        return count
    except:
        return 0

# ==========================================
# 4. ইউজার সাইড ফাংশন (Bot Interactions)
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
        "🔑 **অ্যাপে লগইন:** অ্যাপ বা ওয়েবসাইটে লগইন করার জন্য `/login` কমান্ডটি ব্যবহার করুন।"
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
        try:
            await update.callback_query.edit_message_text(welcome_msg, reply_markup=reply_markup, parse_mode="Markdown")
        except BadRequest:
            pass
    else:
        await update.message.reply_text(welcome_msg, reply_markup=reply_markup, parse_mode="Markdown")

# --- SECURE LOGIN HANDLER (WEB APP OTP) ---
async def generate_login_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    create_user(user_id, update.effective_user.first_name)
    
    # 6 ডিজিটের র‍্যান্ডম কোড তৈরি (OTP)
    code = ''.join(random.choices(string.digits, k=6))
    
    try:
        # ডাটাবেসে কোড সেভ করা হচ্ছে
        db.collection('users').document(user_id).update({
            "web_password": code,
            "pass_generated_at": datetime.now()
        })
        
        msg = (
            f"🔐 **Web/App Login Code**\n\n"
            f"আপনার লগইন কোড: `{code}`\n\n"
            f"⚠️ অ্যাপে ইউজার আইডি `{user_id}` এবং এই কোডটি দিয়ে লগইন করুন। কোডটি কাউকে শেয়ার করবেন না।"
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
                msg = "👤 **প্রোফাইল**\n\nডেটা লোড করা যায়নি। আবার /start দিন।"
            await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="back_home")]]))
            
        elif query.data == "refer_friend":
            config = get_config()
            link = f"https://t.me/{context.bot.username}?start={query.from_user.id}"
            await query.edit_message_text(f"📢 **রেফার লিংক:**\n`{link}`\n\nপ্রতি রেফারে বোনাস: ৳{config['referral_bonus']}", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="back_home")]]))
        
        elif query.data == "show_schedule":
            config = get_config()
            s_time = datetime.strptime(config.get('work_start_time', '15:30'), "%H:%M").strftime("%I:%M %p")
            e_time = datetime.strptime(config.get('work_end_time', '23:00'), "%H:%M").strftime("%I:%M %p")
            msg = f"📅 **সময়সূচী:**\n{config.get('schedule_text', '')}\n\n🕒 শুরু: `{s_time}`\nশেষ: `{e_time}`"
            await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="back_home")]]))
    except BadRequest: pass

# --- Withdrawal System (Bot Side) ---

async def withdraw_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user = get_user(query.from_user.id)
    config = get_config()
    
    if user['balance'] < config['min_withdraw']:
        await query.edit_message_text(f"❌ উইথড্র বাতিল। সর্বনিম্ন উইথড্র অ্যামাউন্ট: ৳{config['min_withdraw']:.2f}", 
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="back_home")]]))
        return ConversationHandler.END
        
    await query.edit_message_text("পেমেন্ট মেথড সিলেক্ট করুন:", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("Bkash", callback_data="m_bkash"), InlineKeyboardButton("Nagad", callback_data="m_nagad")],
        [InlineKeyboardButton("❌ বাতিল", callback_data="cancel")]
    ]))
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
    await update.message.reply_text("কত টাকা উইথড্র করতে চান? (সংখ্যা লিখুন)")
    return WD_AMOUNT

async def withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    config = get_config()
    
    try:
        amount = float(update.message.text)
        
        if amount < config['min_withdraw']:
             await update.message.reply_text(f"❌ সর্বনিম্ন উইথড্র ৳{config['min_withdraw']:.2f}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 হোম", callback_data="back_home")]]))
             return ConversationHandler.END

        if amount > user['balance']:
            await update.message.reply_text("❌ আপনার একাউন্টে পর্যাপ্ত ব্যালেন্স নেই।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 হোম", callback_data="back_home")]]))
            return ConversationHandler.END

        # ব্যালেন্স কাটা এবং রিকোয়েস্ট তৈরি
        db.collection('users').document(user_id).update({"balance": firestore.Increment(-amount)})
        
        wd_ref = db.collection('withdrawals').add({
            "user_id": user_id,
            "user_name": update.effective_user.first_name,
            "amount": amount,
            "method": context.user_data['wd_method'],
            "number": context.user_data['wd_number'],
            "status": "pending",
            "time": datetime.now()
        })
        
        # লগ গ্রুপে পাঠানো
        admin_msg = (
            f"💸 **New Withdrawal Request**\n"
            f"👤 User: `{user_id}`\n"
            f"💰 Amount: ৳{amount:.2f}\n"
            f"📱 Method: {context.user_data['wd_method']} ({context.user_data['wd_number']})"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Approve", callback_data=f"wd_apr_{wd_ref[1].id}_{user_id}"), 
             InlineKeyboardButton("❌ Reject", callback_data=f"wd_rej_{wd_ref[1].id}_{user_id}")]
        ])
        
        await send_log_message(context, admin_msg, kb)
        await update.message.reply_text("✅ উইথড্র রিকোয়েস্ট সফল হয়েছে! এডমিন চেক করে পেমেন্ট করবে।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 হোম", callback_data="back_home")]]))
        
    except ValueError:
        await update.message.reply_text("❌ ভুল ইনপুট। শুধু সংখ্যা ব্যবহার করুন।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 হোম", callback_data="back_home")]]))
    except Exception as e:
        logger.error(f"Withdraw Error: {e}")
        await update.message.reply_text("❌ সমস্যা হয়েছে। পরে চেষ্টা করুন।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 হোম", callback_data="back_home")]]))
        
    return ConversationHandler.END

async def handle_withdrawal_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⚠️ Only Admins can do this!", show_alert=True)
        return
    
    data = query.data.split('_')
    action = data[1]
    wd_id = data[2]
    user_id = data[3]
    
    wd_doc = db.collection('withdrawals').document(wd_id).get()
    if not wd_doc.exists:
        await query.answer("Withdrawal request not found.", show_alert=True)
        return
    
    wd_data = wd_doc.to_dict()
    if wd_data['status'] != 'pending':
        await query.answer(f"Already processed ({wd_data['status']})", show_alert=True)
        return

    amount = wd_data['amount']

    if action == "apr":
        db.collection('withdrawals').document(wd_id).update({"status": "approved", "processed_by": query.from_user.id})
        await query.edit_message_text(f"✅ Approved Withdrawal for `{user_id}` (৳{amount:.2f})", parse_mode="Markdown")
        await context.bot.send_message(chat_id=user_id, text=f"✅ আপনার ৳{amount:.2f} উইথড্র সফল হয়েছে!")
        
    elif action == "rej":
        db.collection('withdrawals').document(wd_id).update({"status": "rejected", "processed_by": query.from_user.id})
        # টাকা ফেরত দেওয়া
        db.collection('users').document(user_id).update({"balance": firestore.Increment(amount)})
        await query.edit_message_text(f"❌ Rejected & Refunded for `{user_id}` (৳{amount:.2f})", parse_mode="Markdown")
        await context.bot.send_message(chat_id=user_id, text=f"❌ আপনার ৳{amount:.2f} উইথড্র বাতিল হয়েছে এবং ব্যালেন্স ফেরত দেওয়া হয়েছে।")

# --- Task Submission System (Bot Side) ---

async def start_task_submission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    config = get_config()
    
    # সময় চেক
    if not is_working_hour():
        s_time = datetime.strptime(config.get('work_start_time', '15:30'), "%H:%M").strftime("%I:%M %p")
        e_time = datetime.strptime(config.get('work_end_time', '23:00'), "%H:%M").strftime("%I:%M %p")
        
        await query.edit_message_text(
            f"⛔ **এখন কাজের সময় নয়!**\n\n"
            f"⏰ কাজের সময়: `{s_time}` থেকে `{e_time}` পর্যন্ত।",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 হোম", callback_data="back_home")]])
        )
        return ConversationHandler.END

    apps = config.get('monitored_apps', [])
    if not apps:
        await query.edit_message_text("❌ বর্তমানে কোনো কাজ নেই।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="back_home")]]))
        return ConversationHandler.END
        
    buttons = []
    for app in apps:
        limit = app.get('limit', 1000)
        count = get_app_task_count(app['id'])
        
        btn_text = f"📱 {app['name']} ({count}/{limit}) - ৳{config['task_price']:.0f}"
        if count >= limit:
            btn_text = f"⛔ {app['name']} (Full) - ৳{config['task_price']:.0f}"
            
        buttons.append([InlineKeyboardButton(btn_text, callback_data=f"sel_{app['id']}")])

    buttons.append([InlineKeyboardButton("❌ বাতিল", callback_data="cancel")])
    
    await query.edit_message_text("কোন অ্যাপে কাজ করতে চান সিলেক্ট করুন:", reply_markup=InlineKeyboardMarkup(buttons))
    return T_APP_SELECT

async def app_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel": return await cancel_conv(update, context)
    
    app_id = query.data.split("sel_")[1]
    config = get_config()
    app = next((a for a in config['monitored_apps'] if a['id'] == app_id), None)
    
    if not app:
        await query.edit_message_text("❌ অ্যাপটি খুঁজে পাওয়া যায়নি।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="back_home")]]))
        return ConversationHandler.END
        
    limit = app.get('limit', 1000)
    count = get_app_task_count(app_id)
    
    if count >= limit:
         await query.edit_message_text(f"⛔ **দুঃখিত!**\n\n`{app['name']}` এর কাজের লিমিট শেষ।", 
                                       parse_mode="Markdown",
                                       reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 হোম", callback_data="back_home")]]))
         return ConversationHandler.END

    context.user_data['tid'] = app_id
    await query.edit_message_text("✍️ **রিভিউ নাম (Review Name)** দিন:")
    return T_REVIEW_NAME

async def get_review_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['rname'] = update.message.text.strip()
    await update.message.reply_text("আপনার ইমেইল এড্রেস দিন:")
    return T_EMAIL

async def get_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['email'] = update.message.text
    await update.message.reply_text("মোবাইল মডেল/ডিভাইস নাম:")
    return T_DEVICE

async def get_device(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['dev'] = update.message.text
    await update.message.reply_text("স্ক্রিনশট এর লিংক দিন অথবা সরাসরি ছবি আপলোড করুন:")
    return T_SS

# ছবি আপলোড বা লিংক নেওয়া
async def save_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data
    config = get_config()
    user = update.effective_user
    
    screenshot_link = ""
    
    if update.message.photo:
        wait_msg = await update.message.reply_text("📤 ছবি আপলোড হচ্ছে... অনুগ্রহ করে অপেক্ষা করুন।")
        try:
            photo = await update.message.photo[-1].get_file()
            img_bytes = io.BytesIO()
            await photo.download_to_memory(img_bytes)
            img_bytes.seek(0)
            
            # ImgBB Upload
            if IMGBB_API_KEY:
                files = {'image': img_bytes}
                payload = {'key': IMGBB_API_KEY}
                response = requests.post("https://api.imgbb.com/1/upload", data=payload, files=files)
                result = response.json()
                if result.get('success'):
                    screenshot_link = result['data']['url']
                else:
                    await wait_msg.edit_text("❌ ছবি আপলোড ব্যর্থ হয়েছে।")
                    return T_SS
            else:
                await wait_msg.edit_text("❌ ImgBB API Key কনফিগার করা নেই।")
                return ConversationHandler.END
            await wait_msg.delete()
        except Exception as e:
            await wait_msg.edit_text("❌ টেকনিক্যাল সমস্যা হয়েছে।")
            return ConversationHandler.END

    elif update.message.text:
        screenshot_link = update.message.text.strip()
    
    else:
        await update.message.reply_text("❌ অনুগ্রহ করে ছবি বা লিংক দিন।")
        return T_SS

    app_name = next((a['name'] for a in config['monitored_apps'] if a['id'] == data['tid']), data['tid'])
    
    task_ref = db.collection('tasks').add({
        "user_id": str(user.id),
        "app_id": data['tid'],
        "review_name": data['rname'],
        "email": data['email'],
        "device": data['dev'],
        "screenshot": screenshot_link,
        "status": "pending",
        "submitted_at": datetime.now(),
        "price": config['task_price']
    })
    
    log_msg = (
        f"📝 **New Task Submitted**\n"
        f"👤 User: `{user.id}`\n"
        f"📱 App: **{app_name}**\n"
        f"✍️ Name: {data['rname']}\n"
        f"🖼 Proof: [View Screenshot]({screenshot_link})\n"
        f"💰 Price: ৳{config['task_price']:.2f}"
    )
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Approve", callback_data=f"t_apr_{task_ref[1].id}_{user.id}"),
         InlineKeyboardButton("❌ Reject", callback_data=f"t_rej_{task_ref[1].id}_{user.id}")]
    ])
    
    await send_log_message(context, log_msg, kb)
    await update.message.reply_text("✅ কাজ জমা হয়েছে! এডমিন চেক করে এপ্রুভ করবেন।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 হোম", callback_data="back_home")]]))
    return ConversationHandler.END

async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update.callback_query:
            await update.callback_query.edit_message_text("❌ বাতিল করা হয়েছে।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 হোম", callback_data="back_home")]]))
        else:
            await update.message.reply_text("❌ বাতিল করা হয়েছে।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 হোম", callback_data="back_home")]]))
    except: pass
    return ConversationHandler.END

async def handle_task_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⚠️ Only Admins can do this!", show_alert=True)
        return

    data = query.data.split('_')
    action = data[1]
    task_id = data[2]
    user_id = data[3]
    
    task_ref = db.collection('tasks').document(task_id)
    task_doc = task_ref.get()
    
    if not task_doc.exists:
        await query.answer("Task not found", show_alert=True)
        return
        
    t_data = task_doc.to_dict()
    if t_data['status'] != 'pending':
        await query.answer(f"Task is already {t_data['status']}", show_alert=True)
        return

    price = t_data.get('price', 0)
    
    if action == "apr":
        task_ref.update({"status": "approved", "approved_at": datetime.now()})
        db.collection('users').document(str(user_id)).update({
            "balance": firestore.Increment(price),
            "total_tasks": firestore.Increment(1)
        })
        await query.edit_message_text(f"✅ Task Approved Manually\nUser: `{user_id}` (৳{price:.2f})", parse_mode="Markdown")
        await context.bot.send_message(chat_id=user_id, text=f"🎉 আপনার কাজটি এপ্রুভ হয়েছে! ৳{price:.2f} যোগ হয়েছে।")
        
    elif action == "rej":
        task_ref.update({"status": "rejected", "processed_by": query.from_user.id})
        await query.edit_message_text(f"❌ Task Rejected Manually\nUser: `{user_id}`", parse_mode="Markdown")
        await context.bot.send_message(chat_id=user_id, text="❌ আপনার কাজটি রিজেক্ট করা হয়েছে। সঠিক তথ্য দিয়ে আবার চেষ্টা করুন।")

# ==========================================
# 5. অটোমেশন (Play Store Monitor)
# ==========================================

def run_automation():
    logger.info("Automation Started...")
    while True:
        try:
            config = get_config()
            apps = config.get('monitored_apps', [])
            log_id = config.get('log_channel_id', OWNER_ID)
            
            for app in apps:
                try:
                    # প্লে স্টোর থেকে লাস্ট ১০টি রিভিউ আনা
                    reviews, _ = play_reviews(app['id'], count=10, sort=Sort.NEWEST)
                    for r in reviews:
                        rid = r['reviewId']
                        r_date = r['at']
                        if r_date < datetime.now() - timedelta(hours=48):
                            continue
                        
                        # যদি রিভিউটি আগে চেক না করা হয়ে থাকে
                        if not db.collection('seen_reviews').document(rid).get().exists:
                            date_str = r_date.strftime("%d-%m-%Y %I:%M %p")
                            ai_txt = get_ai_summary(r['content'], r['score'])
                            
                            # লগ গ্রুপে নোটিফিকেশন
                            msg = (
                                f"🔔 **Play Store Review Found**\n"
                                f"📱 App: `{app['name']}`\n"
                                f"👤 Name: **{r['userName']}**\n"
                                f"📅 Date: `{date_str}`\n"
                                f"⭐ Rating: {r['score']}/5\n"
                                f"💬 Comment: {r['content']}\n"
                                f"🤖 AI Mood: {ai_txt}"
                            )
                            send_telegram_message(msg, chat_id=log_id)
                            db.collection('seen_reviews').document(rid).set({"t": datetime.now()})

                            # অটো অ্যাপ্রুভাল লজিক (শুধুমাত্র ৫ স্টার হলে)
                            if r['score'] == 5:
                                # পেন্ডিং টাস্ক চেক করা
                                p_tasks = db.collection('tasks').where('app_id', '==', app['id']).where('status', '==', 'pending').stream()
                                for t in p_tasks:
                                    td = t.to_dict()
                                    # যদি টাস্কের নাম এবং রিভিউ নাম মিলে যায়
                                    if td['review_name'].lower().strip() == r['userName'].lower().strip():
                                        price = td.get('price', 0)
                                        # টাস্ক এপ্রুভ করা
                                        db.collection('tasks').document(t.id).update({"status": "approved", "approved_at": datetime.now()})
                                        db.collection('users').document(str(td['user_id'])).update({
                                            "balance": firestore.Increment(price),
                                            "total_tasks": firestore.Increment(1)
                                        })

                                        send_telegram_message(
                                            f"🤖 **Auto Approved!**\nUser: `{td['user_id']}`\nApp: {app['name']}\nName: {td['review_name']}", 
                                            chat_id=log_id
                                        )
                                        send_telegram_message(
                                            f"🎉 আপনার কাজটি **অটোমেটিক এপ্রুভ** হয়েছে! ৳{price:.2f} যোগ হয়েছে।", 
                                            chat_id=td['user_id']
                                        )
                                        break
                except Exception as e:
                    pass # App error skip
        except Exception as e:
            logger.error(f"Automation Loop Error: {e}")
        time.sleep(300) # প্রতি ৫ মিনিটে চেক করবে

def send_telegram_message(message, chat_id=None, reply_markup=None):
    if not chat_id: return
    try:
        payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
        if reply_markup:
            if hasattr(reply_markup, 'to_dict'):
                 payload["reply_markup"] = reply_markup.to_dict()
            else:
                 payload["reply_markup"] = reply_markup
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json=payload, timeout=10)
    except: pass

# ==========================================
# 6. এডমিন প্যানেল (Complete)
# ==========================================

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id): return

    kb = [
        [InlineKeyboardButton("👥 Users & Balance", callback_data="adm_users"), InlineKeyboardButton("💰 Finance & Bonus", callback_data="adm_finance")],
        [InlineKeyboardButton("📱 Apps Manage", callback_data="adm_apps"), InlineKeyboardButton("👮 Manage Admins", callback_data="adm_admins")],
        [InlineKeyboardButton("🎨 Buttons & Time", callback_data="adm_content"), InlineKeyboardButton("📢 Log Channel", callback_data="adm_log")],
        [InlineKeyboardButton("📊 Reports & Export", callback_data="adm_reports")],
        [InlineKeyboardButton("🔙 Back to User Mode", callback_data="back_home")]
    ]
    await query.edit_message_text("⚙️ **Super Admin Panel**", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

# --- Admin Reports & Exports ---
async def admin_reports_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    msg = "📊 **Reports & Export**\n\nDownload Approved Tasks data as **CSV**."
    kb = [
        [InlineKeyboardButton("📜 All Time History", callback_data="rep_all")],
        [InlineKeyboardButton("📅 Last 7 Days", callback_data="rep_7d")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]
    ]
    await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

async def export_report_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Generating report...")
    
    data_code = query.data
    now = datetime.now()
    cutoff_date = now - timedelta(days=7) if data_code == "rep_7d" else None
    
    tasks_ref = db.collection('tasks').where('status', '==', 'approved').stream()
    data_rows = []
    
    for t in tasks_ref:
        t_data = t.to_dict()
        approved_at = t_data.get('approved_at')
        if approved_at:
            if cutoff_date and approved_at.replace(tzinfo=None) < cutoff_date.replace(tzinfo=None): continue
            date_str = approved_at.strftime("%Y-%m-%d %H:%M:%S")
        else:
            if cutoff_date: continue
            date_str = "N/A"

        data_rows.append([t.id, t_data.get('user_id'), t_data.get('app_id'), t_data.get('review_name'), t_data.get('price'), date_str])
    
    if not data_rows:
        await query.message.reply_text("❌ No data found.")
        return

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Task ID", "User ID", "App ID", "Review Name", "Price", "Date"])
    writer.writerows(data_rows)
    output.seek(0)
    
    await context.bot.send_document(
        chat_id=query.from_user.id,
        document=io.BytesIO(output.getvalue().encode('utf-8')),
        filename=f"Report_{now.strftime('%Y%m%d')}.csv",
        caption="📊 Export Generated"
    )

# --- Admin Sub Menus ---
async def admin_sub_handlers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == "adm_users":
        kb = [[InlineKeyboardButton("🔍 Manage Specific User", callback_data="find_user")], [InlineKeyboardButton("🔙 Admin Home", callback_data="admin_panel")]]
        await query.edit_message_text("👥 **User Management**", reply_markup=InlineKeyboardMarkup(kb))

    elif data == "adm_finance":
        config = get_config()
        msg = f"💸 **Finance Config**\nRef Bonus: ৳{config['referral_bonus']}\nMin Withdraw: ৳{config['min_withdraw']}"
        kb = [[InlineKeyboardButton("✏️ Change Ref Bonus", callback_data="ed_txt_referral_bonus")], [InlineKeyboardButton("🔙 Admin Home", callback_data="admin_panel")]]
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb))
        
    elif data == "adm_apps":
        config = get_config()
        apps_list = "\n".join([f"- {a['name']} ({a.get('limit','N/A')})" for a in config['monitored_apps']]) if config['monitored_apps'] else "No apps."
        msg = f"📱 **Apps:**\n{apps_list}"
        kb = [
            [InlineKeyboardButton("➕ Add App", callback_data="add_app"), InlineKeyboardButton("➖ Remove App", callback_data="rmv_app")],
            [InlineKeyboardButton("✏️ Edit Limit", callback_data="edit_app_limit_start")],
            [InlineKeyboardButton("🔙 Admin Home", callback_data="admin_panel")]
        ]
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb))
        
    elif data == "adm_content":
        kb = [
            [InlineKeyboardButton("⏰ Start Time", callback_data="set_time_start"), InlineKeyboardButton("⏰ End Time", callback_data="set_time_end")],
            [InlineKeyboardButton("🔘 Button Config", callback_data="ed_btns")],
            [InlineKeyboardButton("➕ Add Custom Btn", callback_data="add_cus_btn"), InlineKeyboardButton("➖ Rmv Custom Btn", callback_data="rmv_cus_btn")],
            [InlineKeyboardButton("🔙 Admin Home", callback_data="admin_panel")]
        ]
        await query.edit_message_text("🎨 **Settings**", reply_markup=InlineKeyboardMarkup(kb))

    elif data == "adm_admins":
        kb = [[InlineKeyboardButton("➕ Add Admin", callback_data="add_new_admin")], [InlineKeyboardButton("➖ Remove Admin", callback_data="rmv_admin_role")], [InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]
        await query.edit_message_text("👮 **Admins**", reply_markup=InlineKeyboardMarkup(kb))
        
    elif data == "adm_log":
        curr = get_config().get('log_channel_id', 'N/A')
        kb = [[InlineKeyboardButton("✏️ Set Channel ID", callback_data="set_log_id")], [InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]
        await query.edit_message_text(f"📢 Log ID: `{curr}`", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

# --- Admin Function Implementations (Add/Edit) ---

async def add_app_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("App Package ID (e.g. com.example):")
    return ADD_APP_ID

async def add_app_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['nid'] = update.message.text.strip()
    await update.message.reply_text("App Name:")
    return ADD_APP_NAME

async def add_app_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['nname'] = update.message.text.strip()
    await update.message.reply_text("Task Limit:")
    return ADD_APP_LIMIT

async def add_app_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        limit = int(update.message.text.strip())
        config = get_config()
        apps = config.get('monitored_apps', [])
        apps.append({"id": context.user_data['nid'], "name": context.user_data['nname'], "limit": limit})
        update_config({"monitored_apps": apps})
        await update.message.reply_text("✅ App Added!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin", callback_data="admin_panel")]]))
        return ConversationHandler.END
    except: return ADD_APP_LIMIT

async def rmv_app_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    apps = get_config().get('monitored_apps', [])
    if not apps:
        await update.callback_query.answer("No apps", show_alert=True)
        return ConversationHandler.END
    btns = [[InlineKeyboardButton(f"🗑️ {a['name']}", callback_data=f"rm_{i}")] for i, a in enumerate(apps)]
    await update.callback_query.edit_message_text("Select to Remove:", reply_markup=InlineKeyboardMarkup(btns))
    return REMOVE_APP_SELECT

async def rmv_app_sel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    idx = int(update.callback_query.data.split("rm_")[1])
    config = get_config()
    apps = config.get('monitored_apps', [])
    if 0 <= idx < len(apps):
        del apps[idx]
        update_config({"monitored_apps": apps})
        await update.callback_query.edit_message_text("✅ Removed!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin", callback_data="admin_panel")]]))
    return ConversationHandler.END

async def find_user_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("🔍 Enter User ID:")
    return ADMIN_USER_SEARCH

async def find_user_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.text.strip()
    user = get_user(uid)
    if not user:
        await update.message.reply_text("User Not Found.")
        return ConversationHandler.END
    context.user_data['mng_uid'] = uid
    msg = f"User: {user.get('name')}\nBal: {user.get('balance')}\nStatus: {'Blocked' if user.get('is_blocked') else 'Active'}"
    kb = [[InlineKeyboardButton("➕ Add Bal", callback_data="u_add_bal"), InlineKeyboardButton("➖ Cut Bal", callback_data="u_cut_bal")],
          [InlineKeyboardButton("⛔ Block/Unblock", callback_data="u_toggle_block")], [InlineKeyboardButton("🔙 Cancel", callback_data="cancel")]]
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb))
    return ADMIN_USER_ACTION

async def user_action_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data
    uid = context.user_data['mng_uid']
    if data == "cancel": return await cancel_conv(update, context)
    
    if data == "u_toggle_block":
        user = get_user(uid)
        db.collection('users').document(uid).update({"is_blocked": not user.get('is_blocked', False)})
        await update.callback_query.edit_message_text("✅ Status Changed!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin", callback_data="admin_panel")]]))
        return ConversationHandler.END
    elif "bal" in data:
        context.user_data['bal_action'] = "add" if "add" in data else "cut"
        await update.callback_query.edit_message_text("Enter Amount:")
        return ADMIN_USER_AMOUNT

async def user_balance_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amt = float(update.message.text)
        uid = context.user_data['mng_uid']
        val = amt if context.user_data['bal_action'] == "add" else -amt
        db.collection('users').document(uid).update({"balance": firestore.Increment(val)})
        await update.message.reply_text("✅ Balance Updated!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin", callback_data="admin_panel")]]))
    except: pass
    return ConversationHandler.END

# Text Editing (Referral, Time etc)
async def edit_text_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key_map = {"ed_txt_referral_bonus": "referral_bonus", "set_log_id": "log_channel_id", "set_time_start": "work_start_time", "set_time_end": "work_end_time"}
    context.user_data['edit_key'] = key_map.get(update.callback_query.data)
    await update.callback_query.edit_message_text("Enter new value:")
    return ADMIN_EDIT_TEXT_VAL

async def edit_text_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text.strip()
    key = context.user_data['edit_key']
    if key == "referral_bonus": val = float(val)
    update_config({key: val})
    await update.message.reply_text("✅ Saved!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin", callback_data="admin_panel")]]))
    return ConversationHandler.END

# Button Editing
async def edit_buttons_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    btns = get_config().get('buttons', DEFAULT_CONFIG['buttons'])
    kb = []
    for k, v in btns.items():
        kb.append([InlineKeyboardButton(f"{'✅' if v['show'] else '❌'} {v['text']}", callback_data=f"btntog_{k}")])
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="adm_content")])
    await update.callback_query.edit_message_text("Toggle Buttons:", reply_markup=InlineKeyboardMarkup(kb))

async def button_action_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = update.callback_query.data.split("_")[1]
    config = get_config()
    config['buttons'][key]['show'] = not config['buttons'][key]['show']
    update_config({"buttons": config['buttons']})
    await edit_buttons_menu(update, context)

# Admin Management
async def add_admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("Enter Telegram ID to Make Admin:")
    return ADMIN_ADD_ADMIN_ID

async def add_admin_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.text.strip()
    db.collection('users').document(uid).set({"is_admin": True}, merge=True)
    await update.message.reply_text("✅ Admin Added!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin", callback_data="admin_panel")]]))
    return ConversationHandler.END

async def rmv_admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("Enter ID to Remove Admin:")
    return ADMIN_RMV_ADMIN_ID

async def rmv_admin_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.text.strip()
    if uid == OWNER_ID: return
    db.collection('users').document(uid).update({"is_admin": False})
    await update.message.reply_text("✅ Admin Removed!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin", callback_data="admin_panel")]]))
    return ConversationHandler.END

async def edit_app_limit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    apps = get_config().get('monitored_apps', [])
    if not apps: return ConversationHandler.END
    btns = [[InlineKeyboardButton(f"{a['name']}", callback_data=f"edlim_{i}")] for i, a in enumerate(apps)]
    await update.callback_query.edit_message_text("Select App:", reply_markup=InlineKeyboardMarkup(btns))
    return EDIT_APP_SELECT

async def edit_app_limit_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['ed_app_idx'] = int(update.callback_query.data.split("_")[1])
    await update.callback_query.edit_message_text("Enter New Limit:")
    return EDIT_APP_LIMIT_VAL

async def edit_app_limit_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        limit = int(update.message.text)
        idx = context.user_data['ed_app_idx']
        config = get_config()
        config['monitored_apps'][idx]['limit'] = limit
        update_config({"monitored_apps": config['monitored_apps']})
        await update.message.reply_text("✅ Limit Updated!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin", callback_data="admin_panel")]]))
    except: pass
    return ConversationHandler.END

async def add_custom_btn_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("Button Name:")
    return ADMIN_ADD_BTN_NAME
async def add_custom_btn_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['c_btn_name'] = update.message.text
    await update.message.reply_text("Button URL:")
    return ADMIN_ADD_BTN_LINK
async def add_custom_btn_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = get_config()
    btns = config.get('custom_buttons', [])
    btns.append({"text": context.user_data['c_btn_name'], "url": update.message.text})
    update_config({"custom_buttons": btns})
    await update.message.reply_text("✅ Button Added!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin", callback_data="admin_panel")]]))
    return ConversationHandler.END

async def rmv_custom_btn_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    btns = get_config().get('custom_buttons', [])
    if not btns: return ConversationHandler.END
    kb = [[InlineKeyboardButton(f"🗑️ {b['text']}", callback_data=f"rm_cus_btn_{i}")] for i, b in enumerate(btns)]
    await update.callback_query.edit_message_text("Remove Button:", reply_markup=InlineKeyboardMarkup(kb))
    return REMOVE_CUS_BTN

async def rmv_custom_btn_handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    idx = int(update.callback_query.data.split("_")[4])
    config = get_config()
    btns = config.get('custom_buttons', [])
    if 0 <= idx < len(btns):
        del btns[idx]
        update_config({"custom_buttons": btns})
    await update.callback_query.edit_message_text("✅ Removed!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin", callback_data="admin_panel")]]))
    return ConversationHandler.END


# ==========================================
# 7. মেইন রানার
# ==========================================

app = Flask(__name__)
@app.route('/')
def home(): return "Bot is Alive & Secure!"

def run_flask():
    app.run(host='0.0.0.0', port=PORT)

def main():
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=run_automation, daemon=True).start()

    application = ApplicationBuilder().token(TOKEN).build()

    # Commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("login", generate_login_pass)) # লগইন কোড জেনারেটর

    # Callbacks
    application.add_handler(CallbackQueryHandler(admin_panel, pattern="^admin_panel$"))
    application.add_handler(CallbackQueryHandler(admin_sub_handlers, pattern="^(adm_users|adm_finance|adm_apps|adm_content|adm_admins|adm_log)$"))
    application.add_handler(CallbackQueryHandler(admin_reports_menu, pattern="^adm_reports$"))
    application.add_handler(CallbackQueryHandler(export_report_data, pattern="^(rep_all|rep_7d)$"))
    application.add_handler(CallbackQueryHandler(edit_buttons_menu, pattern="^ed_btns$"))
    application.add_handler(CallbackQueryHandler(button_action_handler, pattern="^btntog_"))
    
    application.add_handler(CallbackQueryHandler(handle_withdrawal_action, pattern="^wd_(apr|rej)_"))
    application.add_handler(CallbackQueryHandler(handle_task_action, pattern="^t_(apr|rej)_"))

    # Conversations
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(start_task_submission, pattern="^submit_task$")],
        states={
            T_APP_SELECT: [CallbackQueryHandler(app_selected, pattern="^sel_")],
            T_REVIEW_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_review_name)],
            T_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_email)],
            T_DEVICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_device)],
            T_SS: [MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND, save_task)]
        },
        fallbacks=[CallbackQueryHandler(cancel_conv, pattern="^cancel")]
    ))
    
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(withdraw_start, pattern="^start_withdraw$")],
        states={
            WD_METHOD: [CallbackQueryHandler(withdraw_method, pattern="^m_(bkash|nagad)$|^cancel$")],
            WD_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_number)],
            WD_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_amount)]
        },
        fallbacks=[CallbackQueryHandler(cancel_conv, pattern="^cancel")]
    ))
    
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(add_app_start, pattern="^add_app$")],
        states={ADD_APP_ID: [MessageHandler(filters.TEXT, add_app_id)], ADD_APP_NAME: [MessageHandler(filters.TEXT, add_app_name)], ADD_APP_LIMIT: [MessageHandler(filters.TEXT, add_app_limit)]},
        fallbacks=[CallbackQueryHandler(cancel_conv)]
    ))
    
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_app_limit_start, pattern="^edit_app_limit_start$")],
        states={EDIT_APP_SELECT: [CallbackQueryHandler(edit_app_limit_select)], EDIT_APP_LIMIT_VAL: [MessageHandler(filters.TEXT, edit_app_limit_save)]},
        fallbacks=[CallbackQueryHandler(cancel_conv)]
    ))

    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(rmv_app_start, pattern="^rmv_app$")],
        states={REMOVE_APP_SELECT: [CallbackQueryHandler(rmv_app_sel)]},
        fallbacks=[CallbackQueryHandler(cancel_conv)]
    ))
    
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(find_user_start, pattern="^find_user$")],
        states={ADMIN_USER_SEARCH: [MessageHandler(filters.TEXT, find_user_result)], ADMIN_USER_ACTION: [CallbackQueryHandler(user_action_handler)], ADMIN_USER_AMOUNT: [MessageHandler(filters.TEXT, user_balance_update)]},
        fallbacks=[CallbackQueryHandler(cancel_conv)]
    ))
    
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_text_start, pattern="^(ed_txt_referral_bonus|set_log_id|set_time_start|set_time_end)$")],
        states={ADMIN_EDIT_TEXT_VAL: [MessageHandler(filters.TEXT, edit_text_save)]},
        fallbacks=[CallbackQueryHandler(cancel_conv)]
    ))
    
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(add_custom_btn_start, pattern="^add_cus_btn$")],
        states={ADMIN_ADD_BTN_NAME: [MessageHandler(filters.TEXT, add_custom_btn_link)], ADMIN_ADD_BTN_LINK: [MessageHandler(filters.TEXT, add_custom_btn_save)]},
        fallbacks=[CallbackQueryHandler(cancel_conv)]
    ))

    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(rmv_custom_btn_start, pattern="^rmv_cus_btn$")],
        states={REMOVE_CUS_BTN: [CallbackQueryHandler(rmv_custom_btn_handle)]},
        fallbacks=[CallbackQueryHandler(cancel_conv)]
    ))

    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(add_admin_start, pattern="^add_new_admin$")],
        states={ADMIN_ADD_ADMIN_ID: [MessageHandler(filters.TEXT, add_admin_save)]},
        fallbacks=[CallbackQueryHandler(cancel_conv)]
    ))
    
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(rmv_admin_start, pattern="^rmv_admin_role$")],
        states={ADMIN_RMV_ADMIN_ID: [MessageHandler(filters.TEXT, rmv_admin_save)]},
        fallbacks=[CallbackQueryHandler(cancel_conv)]
    ))

    application.add_handler(CallbackQueryHandler(common_callback, pattern="^(my_profile|refer_friend|back_home|show_schedule)$"))

    print("🚀 Bot Started on Render...")
    application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
