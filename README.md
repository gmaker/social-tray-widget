# Social Tray Widget

A lightweight Windows system-tray widget that shows your **TikTok + YouTube + Instagram + Telegram + VK** followers, views, and likes side by side — right on the taskbar.

![Screenshot](screenshots/screenshot.png)

Every platform is read through its **official API** — OAuth for TikTok, YouTube and Instagram, an MTProto user session for Telegram, a service key for VK. Nothing here scrapes a page or replays a private endpoint, so nothing here gets you rate-limited or banned.

---

## Features

- Two tray icons: **Σ followers** · **Σ views** — click either for the detail popup
- One table for every platform, plus totals: followers · views · likes
- **Deltas**: green `(+12)` / red `(-3)` next to every value, counting from the last time you acknowledged them. Click the popup to rebase — the deltas survive restarts
- Flip-digit animation in the popup (Solari board style)
- Sound notification on new followers (configurable WAV, mutable from the tray)
- Enable/disable any platform from the tray menu
- Tokens refresh themselves — after setup you never touch a developer console again
- One provider class per platform: adding a fourth is one file plus one line

---

## Requirements

- Windows 10/11
- Python 3.10+
- A developer app per platform you enable (see **Setup**)

```
pip install -r requirements.txt
```

---

## Setup

Copy `settings.json.example` to `settings.json`, then fill in only the platforms you want. `settings.json`, `tokens/` and `state.json` are all in `.gitignore` — no credential is ever committed.

Run with `start.bat`, or:

```
python run.py
```

### TikTok

Create an app at [developers.tiktok.com](https://developers.tiktok.com/) with the scopes `user.info.basic`, `user.info.profile`, `user.info.stats`, `video.list`, and the redirect URI `http://localhost:8080/callback`. Put the **Client Key** and **Client Secret** into `settings.json`. The first run opens a browser.

### YouTube

Create a Google Cloud project, enable **YouTube Data API v3**, and make an OAuth client of type *Web application* with the redirect URI `http://localhost:8080/callback`. Put the **Client ID** and **Client Secret** into `settings.json`. The first run opens a browser.

> **Publish the consent screen.** While it sits in *Testing* mode Google expires refresh tokens after 7 days and every poll then dies with `invalid_grant`.

YouTube rounds the public subscriber count to 3 significant figures — the API can't return the exact number even to the channel owner.

### Instagram

The account must be **professional** (Business or Creator) and **public**.

1. Create a Meta app at [developers.facebook.com](https://developers.facebook.com/) and add the **Instagram API with Instagram Login** product.
2. **Roles** tab → grant the account the *Instagram Tester* role. Then accept the invite from the account itself: Instagram → Settings → Apps and websites → Tester invites.
3. **Instagram → API setup with Instagram login → Generate access tokens** → copy the token into `providers.instagram.setup_token`.

That's it — no app secret, no redirect URI, no browser flow. The dashboard token is already valid for 60 days; the widget adopts it on first run, moves it to `tokens/instagram.json`, blanks `setup_token`, and refreshes it from then on.

### Telegram

Bot API exposes neither post views nor reactions, so this provider signs in as **you** over MTProto and reads what the Telegram apps show.

1. Get **api_id** and **api_hash** at [my.telegram.org](https://my.telegram.org/) → API development tools (Short name is strictly alphanumeric).
2. Put them and the channel's `@username` into `providers.telegram` in `settings.json`. A private channel needs its numeric `-100…` id instead, and the signed-in account must be able to see it.
3. Run `python telegram_login.py` in a real terminal and sign in with the phone number + the code Telegram sends (and the 2FA password, if set). This writes `tokens/telegram.session`; the widget itself never prompts. Any Python 3.10+ will do — the script re-launches itself inside the app's own `.venv`, creating it and installing the requirements on first use.

> **The session file is a password.** `tokens/telegram.session` grants full access to the account — it lives in the gitignored `tokens/` folder; don't copy it anywhere. Revoking it (Telegram → Settings → Devices) just means running `telegram_login.py` once more.

### VK / VK Video / VK Clips

One community can feed three rows: **VK** (members + wall post likes/views), **VK Video** (long-video likes/views — a VK Video channel is its backing community), and **VK Clips** (the short vertical videos). The VK Video and VK Clips rows show a dash for followers so the community's members aren't counted twice; in the popup the members number is centred over all three.

1. Create any app at [dev.vk.com](https://dev.vk.com/) → Приложения → Создать приложение (type Мини-приложение, any category). No verification needed to start; VK's docs warn unverified business profiles may be blocked 60 days after the first app.
2. Copy the **сервисный ключ доступа** from the app's settings into `providers.vk.service_token` (and `providers.vkvideo` / `providers.vkclips` — the same key works for all three).
3. Set `group` to the community's screen name or numeric id (find the id in any video URL: `vkvideo.ru/video-XXXXXXX_YYY`).

Notes: the docs claim `video.get` needs a user token — in practice the service key works. A community access key from group settings is *not* enough for any of these. VK Clips has no list API at all, so the widget finds each clip by scanning the community's video-id neighbourhood — it needs at least one regular video to anchor that scan; a clip-only community can't be counted. Fold VK Clips into VK Video with the tray's **Merge VK Video + Clips** for one combined video number.

<details>
<summary><b>Instagram troubleshooting</b> — three errors that look like something else</summary>

| Error | What it actually means |
|---|---|
| `Session key invalid … code 452` when exchanging the token | The token is **already long-lived**; this product has no short-lived stage and nothing to exchange. Just paste it into `setup_token` and let the widget adopt it. Don't call `ig_exchange_token`. |
| `UserInvalidCredentials` at the dashboard login | The account has no native Instagram password because it signs in through Facebook. Set one: Accounts Center → Password and security. |
| `OAuthException: your account's future activity history off Meta technologies is currently turned off` | Turn **off** "Disconnect future activity" in Accounts Center → Your activity off Meta technologies. Leave it off — it breaks token refresh too. |

Posts made before the account switched to professional never report insights, so their views can't be counted — the widget detects them once and skips them afterwards.
</details>

---

## What exactly is counted

| Row | Followers | Likes | Views |
|---|---|---|---|
| **TikTok** | account followers, exact | the account's total likes counter | plays summed over every video |
| **YouTube** | subscribers (YouTube rounds the public number to 3 significant figures) | likes summed over every upload — Shorts and unlisted videos included | the channel's total view counter — all content, Shorts included |
| **Instagram** | followers, exact | likes summed over every post | lifetime views summed over every post's insights¹ |
| **Telegram** | channel subscribers, exact | reactions summed over every post | views summed over every post |
| **VK** | community members, exact | likes summed over every wall post | wall post *impressions* — how many times posts appeared in feeds² |
| **VK Video** | shared with VK — one community, one number (the popup merges the cell) | likes summed over the long videos | plays summed over the long videos |
| **VK Clips** | shared with VK | likes summed over every clip | plays summed over every clip³ |

¹ Instagram: posts published before the account became professional never report insights and are skipped; Stories are not included (their per-story stats die 24h after expiry, and the API offers no per-story history).
² VK's post counter is *feed impressions*, not video plays — the VK rows measure different things and never double-count.
³ **VK Clips** are the short vertical videos. The public API has no list method for them, so the widget discovers each clip's id by scanning the community's video-id neighbourhood and reads its stats one clips-only batch at a time (see the VK section). It needs at least one regular video to anchor the id space — a clip-only community can't be counted. Fold this row into VK Video from the tray (**Merge VK Video + Clips**) for one combined video number.

---

## Settings reference

| Key | Default | Description |
|-----|---------|-------------|
| `poll_interval` | `60` | Seconds between polls |
| `sound_enabled` | `true` | Play a sound on new followers |
| `sound_volume` | `1.0` | Playback volume (0.0 – 1.0) |
| `sound_followers` | `snd/2.wav` | WAV played when followers increase |
| `color_subs` | `[57,135,229]` | RGB of the followers column and tray icon |
| `color_views` | `[25,158,112]` | RGB of the views column and tray icon |
| `color_likes` | `[201,133,0]` | RGB of the likes column |
| `merge_vkvideo_clips` | `false` | Show VK Clips as one row with VK Video (also toggleable from the tray menu) |

Per platform, under `providers.<name>`:

| Key | Platforms | Default | Description |
|-----|-----------|---------|-------------|
| `enabled` | all | `false` | Also toggleable from the tray menu |
| `color` | all | — | RGB of the platform's name in the popup |
| `client_key` / `client_secret` | tiktok | — | From the TikTok app |
| `client_id` / `client_secret` | youtube | — | From the Google OAuth client |
| `redirect_uri` | tiktok, youtube | `http://localhost:8080/callback` | Must match the console exactly |
| `setup_token` | instagram | — | Dashboard token; consumed on first run |
| `api_id` / `api_hash` | telegram | — | From my.telegram.org |
| `channel` | telegram | — | `@username`, or `-100…` id for a private channel |
| `proxy` | telegram | `""` | Empty = follow the Windows system proxy (for ISPs that block MTProto directly); `none` = force direct; or `socks5://host:port` |
| `service_token` | vk, vkvideo, vkclips | — | Service key of any VK ID app; one key can serve all three rows |
| `group` | vk, vkvideo, vkclips | — | Community screen name or numeric id (no minus) |
| `count_views` | tiktok, instagram, telegram, vk, vkvideo, vkclips | `true` | Off = skip the views calls |
| `views_refresh_min` | instagram, telegram, vk, vkvideo, vkclips | `15` | Minutes between views/likes passes |
| `count_likes` | youtube | `true` | Off = skip the uploads walk |
| `likes_refresh_min` | youtube | `15` | Minutes between likes passes |

---

## Tray menu

Right-click either tray icon:

- **Platforms** — enable/disable each source (saved to `settings.json`)
- **Show** — toggle the likes/views columns (followers stay pinned)
- **Merge VK Video + Clips** — one combined video row (shown only when both are enabled)
- **Refresh now** — poll immediately
- **Sound: ON/OFF** — mute the follower sound
- **Exit**

---

## How it stays inside the rate limits

Followers cost one call per platform and stay live at `poll_interval`. Views and likes are the expensive ones, so each is cached for `views_refresh_min` / `likes_refresh_min` minutes:

- **YouTube** has no channel-level like total, so likes mean walking the uploads playlist 50 videos at a time — `2 × ceil(uploads / 50)` units of the 10,000/day quota per pass. Beware that `statistics.videoCount` counts only *public* videos and understates this badly: a channel reporting 133 can hold 300+ items in its uploads playlist. Once a minute that alone would exhaust the quota; every 15 minutes it lands near a quarter of it.
- **Instagram**'s limit is `4800 × impressions per 24h`, so a quiet account has a small budget. Views come from `/insights` (the documented `view_count` field on the media node is silently omitted by this product), batched 50 posts per call via `?ids=`. A pass costs 2 calls, not 50.
- **Telegram** views and reactions mean walking the channel history. The recent tail (the last ~500 posts) is re-read every pass so a fresh post's views stay live as they climb; older posts, whose counts have settled, are summed once and frozen, capping the walk at ~the tail length however long the history is. Once a day the frozen part is recomputed to absorb deletions and any late growth.
- **VK / VK Video / VK Clips** walk 100 items per request under the service key's 5 rps; all three are cached for `views_refresh_min` minutes, so a poll normally costs one `groups.getById`. VK Clips adds a `clips_count` check and, only when it changes, a short id-scan; a full re-scan happens just once, at first run. The historical (now undocumented) `wall.get` quota of ~5000 calls/day only matters past several thousand posts.

## Colours

The palette is validated, not eyeballed: every colour sits inside the OKLCH lightness band for the popup's `#141414` surface, clears the chroma floor and 3:1 contrast, and stays distinct under protanopia and deuteranopia. Only YouTube keeps its brand red — TikTok wears its other brand colour (cyan, darkened to fit the band) and Instagram the violet end of its gradient, because all three brand reds together read as one wash. Colour encodes the *metric*: a column is one hue top to bottom. The platform's name carries its identity. If you change these, re-check them rather than trusting your eye.

---
---

# Social Tray Widget (на русском)

Лёгкий виджет для системного трея Windows: показывает **TikTok + YouTube + Instagram + Telegram + VK** — подписчиков, просмотры и лайки — рядом, прямо на панели задач.

![Скриншот](screenshots/screenshot.png)

Каждая платформа читается через **официальный API** — OAuth у TikTok, YouTube и Instagram, пользовательская MTProto-сессия у Telegram, сервисный ключ у VK. Здесь нет скрейпинга страниц и обращений к приватным эндпоинтам, поэтому нет ни банов, ни блокировок по частоте.

---

## Возможности

- Две иконки в трее: **Σ подписчики** · **Σ просмотры** — клик открывает попап
- Одна таблица на все платформы плюс итоги: подписчики · просмотры · лайки
- **Дельты**: зелёное `(+12)` / красное `(-3)` рядом с каждым числом, считаются с момента последнего подтверждения. Клик по попапу перебазирует; дельты переживают перезапуск
- Анимация цифр в стиле табло Solari
- Звук при новых подписчиках (настраиваемый WAV, мьютится из трея)
- Включение/выключение любой платформы из меню трея
- Токены обновляются сами — после настройки в консоль разработчика больше не возвращаешься
- Один класс на платформу: добавить четвёртую — это один файл и одна строка

---

## Требования

- Windows 10/11
- Python 3.10+
- Приложение разработчика для каждой включаемой платформы (см. **Настройка**)

```
pip install -r requirements.txt
```

---

## Настройка

Скопируйте `settings.json.example` в `settings.json` и заполните только нужные платформы. `settings.json`, `tokens/` и `state.json` прописаны в `.gitignore` — ключи никогда не попадут в репозиторий.

Запуск через `start.bat`, или вручную:

```
python run.py
```

### TikTok

Создайте приложение на [developers.tiktok.com](https://developers.tiktok.com/) со скоупами `user.info.basic`, `user.info.profile`, `user.info.stats`, `video.list` и redirect URI `http://localhost:8080/callback`. Впишите **Client Key** и **Client Secret** в `settings.json`. При первом запуске откроется браузер.

### YouTube

Создайте проект в Google Cloud, включите **YouTube Data API v3**, создайте OAuth-клиент типа *Web application* с redirect URI `http://localhost:8080/callback`. Впишите **Client ID** и **Client Secret** в `settings.json`. При первом запуске откроется браузер.

> **Опубликуйте consent screen.** Пока он в режиме *Testing*, Google убивает refresh-токены через 7 дней, и каждый опрос падает с `invalid_grant`.

YouTube округляет публичное число подписчиков до 3 значащих цифр — точное значение API не отдаёт даже владельцу канала.

### Instagram

Аккаунт должен быть **профессиональным** (Business или Creator) и **открытым**.

1. Создайте приложение на [developers.facebook.com](https://developers.facebook.com/) и добавьте продукт **Instagram API with Instagram Login**.
2. Вкладка **Roles** → выдайте аккаунту роль *Instagram Tester*. Затем примите приглашение из самого аккаунта: Instagram → Настройки → Приложения и сайты → приглашения тестировщика.
3. **Instagram → API setup with Instagram login → Generate access tokens** → скопируйте токен в `providers.instagram.setup_token`.

Всё — ни app secret, ни redirect URI, ни браузера. Токен из дашборда уже действует 60 дней: виджет принимает его при первом запуске, переносит в `tokens/instagram.json`, затирает `setup_token` и дальше продлевает сам.

### Telegram

Bot API не отдаёт ни просмотры постов, ни реакции, поэтому этот провайдер входит по MTProto **от вашего имени** и читает то же, что видят приложения Telegram.

1. Получите **api_id** и **api_hash** на [my.telegram.org](https://my.telegram.org/) → API development tools (Short name — строго буквы и цифры).
2. Впишите их и `@имя` канала в `providers.telegram` в `settings.json`. Приватному каналу нужен числовой id `-100…`, и аккаунт должен его видеть.
3. Запустите `python telegram_login.py` в обычном терминале и войдите: номер телефона + код из Telegram (и пароль 2FA, если включён). Скрипт запишет `tokens/telegram.session`; сам виджет никогда ничего не спрашивает. Подойдёт любой Python 3.10+ — скрипт сам перезапустится в собственном окружении приложения (`.venv`), при первом использовании создав его и установив зависимости.

> **Файл сессии — это пароль.** `tokens/telegram.session` даёт полный доступ к аккаунту — он лежит в игнорируемой git-ом папке `tokens/`; не копируйте его никуда. Если сессию отозвали (Telegram → Настройки → Устройства), просто запустите `telegram_login.py` ещё раз.

### VK / ВК Видео / VK Clips

Одно сообщество кормит три строки: **VK** (подписчики + лайки/показы постов стены), **VK Video** (лайки/просмотры длинных роликов — канал ВК Видео и есть его сообщество) и **VK Clips** (короткие вертикальные видео). В строках VK Video и VK Clips подписчики прочерком, чтобы участники не считались дважды; в попапе число участников отцентрировано по всем трём строкам.

1. Создайте любое приложение на [dev.vk.com](https://dev.vk.com/) → Приложения → Создать приложение (тип «Мини-приложение», категория любая). Для старта верификация не нужна; документация ВК предупреждает, что неверифицированный бизнес-профиль могут заблокировать через 60 дней после первого приложения.
2. Скопируйте **сервисный ключ доступа** из настроек приложения в `providers.vk.service_token` (и в `providers.vkvideo` / `providers.vkclips` — ключ один на все три строки).
3. В `group` впишите короткое имя сообщества или числовой id (id виден в ссылке любого ролика: `vkvideo.ru/video-XXXXXXX_YYY`).

Примечания: документация утверждает, что `video.get` требует пользовательский токен — на практике сервисный ключ работает. Ключа доступа сообщества из настроек группы для этого **не** хватает. У клипов списочного API нет вовсе, поэтому виджет находит каждый клип сканом соседних видео-id сообщества — нужен хотя бы один обычный ролик как якорь; сообщество из одних клипов посчитать нельзя. Строку VK Clips можно слить с VK Video через трей (**Merge VK Video + Clips**) в одно общее видео-число.

<details>
<summary><b>Instagram: разбор ошибок</b> — три штуки, которые означают не то, что написано</summary>

| Ошибка | Что на самом деле |
|---|---|
| `Session key invalid … code 452` при обмене токена | Токен **уже долгоживущий**; в этом продукте нет короткой стадии и менять нечего. Просто вставьте его в `setup_token`. `ig_exchange_token` вызывать не нужно. |
| `UserInvalidCredentials` при логине в дашборде | У аккаунта нет собственного пароля Instagram, вход идёт через Facebook. Задайте пароль: Accounts Center → Пароль и безопасность. |
| `OAuthException: your account's future activity history off Meta technologies is currently turned off` | Выключите «Disconnect future activity» в Accounts Center → Ваша активность вне технологий Meta. И не включайте обратно — это ломает и продление токена. |

Посты, опубликованные до перехода аккаунта на профессиональный, статистику не отдают никогда, поэтому их просмотры не считаются — виджет определяет их один раз и дальше пропускает.
</details>

---

## Что именно считается

| Строка | Подписчики | Лайки | Просмотры |
|---|---|---|---|
| **TikTok** | подписчики аккаунта, точно | суммарный счётчик лайков аккаунта | проигрывания, сумма по всем видео |
| **YouTube** | подписчики (YouTube округляет публичное число до 3 значащих цифр) | сумма лайков всех загрузок — включая Shorts и непубличные ролики | канальный счётчик просмотров — весь контент, включая Shorts |
| **Instagram** | подписчики, точно | сумма лайков всех постов | сумма lifetime-просмотров постов из insights¹ |
| **Telegram** | подписчики канала, точно | сумма реакций всех постов | сумма просмотров всех постов |
| **VK** | участники сообщества, точно | сумма лайков постов стены | *показы* постов стены — сколько раз посты мелькнули в лентах² |
| **VK Video** | общие с VK — сообщество одно, число одно (в попапе ячейка объединена) | сумма лайков длинных роликов | проигрывания длинных роликов |
| **VK Clips** | общие с VK | сумма лайков всех клипов | проигрывания всех клипов³ |

¹ Instagram: посты, опубликованные до перехода аккаунта на профессиональный, статистику не отдают и пропускаются; сторис не входят (их статистика умирает через 24 часа после истечения, истории по ним API не даёт).
² Счётчик постов VK — это *показы в ленте*, а не проигрывания видео: VK-строки меряют разное и никогда не задваиваются.
³ **VK Clips** — короткие вертикальные видео. Списочного метода для них в публичном API нет, поэтому виджет определяет id каждого клипа сканом соседних видео-id сообщества и читает статистику пачками только из клипов (см. раздел VK). Нужен хотя бы один обычный ролик, чтобы задать диапазон id — сообщество из одних клипов посчитать нельзя. Строку можно слить с VK Video через трей (**Merge VK Video + Clips**) в одно общее видео-число.

---

## Описание настроек

| Ключ | По умолчанию | Описание |
|------|-------------|----------|
| `poll_interval` | `60` | Интервал опроса в секундах |
| `sound_enabled` | `true` | Звук при новых подписчиках |
| `sound_volume` | `1.0` | Громкость (0.0 – 1.0) |
| `sound_followers` | `snd/2.wav` | WAV при росте подписчиков |
| `color_subs` | `[57,135,229]` | RGB колонки подписчиков и иконки в трее |
| `color_views` | `[25,158,112]` | RGB колонки просмотров и иконки в трее |
| `color_likes` | `[201,133,0]` | RGB колонки лайков |
| `merge_vkvideo_clips` | `false` | Показывать VK Clips одной строкой с VK Video (переключается и из меню трея) |

По платформам, в секции `providers.<имя>`:

| Ключ | Платформы | По умолчанию | Описание |
|------|-----------|-------------|----------|
| `enabled` | все | `false` | Переключается и из меню трея |
| `color` | все | — | RGB названия платформы в попапе |
| `client_key` / `client_secret` | tiktok | — | Из приложения TikTok |
| `client_id` / `client_secret` | youtube | — | Из OAuth-клиента Google |
| `redirect_uri` | tiktok, youtube | `http://localhost:8080/callback` | Должен точно совпадать с консолью |
| `setup_token` | instagram | — | Токен из дашборда, тратится при первом запуске |
| `api_id` / `api_hash` | telegram | — | С my.telegram.org |
| `channel` | telegram | — | `@имя`, либо id `-100…` для приватного канала |
| `proxy` | telegram | `""` | Пусто = системный прокси Windows (если провайдер режет MTProto напрямую); `none` = принудительно напрямую; либо `socks5://host:port` |
| `service_token` | vk, vkvideo, vkclips | — | Сервисный ключ любого приложения VK ID; один ключ на все три строки |
| `group` | vk, vkvideo, vkclips | — | Короткое имя сообщества или числовой id (без минуса) |
| `count_views` | tiktok, instagram, telegram, vk, vkvideo, vkclips | `true` | Выкл = не запрашивать просмотры |
| `views_refresh_min` | instagram, telegram, vk, vkvideo, vkclips | `15` | Минуты между проходами за просмотрами/лайками |
| `count_likes` | youtube | `true` | Выкл = не обходить плейлист загрузок |
| `likes_refresh_min` | youtube | `15` | Минуты между проходами за лайками |

---

## Меню трея

Правой кнопкой по любой иконке:

- **Platforms** — включить/выключить источник (сохраняется в `settings.json`)
- **Show** — скрыть/показать колонки лайков и просмотров (подписчики всегда видны)
- **Merge VK Video + Clips** — одна общая видео-строка (виден, только когда включены обе)
- **Refresh now** — обновить немедленно
- **Sound: ON/OFF** — мьют звука подписчиков
- **Exit** — выход

---

## Как виджет укладывается в лимиты

Подписчики стоят один запрос на платформу и обновляются живьём каждый `poll_interval`. Просмотры и лайки — дорогие, поэтому кэшируются на `views_refresh_min` / `likes_refresh_min` минут:

- **У YouTube** нет суммы лайков на уровне канала, поэтому лайки — это обход плейлиста загрузок по 50 видео: `2 × ceil(загрузок / 50)` единиц квоты (10 000/сутки) за проход. Осторожно: `statistics.videoCount` считает только **публичные** видео и сильно занижает картину — канал, показывающий 133, легко держит 300+ элементов в плейлисте загрузок. Раз в минуту это выело бы всю квоту в одиночку; раз в 15 минут — около четверти.
- **У Instagram** лимит `4800 × показы за 24 часа`, так что у тихого аккаунта бюджет маленький. Просмотры берутся из `/insights` (задокументированное поле `view_count` у media-ноды этот продукт молча не возвращает), пачками по 50 постов через `?ids=`. Проход стоит 2 запроса, а не 50.
- **У Telegram** просмотры и реакции — это обход истории канала. Свежий хвост (последние ~500 постов) перечитывается каждый проход, чтобы просмотры нового поста росли вживую; более старые посты, у которых счётчики уже устоялись, суммируются один раз и замораживаются — обход ограничен длиной хвоста, какой бы длинной ни была история. Раз в сутки замороженная часть пересчитывается, чтобы учесть удаления и запоздалый рост.
- **VK / ВК Видео / VK Clips** обходятся по 100 элементов за запрос при лимите сервисного ключа 5 запросов/сек; все три кэшируются на `views_refresh_min` минут, так что обычный опрос стоит один `groups.getById`. VK Clips добавляет проверку `clips_count` и, только при её изменении, короткий скан id; полный скан бывает лишь один раз, при первом запуске. Историческая (ныне недокументированная) квота `wall.get` ~5000 вызовов/сутки начинает мешать только после нескольких тысяч постов.

## Цвета

Палитра проверена, а не подобрана на глаз: каждый цвет попадает в полосу светлоты OKLCH для фона попапа `#141414`, проходит порог насыщенности и контраст 3:1 и остаётся различимым при протанопии и дейтеранопии. Красный сохранил только YouTube — TikTok носит свой второй фирменный цвет (циан, притемнённый под полосу), а Instagram фиолетовый край своего градиента: три фирменных красных вместе сливаются в одно пятно. Цвет кодирует **метрику** — колонка одного оттенка сверху донизу. Название платформы отвечает за опознание. Если меняете — перепроверяйте, а не доверяйтесь глазу.
