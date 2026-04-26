import sys
import os
import json
from datetime import datetime
from flask import Flask, request, Response
from dotenv import load_dotenv
from telegram import Update, LabeledPrice, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, PreCheckoutQueryHandler, CallbackQueryHandler
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, JSON, Enum, func
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import enum

# ========== HARDCODED SETTINGS ==========
BOT_TOKEN = "8670790936:AAGrR4VaeKXIrB5fTE8vb5LUPSw2oU6keqk"
ADMIN_USER_ID = 661892014
WEBAPP_URL = "https://kimhongy.github.io/luong/"
DATABASE_URL = "sqlite:////tmp/shop.db"

# ========== DATABASE ==========
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

class OrderStatus(enum.Enum):
    PENDING = "pending"
    PAID = "paid"
    CANCELLED = "cancelled"
    DELIVERED = "delivered"

class Product(Base):
    __tablename__ = 'products'
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    description = Column(String(500))
    price = Column(Float, nullable=False)
    stars_price = Column(Integer, nullable=False)
    image_url = Column(String(500))
    stock = Column(Integer, default=0)
    category = Column(String(50))
    is_active = Column(Integer, default=1)
    rating = Column(Float, default=0.0)
    total_reviews = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

class Order(Base):
    __tablename__ = 'orders'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    username = Column(String(100))
    first_name = Column(String(100))
    items = Column(JSON, nullable=False)
    total_amount = Column(Float, nullable=False)
    status = Column(Enum(OrderStatus), default=OrderStatus.PENDING)
    telegram_payment_id = Column(String(100))
    created_at = Column(DateTime, default=datetime.utcnow)
    paid_at = Column(DateTime)

class Voucher(Base):
    __tablename__ = 'vouchers'
    id = Column(Integer, primary_key=True)
    code = Column(String(20), unique=True, nullable=False)
    discount_percent = Column(Integer)
    discount_amount = Column(Float)
    max_uses = Column(Integer, default=100)
    current_uses = Column(Integer, default=0)
    is_active = Column(Integer, default=1)

class Notification(Base):
    __tablename__ = 'notifications'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    title = Column(String(200), nullable=False)
    message = Column(String(1000), nullable=False)
    type = Column(String(50))
    is_read = Column(Integer, default=0)
    related_order_id = Column(Integer)
    created_at = Column(DateTime, default=datetime.utcnow)

class Review(Base):
    __tablename__ = 'reviews'
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, nullable=False)
    user_id = Column(Integer, nullable=False)
    username = Column(String(100))
    first_name = Column(String(100))
    order_id = Column(Integer)
    rating = Column(Integer, nullable=False)
    comment = Column(String(1000))
    is_verified = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ========== SERVICES ==========
class InventoryManager:
    @staticmethod
    def check_stock(product_id, quantity_needed):
        db = next(get_db())
        product = db.query(Product).get(product_id)
        if not product: return {'available': False, 'message': 'Product not found'}
        if product.stock < quantity_needed: return {'available': False, 'message': f'Only {product.stock} left'}
        return {'available': True}

    @staticmethod
    def reserve_stock(order):
        db = next(get_db())
        for item in order.items:
            product = db.query(Product).get(item['id'])
            if product: product.stock -= item['quantity']
        db.commit()

class NotificationService:
    @staticmethod
    async def send_notification(context, user_id, title, message, ntype="system"):
        try:
            db = next(get_db())
            db.add(Notification(user_id=user_id, title=title, message=message, type=ntype))
            db.commit()
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("📋 View Orders", callback_data="orders")]]) if ntype == "order_update" else None
            await context.bot.send_message(chat_id=user_id, text=f"🔔 *{title}*\n\n{message}", parse_mode='Markdown', reply_markup=keyboard)
        except Exception as e:
            print(f"Notify failed: {e}")

    @staticmethod
    async def notify_admin(context, title, message):
        if ADMIN_USER_ID:
            await context.bot.send_message(chat_id=ADMIN_USER_ID, text=f"👑 *{title}*\n\n{message}", parse_mode='Markdown')

    @staticmethod
    async def notify_order_update(context, order, old_status, new_status):
        messages = {'paid': 'Payment received!', 'delivered': 'Order delivered!', 'cancelled': 'Order cancelled.'}
        msg = messages.get(new_status, f'Order #{order.id} status: {new_status}')
        await NotificationService.send_notification(context=context, user_id=order.user_id, title=f'Order #{order.id}', message=msg, ntype='order_update')

# ========== BOT HANDLERS ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🛍️ Open Shop", web_app=WebAppInfo(url=WEBAPP_URL))],
        [InlineKeyboardButton("📊 Dashboard", web_app=WebAppInfo(url=f"{WEBAPP_URL}/dashboard.html")),
         InlineKeyboardButton("🔍 Search", web_app=WebAppInfo(url=f"{WEBAPP_URL}/search.html"))],
        [InlineKeyboardButton("ℹ️ Help", callback_data="help")]
    ]
    await update.message.reply_text("🌟 *Welcome to Mini Shop!*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = json.loads(update.effective_message.web_app_data.data)
    user = update.effective_user
    if data.get('action') == 'create_invoice':
        payload = data.get('payload', {})
        items = payload.get('items', [])
        total_amount = payload.get('totalAmount', 0)
        voucher_code = payload.get('voucherCode')
        
        for item in items:
            result = InventoryManager.check_stock(item['id'], item['quantity'])
            if not result['available']:
                await update.effective_message.reply_text(f"❌ {item['name']}: {result['message']}")
                return
        
        db = next(get_db())
        order = Order(user_id=user.id, username=user.username, first_name=user.first_name, items=items, total_amount=total_amount)
        db.add(order)
        db.commit()
        
        discount = 0
        if voucher_code:
            voucher = db.query(Voucher).filter(Voucher.code == voucher_code, Voucher.is_active == 1, Voucher.current_uses < Voucher.max_uses).first()
            if voucher:
                discount = (total_amount * voucher.discount_percent / 100) if voucher.discount_percent else (voucher.discount_amount or 0)
                voucher.current_uses += 1
                db.commit()
        
        InventoryManager.reserve_stock(order)
        
        stars_total = sum([item.get('stars_price', 0) * item['quantity'] for item in items])
        if discount > 0 and voucher_code:
            v = db.query(Voucher).filter(Voucher.code == voucher_code).first()
            if v and v.discount_percent:
                stars_total = max(1, int(stars_total * (1 - v.discount_percent / 100)))
        
        title = "Mini Shop Order"
        description = "\n".join([f"• {item['name']} x{item['quantity']} - ${item['price'] * item['quantity']:.2f}" for item in items])
        if discount > 0: description += f"\n\nDiscount: -${discount:.2f}"
        description += f"\n\nTotal: ${total_amount - discount:.2f}"
        
        await context.bot.send_invoice(chat_id=user.id, title=title, description=description, payload=f"order_{order.id}", provider_token="", currency="XTR", prices=[LabeledPrice("Total", stars_total)], start_parameter="shop_order", need_name=True, need_phone_number=True)

async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.effective_message.successful_payment
    order_id = int(payment.invoice_payload.split('_')[1])
    db = next(get_db())
    order = db.query(Order).get(order_id)
    if order:
        order.status = OrderStatus.PAID
        order.telegram_payment_id = payment.telegram_payment_charge_id
        order.paid_at = datetime.utcnow()
        db.commit()
        await NotificationService.notify_admin(context=context, title='🎉 New Order!', message=f'Customer: {order.first_name}\nOrder: #{order.id}\nTotal: ${order.total_amount:.2f}')
        await NotificationService.notify_order_update(context=context, order=order, old_status='pending', new_status='paid')
        if order.total_amount >= 50:
            await NotificationService.send_notification(context=context, user_id=order.user_id, title='🎁 Special Offer!', message='Use code THANKYOU10 for 10% off next order!', ntype='promotion')
        await update.effective_message.reply_text(f"✅ *Payment Successful!*\n\nOrder: #{order.id}\nThank you! 🙏", parse_mode='Markdown')

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("⛔ Unauthorized")
        return
    await update.message.reply_text("Admin Panel:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ Admin Panel", web_app=WebAppInfo(url=f"{WEBAPP_URL}/admin.html"))]]))

async def help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.message.reply_text("🛍️ *Commands*\n/start - Open shop\n/admin - Admin panel", parse_mode='Markdown')

# ========== FLASK APP WITH WEBHOOK ==========
app = Flask(__name__)

ptb_app = Application.builder().token(BOT_TOKEN).build()
ptb_app.add_handler(CommandHandler("start", start))
ptb_app.add_handler(CommandHandler("admin", admin_command))
ptb_app.add_handler(CallbackQueryHandler(help_callback, pattern="help"))
ptb_app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, web_app_data))
ptb_app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
ptb_app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))

@app.route('/')
def health_check():
    return Response('OK', status=200)

@app.route('/webhook', methods=['POST'])
def webhook():
    if request.method == 'POST':
        update = Update.de_json(request.get_json(force=True), ptb_app.bot)
        ptb_app.update_queue.put_nowait(update)
    return Response('ok', status=200)

# ========== API ROUTES (សម្រាប់ Frontend) ==========
from flask import request as flask_request, jsonify
from models import Product as DB_Product, Order as DB_Order, Voucher as DB_Voucher, Review as DB_Review
from flask_cors import CORS
import base64
from sqlalchemy import func

CORS(app)

@app.route('/api/products', methods=['GET'])
def get_products():
    db = next(get_db())
    products = db.query(DB_Product).filter(DB_Product.is_active == 1).all()
    return jsonify([{'id': p.id, 'name': p.name, 'description': p.description, 'price': p.price, 'stars_price': p.stars_price, 'image_url': p.image_url, 'stock': p.stock, 'category': p.category, 'rating': p.rating, 'total_reviews': p.total_reviews} for p in products])

@app.route('/api/products', methods=['POST'])
def add_product():
    db = next(get_db())
    data = flask_request.json
    product = DB_Product(name=data['name'], description=data.get('description', ''), price=data['price'], stars_price=data['stars_price'], image_url=data.get('image_url', ''), stock=data.get('stock', 0), category=data.get('category', 'General'))
    db.add(product)
    db.commit()
    return jsonify({'message': 'Added', 'id': product.id}), 201

@app.route('/api/products/<int:pid>', methods=['GET'])
def get_product(pid):
    db = next(get_db())
    p = db.query(DB_Product).get(pid)
    if not p: return jsonify({'error': 'Not found'}), 404
    return jsonify({'id': p.id, 'name': p.name, 'description': p.description, 'price': p.price, 'stars_price': p.stars_price, 'image_url': p.image_url, 'stock': p.stock, 'category': p.category, 'rating': p.rating, 'total_reviews': p.total_reviews})

@app.route('/api/products/<int:pid>', methods=['DELETE'])
def delete_product(pid):
    db = next(get_db())
    p = db.query(DB_Product).get(pid)
    if p: p.is_active = 0; db.commit()
    return jsonify({'message': 'Deleted'})

@app.route('/api/products/search', methods=['GET'])
def search_products():
    db = next(get_db())
    q = flask_request.args.get('q', '')
    query = db.query(DB_Product).filter(DB_Product.is_active == 1)
    if q: query = query.filter(DB_Product.name.ilike(f'%{q}%') | DB_Product.description.ilike(f'%{q}%'))
    return jsonify([{'id': p.id, 'name': p.name, 'price': p.price, 'stars_price': p.stars_price, 'image_url': p.image_url, 'stock': p.stock, 'category': p.category, 'rating': p.rating} for p in query.limit(30).all()])

@app.route('/api/categories', methods=['GET'])
def get_categories():
    db = next(get_db())
    return jsonify([c[0] for c in db.query(DB_Product.category).filter(DB_Product.is_active == 1).distinct().all() if c[0]])

@app.route('/api/orders', methods=['GET'])
def get_orders():
    db = next(get_db())
    orders = db.query(DB_Order).order_by(DB_Order.created_at.desc()).all()
    return jsonify([{'id': o.id, 'user_id': o.user_id, 'username': o.username, 'first_name': o.first_name, 'items': o.items, 'total_amount': o.total_amount, 'status': o.status.value, 'created_at': o.created_at.isoformat()} for o in orders])

@app.route('/api/customer/orders', methods=['GET'])
def customer_orders():
    uid = flask_request.args.get('user_id', type=int)
    db = next(get_db())
    orders = db.query(DB_Order).filter(DB_Order.user_id == uid).order_by(DB_Order.created_at.desc()).all()
    return jsonify([{'id': o.id, 'items': o.items, 'total_amount': o.total_amount, 'status': o.status.value, 'created_at': o.created_at.isoformat()} for o in orders])

@app.route('/api/customer/stats', methods=['GET'])
def customer_stats():
    uid = flask_request.args.get('user_id', type=int)
    db = next(get_db())
    orders = db.query(DB_Order).filter(DB_Order.user_id == uid).all()
    total = sum(o.total_amount for o in orders if o.status in [OrderStatus.PAID, OrderStatus.DELIVERED])
    done = len([o for o in orders if o.status in [OrderStatus.PAID, OrderStatus.DELIVERED]])
    return jsonify({'total_spent': total, 'total_orders': len(orders), 'completed_orders': done, 'pending_orders': len([o for o in orders if o.status == OrderStatus.PENDING]), 'average_order_value': total / done if done > 0 else 0})

@app.route('/api/reviews', methods=['POST'])
def add_review():
    data = flask_request.json
    db = next(get_db())
    review = DB_Review(product_id=data['product_id'], user_id=data['user_id'], username=data.get('username'), first_name=data.get('first_name'), rating=data['rating'], comment=data.get('comment', ''))
    db.add(review)
    avg_rating = db.query(func.avg(DB_Review.rating)).filter(DB_Review.product_id == data['product_id']).scalar() or 0
    total_reviews = db.query(func.count(DB_Review.id)).filter(DB_Review.product_id == data['product_id']).scalar() or 0
    product = db.query(DB_Product).get(data['product_id'])
    if product:
        product.rating = round(float(avg_rating), 1)
        product.total_reviews = total_reviews
    db.commit()
    return jsonify({'message': 'Added'}), 201

@app.route('/api/reviews/<int:pid>', methods=['GET'])
def get_reviews(pid):
    db = next(get_db())
    avg = db.query(func.avg(DB_Review.rating)).filter(DB_Review.product_id == pid).scalar() or 0
    reviews = db.query(DB_Review).filter(DB_Review.product_id == pid).order_by(DB_Review.created_at.desc()).limit(20).all()
    return jsonify({'average_rating': round(float(avg), 1), 'total_reviews': len(reviews), 'reviews': [{'id': r.id, 'first_name': r.first_name or 'Anonymous', 'rating': r.rating, 'comment': r.comment, 'created_at': r.created_at.isoformat()} for r in reviews]})

@app.route('/api/vouchers', methods=['GET'])
def get_vouchers():
    db = next(get_db())
    return jsonify([{'id': v.id, 'code': v.code, 'discount_percent': v.discount_percent, 'discount_amount': v.discount_amount, 'max_uses': v.max_uses, 'current_uses': v.current_uses} for v in db.query(DB_Voucher).all()])

@app.route('/api/vouchers', methods=['POST'])
def add_voucher():
    db = next(get_db())
    data = flask_request.json
    v = DB_Voucher(code=data['code'], discount_percent=data.get('discount_percent'), discount_amount=data.get('discount_amount'), max_uses=data.get('max_uses', 100))
    db.add(v)
    db.commit()
    return jsonify({'message': 'Created', 'id': v.id}), 201

@app.route('/api/vouchers/validate', methods=['POST'])
def validate_voucher():
    db = next(get_db())
    v = db.query(DB_Voucher).filter(DB_Voucher.code == flask_request.json.get('code'), DB_Voucher.is_active == 1, DB_Voucher.current_uses < DB_Voucher.max_uses).first()
    if v: return jsonify({'valid': True, 'discount_percent': v.discount_percent, 'discount_amount': v.discount_amount})
    return jsonify({'valid': False}), 404

# ========== STARTUP ==========
def set_webhook():
    render_url = os.environ.get('RENDER_EXTERNAL_URL')
    if render_url:
        webhook_url = f"{render_url}/webhook"
        ptb_app.bot.delete_webhook()
        ptb_app.bot.set_webhook(url=webhook_url)
        print(f"Webhook set to: {webhook_url}")

if __name__ == '__main__':
    set_webhook()
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)
