import os
import re
import fitz  # PyMuPDF
import requests
import base64
import json
from dotenv import load_dotenv
import subprocess
import argparse
import sys
import time

load_dotenv()

# Используем локальный OpenAI-совместимый API, либо значения из .env
API_URL = os.getenv("API_URL", "http://localhost:8000/v1/chat/completions")
MODEL_NAME = os.getenv("MODEL_NAME", "gemini-1.5-pro")
API_KEY = os.getenv("GEMINI_API_KEY")

def generate_content_api(messages):
    """Отправка запроса к локальному API."""
    payload = {
        "model": MODEL_NAME,
        "messages": messages
    }
    headers = {
        "Content-Type": "application/json"
    }
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"

    try:
        response = requests.post(API_URL, json=payload, headers=headers, timeout=120)
        response.raise_for_status()
        return response.json()['choices'][0]['message']['content']
    except Exception as e:
        print(f"\nОшибка при вызове API: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"Ответ сервера: {e.response.text}")
        return ""

class RateLimiter:
    def __init__(self, rpm=15):
        self.delay = 60.0 / rpm
        self.last_call = 0

    def wait(self):
        now = time.time()
        elapsed = now - self.last_call
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self.last_call = time.time()

limiter = RateLimiter(rpm=15)

def clean_latex_response(text):
    text = text.strip()
    match = re.search(r'```(?:latex)?\s*(.*?)\s*```', text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text.strip()

def clean_json_response(text):
    text = text.strip()
    # Пытаемся найти массив JSON с помощью регулярного выражения
    match = re.search(r'\[\s*\{.*?\}\s*\]', text, re.DOTALL)
    if match:
        text = match.group(0)
    else:
        if text.startswith("```json"):
            text = text[7:]
        elif text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
            
    # Исправляем невалидные escape-последовательности, которые ломают json.loads
    # (например, когда модель пишет \quad вместо \\quad внутри JSON)
    text = re.sub(r'\\([^"\\/bfnrtu])', r'\\\\\1', text)
    return text.strip()

def process_single_page(page, page_num):
    """Оцифровка и проверка страницы за один запрос к ИИ."""
    limiter.wait()
    zoom = 2.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat)
    img_data = pix.tobytes("png")
    
    image_base64 = base64.b64encode(img_data).decode('utf-8')
    
    prompt = (
        "Ты — ведущий эксперт по оцифровке математических лекций мехмата.\n"
        "Твоя задача: распознать текст с изображения и перевести его в идеальный LaTeX.\n\n"
        "СТРОГИЕ ПРАВИЛА:\n"
        "1. ВЕРСТКА: Текст КАТЕГОРИЧЕСКИ НЕ ДОЛЖЕН выходить за края (Overfull hbox). Если строка или формула длинная — ОБЯЗАТЕЛЬНО разбивай ее.\n"
        "   - Для формул: используй align*, split или multline.\n"
        "   - Для длинных перечислений: если элементы списка не влезают, переноси их на новые строки.\n"
        "2. ПРОВЕРКА: Сразу после оцифровки проверь свой код на незакрытые скобки, знаки $ и синтаксические ошибки.\n"
        "3. ФОРМАТ: Выдавай ТОЛЬКО чистый код LaTeX для тела документа. Никаких преамбул, никаких ```latex в начале.\n"
        "4. РИСУНКИ: Сложные схемы описывай подробным комментарием % [Рисунок: ...], простые рисуй через TikZ.\n\n"
        "Сделай это максимально точно и аккуратно, следи за тем, чтобы левый и правый край текста были в пределах разумного."
    )
    
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{image_base64}"}
                }
            ]
        }
    ]
    
    start_time = time.time()
    print(f"   [Запрос к {MODEL_NAME}...] ", end="", flush=True)
    response_text = generate_content_api(messages)
    duration = time.time() - start_time
    print(f"готово ({duration:.1f} сек.)")
    
    return clean_latex_response(response_text)

def get_preamble():
    return r"""\documentclass[12pt,a4paper]{article}
\usepackage[utf8]{inputenc}
\usepackage[T2A]{fontenc}
\usepackage[russian]{babel}
\usepackage{amsmath,amssymb,amsthm}
\usepackage[left=2.5cm,right=1.5cm,top=2cm,bottom=2cm]{geometry}
\usepackage{microtype}
\usepackage{graphicx}
\usepackage{tikz}

\sloppy
\allowdisplaybreaks
\emergencystretch 3em % Помогает избежать Overfull hbox за счет растяжения пробелов в строке

\begin{document}
"""



def auto_compile_and_fix(output_path):
    max_retries = 3
    directory = os.path.dirname(os.path.abspath(output_path))
    filename = os.path.basename(output_path)
    
    for attempt in range(max_retries):
        print(f"[{attempt + 1}/{max_retries}] Запуск pdflatex...")
        result = subprocess.run(
            ["pdflatex", "-interaction=nonstopmode", filename],
            cwd=directory,
            capture_output=True,
            text=True
        )
        
        if result.returncode == 0:
            print("Компиляция успешно завершена.")
            return True
            
        print(f"Ошибка компиляции! Попытка автоисправления ({attempt + 1}/{max_retries})...")
        
        if attempt < max_retries - 1:
            log_output = result.stdout
            log_tail = log_output[-3000:]
            
            with open(output_path, "r", encoding="utf-8") as f:
                code = f.read()
                
            prompt = (
                "Ты — системный компилятор LaTeX и эксперт по исправлению ошибок.\n"
                "При компиляции возникла проблема: либо критическая ошибка, либо текст вылетел за поля (Overfull \\hbox).\n"
                "Твоя задача: проанализировать лог и исправить код так, чтобы всё скомпилировалось идеально и текст НЕ ВЫХОДИЛ за границы.\n"
                "Если видишь Overfull \\hbox — добавь переносы строк (\\\\) или разбей формулу.\n"
                "Вместо того чтобы переписывать весь код целиком, верни список замен.\n"
                "Используй строго следующий формат тегов для каждой замены (без подсветки синтаксиса, просто текст):\n"
                "<old>\n"
                "Точный фрагмент старого кода\n"
                "</old>\n"
                "<new>\n"
                "Исправленный фрагмент\n"
                "</new>\n\n"
                "КРИТИЧЕСКИ ВАЖНО:\n"
                "1. НЕ ПИШИ ВООБЩЕ НИЧЕГО КРОМЕ этих тегов. Никакого приветствия, никаких пояснений, никаких блоков ```. ТОЛЬКО теги <old> и <new>.\n"
                "2. Текст внутри <old> должен СТРОГО, СИМВОЛ В СИМВОЛ (включая все пробелы, табуляции и переносы строк) совпадать с фрагментом из исходного кода. Иначе замена не сработает!\n\n"
                f"Лог:\n{log_tail}\n\n"
                f"Код LaTeX:\n{code}"
            )
            
            limiter.wait()
            start_time = time.time()
            print("   [Анализ ошибки ИИ...] ", end="", flush=True)
            try:
                messages = [{"role": "user", "content": prompt}]
                response_text = generate_content_api(messages)
                
                replacements = []
                # Ищем все пары <old> и <new> с помощью регулярного выражения
                for match in re.finditer(r'<old>\s*(.*?)\s*</old>\s*<new>\s*(.*?)\s*</new>', response_text, re.DOTALL):
                    replacements.append({
                        "old": match.group(1).strip(),
                        "new": match.group(2).strip()
                    })

                if not replacements:
                    print("Модель не вернула ни одной замены в правильном формате <old>...</old> <new>...</new>")
                    print(f"--- Сырой ответ ИИ ---\n{response_text}\n----------------------")
                    return False

                fixed_code = code
                applied_changes = 0
                for rep in replacements:
                    old_text = rep["old"]
                    new_text = rep["new"]
                    if old_text and old_text in fixed_code:
                        fixed_code = fixed_code.replace(old_text, new_text)
                        applied_changes += 1
                
                if applied_changes > 0:
                    with open(output_path, "w", encoding="utf-8") as f:
                        f.write(fixed_code)
                    duration = time.time() - start_time
                    print(f"код исправлен, применено замен: {applied_changes} ({duration:.1f} сек.)")
                else:
                    print("Модель вернула замены, которые не найдены в исходном коде.")
                    return False
                    
            except Exception as e:
                print(f"Ошибка при запросе к ИИ: {e}")
                return False
        else:
            print("Не удалось исправить ошибки автоматически. Завершение работы.")
            return False
            
    return False

def merge_pdfs(pdf_list, output_filename):
    """Объединение нескольких PDF в один."""
    print(f"\nСклеивание {len(pdf_list)} фрагментов в '{output_filename}'...")
    result = fitz.open()
    for pdf in pdf_list:
        if os.path.exists(pdf):
            with fitz.open(pdf) as mfile:
                result.insert_pdf(mfile)
        else:
            print(f"Предупреждение: файл {pdf} не найден и будет пропущен.")
    result.save(output_filename)
    result.close()
    print("Объединение завершено.")

def process_pdf(pdf_path, output_base, start_page=1):
    with fitz.open(pdf_path) as doc:
        total_pages = len(doc)
        CHUNK_SIZE = 10
        
        # Корректируем начальную страницу (пользователь вводит 1, в коде 0)
        start_idx = max(0, start_page - 1)
        
        generated_pdfs = []
        total_start = time.time()
        
        # Цикл по блокам по CHUNK_SIZE страниц
        for chunk_start in range(start_idx, total_pages, CHUNK_SIZE):
            chunk_end = min(chunk_start + CHUNK_SIZE, total_pages)
            print(f"\n{'='*20}")
            print(f"ОБРАБОТКА БЛОКА: страницы {chunk_start + 1} - {chunk_end}")
            print(f"{'='*20}")
            
            latex_pages = []
            for page_num in range(chunk_start, chunk_end):
                print(f"\n--- Страница {page_num + 1} из {total_pages} ---")
                page = doc.load_page(page_num)
                verified_tex = process_single_page(page, page_num + 1)
                page_text = f"% ---------- СТРАНИЦА {page_num + 1} ----------\n{verified_tex}\n"
                latex_pages.append(page_text)
                
            # Формируем имя для промежуточного .tex и .pdf
            chunk_name = f"part_{chunk_start+1:03d}_{chunk_end:03d}"
            chunk_tex_path = f"{chunk_name}.tex"
            chunk_pdf_path = f"{chunk_name}.pdf"
            
            print(f"\nСборка блока {chunk_name}...")
            final_chunk_content = get_preamble() + "\n\\newpage\n".join(latex_pages) + "\n\n\\end{document}\n"
            
            with open(chunk_tex_path, "w", encoding="utf-8") as f:
                f.write(final_chunk_content)
            
            print(f"Блок сохранен в '{chunk_tex_path}'. Запуск компиляции...")
            if auto_compile_and_fix(chunk_tex_path):
                generated_pdfs.append(chunk_pdf_path)
            else:
                print(f"Критическая ошибка при сборке блока {chunk_name}. Пробуем продолжить со следующего.")

        # После завершения всех циклов, объединяем PDF
        if generated_pdfs:
            final_pdf_name = output_base.replace(".tex", ".pdf") if output_base.endswith(".tex") else f"{output_base}.pdf"
            merge_pdfs(generated_pdfs, final_pdf_name)
        else:
            print("\nНет скомпилированных фрагментов для объединения.")

        total_duration = time.time() - total_start
        print(f"\nВсего затрачено времени: {total_duration//60:.0f} мин. {total_duration%60:.0f} сек.")

def main():
    parser = argparse.ArgumentParser(description="Оцифровка лекций с контролем RPM.")
    parser.add_argument("pdf_path", nargs='?', help="Путь к PDF")
    parser.add_argument("output_path", nargs='?', default="result.tex", help="Путь для .tex")
    parser.add_argument("--check-only", metavar="FILE.tex", help="Только проверка ИИ + компиляция")
    parser.add_argument("--compile-only", metavar="FILE.tex", help="Только компиляция")
    parser.add_argument("--start-page", type=int, default=1, help="С какой страницы начинать (1-indexed)")

    args = parser.parse_args()
    # model = genai.GenerativeModel(MODEL_NAME) # Больше не нужно

    if args.compile_only:
        auto_compile_and_fix(args.compile_only)
    elif args.check_only:
        # Теперь --check-only работает через механизм автоисправления ошибок компиляции
        auto_compile_and_fix(args.check_only)
    else:
        if not args.pdf_path:
            parser.print_help()
            sys.exit(1)
        process_pdf(args.pdf_path, args.output_path, start_page=args.start_page)

if __name__ == "__main__":
    main()
