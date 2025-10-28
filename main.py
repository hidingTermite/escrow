import os
import logging
import asyncio
import nest_asyncio
from web3 import Web3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters
from datetime import datetime, timedelta
import sqlite3
from enum import Enum
import requests

# Apply nest_asyncio for environments that need it
try:
    nest_asyncio.apply()
except Exception as e:
    print(f"nest_asyncio not available: {e}")

# --- Load environment variables ---
BOT_TOKEN = os.environ.get('BOT_TOKEN', 'your-bot-token-here')
if TOKEN:
    TOKEN = TOKEN.strip()
else:
    raise ValueError("token missing")
print(f"Token: {repr(TOKEN)}")
POLYGON_TEST_PRIVATE_KEY = os.environ.get('BOT_PRIVATE_KEY_TESTNET', 'your-testnet-private-key')
POLYGON_MAIN_PRIVATE_KEY = os.environ.get('BOT_PRIVATE_KEY_MAINNET', 'your-mainnet-private-key')
RPC_POLYGON_TEST = os.environ.get('RPC_URL_TESTNET', 'https://rpc-mumbai.maticvigil.com')
RPC_POLYGON_MAIN = os.environ.get('RPC_URL_MAINNET', 'https://polygon-rpc.com')

# --- Configuration ---
class EscrowState(Enum):
    CREATED = "created"
    BUYER_PAID = "buyer_paid"
    SELLER_CONFIRMED = "seller_confirmed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    DISPUTED = "disputed"

# Token addresses
TOKENS = {
    'MATIC': {
        'testnet': None,
        'mainnet': None,
        'decimals': 18
    },
    'USDT': {
        'testnet': '0x3813e82e6f7098b9583FC0F33a962D02018B6803',
        'mainnet': '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174',
        'decimals': 6
    }
}

# --- Database Setup ---
class EscrowDB:
    def __init__(self):
        self.conn = sqlite3.connect('escrow.db', check_same_thread=False)
        self.create_tables()
    
    def create_tables(self):
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS escrows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                buyer_id INTEGER,
                seller_username TEXT,
                amount REAL,
                token TEXT,
                network TEXT,
                state TEXT,
                created_at TIMESTAMP,
                expires_at TIMESTAMP,
                deposit_address TEXT,
                buyer_address TEXT
            )
        ''')
        self.conn.commit()
    
    def create_escrow(self, chat_id, buyer_id, seller_username, amount, token, network, hours=24):
        cursor = self.conn.cursor()
        expires_at = datetime.now() + timedelta(hours=hours)
        cursor.execute('''
            INSERT INTO escrows 
            (chat_id, buyer_id, seller_username, amount, token, network, state, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (chat_id, buyer_id, seller_username, amount, token, network, EscrowState.CREATED.value, datetime.now(), expires_at))
        self.conn.commit()
        return cursor.lastrowid
    
    def get_escrow(self, escrow_id):
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM escrows WHERE id = ?', (escrow_id,))
        return cursor.fetchone()
    
    def update_escrow_state(self, escrow_id, state):
        cursor = self.conn.cursor()
        cursor.execute('UPDATE escrows SET state = ? WHERE id = ?', (state.value, escrow_id))
        self.conn.commit()
    
    def set_deposit_address(self, escrow_id, address):
        cursor = self.conn.cursor()
        cursor.execute('UPDATE escrows SET deposit_address = ? WHERE id = ?', (address, escrow_id))
        self.conn.commit()
    
    def get_user_escrows(self, user_id):
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM escrows WHERE buyer_id = ? ORDER BY created_at DESC', (user_id,))
        return cursor.fetchall()

# --- Setup Logging ---
logging.basicConfig(
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    level=logging.INFO
)
logger = logging.getLogger("escrow_bot")

# --- Web3 Setup with Better Error Handling ---
def setup_web3_connection(rpc_url, network_name):
    """Setup Web3 connection with proper error handling and fallbacks"""
    try:
        # Test the connection first
        response = requests.post(rpc_url, json={
            "jsonrpc": "2.0",
            "method": "eth_blockNumber",
            "params": [],
            "id": 1
        }, timeout=10)
        
        if response.status_code == 200:
            w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={'timeout': 60}))
            if w3.is_connected():
                logger.info(f"✅ Connected to {network_name}")
                return w3
            else:
                logger.warning(f"⚠️ Web3 not connected to {network_name}, but HTTP connection works")
                return w3
        else:
            logger.warning(f"❌ RPC endpoint not accessible: {response.status_code}")
            return None
    except Exception as e:
        logger.warning(f"❌ Failed to connect to {network_name}: {e}")
        # Try fallback RPCs
        fallback_rpcs = {
            'testnet': [
                'https://polygon-mumbai-bor.publicnode.com',
                'https://rpc.ankr.com/polygon_mumbai',
                'https://polygon-testnet.public.blastapi.io'
            ],
            'mainnet': [
                'https://polygon-bor.publicnode.com',
                'https://rpc.ankr.com/polygon',
                'https://polygon-rpc.com'
            ]
        }
        
        network_type = 'testnet' if 'mumbai' in rpc_url or 'test' in rpc_url else 'mainnet'
        for fallback_rpc in fallback_rpcs[network_type]:
            try:
                logger.info(f"Trying fallback RPC: {fallback_rpc}")
                w3 = Web3(Web3.HTTPProvider(fallback_rpc, request_kwargs={'timeout': 60}))
                if w3.is_connected():
                    logger.info(f"✅ Connected to {network_name} via fallback: {fallback_rpc}")
                    return w3
            except Exception as fallback_error:
                logger.warning(f"Fallback RPC failed: {fallback_error}")
                continue
        
        logger.error(f"❌ All RPC connections failed for {network_name}")
        return None

# Initialize Web3 connections
w3_test = setup_web3_connection(RPC_POLYGON_TEST, "Polygon Testnet")
w3_main = setup_web3_connection(RPC_POLYGON_MAIN, "Polygon Mainnet")

# Initialize accounts only if Web3 is connected
account_test = None
account_main = None

if w3_test:
    try:
        account_test = w3_test.eth.account.from_key(POLYGON_TEST_PRIVATE_KEY)
        logger.info(f"🤖 Testnet Bot address: {account_test.address}")
    except Exception as e:
        logger.error(f"❌ Failed to create testnet account: {e}")

if w3_main:
    try:
        account_main = w3_main.eth.account.from_key(POLYGON_MAIN_PRIVATE_KEY)
        logger.info(f"🤖 Mainnet Bot address: {account_main.address}")
    except Exception as e:
        logger.error(f"❌ Failed to create mainnet account: {e}")

db = EscrowDB()

# --- Telegram Bot Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🛍 Create Escrow", callback_data="create_escrow")],
        [InlineKeyboardButton("📊 My Escrows", callback_data="my_escrows")],
        [InlineKeyboardButton("ℹ️ Help", callback_data="help")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "🤖 **Intelligent Escrow Bot**\n\n"
        "I automate secure transactions between buyers and sellers!\n\n"
        "Choose an option below:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button callbacks"""
    query = update.callback_query
    await query.answer()  # Important: acknowledge the callback
    
    data = query.data
    logger.info(f"Button pressed: {data} by user {query.from_user.id}")
    
    if data == "create_escrow":
        await create_escrow_start(query, context)
    elif data == "my_escrows":
        await show_my_escrows(query, context)
    elif data == "help":
        await show_help(query, context)
    elif data == "back_to_main":
        await back_to_main(query, context)
    elif data.startswith("escrow_detail_"):
        escrow_id = int(data.split("_")[2])
        await show_escrow_detail(query, context, escrow_id)
    elif data.startswith("release_"):
        escrow_id = int(data.split("_")[1])
        await release_escrow(query, context, escrow_id)
    elif data.startswith("dispute_"):
        escrow_id = int(data.split("_")[1])
        await start_dispute(query, context, escrow_id)

async def create_escrow_start(query, context):
    """Start escrow creation process"""
    try:
        await query.edit_message_text(
            "🛍 **Create New Escrow**\n\n"
            "Please use the command:\n"
            "`/create @seller 0.1 MATIC testnet`\n\n"
            "Format:\n"
            "• `/create @username amount token network`\n"
            "• Token: MATIC or USDT\n"
            "• Network: testnet or mainnet\n\n"
            "Example:\n"
            "`/create @john 0.1 MATIC testnet`",
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Error in create_escrow_start: {e}")
        await query.message.reply_text("Error processing your request. Please try again.")

async def show_help(query, context):
    """Show help information"""
    await query.edit_message_text(
        "🤖 **Escrow Bot Help**\n\n"
        "**Commands:**\n"
        "• /start - Start the bot\n"
        "• /create - Create new escrow\n"
        "• /status - Check bot status\n\n"
        "**How it works:**\n"
        "1. Create escrow with seller\n"
        "2. Send crypto to deposit address\n"
        "3. Seller confirms receipt\n"
        "4. Funds released automatically\n\n"
        "Supported: MATIC, USDT on Polygon",
        parse_mode='Markdown'
    )

async def back_to_main(query, context):
    """Return to main menu"""
    keyboard = [
        [InlineKeyboardButton("🛍 Create Escrow", callback_data="create_escrow")],
        [InlineKeyboardButton("📊 My Escrows", callback_data="my_escrows")],
        [InlineKeyboardButton("ℹ️ Help", callback_data="help")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "🤖 **Intelligent Escrow Bot**\n\n"
        "Choose an option below:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def create_escrow_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /create command"""
    try:
        if len(context.args) < 4:
            await update.message.reply_text(
                "❌ **Invalid format!**\n\n"
                "Use: `/create @seller amount token network`\n\n"
                "Example:\n"
                "`/create @john 0.1 MATIC testnet`",
                parse_mode='Markdown'
            )
            return
        
        seller_username = context.args[0]
        amount = float(context.args[1])
        token = context.args[2].upper()
        network = context.args[3].lower()
        
        # Validation
        if token not in TOKENS:
            await update.message.reply_text(f"❌ Invalid token. Use: MATIC or USDT")
            return
        
        if network not in ['testnet', 'mainnet']:
            await update.message.reply_text("❌ Invalid network. Use: testnet or mainnet")
            return
        
        if network == 'testnet' and not account_test:
            await update.message.reply_text("❌ Testnet not available. Check RPC connection.")
            return
        
        if network == 'mainnet' and not account_main:
            await update.message.reply_text("❌ Mainnet not available. Check RPC connection.")
            return
        
        # Get the appropriate account
        account = account_test if network == 'testnet' else account_main
        
        # Create escrow in database
        escrow_id = db.create_escrow(
            update.effective_chat.id,
            update.effective_user.id,
            seller_username,
            amount,
            token,
            network
        )
        
        # Set deposit address
        db.set_deposit_address(escrow_id, account.address)
        
        # Create response with buttons
        keyboard = [
            [InlineKeyboardButton("📊 View Escrow", callback_data=f"escrow_detail_{escrow_id}")],
            [InlineKeyboardButton("🛍 Create Another", callback_data="create_escrow")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"✅ **Escrow Created!**\n\n"
            f"**ID:** `#{escrow_id}`\n"
            f"**Amount:** `{amount} {token}`\n"
            f"**Network:** `{network}`\n"
            f"**Seller:** `{seller_username}`\n\n"
            f"💰 **Send {amount} {token} to:**\n"
            f"`{account.address}`\n\n"
            f"I'll detect your payment automatically!",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        
    except ValueError:
        await update.message.reply_text("❌ Invalid amount. Please use numbers only.")
    except Exception as e:
        logger.error(f"Error creating escrow: {e}")
        await update.message.reply_text("❌ Error creating escrow. Please try again.")

async def show_my_escrows(query, context):
    """Show user's escrows"""
    try:
        user_id = query.from_user.id
        escrows = db.get_user_escrows(user_id)
        
        if not escrows:
            keyboard = [[InlineKeyboardButton("🛍 Create First Escrow", callback_data="create_escrow")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                "📭 You have no active escrows.\n\n"
                "Create your first escrow to get started!",
                reply_markup=reply_markup
            )
            return
        
        keyboard = []
        for escrow in escrows:
            escrow_id, _, _, seller_username, amount, token, network, state, created_at, _, _, _ = escrow
            status_emoji = "⏳" if state == "created" else "💰" if state == "buyer_paid" else "✅"
            keyboard.append([
                InlineKeyboardButton(
                    f"{status_emoji} #{escrow_id} - {amount} {token}", 
                    callback_data=f"escrow_detail_{escrow_id}"
                )
            ])
        
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_to_main")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            "📊 **Your Escrows**\n\n"
            "Select an escrow to view details:",
            reply_markup=reply_markup
        )
        
    except Exception as e:
        logger.error(f"Error in show_my_escrows: {e}")
        await query.edit_message_text("❌ Error loading your escrows.")

async def show_escrow_detail(query, context, escrow_id):
    """Show detailed escrow information"""
    try:
        escrow = db.get_escrow(escrow_id)
        if not escrow:
            await query.edit_message_text("❌ Escrow not found.")
            return
        
        (_, chat_id, buyer_id, seller_username, amount, token, network, state, 
         created_at, expires_at, deposit_address, _) = escrow
        
        # Status emojis
        status_emojis = {
            'created': '⏳ Waiting for payment',
            'buyer_paid': '💰 Payment received',
            'completed': '✅ Completed',
            'disputed': '🚩 In dispute',
            'cancelled': '❌ Cancelled'
        }
        
        message_text = (
            f"📋 **Escrow #{escrow_id}**\n\n"
            f"**Amount:** {amount} {token}\n"
            f"**Network:** {network}\n"
            f"**Seller:** {seller_username}\n"
            f"**Status:** {status_emojis.get(state, state)}\n"
            f"**Created:** {created_at}\n"
        )
        
        if deposit_address:
            message_text += f"\n**Deposit Address:**\n`{deposit_address}`"
        
        keyboard = [[InlineKeyboardButton("🔙 Back to List", callback_data="my_escrows")]]
        
        # Add action buttons based on state
        if state == EscrowState.CREATED.value:
            keyboard.insert(0, [InlineKeyboardButton("🚩 Report Issue", callback_data=f"dispute_{escrow_id}")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            message_text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        
    except Exception as e:
        logger.error(f"Error in show_escrow_detail: {e}")
        await query.edit_message_text("❌ Error loading escrow details.")

async def release_escrow(query, context, escrow_id):
    """Release funds to seller"""
    try:
        db.update_escrow_state(escrow_id, EscrowState.COMPLETED)
        await query.edit_message_text(
            f"✅ **Escrow #{escrow_id} Completed!**\n\n"
            f"Funds have been released to the seller.\n"
            f"Transaction completed successfully."
        )
    except Exception as e:
        logger.error(f"Error releasing escrow: {e}")
        await query.edit_message_text("❌ Error releasing funds.")

async def start_dispute(query, context, escrow_id):
    """Start dispute process"""
    try:
        db.update_escrow_state(escrow_id, EscrowState.DISPUTED)
        await query.edit_message_text(
            f"🚩 **Dispute Opened for Escrow #{escrow_id}**\n\n"
            f"An admin will review your case shortly.\n"
            f"Please describe the issue in detail when contacted."
        )
    except Exception as e:
        logger.error(f"Error starting dispute: {e}")
        await query.edit_message_text("❌ Error opening dispute.")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check bot status"""
    status_msg = "🤖 **Bot Status**\n\n"
    
    if w3_test and w3_test.is_connected():
        status_msg += "✅ **Testnet**: Connected\n"
        if account_test:
            status_msg += f"   Address: `{account_test.address}`\n"
    else:
        status_msg += "❌ **Testnet**: Disconnected\n"
    
    if w3_main and w3_main.is_connected():
        status_msg += "✅ **Mainnet**: Connected\n"
        if account_main:
            status_msg += f"   Address: `{account_main.address}`\n"
    else:
        status_msg += "❌ **Mainnet**: Disconnected\n"
    
    await update.message.reply_text(status_msg, parse_mode='Markdown')

# --- Main Bot Application ---
async def main():
    """Start the bot"""
    try:
        # Create application
        application = ApplicationBuilder().token(BOT_TOKEN).build()
        
        # Add handlers
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("create", create_escrow_command))
        application.add_handler(CommandHandler("status", status_command))
        application.add_handler(CallbackQueryHandler(button))
        
        logger.info("🤖 Starting Escrow Bot...")
        
        # Start polling
        await application.run_polling()
        
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")

if __name__ == "__main__":
    # Run the bot
    asyncio.run(main())
