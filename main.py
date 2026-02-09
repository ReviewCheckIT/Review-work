import os
import json
import logging
import threading
import time
import asyncio
import csv
import io
import random  # Added
import string  # Added
from datetime import datetime, timedelta
import requests
import firebase_admin
from firebase_admin import credentials, firestore
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
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
# 1. Configuration and Setup
# ==========================================

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ENV Variables
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OWNER_ID = os.environ.get("OWNER_ID", "") 
FIREBASE_JSON = os.environ.get("FIREBASE_CREDENTIALS", "firebase_key.json")
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', "")
IMGBB_API_KEY = os.environ.get('IMGBB_API_KEY', "")
PORT = int(os.environ.get("PORT", 8080))

# Gemini AI Setup
model = None
if AI_AVAILABLE and GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel('gemini-1.5-flash')
    except Exception as e:
        logger.error(f"Gemini AI Config Error: {e}")

# Firebase Connection
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
# 2. Global Config & State
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
# 3. Helper Functions
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
        if start < end:
            return start <= now <= end
        else: 
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

def generate_web_password(length=6):
    """Generates a simple alphanumeric password"""
    chars = string.ascii_letters + string.digits
    return ''.join(random.choice(chars) for _ in range(length))

def create_user(user_id, first_name, referrer_id=None):
    """Creates user and ensures they have a web password"""
    user_ref = db.collection('users').document(str(user_id))
    doc = user_ref.get()
    
    if not doc.exists:
        # New User
        password = generate_web_password()
        user_data = {
            "id": str(user_id),
            "name": first_name,
            "balance": 0.0,
            "total_tasks": 0,
            "web_password": password,
            "joined_at": datetime.now(),
            "referrer": referrer_id if referrer_id and referrer_id.isdigit() and str(referrer_id) != str(user_id) else None,
            "is_blocked": False,
            "is_admin": str(user_id) == str(OWNER_ID)
        }
        user_ref.set(user_data)
        return user_data
    else:
        # Existing user check for password
        data = doc.to_dict()
        if "web_password" not in data:
            new_pass = generate_web_password()
            user_ref.update({"web_password": new_pass})
            data["web_password"] = new_pass
        return data

async def send_log_message(context, text, reply_markup=None):
    config = get_config()
    chat_id = config.get('log_channel_id')
    target_id = chat_id if chat_id else OWNER_ID
    if target_id:
        try:
            await context.bot.send_message(chat_id=target_id, text=text, reply_markup=reply_markup, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Log Send Error: {e}")

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
# 4. User Side Functions
# ==========================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    referrer = args[0] if args and args[0].isdigit() else None
    
    # Create or Get User (Generates Password if missing)
    db_user = create_user(user.id, user.first_name, referrer)

    if db_user and db_user.get('is_blocked'):
        await update.message.reply_text("⛔ আপনাকে ব্লক করা হয়েছে।")
        return

    web_pass = db_user.get('web_password', 'Error')
    config = get_config()
    btns_conf = config.get('buttons', DEFAULT_CONFIG['buttons'])

    welcome_msg = (
        f"আসসালামু আলাইকুম, {user.first_name}! 🌙\n\n"
        f"🌐 **Web Dashboard Login:**\n"
        f"🆔 User ID: `{user.id}`\n"
        f"🔑 Password: `{web_pass}`\n"
        f"🔗 [Web Dashboard Link](https://YOUR-WEBSITE-LINK.com)\n\n"
        f"🗒 **কাজের নিয়মাবলী:**\n{config.get('rules_text', '')}\n"
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
        except BadRequest: pass
    else:
        await update.message.reply_text(welcome_msg, reply_markup=reply_markup, parse_mode="Markdown")

# [REST OF THE BOT CODE IS SAME AS PROVIDED IN INPUT. 
# PASTE THE REST OF THE FUNCTIONS (Withdrawal, Task, Admin, Automation) HERE]
# ... (Functions: common_callback, withdraw_start... to run_automation) ...

# NOTE: For brevity, I am not repeating the 800 lines of existing logic. 
# Just use the code provided in the prompt, but REPLACE the 'start' and 'create_user' 
# functions with the ones above.

# ==========================================
# 7. মেইন রানার (UPDATED)
# ==========================================

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is Alive & Running!", 200

@app.route('/health')
def health():
    return "OK", 200

def run_flask():
    # Render বা অন্যান্য ক্লাউড পোর্টে রান করার জন্য
    try:
        print(f"🌐 Starting Web Server on Port {PORT}")
        app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)
    except Exception as e:
        logger.error(f"Flask Server Error: {e}")

def main():
    # ১. প্রথমে ওয়েব সার্ভার স্টার্ট করুন (Backgroud Thread)
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # ২. এরপর টেলিগ্রাম বট স্টার্ট করুন
    application = ApplicationBuilder().token(TOKEN).build()

    # কমান্ড হ্যান্ডলার
    application.add_handler(CommandHandler("start", start))
    
    # এডমিন প্যানেল হ্যান্ডলার
    application.add_handler(CallbackQueryHandler(admin_panel, pattern="^admin_panel$"))
    application.add_handler(CallbackQueryHandler(admin_sub_handlers, pattern="^(adm_users|adm_finance|adm_apps|adm_content|adm_admins|adm_log)$"))

    # রিপোর্ট হ্যান্ডলার
    application.add_handler(CallbackQueryHandler(admin_reports_menu, pattern="^adm_reports$"))
    application.add_handler(CallbackQueryHandler(admin_reports_apps_selection, pattern="^rep_apps$"))
    application.add_handler(CallbackQueryHandler(admin_show_app_timeframes, pattern="^sel_rep_app_"))
    application.add_handler(CallbackQueryHandler(export_report_data, pattern="^(rep_all|rep_7d|rep_24h|repex_.*)$"))

    # বাটন এডিট হ্যান্ডলার
    application.add_handler(CallbackQueryHandler(edit_buttons_menu, pattern="^ed_btns$"))
    application.add_handler(CallbackQueryHandler(button_action_handler, pattern="^(btntog_|btnren_)"))

    # একশন হ্যান্ডলার
    application.add_handler(CallbackQueryHandler(handle_withdrawal_action, pattern="^wd_(apr|rej)_"))
    application.add_handler(CallbackQueryHandler(handle_task_action, pattern="^t_(apr|rej)_"))

    # টাস্ক সাবমিশন কনভারসেশন
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

    # উইথড্রয়াল কনভারসেশন
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(withdraw_start, pattern="^start_withdraw$")],
        states={
            WD_METHOD: [CallbackQueryHandler(withdraw_method, pattern="^m_(bkash|nagad)$|^cancel$")],
            WD_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_number)],
            WD_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_amount)]
        },
        fallbacks=[CallbackQueryHandler(cancel_conv, pattern="^cancel")]
    ))

    # অ্যাপ অ্যাড কনভারসেশন
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(add_app_start, pattern="^add_app$")],
        states={
            ADD_APP_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_app_id)],
            ADD_APP_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_app_name)],
            ADD_APP_LIMIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_app_limit)]
        },
        fallbacks=[CallbackQueryHandler(cancel_conv)]
    ))

    # অ্যাপ লিমিট এডিট কনভারসেশন
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_app_limit_start, pattern="^edit_app_limit_start$")],
        states={
            EDIT_APP_SELECT: [CallbackQueryHandler(edit_app_limit_select, pattern="^edlim_")],
            EDIT_APP_LIMIT_VAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_app_limit_save)]
        },
        fallbacks=[CallbackQueryHandler(cancel_conv)]
    ))

    # রিমুভ অ্যাপ কনভারসেশন
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(rmv_app_start, pattern="^rmv_app$")],
        states={REMOVE_APP_SELECT: [CallbackQueryHandler(rmv_app_sel)]},
        fallbacks=[CallbackQueryHandler(cancel_conv)]
    ))

    # ইউজার ম্যানেজ কনভারসেশন
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(find_user_start, pattern="^find_user$")],
        states={
            ADMIN_USER_SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, find_user_result)],
            ADMIN_USER_ACTION: [CallbackQueryHandler(user_action_handler, pattern="^(u_add_bal|u_cut_bal|u_toggle_block|u_toggle_admin)$|^cancel$")],
            ADMIN_USER_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, user_balance_update)]
        },
        fallbacks=[CallbackQueryHandler(cancel_conv)]
    ))

    # টেক্সট এডিট কনভারসেশন
    application.add_handler(ConversationHandler(
        entry_points=[
            CallbackQueryHandler(edit_text_start, pattern="^(ed_txt_rules|ed_txt_schedule|ed_txt_referral_bonus)$"),
            CallbackQueryHandler(button_action_handler, pattern="^btnren_")
        ],
        states={
            ADMIN_EDIT_TEXT_VAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_text_save)],
            ADMIN_EDIT_BTN_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, button_rename_save)]
        },
        fallbacks=[CallbackQueryHandler(cancel_conv)]
    ))

    # কাস্টম বাটন অ্যাড কনভারসেশন
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(add_custom_btn_start, pattern="^add_cus_btn$")],
        states={
            ADMIN_ADD_BTN_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_custom_btn_link)],
            ADMIN_ADD_BTN_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_custom_btn_save)]
        },
        fallbacks=[CallbackQueryHandler(cancel_conv)]
    ))

    # রিমুভ কাস্টম বাটন কনভারসেশন
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(rmv_custom_btn_start, pattern="^rmv_cus_btn$")],
        states={
            REMOVE_CUS_BTN: [CallbackQueryHandler(rmv_custom_btn_handle, pattern="^rm_cus_btn_")]
        },
        fallbacks=[CallbackQueryHandler(cancel_conv)]
    ))

    # টাইম সেটিং কনভারসেশন
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(set_time_start_handler, pattern="^set_time_start$")],
        states={ADMIN_SET_START_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_time_start_save)]},
        fallbacks=[CallbackQueryHandler(cancel_conv)]
    ))
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(set_time_end_handler, pattern="^set_time_end$")],
        states={ADMIN_SET_END_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_time_end_save)]},
        fallbacks=[CallbackQueryHandler(cancel_conv)]
    ))

    # এডমিন অ্যাড/রিমুভ কনভারসেশন
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(add_admin_start, pattern="^add_new_admin$")],
        states={ADMIN_ADD_ADMIN_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_admin_save)]},
        fallbacks=[CallbackQueryHandler(cancel_conv)]
    ))
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(rmv_admin_start, pattern="^rmv_admin_role$")],
        states={ADMIN_RMV_ADMIN_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, rmv_admin_save)]},
        fallbacks=[CallbackQueryHandler(cancel_conv)]
    ))

    # লগ চ্যানেল কনভারসেশন
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(set_log_start, pattern="^set_log_id$")],
        states={ADMIN_SET_LOG_CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_log_save)]},
        fallbacks=[CallbackQueryHandler(cancel_conv)]
    ))

    # সাধারণ কলব্যাক হ্যান্ডলার
    application.add_handler(CallbackQueryHandler(common_callback, pattern="^(my_profile|refer_friend|back_home|show_schedule)$"))

    # ৩. অটোমেশন থ্রেড
    automation_thread = threading.Thread(target=run_automation, daemon=True)
    automation_thread.start()

    print("🚀 Bot Started on Render...")
    # Polling চালু করা (এটি লুপে চলবে)
    application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()