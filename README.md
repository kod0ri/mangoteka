# Манґотека

Завантажує мангу з **[manga.in.ua](https://manga.in.ua)** та **[faust-web.com](https://faust-web.com)**
у форматах **CBZ · PDF · EPUB · EPUB для Kindle** (через [KCC](https://github.com/ciromattia/kcc)).

Два інтерфейси: веб-UI та CLI. Призначений для локального запуску або деплою на VPS.

---

## Швидкий старт

```bash
docker compose up -d --build
# → http://localhost:8765
```

Бібліотека зберігається у `./data/library/` і переживає перезапуски контейнера.

```bash
docker compose down          # зупинити
docker compose logs -f       # логи
git pull && docker compose up -d --build  # оновлення
```

Порт за замовчуванням — `8765`. Змінити: `WEB_PORT=9000 docker compose up -d --build`.

---

## Локальний запуск без Docker

Потрібно: Python ≥ 3.10. KCC потрібен тільки для формату Kindle.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .

manga-dl-web          # веб-UI → http://localhost:8000

# або CLI
manga-dl title   'https://manga.in.ua/mangas/.../...html' --from 1 --to 10
manga-dl chapter 'https://manga.in.ua/chapters/....html'

manga-dl title   'https://faust-web.com/manga/nazva-slugom'
manga-dl chapter 'https://faust-web.com/manga/nazva/tom-1/rozdil-1'
```

**KCC** (тільки для формату Kindle): у Docker вже вбудований. Нативно:
```bash
pip install "kindlecomicconverter @ git+https://github.com/ciromattia/kcc.git"
# або Arch: yay -S kcc
```

---

## Веб-UI

1. Встав URL тайтлу або глави → натисни **Знайти глави**
2. Обери глави, формат (`CBZ / PDF / EPUB / EPUB Kindle`), режим (`файл на главу` або `один файл на том`)
3. Натисни **Завантажити**
4. Спостерігай прогрес на сторінці задачі. Кнопки: ⏸ Пауза · ■ Зупинити · ✕ Скасувати
5. Готові файли — у `/library`

Якщо якась глава впала — кнопка 🔄 повторить тільки її (або одразу всі помилкові).
Після рестарту контейнера завершені задачі видно в історії; файли на диску зберігаються.

---

## CLI

```
manga-dl title URL [OPTIONS]
manga-dl chapter URL [OPTIONS]
```

| Опція | За замовчуванням | Опис |
|---|---|---|
| `-o / --out` | `./data/library` | куди писати файли |
| `-c / --concurrency` | `8` | паралельних завантажень |
| `--from N` | — | з глави N (тільки `title`) |
| `--to N` | — | по главу N включно (тільки `title`) |
| `--list-only` | — | показати список і вийти (тільки `title`) |
| `--keep-raw` | — | зберегти вихідні JPEG поруч із CBZ |

---

## Формати виводу

| Формат | Чим | Примітки |
|---|---|---|
| **CBZ** | zip JPEG | читається будь-яким CBZ-ридером |
| **PDF** | img2pdf | лосслес, без EXIF-ротації |
| **EPUB** | ebooklib | фіксований layout, SVG-обгортка для масштабування |
| **EPUB Kindle** | KCC | оптимізовано для Send-to-Kindle; надійніше ніж рідний EPUB |

---

## Структура

```
src/manga_in_ua_dl/
├── scraper.py         — MangaClient (manga.in.ua) + make_client() factory
├── faust_scraper.py   — FaustClient (faust-web.com REST API)
├── downloader.py      — паралельне завантаження, глобальний семафор, 429+Retry-After
├── converters.py      — CBZ / PDF / EPUB / Kindle EPUB
├── packager.py        — legacy CBZ для CLI
├── cli.py             — Click CLI
└── web/
    ├── app.py         — FastAPI ендпойнти
    ├── jobs.py        — JobStore: стейт-машина + SQLite-персистентність
    ├── library.py     — бібліотека на диску
    ├── __main__.py    — точка входу manga-dl-web
    └── templates/     — Jinja2 + HTMX
```

---

## Налаштування

| Змінна | Дефолт | Опис |
|---|---|---|
| `MANGA_DL_HOST` | `127.0.0.1` | bind-адреса (в Docker: `0.0.0.0`) |
| `MANGA_DL_PORT` | `8000` | порт |
| `MANGA_DL_DATA` | `./data` | де зберігаються бібліотека і `jobs.db` |
| `MANGA_DL_RELOAD` | — | `1` для auto-reload у розробці |

---

## Деплой на VPS

```bash
git clone <repo> mangoteka && cd mangoteka
docker compose up -d --build
```

Поверх — reverse-proxy для HTTPS (Caddy):

```caddy
manga.example.com {
    reverse_proxy localhost:8765
}
```

На безкоштовних tier'ах (Fly.io, Render) диск ефемерний — для постійного зберігання
приєднай volume / persistent disk до `/data`.

---

## Юридичне

Для особистого використання. Поважай авторів і перекладачів, підтримуй їх там, де можеш.
