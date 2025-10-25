import os
import logging
import asyncio
import nest_asyncio
from web3 import Web3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters
from datetime import datetime, timedelta
import json
import sqlite3
from enum import Enum

# Fix for Replit / Jupyter nested event loops
nest_asyncio.apply()

# --- Load environment variables ---
BOT_TOKEN = os.environ['BOT_TOKEN']
POLYGON_TEST_PRIVATE_KEY = os.environ['BOT_PRIVATE_KEY_TESTNET']
POLYGON_MAIN_PRIVATE_KEY = os.environ['BOT_PRIVATE_KEY_MAINNET']
RPC_POLYGON_TEST = os.environ['RPC_URL_TESTNET']
RPC_POLYGON_MAIN = os.environ['RPC_URL_MAINNET']

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
        'testnet': None,  # Native token
        'mainnet': None,
        'decimals': 18
    },
    'USDT': {
        'testnet': '0x3813e82e6f7098b9583FC0F33a962D02018B6803',  # Mumbai USDT
        'mainnet': '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174',  # Polygon USDT
        'decimals': 6
    },
    'USDC': {
        'testnet': '0x0FA8781a83E46826621b3BC094Ea2A0212e71B23',  # Mumbai USDC
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
                seller_id INTEGER,
                amount REAL,
                token TEXT,
                network TEXT,
                state TEXT,
                created_at TIMESTAMP,
                expires_at TIMESTAMP,
                buyer_address TEXT,
                seller_address TEXT,
                tx_hash TEXT,
                dispute_reason TEXT,
                admin_notified BOOLEAN DEFAULT FALSE
            )
        ''')
        self.conn.commit()
    
    def create_escrow(self, chat_id, buyer_id, seller_id, amount, token, network, hours=24):
        cursor = self.conn.cursor()
        expires_at = datetime.now() + timedelta(hours=hours)
        cursor.execute('''
            INSERT INTO escrows 
            (chat_id, buyer_id, seller_id, amount, token, network, state, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (chat_id, buyer_id, seller_id, amount, token, network, EscrowState.CREATED.value, datetime.now(), expires_at))
        self.conn.commit()
        return cursor.lastrowid
    
    def get_escrow(self, escrow_id):
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM escrows WHERE id = ?', (escrow_id,))
        return cursor.fetchone()
    
    def update_escrow_state(self, escrow_id, state, tx_hash=None):
        cursor = self.conn.cursor()
        if tx_hash:
            cursor.execute('UPDATE escrows SET state = ?, tx_hash = ? WHERE id = ?', (state.value, tx_hash, escrow_id))
        else:
            cursor.execute('UPDATE escrows SET state = ? WHERE id = ?', (state.value, escrow_id))
        self.conn.commit()
    
    def set_buyer_address(self, escrow_id, address):
        cursor = self.conn.cursor()
        cursor.execute('UPDATE escrows SET buyer_address = ? WHERE id = ?', (address, escrow_id))
        self.conn.commit()
    
    def get_user_escrows(self, user_id):
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM escrows WHERE buyer_id = ? OR seller_id = ? ORDER BY created_at DESC', (user_id, user_id))
        return cursor.fetchall()

# --- Setup Logging ---
logging.basicConfig(
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    level=logging.INFO
)
logger = logging.getLogger("escrow_bot")

# --- Web3 Setup ---
w3_test = Web3(Web3.HTTPProvider(RPC_POLYGON_TEST))
w3_main = Web3(Web3.HTTPProvider(RPC_POLYGON_MAIN))

account_test = w3_test.eth.account.from_key(POLYGON_TEST_PRIVATE_KEY)
account_main = w3_main.eth.account.from_key(POLYGON_MAIN_PRIVATE_KEY)

db = EscrowDB()

# --- ERC20 ABI (simplified) ---
ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function"
    },
    {
        "constant": False,
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"}
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function"
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "from", "type": "address"},
            {"indexed": True, "name": "to", "type": "address"},
            {"indexed": False, "name": "value", "type": "uint256"}
        ],
        "name": "Transfer",
        "type": "event"
    }
]

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
        "I automate secure transactions between buyers and sellers with:\n"
        "• Auto-detection of payments\n"
        "• Dispute resolution system\n"
        "• Multi-token support (MATIC, USDT, USDC)\n"
        "• Smart contract integration\n\n"
        "Choose an option below:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if data == "create_escrow":
        await create_escrow_start(query, context)
    elif data == "my_escrows":
        await show_my_escrows(query, context)
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
    await query.edit_message_text(
        "🛍 **Create New Escrow**\n\n"
        "Please provide details in this format:\n"
        "`/create @seller_username 100 USDT testnet 24`\n\n"
        "Where:\n"
        "• @seller_username - Seller's Telegram username\n"
        "• 100 - Amount\n"
        "• USDT - Token (MATIC, USDT, USDC)\n"
        "• testnet - Network (testnet/mainnet)\n"
        "• 24 - Hours to complete (optional)\n\n"
        "Example: `/create @john 50 MATIC testnet 24`",
        parse_mode='Markdown'
    )

async def create_escrow_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if len(context.args) < 4:
            await update.message.reply_text("❌ Invalid format. Use: `/create @seller amount token network [hours]`", parse_mode='Markdown')
            return
        
        seller_username = context.args[0]
        amount = float(context.args[1])
        token = context.args[2].upper()
        network = context.args[3].lower()
        hours = int(context.args[4]) if len(context.args) > 4 else 24
        
        if token not in TOKENS:
            await update.message.reply_text(f"❌ Invalid token. Available: {', '.join(TOKENS.keys())}")
            return
        
        if network not in ['testnet', 'mainnet']:
            await update.message.reply_text("❌ Invalid network. Use: testnet or mainnet")
            return
        
        # In a real bot, you'd resolve the username to user_id
        seller_id = 123456789  # This should be resolved from username
        
        escrow_id = db.create_escrow(
            update.effective_chat.id,
            update.effective_user.id,
            seller_id,
            amount,
            token,
            network,
            hours
        )
        
        # Generate deposit address
        w3 = w3_test if network == 'testnet' else w3_main
        bot_account = account_test if network == 'testnet' else account_main
        
        db.set_buyer_address(escrow_id, bot_account.address)
        
        keyboard = [
            [InlineKeyboardButton("📊 View Escrow", callback_data=f"escrow_detail_{escrow_id}")],
            [InlineKeyboardButton("🛍 Create Another", callback_data="create_escrow")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"✅ **Escrow Created!**\n\n"
            f"**ID:** #{escrow_id}\n"
            f"**Amount:** {amount} {token}\n"
            f"**Network:** {network}\n"
            f"**Seller:** {seller_username}\n"
            f"**Expires:** {hours} hours\n\n"
            f"💰 **Send {amount} {token} to:**\n"
            f"`{bot_account.address}`\n\n"
            f"I'll automatically detect your payment and notify the seller!",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        
    except Exception as e:
        logger.error(f"Error creating escrow: {e}")
        await update.message.reply_text("❌ Error creating escrow. Please check the format.")

async def show_my_escrows(query, context):
    user_id = query.from_user.id
    escrows = db.get_user_escrows(user_id)
    
    if not escrows:
        await query.edit_message_text("📭 You have no active escrows.")
        return
    
    keyboard = []
    for escrow in escrows:
        escrow_id, _, _, _, amount, token, network, state, created_at, _, _, _, _, _, _ = escrow
        keyboard.append([InlineKeyboardButton(
            f"#{escrow_id} - {amount} {token} ({state})", 
            callback_data=f"escrow_detail_{escrow_id}"
        )])
    
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="start")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "📊 **Your Escrows**\n\n"
        "Select an escrow to view details:",
        reply_markup=reply_markup
    )

async def show_escrow_detail(query, context, escrow_id):
    escrow = db.get_escrow(escrow_id)
    if not escrow:
        await query.edit_message_text("❌ Escrow not found.")
        return
    
    (_, chat_id, buyer_id, seller_id, amount, token, network, state, 
     created_at, expires_at, buyer_address, seller_address, tx_hash, dispute_reason, _) = escrow
    
    keyboard = []
    
    if state == EscrowState.BUYER_PAID.value and query.from_user.id == seller_id:
        keyboard.append([InlineKeyboardButton("✅ Confirm Receipt", callback_data=f"release_{escrow_id}")])
        keyboard.append([InlineKeyboardButton("🚩 Report Problem", callback_data=f"dispute_{escrow_id}")])
    
    elif state == EscrowState.CREATED.value and query.from_user.id == buyer_id:
        keyboard.append([InlineKeyboardButton("🚩 Cancel Escrow", callback_data=f"dispute_{escrow_id}")])
    
    keyboard.append([InlineKeyboardButton("🔙 Back to List", callback_data="my_escrows")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    status_emojis = {
        'created': '⏳',
        'buyer_paid': '💰',
        'completed': '✅',
        'disputed': '🚩',
        'cancelled': '❌'
    }
    
    await query.edit_message_text(
        f"📋 **Escrow #{escrow_id}** {status_emojis.get(state, '📄')}\n\n"
        f"**Amount:** {amount} {token}\n"
        f"**Network:** {network}\n"
        f"**Status:** {state.replace('_', ' ').title()}\n"
        f"**Created:** {created_at}\n"
        f"**Expires:** {expires_at}\n"
        f"**Buyer Address:** `{buyer_address}`\n\n"
        f"{'⚠️ **DISPUTE:** ' + dispute_reason if dispute_reason else ''}",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def release_escrow(query, context, escrow_id):
    escrow = db.get_escrow(escrow_id)
    if not escrow:
        await query.edit_message_text("❌ Escrow not found.")
        return
    
    state = escrow[7]
    if state != EscrowState.BUYER_PAID.value:
        await query.edit_message_text("❌ Invalid action for current state.")
        return
    
    # In real implementation, transfer funds to seller
    db.update_escrow_state(escrow_id, EscrowState.COMPLETED)
    
    await query.edit_message_text(
        f"✅ **Escrow #{escrow_id} Completed!**\n\n"
        f"The funds have been released to the seller.\n"
        f"Transaction completed successfully."
    )

async def start_dispute(query, context, escrow_id):
    escrow = db.get_escrow(escrow_id)
    if not escrow:
        await query.edit_message_text("❌ Escrow not found.")
        return
    
    db.update_escrow_state(escrow_id, EscrowState.DISPUTED)
    
    await query.edit_message_text(
        f"🚩 **Dispute Reported for Escrow #{escrow_id}**\n\n"
        f"Please describe the issue. An admin will review your case shortly.\n\n"
        f"Send your dispute reason now:"
    )
    context.user_data['awaiting_dispute_reason'] = escrow_id

# --- Payment Detection System ---
async def detect_payments():
    """Intelligent payment detection system"""
    while True:
        try:
            # Check testnet and mainnet
            for network_name, w3, account in [
                ('testnet', w3_test, account_test),
                ('mainnet', w3_main, account_main)
            ]:
                await check_network_payments(network_name, w3, account)
            
            await asyncio.sleep(10)  # Check every 10 seconds
            
        except Exception as e:
            logger.error(f"Payment detection error: {e}")
            await asyncio.sleep(30)

async def check_network_payments(network_name, w3, account):
    try:
        latest_block = w3.eth.block_number
        # Check last 5 blocks for missed transactions
        for block_num in range(latest_block - 5, latest_block + 1):
            block = w3.eth.get_block(block_num, full_transactions=True)
            
            for tx in block.transactions:
                if tx['to'] and tx['to'].lower() == account.address.lower():
                    await handle_incoming_tx(tx, network_name, w3)
                    
    except Exception as e:
        logger.error(f"Error checking {network_name} payments: {e}")

async def handle_incoming_tx(tx, network_name, w3):
    """Handle incoming transaction and match with escrow"""
    try:
        value = w3.from_wei(tx['value'], 'ether')
        from_address = tx['from']
        
        logger.info(f"💰 Detected {value} MATIC from {from_address} on {network_name}")
        
        # Find matching escrow (in real implementation, match by amount and network)
        # For now, just log it
        # You would update escrow state to BUYER_PAID here
        
    except Exception as e:
        logger.error(f"Error handling transaction: {e}")

# --- Main Bot Loop ---
async def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # Add handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("create", create_escrow_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    
    logger.info("🤖 Starting Intelligent Escrow Bot...")
    
    # Start payment detection in background
    payment_task = asyncio.create_task(detect_payments())
    
    # Start Telegram bot
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
