import asyncio
import os
import zipfile
import shutil
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from pypdf import PdfWriter

TOKEN = os.getenv("BOT_TOKEN")

bot = Bot(token=TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    await message.answer(
        "Привет! Я умею объединять накладные.\n\n"
        "1. Пришли мне ZIP-архив с PDF-накладными.\n"
        "2. Я вытащу все PDF и склею их в один файл."
    )

@dp.message(F.document.mime_type.in_({"application/zip", "application/x-zip-compressed", "application/octet-stream"}))
async def handle_zip(message: types.Message):
    user_id = message.from_user.id
    temp_dir = f"temp_{user_id}"
    zip_path = f"{temp_dir}.zip"
    output_filename = f"merged_{user_id}.pdf"

    os.makedirs(temp_dir, exist_ok=True)
    status_msg = await message.answer("Скачиваю и обрабатываю архив...")

    try:
        file = await bot.get_file(message.document.file_id)
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
            await message.answer("В архиве не найдено ни одного PDF-файла.")
            return

        merger = PdfWriter()
        for pdf in pdf_files:
            merger.append(pdf)
        merger.write(output_filename)
        merger.close()

        await message.answer_document(
            types.FSInputFile(output_filename),
            caption=f"Готово! Склеил {len(pdf_files)} накладных из архива."
        )

    except zipfile.BadZipFile:
        await message.answer("Не удалось распаковать архив. Убедись, что файл является ZIP-архивом.")
    except Exception as e:
        await message.answer(f"Произошла ошибка: {e}")

    finally:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
        if os.path.exists(zip_path):
            os.remove(zip_path)
        if os.path.exists(output_filename):
            os.remove(output_filename)
        await status_msg.delete()

@dp.message()
async def unknown_message(message: types.Message):
    await message.answer("Пришли мне ZIP-архив с PDF-накладными, и я их объединю.")

async def main():
    print("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
