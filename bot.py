import asyncio
import hashlib
import json
import os
import re
import shutil
import time
import zipfile
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from pypdf import PdfReader, PdfWriter

TOKEN = os.getenv("BOT_TOKEN")
# На Railway сюда монтируется Volume, чтобы память не стиралась при перезапуске
DATA_DIR = os.getenv("DATA_DIR", ".")
SEEN_FILE = os.path.join(DATA_DIR, "seen.json")
KEEP_DAYS = 3  # через сколько дней накладная забывается

bot = Bot(token=TOKEN)
dp = Dispatcher()

# user_id -> file_id последнего архива (для кнопки «Склеить всё заново»)
last_zip = {}


def load_seen():
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_seen(seen):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = SEEN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(seen, f)
    os.replace(tmp, SEEN_FILE)


def user_seen(seen, user_id):
    # выкидываем всё старше KEEP_DAYS
    border = time.time() - KEEP_DAYS * 86400
    items = {k: t for k, t in seen.get(str(user_id), {}).items() if t > border}
    seen[str(user_id)] = items
    return items


def pdf_key(path):
    """Отпечаток накладной: по тексту внутри PDF (он не меняется,
    даже если Kaspi пересоздал файл), а если текста нет — по байтам файла."""
    try:
        text = "".join(page.extract_text() or "" for page in PdfReader(path).pages)
    except Exception:
        text = ""
    text = re.sub(r"\s+", "", text)
    if text:
        return "t:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    with open(path, "rb") as f:
        return "b:" + hashlib.sha256(f.read()).hexdigest()


def keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="Склеить всё заново", callback_data="all")
    kb.button(text="Сбросить память", callback_data="reset")
    kb.adjust(1)
    return kb.as_markup()


@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    await message.answer(
        "Привет! Я умею объединять накладные.\n\n"
        "1. Пришли мне ZIP-архив с PDF-накладными.\n"
        "2. Я склею их в один файл.\n\n"
        "Накладные, которые я уже склеивал, второй раз не добавляю — "
        "в PDF попадут только новые. "
        f"Память хранится {KEEP_DAYS} дня, сбросить: /reset"
    )


@dp.message(Command("reset"))
async def reset_cmd(message: types.Message):
    seen = load_seen()
    seen.pop(str(message.from_user.id), None)
    save_seen(seen)
    await message.answer("Память очищена. Следующий архив склею целиком.")


async def process_zip(chat_id, user_id, file_id, only_new):
    temp_dir = f"temp_{user_id}"
    zip_path = f"{temp_dir}.zip"
    output_filename = f"merged_{user_id}.pdf"

    os.makedirs(temp_dir, exist_ok=True)
    status_msg = await bot.send_message(chat_id, "Скачиваю и обрабатываю архив...")

    try:
        file = await bot.get_file(file_id)
        await bot.download_file(file.file_path, zip_path)

        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(temp_dir)

        pdf_files = sorted([
            os.path.join(root, f)
            for root, _, files in os.walk(temp_dir)
            for f in files
            if f.lower().endswith(".pdf")
        ])

        if not pdf_files:
            await bot.send_message(chat_id, "В архиве не найдено ни одного PDF-файла.")
            return

        seen = load_seen()
        known = user_seen(seen, user_id)

        to_merge = []
        keys_in_zip = set()
        for pdf in pdf_files:
            key = pdf_key(pdf)
            if key in keys_in_zip:
                continue  # дубль внутри одного архива
            keys_in_zip.add(key)
            if only_new and key in known:
                continue
            to_merge.append((pdf, key))

        old_count = len(keys_in_zip) - len(to_merge)

        if not to_merge:
            save_seen(seen)
            await bot.send_message(
                chat_id,
                f"Все {len(keys_in_zip)} накладных из архива уже были склеены раньше. Новых нет.",
                reply_markup=keyboard(),
            )
            return

        merger = PdfWriter()
        for pdf, _ in to_merge:
            merger.append(pdf)
        merger.write(output_filename)
        merger.close()

        now = time.time()
        for _, key in to_merge:
            known[key] = now
        save_seen(seen)

        if old_count:
            caption = (f"Готово! В архиве {len(keys_in_zip)} накладных: "
                       f"{old_count} уже были, склеил {len(to_merge)} новых.")
        else:
            caption = f"Готово! Склеил {len(to_merge)} накладных из архива."

        await bot.send_document(
            chat_id,
            types.FSInputFile(output_filename),
            caption=caption,
            reply_markup=keyboard(),
        )

    except zipfile.BadZipFile:
        await bot.send_message(chat_id, "Не удалось распаковать архив. Убедись, что файл является ZIP-архивом.")
    except Exception as e:
        await bot.send_message(chat_id, f"Произошла ошибка: {e}")

    finally:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
        if os.path.exists(zip_path):
            os.remove(zip_path)
        if os.path.exists(output_filename):
            os.remove(output_filename)
        await status_msg.delete()


@dp.message(F.document.mime_type.in_({"application/zip", "application/x-zip-compressed", "application/octet-stream"}))
async def handle_zip(message: types.Message):
    last_zip[message.from_user.id] = message.document.file_id
    await process_zip(message.chat.id, message.from_user.id, message.document.file_id, only_new=True)


@dp.callback_query(F.data == "all")
async def merge_all(callback: types.CallbackQuery):
    file_id = last_zip.get(callback.from_user.id)
    if not file_id:
        await callback.answer("Пришли архив ещё раз — я его уже не помню.", show_alert=True)
        return
    await callback.answer()
    await process_zip(callback.message.chat.id, callback.from_user.id, file_id, only_new=False)


@dp.callback_query(F.data == "reset")
async def reset_btn(callback: types.CallbackQuery):
    seen = load_seen()
    seen.pop(str(callback.from_user.id), None)
    save_seen(seen)
    await callback.answer("Память очищена", show_alert=True)


@dp.message()
async def unknown_message(message: types.Message):
    await message.answer("Пришли мне ZIP-архив с PDF-накладными, и я их объединю.")


async def main():
    print("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
