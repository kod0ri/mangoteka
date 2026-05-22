# manga.in.ua downloader

Завантажує мангу з [manga.in.ua](https://manga.in.ua) у форматі **CBZ**, **PDF** або **EPUB**
(оптимізований для Kindle Paperwhite 11-12 через [KCC](https://github.com/ciromattia/kcc)).

Має два інтерфейси:

- **CLI** — `manga-dl chapter URL` / `manga-dl title URL`
- **Веб** — простий локальний UI на FastAPI з вибором глав та формату

---

## Запуск у Docker (рекомендовано)

KCC і всі залежності вже всередині образу — нічого ставити на хост не треба.

```bash
docker compose up -d --build
# → http://localhost:8765
```

Завантажені файли осідатимуть у `./data/` поруч із репозиторієм.

Зупинка: `docker compose down`. Оновлення: `git pull && docker compose up -d --build`.

Порт за замовчуванням — `8765`. Змінити: `WEB_PORT=9000 docker compose up -d --build`
або експортувати `WEB_PORT` в `.env` біля `docker-compose.yml`.

## Локальний запуск без Docker

Потрібно: Python ≥ 3.10. EPUB-формат додатково потребує KCC (для CBZ/PDF — не треба).

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .

# вебка
manga-dl-web                        # → http://localhost:8000

# або CLI
manga-dl title 'https://manga.in.ua/mangas/.../...html' --from 1 --to 10
manga-dl chapter 'https://manga.in.ua/chapters/....html'
```

**KCC для EPUB** (Arch Linux): `yay -S kcc`. На інших дистрибутивах — встанови з джерел згідно з [інструкціями KCC](https://github.com/ciromattia/kcc#installation), або просто користуйся Docker-варіантом, де KCC уже всередині.

## Розгортання на VPS

Той самий `docker-compose.yml`:

```bash
git clone <repo> manga-dl && cd manga-dl
docker compose up -d --build
```

Зверху додай reverse-proxy для HTTPS (Caddy найпростіше):

```caddy
manga.example.com {
    reverse_proxy localhost:8000
}
```

Або на безкоштовному tier'і:

- **Fly.io**: `fly launch` → авто-детект Dockerfile → `fly deploy`
- **Render**: створи Web Service з типом Docker, підключи репо

Враховуй: у безкоштовних tier'ах диск ефемерний — `./data/` стиратиметься на рестартах. Для постійного зберігання приєднай volume / persistent disk.

## Налаштування

| Змінна             | Дефолт      | Опис                                     |
|--------------------|-------------|------------------------------------------|
| `MANGA_DL_HOST`    | `127.0.0.1` | bind-адреса (в Docker уже `0.0.0.0`)     |
| `MANGA_DL_PORT`    | `8000`      | порт                                     |
| `MANGA_DL_DATA`    | `./data`    | де зберігаються артефакти                |
| `MANGA_DL_RELOAD`  | —           | `1` для auto-reload у devel              |

## Що під капотом

- **Збір сторінок**: GET сторінки глави → витяг `site_login_hash` з JS → AJAX `mod=load_chapters_image` повертає `<img data-src=...>`
- **Список глав**: AJAX `mod=load_chapters` з `news_category=54` (примусово; на сторінці тайтла дефолтна категорія повертає інший рендер)
- **Завантаження**: async httpx, семафор 6-8 паралельних, retry x3, skip-on-404 (на сайті є мертві посилання, як обкладинка `_000a.jpg` Tokyo Ghoul)
- **CBZ** — ZIP без стиснення (JPEG уже стиснутий)
- **PDF** — img2pdf, лосслес
- **EPUB** — `kcc-c2e --profile KPW5 --manga-style --upscale` (RTL, оптимізовано під екран Paperwhite)

## Структура

```
src/manga_in_ua_dl/
├── scraper.py        — HTTP клієнт + парсинг manga.in.ua
├── downloader.py     — паралельне завантаження картинок
├── packager.py       — CBZ
├── converters.py     — PDF (img2pdf) + EPUB (KCC subprocess)
├── cli.py            — CLI
└── web/
    ├── app.py        — FastAPI ендпойнти
    ├── jobs.py       — in-memory job tracker з прогресом
    ├── __main__.py   — `manga-dl-web` точка входу
    └── templates/    — Jinja2 + HTMX
```

## Юридичне

Інструмент призначений для особистого використання — резервне копіювання офіційно
доступного контенту manga.in.ua. Поважай авторів і перекладачів, підтримуй їх там,
де можеш.
# mangoteka
