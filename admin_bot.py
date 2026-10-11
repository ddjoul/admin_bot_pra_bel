import asyncio
import hashlib
import json
import logging
import os
import secrets
import signal
from urllib.parse import urlparse

from aiohttp import web
from aiogram.exceptions import TelegramBadRequest
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv
from supabase import create_client

load_dotenv(Path(__file__).resolve().parent / '.env', encoding='utf-8-sig')
required = ('ADMIN_BOT_TOKEN', 'ADMIN_TELEGRAM_ID', 'SUPABASE_URL', 'SUPABASE_SECRET_KEY')
missing = [k for k in required if not os.getenv(k, '').strip()]
if missing:
    raise SystemExit('Заполни .env: ' + ', '.join(missing))
ADMIN = int(os.environ['ADMIN_TELEGRAM_ID'])
bot = Bot(os.environ['ADMIN_BOT_TOKEN'].strip())
db = create_client(os.environ['SUPABASE_URL'].strip(), os.environ['SUPABASE_SECRET_KEY'].strip())
dp = Dispatcher()
lock = asyncio.Lock()
statuses = {}
PAGE = 5
menu = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text='комментарии'), KeyboardButton(text='стихи / переводы')],
    [KeyboardButton(text='статистика')],
], resize_keyboard=True, is_persistent=True)

async def query(build):
    return await asyncio.to_thread(lambda: build().execute().data)

def table(kind):
    return 'comments' if kind == 'c' else 'posts'

def waiting(kind):
    return 'pending' if kind == 'c' else 'draft'

def snapshot(kind, row):
    keys = (['post_id', 'display_name', 'body', 'status', 'status_id', 'created_at'] if kind == 'c' else
            ['author_id', 'title', 'body', 'category', 'category_id', 'language_code', 'language_id',
             'original_author', 'original_language', 'status', 'status_id', 'created_at', 'published_at'])
    return {k: row.get(k) for k in keys}

def digest(kind, row):
    raw = json.dumps(snapshot(kind, row), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]

def kb(options):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in line]
        for line in options
    ])

async def get_row(kind, rid):
    rows = await query(lambda: db.table(table(kind)).select('*').eq('id', rid).limit(1))
    return rows[0] if rows else None

async def send_chunks(chat, text):
    text = text or '(пустой текст)'
    for i in range(0, len(text), 1800):
        await bot.send_message(chat, text[i:i+1800])

async def show_item(chat, kind, row, confirm=None):
    h = digest(kind, row)
    rid = row['id']
    if kind == 'c':
        posts = await query(lambda: db.table('posts').select('title').eq('id', row['post_id']).limit(1))
        title = posts[0].get('title') if posts else '(произведение не найдено)'
        header = f"комментарий\nимя: {row['display_name']}\nк произведению: {title}\nID: {rid}"
    else:
        authors = await query(lambda: db.table('authors').select('name').eq('id', row['author_id']).limit(1))
        author = authors[0]['name'] if authors else '(профиль не найден)'
        category = 'пераклад' if row['category'] == 'translation' else 'верш'
        header = f"{category}\nназвание: {row.get('title') or '***'}\nавтор / переводчик: {author}\nязык: {row.get('language_code')}\nID: {rid}"
        if row['category'] == 'translation':
            header += f"\nавтор оригинала: {row.get('original_author')}\nязык оригинала: {row.get('original_language')}"
    await send_chunks(chat, header)
    await send_chunks(chat, row.get('body'))
    if confirm:
        decision = 'опубликовать' if confirm == 'a' else 'отклонить'
        text = f'{decision} эту запись?'
        buttons = [[('да, ' + decision, f'{kind}:y{confirm}:{rid}:{h}')],
                   [('назад к решению', f'{kind}:v:{rid}:{h}')]]
    else:
        text = 'выбери действие для записи выше:'
        buttons = [[('опубликовать', f'{kind}:a:{rid}:{h}'), ('отклонить', f'{kind}:r:{rid}:{h}')]]
    await bot.send_message(chat, text, reply_markup=kb(buttons))

async def queue(chat, kind, offset=0):
    def build():
        q = db.table(table(kind)).select('*').eq('status', waiting(kind))
        if kind == 'p':
            q = q.in_('category', ['poem', 'translation'])
        return q.order('created_at').order('id').range(offset, offset+PAGE)
    rows = await query(build)
    if not rows:
        await bot.send_message(chat, 'очередь пуста.' if offset == 0 else 'здесь записей больше нет. обнови очередь.', reply_markup=menu)
        return
    await bot.send_message(chat, 'очередь комментариев:' if kind == 'c' else 'очередь стихов и переводов:', reply_markup=menu)
    for row in rows[:PAGE]:
        await show_item(chat, kind, row)
    options = [[('обновить с начала', f'q:{kind}:0')]]
    if len(rows)>PAGE:
        options.insert(0, [('следующие 5', f'q:{kind}:{offset+PAGE}')])
    await bot.send_message(chat, 'на странице максимум 5 записей. после решений обнови очередь.', reply_markup=kb(options))

async def stats(chat):
    lines = []
    for kind, code, label in [('c','pending','комментарии на проверке'),('p','draft','стихи / переводы на проверке')]:
        def build():
            q = db.table(table(kind)).select('id', count='exact', head=True).eq('status',code)
            return q.in_('category',['poem','translation']) if kind == 'p' else q
        count = await asyncio.to_thread(lambda: build().execute().count)
        lines.append(f'{label}: {count}')
    await bot.send_message(chat, '\n'.join(lines), reply_markup=menu)

@dp.message()
async def message(m: Message):
    if not m.from_user or m.from_user.id != ADMIN or m.chat.type != 'private':
        return
    async with lock:
        text = (m.text or '').strip()
        if text == 'комментарии' or text == '/comments':
            await queue(m.chat.id,'c')
        elif text == 'стихи / переводы' or text == '/posts':
            await queue(m.chat.id,'p')
        elif text == 'статистика' or text == '/stats':
            await stats(m.chat.id)
        else:
            await m.answer('админ-панель пра.бел\n/comments — комментарии\n/posts — стихи и переводы\n/stats — очередь\n\nотклонение не удаляет запись: она остаётся в базе со статусом rejected.', reply_markup=menu)

@dp.callback_query(F.data)
async def callback(c: CallbackQuery):
    if c.from_user.id != ADMIN or not c.message or c.message.chat.type != 'private':
        await c.answer('нет доступа', show_alert=True)
        return
    try:
        await c.answer()
    except TelegramBadRequest as exc:
        if 'query is too old' not in str(exc).lower() and 'query id is invalid' not in str(exc).lower():
            raise
    async with lock:
        parts = c.data.split(':')
        if len(parts)==3 and parts[0]=='q' and parts[1] in ('c','p'):
            try:
                offset = int(parts[2])
            except ValueError:
                return
            if offset < 0:
                return
            await queue(ADMIN,parts[1],offset)
            return
        if len(parts)!=4:
            return
        kind, action, rid, oldhash = parts
        if kind not in ('c','p') or action not in ('v','a','r','ya','yr'):
            return
        row = await get_row(kind,rid)
        if not row or row.get('status') != waiting(kind):
            await bot.send_message(ADMIN,'запись уже обработана или удалена. обнови очередь.')
            return
        if kind=='p' and row.get('category') not in ('poem','translation'):
            return
        if action == 'v':
            await show_item(ADMIN,kind,row)
            return
        if digest(kind,row)!=oldhash:
            await bot.send_message(ADMIN,'запись изменилась после просмотра. проверь актуальную версию:')
            await show_item(ADMIN,kind,row)
            return
        if action in ('a','r'):
            await show_item(ADMIN,kind,row,confirm=action)
            return
        target = 'published' if action == 'ya' else 'rejected'
        payload = {'status':target,'status_id':statuses[kind][target]}
        if kind=='p':
            payload['published_at'] = datetime.now(timezone.utc).isoformat() if target=='published' else None
        def update():
            q = db.table(table(kind)).update(payload).eq('id',rid)
            # Optimistic comparison: publish only the same version that was reviewed.
            for field,value in snapshot(kind,row).items():
                q = q.is_(field,'null') if value is None else q.eq(field,value)
            return q
        result = await query(update)
        if not result:
            await bot.send_message(ADMIN,'запись изменилась или обновление не подтвердилось. обнови очередь и проверь статус.')
            return
        if c.message:
            await c.message.edit_reply_markup(reply_markup=None)
        await bot.send_message(ADMIN,('опубликовано' if target=='published' else 'отклонено')+'\nID: '+rid,reply_markup=menu)

@dp.errors()
async def errors(event):
    logging.error('Moderation error: %s',type(event.exception).__name__)
    u = event.update
    uid = u.message.from_user.id if u.message and u.message.from_user else u.callback_query.from_user.id if u.callback_query else None
    if uid == ADMIN:
        try:
            await bot.send_message(ADMIN,'не удалось выполнить действие. обнови очередь и проверь текущий статус перед повтором.')
        except Exception:
            pass
    return True

async def load_statuses():
    loaded = {}
    for kind, name in [('c', 'comment_statuses'), ('p', 'post_statuses')]:
        rows = await query(lambda name=name: db.table(name).select('id,code'))
        loaded[kind] = {x['code']: x['id'] for x in rows}
        needed = {waiting(kind), 'published', 'rejected'}
        if not needed.issubset(loaded[kind]):
            raise RuntimeError(
                f'В {name} отсутствуют статусы {needed - set(loaded[kind])}. '
                'Выполни setup_admin.sql'
            )
    statuses.update(loaded)


async def startup_retry(operation, label):
    for attempt in range(1, 9):
        try:
            return await operation()
        except (RuntimeError, TelegramBadRequest):
            raise
        except Exception as exc:
            logging.warning('%s: %s; attempt %s/8', label, type(exc).__name__, attempt)
            if attempt == 8:
                raise
            await asyncio.sleep(min(2 ** (attempt - 1), 20))


async def main():
    logging.basicConfig(level=logging.INFO)
    mode = os.getenv('BOT_MODE', 'auto').strip().lower()
    base_url = (
        os.getenv('WEBHOOK_BASE_URL', '').strip()
        or os.getenv('RENDER_EXTERNAL_URL', '').strip()
    ).rstrip('/')
    if mode not in {'auto', 'webhook', 'polling'}:
        raise RuntimeError('BOT_MODE: auto, webhook или polling')
    use_webhook = mode == 'webhook' or (mode == 'auto' and bool(base_url))
    if use_webhook:
        parsed = urlparse(base_url)
        if parsed.scheme != 'https' or not parsed.netloc or parsed.query or parsed.fragment:
            raise RuntimeError('Укажи WEBHOOK_BASE_URL=https://имя-сервиса.onrender.com')

    state = {'ready': False}
    webhook_path = '/telegram/webhook'
    secret = os.getenv('WEBHOOK_SECRET', '').strip() or secrets.token_urlsafe(32)

    @web.middleware
    async def readiness(request, handler):
        if request.path == webhook_path and not state['ready']:
            return web.Response(status=503, text='Starting')
        return await handler(request)

    async def health(request):
        return web.json_response({'service': 'admin_bot', 'ready': state['ready']})

    async def ready(request):
        return web.json_response(
            {'ready': state['ready']}, status=200 if state['ready'] else 503
        )

    app = web.Application(middlewares=[readiness])
    app.router.add_get('/', health)
    app.router.add_get('/health', health)
    app.router.add_get('/ready', ready)
    if use_webhook:
        SimpleRequestHandler(
            dispatcher=dp, bot=bot, secret_token=secret,
            handle_in_background=False
        ).register(app, path=webhook_path)

    runner = web.AppRunner(app)
    await runner.setup()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals = []
    try:
        site = web.TCPSite(runner, '0.0.0.0', int(os.getenv('PORT', '10000')))
        await site.start()
        logging.info('HTTP server started')
        await startup_retry(load_statuses, 'Loading statuses')
        if use_webhook:
            async def configure_webhook():
                await bot.set_webhook(
                    url=base_url + webhook_path,
                    secret_token=secret,
                    allowed_updates=['message', 'callback_query'],
                    drop_pending_updates=False,
                    max_connections=1
                )
            await startup_retry(configure_webhook, 'Setting webhook')
            state['ready'] = True
            logging.info('Webhook mode started')
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, stop.set)
                    installed_signals.append(sig)
                except (NotImplementedError, RuntimeError):
                    pass
            await stop.wait()
        else:
            async def remove_webhook():
                await bot.delete_webhook(drop_pending_updates=False)
            await startup_retry(remove_webhook, 'Removing webhook')
            state['ready'] = True
            logging.info('Polling mode started')
            await dp.start_polling(
                bot, handle_as_tasks=False,
                allowed_updates=['message', 'callback_query']
            )
    finally:
        state['ready'] = False
        for sig in installed_signals:
            loop.remove_signal_handler(sig)
        # Keep webhook configured so Telegram can wake the service after sleep.
        await runner.cleanup()
        await bot.session.close()


if __name__ == '__main__':
    asyncio.run(main())