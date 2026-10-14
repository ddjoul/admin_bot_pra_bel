import asyncio
import json
import uuid
from datetime import datetime, timezone
from aiogram import F
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramBadRequest
import admin_bot as core

CATEGORIES = {'poem': 'верш', 'translation': 'пераклад', 'news': 'навіна', 'culture': 'культура', 'language': 'мова', 'idea': 'ідэя'}
old_snapshot = core.snapshot
old_show = core.show_item
old_queue = core.queue

def snapshot(kind, row):
    value = old_snapshot(kind, row)
    if kind == 'p':
        value.update(tags=row.get('tags'), content=row.get('content'))
    else:
        value.update(parent_id=row.get('parent_id'), author_telegram_id=row.get('author_telegram_id'))
    return value

core.snapshot = snapshot

async def show_item(chat, kind, row, confirm=None):
    if kind == 'c':
        return await old_show(chat, kind, row, confirm)
    authors = await core.query(lambda: core.db.table('authors').select('name').eq('id', row['author_id']).limit(1))
    name = authors[0]['name'] if authors else '(профиль не найден)'
    header = CATEGORIES.get(row.get('category'), 'материал') + '\nназвание: ' + (row.get('title') or '***') + '\nавтор: ' + name + '\nязык: ' + str(row.get('language_code')) + '\nID: ' + row['id']
    if row.get('category') == 'translation':
        header += '\nавтор оригинала: ' + str(row.get('original_author')) + '\nязык оригинала: ' + str(row.get('original_language'))
    header += '\nхештеги: ' + (' '.join('#' + t for t in row.get('tags', [])) or 'нет')
    await core.send_chunks(chat, header)
    content = row.get('content')
    if row.get('category') not in ('poem', 'translation') and isinstance(content, list) and content:
        for block in content:
            if block.get('type') == 'text':
                await core.send_chunks(chat, block.get('text'))
            elif block.get('type') == 'image':
                url = core.db.storage.from_('photos').get_public_url(block['path'])
                await core.bot.send_photo(chat, url, caption=block.get('alt') or None)
    else:
        await core.send_chunks(chat, row.get('body'))
    rid, h = row['id'], core.digest('p', row)
    if confirm:
        decision = 'опубликовать' if confirm == 'a' else 'отклонить'
        buttons = [[('да, ' + decision, f'p:y{confirm}:{rid}:{h}')], [('назад', f'p:v:{rid}:{h}')]]
        text = decision + ' этот материал?'
    else:
        buttons = [[('опубликовать', f'p:a:{rid}:{h}'), ('отклонить', f'p:r:{rid}:{h}')]]
        text = 'выбери действие для материала выше:'
    await core.bot.send_message(chat, text, reply_markup=core.kb(buttons))

core.show_item = show_item

async def queue(chat, kind, offset=0):
    if kind != 'p':
        return await old_queue(chat, kind, offset)
    rows = await core.query(lambda: core.db.table('posts').select('*').eq('status', 'draft').in_('category', list(CATEGORIES)).order('created_at').order('id').range(offset, offset + core.PAGE))
    if not rows:
        await core.bot.send_message(chat, 'очередь пуста.', reply_markup=core.menu)
        return
    await core.bot.send_message(chat, 'очередь стихов, переводов и статей:', reply_markup=core.menu)
    for row in rows[:core.PAGE]:
        await show_item(chat, 'p', row)
    options = [[('обновить с начала', 'q:p:0')]]
    if len(rows) > core.PAGE:
        options.insert(0, [('следующие 5', f'q:p:{offset + core.PAGE}')])
    await core.bot.send_message(chat, 'после решений обнови очередь.', reply_markup=core.kb(options))

core.queue = queue

async def stats(chat):
    lines = []
    for table, status, label in [('comments', 'pending', 'комментарии'), ('posts', 'draft', 'стихи, переводы и статьи')]:
        count = await asyncio.to_thread(lambda table=table, status=status: core.db.table(table).select('id', count='exact', head=True).eq('status', status).execute().count)
        lines.append(label + ' на проверке: ' + str(count))
    await core.bot.send_message(chat, '\n'.join(lines), reply_markup=core.menu)

core.stats = stats

async def callback(c):
    parts = (c.data or '').split(':')
    if not ((len(parts) == 3 and parts[:2] == ['q', 'p']) or (len(parts) == 4 and parts[0] == 'p')):
        raise SkipHandler
    if c.from_user.id != core.ADMIN or not c.message or c.message.chat.type != 'private':
        await c.answer('нет доступа', show_alert=True)
        return
    try:
        await c.answer()
    except TelegramBadRequest as exc:
        if 'query is too old' not in str(exc).lower() and 'query id is invalid' not in str(exc).lower():
            raise
    async with core.lock:
        if len(parts) == 3:
            try:
                offset = int(parts[2])
            except ValueError:
                return
            if offset >= 0:
                await queue(core.ADMIN, 'p', offset)
            return
        _, action, rid, oldhash = parts
        if action not in ('v', 'a', 'r', 'ya', 'yr'):
            return
        try:
            uuid.UUID(rid)
        except ValueError:
            return
        row = await core.get_row('p', rid)
        if not row or row.get('status') != 'draft' or row.get('category') not in CATEGORIES:
            await core.bot.send_message(core.ADMIN, 'материал уже обработан или удалён. обнови очередь.')
            return
        if action == 'v':
            await show_item(core.ADMIN, 'p', row)
            return
        if core.digest('p', row) != oldhash:
            await core.bot.send_message(core.ADMIN, 'материал изменился; проверь актуальный текст, изображения и теги:')
            await show_item(core.ADMIN, 'p', row)
            return
        if action in ('a', 'r'):
            await show_item(core.ADMIN, 'p', row, confirm=action)
            return
        target = 'published' if action == 'ya' else 'rejected'
        payload = {'status': target, 'status_id': core.statuses['p'][target], 'published_at': datetime.now(timezone.utc).isoformat() if target == 'published' else None}
        def update():
            q = core.db.table('posts').update(payload).eq('id', rid)
            for field, value in snapshot('p', row).items():
                if value is None:
                    q = q.is_(field, 'null')
                elif field == 'content':
                    q = q.eq(field, json.dumps(value, ensure_ascii=False))
                elif field == 'tags':
                    q = q.eq(field, '{' + ','.join(json.dumps(t, ensure_ascii=False) for t in value) + '}')
                else:
                    q = q.eq(field, value)
            return q
        result = await core.query(update)
        if not result:
            await core.bot.send_message(core.ADMIN, 'версия изменилась или обновление не подтвердилось. обнови очередь.')
            return
        await core.bot.send_message(core.ADMIN, ('опубликовано' if target == 'published' else 'отклонено') + '\nID: ' + rid, reply_markup=core.menu)
        try:
            await c.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass

core.dp.callback_query.register(callback, F.data)
core.dp.callback_query.handlers.insert(0, core.dp.callback_query.handlers.pop())

if __name__ == '__main__':
    asyncio.run(core.main())
