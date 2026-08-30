# app.py
# Запуск: streamlit run app.py

import os
import time
import logging
import re
from datetime import datetime
from typing import List, Dict, Optional

import requests
import pandas as pd
import streamlit as st

# -----------------------------------------------------------------------------
# НАСТРОЙКИ СТРАНИЦЫ
# -----------------------------------------------------------------------------

st.set_page_config(
    page_title="App Store Reviews Scraper",
    page_icon="📱",
    layout="wide",
)

CSV_FILE = "app_store_reviews.csv"
LOG_FILE = "scraper.log"
BASE_RSS_URL = "https://itunes.apple.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
    ),
    "Accept": "application/json",
}

# -----------------------------------------------------------------------------
# ИНИЦИАЛИЗАЦИЯ SESSION STATE
# -----------------------------------------------------------------------------

if "reviews" not in st.session_state:
    st.session_state.reviews = []          # список собранных отзывов (dict)
if "seen_keys" not in st.session_state:
    st.session_state.seen_keys = set()     # ключи для дедупликации
if "log_messages" not in st.session_state:
    st.session_state.log_messages = []     # текстовые логи для UI
if "is_running" not in st.session_state:
    st.session_state.is_running = False
if "stats" not in st.session_state:
    st.session_state.stats = {"found": 0, "russian": 0, "skipped": 0}


# -----------------------------------------------------------------------------
# ЛОГИРОВАНИЕ (в файл + в интерфейс)
# -----------------------------------------------------------------------------

def setup_logging(log_file: str) -> None:
    logger = logging.getLogger()
    if logger.handlers:
        return  # чтобы при каждом rerun Streamlit не плодились обработчики
    logger.setLevel(logging.INFO)
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(levelname)s - %(asctime)s - %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(file_handler)


def ui_log(message: str, level: str = "info") -> None:
    """Пишет сообщение в файл-лог и добавляет в session_state для показа в интерфейсе."""
    if level == "warning":
        logging.warning(message)
    elif level == "error":
        logging.error(message)
    else:
        logging.info(message)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    st.session_state.log_messages.append(f"[{ts}] {level.upper()}: {message}")
    # ограничим размер лога, чтобы не разрастался бесконечно
    if len(st.session_state.log_messages) > 500:
        st.session_state.log_messages = st.session_state.log_messages[-500:]


# -----------------------------------------------------------------------------
# ЗАПРОС СТРАНИЦЫ ОТЗЫВОВ (JSON RSS FEED)
# -----------------------------------------------------------------------------

def build_reviews_url(country: str, app_id: str, page: int) -> str:
    return (
        f"{BASE_RSS_URL}/{country}/rss/customerreviews/"
        f"page={page}/id={app_id}/sortby=mostrecent/json"
    )


def fetch_page(url: str, page_num: int, max_retries_429: int, base_delay: int):
    """Возвращает (json_data, status_code). Обрабатывает 429 с ретраями."""
    attempt = 0
    while attempt <= max_retries_429:
        try:
            ui_log(f"Запрос страницы {page_num}: {url}")
            resp = requests.get(url, headers=HEADERS, timeout=15)
            status = resp.status_code
            ui_log(f"Статус ответа страницы {page_num}: {status}")

            if status == 200:
                try:
                    return resp.json(), status
                except ValueError:
                    ui_log(f"Не удалось распарсить JSON на странице {page_num}", "error")
                    return None, status
            elif status == 429:
                wait_time = (2 ** attempt) * base_delay
                ui_log(
                    f"429 на странице {page_num}. Попытка {attempt+1}/{max_retries_429}. "
                    f"Ждём {wait_time} сек.",
                    "warning",
                )
                time.sleep(wait_time)
                attempt += 1
            else:
                ui_log(f"Неуспешный статус {status} на странице {page_num}", "error")
                return None, status
        except requests.RequestException as e:
            ui_log(f"Ошибка сети на странице {page_num}: {e}", "error")
            return None, 0

    ui_log(f"Превышено число попыток для страницы {page_num} (429)", "error")
    return None, 429


# -----------------------------------------------------------------------------
# ПАРСИНГ JSON
# -----------------------------------------------------------------------------

def parse_reviews_from_json(data: Dict, page_num: int) -> List[Dict]:
    """
    Извлекает отзывы из JSON фида iTunes RSS.
    Первый элемент feed.entry обычно является метаданными приложения (без im:rating) —
    его нужно пропустить.
    """
    reviews = []
    if not data:
        return reviews

    entries = data.get("feed", {}).get("entry", [])
    if isinstance(entries, dict):
        entries = [entries]

    for entry in entries:
        if "im:rating" not in entry:
            continue
        try:
            rating = None
            rating_label = entry.get("im:rating", {}).get("label")
            if rating_label and rating_label.isdigit():
                rating = int(rating_label)

            title = entry.get("title", {}).get("label", "")
            text = entry.get("content", {}).get("label", "")
            author = entry.get("author", {}).get("name", {}).get("label", "")
            version = entry.get("im:version", {}).get("label", "")
            review_id = entry.get("id", {}).get("label", "")

            date = ""
            updated_label = entry.get("updated", {}).get("label")
            if updated_label:
                try:
                    dt = datetime.fromisoformat(updated_label.replace("Z", "+00:00"))
                    date = dt.strftime("%Y-%m-%d")
                except ValueError:
                    date = updated_label

            reviews.append({
                "review_id": review_id,
                "rating": rating,
                "title": title,
                "text": text,
                "date": date,
                "author": author,
                "version": version,
            })
        except Exception as e:
            ui_log(f"Ошибка парсинга отдельного отзыва на странице {page_num}: {e}", "warning")
            continue

    ui_log(f"На странице {page_num} найдено {len(reviews)} отзывов (до фильтрации по языку)")
    return reviews


# -----------------------------------------------------------------------------
# ЯЗЫКОВАЯ ФИЛЬТРАЦИЯ
# -----------------------------------------------------------------------------

def is_russian_text(text: str) -> bool:
    if not text:
        return False
    return bool(re.search(r"[А-Яа-я]", text))


# -----------------------------------------------------------------------------
# ДЕДУПЛИКАЦИЯ
# -----------------------------------------------------------------------------

def make_review_key(review: Dict) -> str:
    review_id = str(review.get("review_id", "") or "")
    if review_id:
        return f"id:{review_id}"
    author = str(review.get("author", "") or "")
    date = str(review.get("date", "") or "")
    text = str(review.get("text", "") or "")
    return f"key:{author.strip()}|{date.strip()}|{text.strip()}"


def is_duplicate(review: Dict, seen_keys: set) -> bool:
    key = make_review_key(review)
    if key in seen_keys:
        return True
    seen_keys.add(key)
    return False


# -----------------------------------------------------------------------------
# ЗАГРУЗКА / СОХРАНЕНИЕ ОТЗЫВОВ
# -----------------------------------------------------------------------------

def load_reviews_from_dataframe(df: pd.DataFrame) -> List[Dict]:
    df = df.fillna("")
    return df.to_dict(orient="records")


def reviews_to_csv_bytes(reviews: List[Dict]) -> bytes:
    cols = ["review_id", "rating", "title", "text", "date", "author", "version"]
    if not reviews:
        df = pd.DataFrame(columns=cols)
    else:
        df = pd.DataFrame(reviews)
        for c in cols:
            if c not in df.columns:
                df[c] = ""
        df = df[cols]
    # utf-8-sig, чтобы корректно открывалось в Excel с кириллицей
    return df.to_csv(index=False).encode("utf-8-sig")


def save_reviews_to_local_csv(reviews: List[Dict], filename: str) -> None:
    cols = ["review_id", "rating", "title", "text", "date", "author", "version"]
    if not reviews:
        pd.DataFrame(columns=cols).to_csv(filename, index=False, encoding="utf-8")
        return
    df = pd.DataFrame(reviews)
    for c in cols:
        if c not in df.columns:
            df[c] = ""
    df = df[cols]
    df.to_csv(filename, index=False, encoding="utf-8")


# -----------------------------------------------------------------------------
# ОСНОВНАЯ ЛОГИКА СБОРА
# -----------------------------------------------------------------------------

def collect_reviews(
    app_id: str,
    country: str,
    max_pages: int,
    request_delay: int,
    max_retries_429: int,
    save_to_disk: bool,
    progress_bar,
    log_placeholder,
    on_update=render_metrics,
) -> None:
    ui_log(f"Начало сбора отзывов для App ID: {app_id} (витрина: {country})")

    seen_keys = st.session_state.seen_keys
    all_reviews = st.session_state.reviews
    had_existing = bool(all_reviews)

    total_found = 0
    total_russian = 0
    total_skipped_non_ru = 0

    for page in range(1, max_pages + 1):
        progress_bar.progress(page / max_pages, text=f"Страница {page} из {max_pages}")

        url = build_reviews_url(country, app_id, page)
        data, status = fetch_page(url, page, max_retries_429, request_delay)

        if status != 200 or not data:
            ui_log(f"Не удалось получить страницу {page} (статус {status}). Останавливаемся.", "error")
            break

        raw_reviews = parse_reviews_from_json(data, page)
        if not raw_reviews:
            ui_log(f"На странице {page} отзывов не найдено — конец пагинации.")
            break

        new_on_page = 0
        for rev in raw_reviews:
            total_found += 1

            if is_duplicate(rev, seen_keys):
                continue

            if not is_russian_text(rev["text"]):
                total_skipped_non_ru += 1
                continue

            all_reviews.append(rev)
            total_russian += 1
            new_on_page += 1

        ui_log(f"Страница {page}: получено {len(raw_reviews)}, новых русских добавлено: {new_on_page}")

        # обновляем session_state и лог в интерфейсе на лету
        st.session_state.reviews = all_reviews
        st.session_state.seen_keys = seen_keys
        log_placeholder.text_area(
            "Лог выполнения",
            value="\n".join(st.session_state.log_messages[-200:]),
            height=300,
        )

        if save_to_disk:
            save_reviews_to_local_csv(all_reviews, CSV_FILE)

        if had_existing and new_on_page == 0:
            ui_log("Новых отзывов не найдено — данные актуальны, останавливаемся.")
            break

        time.sleep(request_delay)

    st.session_state.stats["found"] += total_found
    st.session_state.stats["russian"] += total_russian
    st.session_state.stats["skipped"] += total_skipped_non_ru

    if on_update:
            on_update()

    ui_log(
        f"Сбор завершён. Просмотрено: {total_found}, "
        f"добавлено новых русских: {total_russian}, пропущено не на русском: {total_skipped_non_ru}"
    )
    progress_bar.progress(1.0, text="Готово")


# -----------------------------------------------------------------------------
# ИНТЕРФЕЙС STREAMLIT
# -----------------------------------------------------------------------------

setup_logging(LOG_FILE)

st.title("📱 App Store Reviews Scraper")
st.caption("Сбор отзывов из App Store через публичный RSS-фид iTunes с фильтрацией по языку.")

with st.sidebar:
    st.header("Настройки")

    app_id = st.text_input("App ID", value="570060128", help="Числовой ID приложения из URL apps.apple.com")
    country = st.text_input("Витрина (страна)", value="ru", help="Например: ru, us, de")
    max_pages = st.slider("Максимум страниц", min_value=1, max_value=10, value=10)
    request_delay = st.slider("Задержка между запросами (сек)", min_value=1, max_value=10, value=2)
    max_retries_429 = st.slider("Макс. ретраев при 429", min_value=1, max_value=10, value=5)

    st.divider()
    save_to_disk = st.checkbox(
        "Дублировать сохранение в локальный CSV (app_store_reviews.csv)",
        value=False,
        help="На Streamlit Cloud файловая система не персистентна между перезапусками — используйте кнопку скачивания ниже.",
    )

    st.divider()
    st.subheader("Продолжить с ранее собранных данных")
    uploaded_file = st.file_uploader("Загрузить существующий CSV", type=["csv"])
    if uploaded_file is not None and st.button("Загрузить в текущую сессию"):
        try:
            df_uploaded = pd.read_csv(uploaded_file, encoding="utf-8")
            loaded = load_reviews_from_dataframe(df_uploaded)
            st.session_state.reviews = loaded
            st.session_state.seen_keys = {make_review_key(r) for r in loaded}
            st.success(f"Загружено {len(loaded)} отзывов из файла.")
        except Exception as e:
            st.error(f"Ошибка при чтении CSV: {e}")

    if st.button("Очистить данные текущей сессии"):
        st.session_state.reviews = []
        st.session_state.seen_keys = set()
        st.session_state.log_messages = []
        st.session_state.stats = {"found": 0, "russian": 0, "skipped": 0}
        st.success("Данные очищены.")

col1, col2 = st.columns([1, 3])

with col1:
    start_clicked = st.button("▶️ Начать сбор отзывов", type="primary", disabled=st.session_state.is_running)

with col2:
    metric_placeholder = st.empty()

def render_metrics():
    with metric_placeholder.container():
        m1, m2, m3 = st.columns(3)
        m1.metric("Всего просмотрено", st.session_state.stats["found"])
        m2.metric("Добавлено русских", st.session_state.stats["russian"])
        m3.metric("Пропущено (не рус.)", st.session_state.stats["skipped"])

render_metrics()  # первичная отрисовка (например, с нулями или с прошлыми значениями)

progress_bar = st.progress(0.0)
log_placeholder = st.empty()
log_placeholder.text_area(
    "Лог выполнения",
    value="\n".join(st.session_state.log_messages[-200:]),
    height=300,
)

if start_clicked:
    if not app_id or not app_id.isdigit():
        st.error("Некорректный App ID. ID должен быть числовым.")
    else:
        st.session_state.is_running = True
        try:
            with st.spinner("Идёт сбор отзывов..."):
                collect_reviews(
                    app_id=app_id,
                    country=country,
                    max_pages=max_pages,
                    request_delay=request_delay,
                    max_retries_429=max_retries_429,
                    save_to_disk=save_to_disk,
                    progress_bar=progress_bar,
                    log_placeholder=log_placeholder,
                )
            render_metrics()  # <-- обязательно обновляем метрики после сбора
        except Exception as e:
            ui_log(f"Критическая ошибка во время выполнения: {e}", "error")
            st.error(f"Критическая ошибка: {e}")
        finally:
            st.session_state.is_running = False
            render_metrics()  # <-- на всякий случай обновим и в finally

st.divider()
st.subheader("Собранные отзывы")

if st.session_state.reviews:
    df_result = pd.DataFrame(st.session_state.reviews)
    cols = ["review_id", "rating", "title", "text", "date", "author", "version"]
    for c in cols:
        if c not in df_result.columns:
            df_result[c] = ""
    df_result = df_result[cols]

    st.dataframe(df_result, use_container_width=True, height=400)

    csv_bytes = reviews_to_csv_bytes(st.session_state.reviews)
    st.download_button(
        label="⬇️ Скачать CSV",
        data=csv_bytes,
        file_name="app_store_reviews.csv",
        mime="text/csv",
    )
else:
    st.info("Пока нет собранных отзывов. Настройте параметры слева и нажмите «Начать сбор отзывов».")
