# escrow_bot.py
import os
import logging
import re
from decimal import Decimal
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from sqlalchemy import create_engine, Column, Integer, String, Numeric, DateTime
from sqlalchemy.orm import sessionmaker, declarative_base
from datetime import datetime

# Load env
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
OWNER_ID = int(os.getenv("OWNER_ID", 0))  # You only
USDT_POLYGON_ADDR = os.getenv("USDT_POLYGON_ADDR", "")
CBE_ADDR = os.getenv("CBE_ADDR", "")
TELEBIRR_PHONE = os.getenv("TELEBIRR_PHONE", "")
BOT_NAME = os.getenv("BOT_NAME", "EscrowBot")
SCREENSHOT_ADMIN = os.getenv("SCREENSHOT_ADMIN", "@DNNGL")

if not TELEGRAM_TOKEN:
    raise RuntimeError("Set TELEGRAM_TOKEN in .env")

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# DB setup
engine = create_engine('sqlite:///escrow.db', connect_args={"check_same_thread": False})
Session = sessionmaker(bind=engine)
Base = declarative_base()

class Escrow(Base):
    __tablename__ = "escrows"
    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(String, nullable=False)
    creator_id = Column(String, nullable=True)
    buyer_username = Column(String, nullable=False)
    buyer_id = Column(String, nullable=False)
    seller_username = Column(String, nullable=False)
    seller_id = Column(String, nullable=False)
    amount = Column(Numeric(30, 8), nullable=False)
    currency = Column(String, default="")
    status = Column(String, default="INIT")
    seller_payment_info = Column(String, nullable=True)

class TransactionLog(Base):
    __tablename__ = "transaction_logs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    escrow_id = Column(Integer, nullable=False)
    group_id = Column(String, nullable=False)
    buyer_username = Column(String, nullable=False)
    seller_username = Column(String, nullable=False)
    amount = Column(Numeric(30, 8), nullable=False)
    currency = Column(String, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(engine)

# ---------------- HELPERS ----------------
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID

async def send_admins(app, text):
    for aid in ADMIN_IDS:
        try:
            await app.bot.send_message(chat_id=aid, text=text, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.warning("Failed notifying admin %s: %s", aid, e)

def parse_amount_token(token: str):
    token = token.strip()
    if token.startswith("$"):
        try:
            val = Decimal(token[1:])
            return val, "USD"
        except Exception:
            raise ValueError("Invalid dollar amount format. Use like $12 or $12.50")
    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)(?:\s*([A-Za-z]+))?$", token)
    if not m:
        raise ValueError("Invalid amount. Use $12 or 12ETB or 12")
    amt = Decimal(m.group(1))
    cur = (m.group(2) or "").upper()
    return amt, cur

def format_payment_instructions(amount, currency):
    if currency == "USD":
        return f"Pay *{amount} USD* as USDT (Polygon) to:\n`{USDT_POLYGON_ADDR}`\nAfter sending screenshot of payment to {SCREENSHOT_ADMIN} and say `/paid <escrow_id>` in the group."
    elif currency == "ETB":
        return (
            f"Pay *{amount} ETB* using one of these:\n"
            f"• Telebirr (phone): `{TELEBIRR_PHONE}`\n"
            f"• CBE account: `{CBE_ADDR}`\n\nAfter sending screenshot of payment to {SCREENSHOT_ADMIN} and say `/paid <escrow_id>` in the group."
        )
    else:
        return f"Amount: *{amount} {currency or ''}*\nProvide payment by mutual agreement. After paying send screenshot to {SCREENSHOT_ADMIN} and say `/paid <escrow_id>`."

def get_full_guide():
    return (
        "*Escrow Bot Guide*\n\n"
        "1. To create an escrow (must be used in a group):\n"
        "   `/escrow @buyer_username @seller_username <amount>`\n"
        "   Examples: `/escrow @alice @bob $12` or `/escrow @alice @bob 150ETB`\n\n"
        "2. After escrow creation, the bot will post payment instructions. Buyer pays off-platform using those instructions.\n\n"
        "3. When buyer pays, buyer must send in the same group:\n"
        "   `/paid <escrow_id>`\n"
        "   *Only the mentioned buyer* for that escrow can call `/paid`.\n\n"
        "4. Admins will be notified and can confirm with:\n"
        "   `/confirm <escrow_id>`\n\n"
        "5. After admin confirmation, the bot asks seller to release the item. When buyer receives the item, buyer sends:\n"
        "   `/received <escrow_id>`\n"
        "   *Only the mentioned buyer* can call `/received`.\n\n"
        "6. After buyer `/received`, seller must send to the group:\n"
        "   `/payment <escrow_id> <address_or_info>`\n"
        "   The bot will tag the username who sent it.\n\n"
        "7. Admin will be notified with the seller's payout info; after admin pays seller out-of-band, admin marks completion:\n"
        "   `/completed <escrow_id>`\n\n"
        "8. To raise a dispute (in the group):\n"
        "   `/dispute <escrow_id>`\n"
        "   The bot will tag `@DNNGL` and ask to contact admin.\n\n"
        "— Keep all payments off-chain (Telebirr/CBE/USDT) secure and provide transaction receipts to admins.\n"
    )

# ---------------- COMMAND HANDLERS ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    guide = get_full_guide()
    await update.message.reply_text(guide, parse_mode=ParseMode.MARKDOWN)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    guide = get_full_guide()
    await update.message.reply_text(guide, parse_mode=ParseMode.MARKDOWN)

async def escrow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or msg.chat.type not in ("group", "supergroup"):
        await msg.reply_text("Create escrows *in a group* using `/escrow @buyer @seller <amount>`", parse_mode=ParseMode.MARKDOWN)
        return

    args = context.args
    if len(args) < 3:
        await msg.reply_text("Usage: `/escrow @buyer @seller <amount>`", parse_mode=ParseMode.MARKDOWN)
        return

    buyer = args[0].lstrip("@")
    seller = args[1].lstrip("@")
    amount_token = " ".join(args[2:]).strip()

    try:
        amount, currency = parse_amount_token(amount_token)
    except Exception as e:
        await msg.reply_text(f"Invalid amount: {e}")
        return

    sess = Session()
    try:
        esc = Escrow(
            group_id=str(msg.chat.id),
            creator_id=str(msg.from_user.id),
            buyer_username=buyer,
            buyer_id=str(msg.from_user.id),
            seller_username=seller,
            seller_id="",
            amount=amount,
            currency=currency,
            status="INIT"
        )
        sess.add(esc)
        sess.commit()
        esc_id = esc.id

        payinstr = format_payment_instructions(amount, currency)
        reply = (
            f"🔒 *Escrow created* — ID: `{esc_id}`\n"
            f"Buyer: `@{buyer}`\nSeller: `@{seller}`\nAmount: *{amount}* {currency or ''}\n\n"
            f"{payinstr}\n\n"
            f"Buyer, after you pay, say in this group: `/paid {esc_id}`"
        )
        await msg.reply_text(reply, parse_mode=ParseMode.MARKDOWN)
    finally:
        sess.close()

# ---------------- /paid ----------------
        # ---------------- /paid ----------------
async def paid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
            msg = update.message
            args = context.args
            if len(args) < 1:
                await msg.reply_text("Usage: `/paid <escrow_id>`", parse_mode=ParseMode.MARKDOWN)
                return
            try:
                esc_id = int(args[0])
            except Exception:
                await msg.reply_text("Invalid escrow id.")
                return

            sess = Session()
            try:
                esc = sess.query(Escrow).filter_by(id=esc_id, group_id=str(msg.chat.id)).first()
                if not esc:
                    await msg.reply_text("Escrow not found in this group.")
                    return

                caller = msg.from_user
                caller_un = (caller.username or "").lower()
                # allow buyer or admin
                if caller_un != (esc.buyer_username or "").lower() and not is_admin(caller.id):
                    await msg.reply_text("Only the designated buyer can mark as paid.", parse_mode=ParseMode.MARKDOWN)
                    return

                if esc.status != "INIT":
                    await msg.reply_text(f"Escrow is not awaiting payment (status={esc.status}).", parse_mode=ParseMode.MARKDOWN)
                    return

                # update status and log transaction
                esc.status = "PAID"
                sess.commit()

                txn = TransactionLog(
                    escrow_id=esc.id,
                    group_id=esc.group_id,
                    buyer_username=esc.buyer_username,
                    seller_username=esc.seller_username,
                    amount=esc.amount,
                    currency=esc.currency
                )
                sess.add(txn)
                sess.commit()

                # Build admin mentions for GROUP message (clickable links)
                admin_mentions = []
                for aid in ADMIN_IDS:
                    try:
                        # attempt to get username for nicer label
                        admin_chat = await context.application.bot.get_chat(aid)
                        label = f"@{admin_chat.username}" if getattr(admin_chat, "username", None) else f"Admin"
                    except Exception:
                        # fallback label
                        label = "Admin"
                    # use clickable mention by id so it pings reliably in group
                    admin_mentions.append(f"[{label}](tg://user?id={aid})")
                admins_text = " ".join(admin_mentions) if admin_mentions else "Admins"

                group_title = msg.chat.title or msg.chat.id
                # Post the payment report *in the group* and mention admins
                group_report = (
                    f"🔔 *Payment reported*\n"
                    f"Escrow ID: `{esc.id}`\n"
                    f"Group: `{group_title}`\n"
                    f"Buyer: `@{esc.buyer_username}`\n"
                    f"Seller: `@{esc.seller_username}`\n"
                    f"Amount: *{esc.amount}* {esc.currency or ''}\n\n"
                    f"{admins_text} — please verify and run: `/confirm {esc.id}`"
                )

                # send the report into the group (not private)
                try:
                    await context.bot.send_message(chat_id=int(esc.group_id), text=group_report, parse_mode=ParseMode.MARKDOWN)
                except Exception as e:
                    logger.warning("Could not send group payment report: %s", e)
                    await msg.reply_text("Payment recorded but failed to notify admins in group. Please contact admins manually.")

                # confirm to caller
                await msg.reply_text(f"Buyer `@{esc.buyer_username}` reported payment for escrow `{esc.id}`. Admins have been notified in the group.", parse_mode=ParseMode.MARKDOWN)

            finally:
                sess.close()

# ---------------- /confirm ----------------
# ---------------- /confirm ----------------
async def confirm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = msg.from_user
    chat = msg.chat

    # Must run inside a group
    if chat.type not in ["group", "supergroup"]:
        await msg.reply_text("❌ This command must be used in the group.")
        return

    # Only admins allowed
    if not is_admin(user.id):
        await msg.reply_text("❌ Only admins can confirm payments.")
        return

    if len(context.args) < 1:
        await msg.reply_text("Usage: `/confirm <escrow_id>`")
        return

    try:
        esc_id = int(context.args[0])
    except Exception:
        await msg.reply_text("Invalid escrow id.")
        return

    sess = Session()
    try:
        esc = sess.query(Escrow).filter_by(id=esc_id, group_id=str(chat.id)).first()
        if not esc:
            await msg.reply_text("❌ Escrow not found in this group.")
            return

        # Only allow confirming if buyer has marked as paid
        if esc.status != "PAID":
            await msg.reply_text(f"❌ Cannot confirm. Escrow status is `{esc.status}`. Buyer must first report payment with `/paid {esc_id}`.")
            return

        # Confirm the payment
        esc.status = "CONFIRMED"
        sess.commit()

        # Notify the group and the seller
        await msg.reply_text(
            f"✅ Payment for escrow `{esc.id}` confirmed by admin.\n"
            f"Seller `@{esc.seller_username}`, please release the item to buyer `@{esc.buyer_username}`.\n"
            f"After buyer receives, they will run `/received {esc.id}`.",
            parse_mode=ParseMode.MARKDOWN
        )
    finally:
        sess.close()
    
 # ---------------- /received ----------------
async def received_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
     msg = update.message
     if len(context.args) < 1:
         await msg.reply_text("Usage: `/received <escrow_id>`", parse_mode=ParseMode.MARKDOWN)
         return
     try:
         esc_id = int(context.args[0])
     except Exception:
         await msg.reply_text("Invalid id.")
         return

     sess = Session()
     try:
         esc = sess.query(Escrow).filter_by(id=esc_id, group_id=str(msg.chat.id)).first()
         if not esc:
             await msg.reply_text("Escrow not found in this group.")
             return

         caller = msg.from_user
         caller_un = (caller.username or "").lower()

         # ✅ Allow only the buyer (and optionally admin)
         if caller_un != (esc.buyer_username or "").lower() and not is_admin(caller.id):
             await msg.reply_text("❌ Only the designated *buyer* can mark as received.", parse_mode=ParseMode.MARKDOWN)
             return

         if esc.status != "CONFIRMED":
             await msg.reply_text(f"Escrow not in CONFIRMED state (status={esc.status}).", parse_mode=ParseMode.MARKDOWN)
             return

         esc.status = "RECEIVED"
         sess.commit()

         await msg.reply_text(
             f"Buyer `@{esc.buyer_username}` confirmed receipt for escrow `{esc.id}`.\n"
             f"Seller, please send your payout info in this group using `/payment {esc.id} <address>`",
             parse_mode=ParseMode.MARKDOWN
         )
     finally:
         sess.close()

# ---------------- /payment ----------------
# ---------------- /payment ----------------
# ---------------- /payment ----------------
async def payment_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = msg.chat
    caller = msg.from_user

    if len(context.args) < 2:
        await msg.reply_text("Usage: `/payment <escrow_id> <address_or_info>`", parse_mode=ParseMode.MARKDOWN)
        return

    try:
        esc_id = int(context.args[0])
    except Exception:
        await msg.reply_text("Invalid escrow id.", parse_mode=ParseMode.MARKDOWN)
        return

    info = " ".join(context.args[1:]).strip()

    sess = Session()
    try:
        esc = sess.query(Escrow).filter_by(id=esc_id, group_id=str(chat.id)).first()
        if not esc:
            await msg.reply_text("❌ Escrow not found in this group.", parse_mode=ParseMode.MARKDOWN)
            return

        caller_un = (caller.username or "").lower()
        if caller_un != (esc.seller_username or "").lower():
            await msg.reply_text("❌ Only the designated seller can submit payment info.", parse_mode=ParseMode.MARKDOWN)
            return

        # Allow only after buyer received the item
        if esc.status != "RECEIVED":
            await msg.reply_text(f"❌ You cannot submit payment info yet. Escrow status is `{esc.status}`.", parse_mode=ParseMode.MARKDOWN)
            return

        # Update escrow
        esc.seller_payment_info = info
        esc.status = "PAYMENT_PROVIDED"
        sess.commit()

        # Mention admins in the group
        admin_mentions = []
        for aid in ADMIN_IDS:
            try:
                admin_chat = await context.application.bot.get_chat(aid)
                label = f"@{admin_chat.username}" if getattr(admin_chat, "username", None) else "Admin"
            except Exception:
                label = "Admin"
            admin_mentions.append(f"[{label}](tg://user?id={aid})")
        admins_text = " ".join(admin_mentions) if admin_mentions else "Admins"

        await msg.reply_text(
            f"💰 Seller `@{esc.seller_username}` submitted payout info for escrow `{esc.id}`.\n"
            f"Payout info: `{info}`\n"
            f"{admins_text} — please process payment off-chain and mark `/completed {esc.id}` once done.",
            parse_mode=ParseMode.MARKDOWN
        )

    finally:
        sess.close()

#completed
# ---------------- /completed ----------------
async def completed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = msg.from_user
    chat = msg.chat

    # Only admins can complete
    if not is_admin(user.id):
        await msg.reply_text("❌ Only admins can complete escrows.", parse_mode=ParseMode.MARKDOWN)
        return

    if len(context.args) < 1:
        await msg.reply_text("Usage: `/completed <escrow_id>`", parse_mode=ParseMode.MARKDOWN)
        return

    try:
        esc_id = int(context.args[0])
    except Exception:
        await msg.reply_text("Invalid escrow id.", parse_mode=ParseMode.MARKDOWN)
        return

    sess = Session()
    try:
        esc = sess.query(Escrow).filter_by(id=esc_id, group_id=str(chat.id)).first()
        if not esc:
            await msg.reply_text("❌ Escrow not found.", parse_mode=ParseMode.MARKDOWN)
            return

        # Ensure escrow has gone through all necessary steps
        if esc.status not in ["PAYMENT_PROVIDED", "RECEIVED"]:
            await msg.reply_text(f"❌ Escrow cannot be completed. Current status: `{esc.status}`", parse_mode=ParseMode.MARKDOWN)
            return

        # Mark completed
        esc.status = "COMPLETED"
        sess.commit()

        # Notify group
        await msg.reply_text(
            f"✅ Escrow `{esc.id}` completed. Seller `@{esc.seller_username}` has been paid.\n"
            f"Buyer `@{esc.buyer_username}` and admins: transaction fully done.",
            parse_mode=ParseMode.MARKDOWN
        )

    finally:
        sess.close()


# ---------------- /status ----------------
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("Usage: `/status <escrow_id>`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        esc_id = int(context.args[0])
    except Exception:
        await update.message.reply_text("Invalid id.")
        return
    sess = Session()
    try:
        esc = sess.query(Escrow).filter_by(id=esc_id).first()
        if not esc:
            await update.message.reply_text("Escrow not found.")
            return
        reply = (
            f"Escrow ID: `{esc.id}`\nGroup: `{esc.group_id}`\nBuyer: `@{esc.buyer_username}`\nSeller: `@{esc.seller_username}`\n"
            f"Amount: *{esc.amount}* {esc.currency or ''}\nStatus: *{esc.status}*\n"
            f"Seller payout info: `{esc.seller_payment_info or 'N/A'}`"
        )
        await update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN)
    finally:
        sess.close()

# ---------------- /dispute ----------------
async def dispute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if len(context.args) < 1:
        await msg.reply_text("Usage: `/dispute <escrow_id>`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        esc_id = int(context.args[0])
    except Exception:
        await msg.reply_text("Invalid id.")
        return
    sess = Session()
    try:
        esc = sess.query(Escrow).filter_by(id=esc_id).first()
        if not esc:
            await msg.reply_text("Escrow not found.")
            return
        esc.status = "DISPUTE"
        sess.commit()
        try:
            await update.message.reply_text(f"⚠️ Dispute opened for escrow `{esc_id}`. @{'d374ult'}, please intervene. Parties, contact admin.", parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await update.message.reply_text(f"⚠️ Dispute opened for escrow `{esc_id}`. Please contact admins.", parse_mode=ParseMode.MARKDOWN)

        await send_admins(context.application, f"Dispute opened for escrow {esc_id} in group {msg.chat.title or msg.chat.id}.")
    finally:
        sess.close()

# ---------------- /cap ----------------
async def cap_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.message.from_user.id):
        return

    sess = Session()
    try:
        escrows = sess.query(Escrow).filter(Escrow.status.in_(["PAID", "CONFIRMED", "RECEIVED", "PAYMENT_PROVIDED", "COMPLETED"])).all()
        total_usd = sum(esc.amount for esc in escrows if esc.currency.upper() == "USD")
        total_etb = sum(esc.amount for esc in escrows if esc.currency.upper() == "ETB")

        await update.message.reply_text(
            f"💰 Total Escrow Transactions:\n• USD/USDT: {total_usd}\n• ETB: {total_etb}",
            parse_mode=ParseMode.MARKDOWN
        )
    finally:
        sess.close()

# ---------------- MAIN ----------------
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("escrow", escrow_cmd))
    app.add_handler(CommandHandler("paid", paid_cmd))
    app.add_handler(CommandHandler("confirm", confirm_cmd))
    app.add_handler(CommandHandler("received", received_cmd))
    app.add_handler(CommandHandler("payment", payment_cmd))
    app.add_handler(CommandHandler("completed", completed_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("dispute", dispute_cmd))
    app.add_handler(CommandHandler("cap", cap_cmd))

    logger.info("Escrow Bot started.")
    app.run_polling()

if __name__ == "__main__":
    main()
