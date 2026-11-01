# Карта существующих инструментов «Чушпана»

Обновлено в этапе 3/13, 30 октября 2026 года. Исходники, безопасные автотесты
и адресные живые проверки собственных тестовых окон. Музыка не запускалась.
См. [отчёт этапа 3](CHUSHPAN_STAGE3_REPORT.md), [аудит](CHUSHPAN_AUDIT.md)
и [дорожную карту](CHUSHPAN_ROADMAP.md).

## Регистрация и общие условия

`backend/tools/__init__.py:build_registry()` собирает включённые группы из
`tools.enabled`. Каждый `Tool` содержит имя, async handler, описание, параметры,
required и флаги slow/quiet. `Tool.schema()`/`ToolRegistry.schemas()` из
`backend/tools/registry.py` создают Ollama function schemas. `Conversation`
передаёт их в `/api/chat` и вызывает `ToolRegistry.call()`.

Ниже каждая строка указывает функцию регистрации перед этой общей цепочкой.
Все 29 основных инструментов доступны и в тексте, и голосом через общий реестр,
если включена группа и выполнены внешние условия. Это доступность интерфейса,
а не подтверждение живого распознавания. `memory` требует переданный `MemoryStore`.
Текущие группы: weather/news/system/timers/clock/memory/media.

«Обычные права процесса» означает, что специальный TCC доступ не нужен для операции.
Теперь все зарегистрированные handler проходят общий gate: типы/схема, контекст,
intent для изменения состояния, одноразовое подтверждение опасного действия.
Детали: [архитектура и безопасность](CHUSHPAN_ARCHITECTURE.md).
Разрешение микрофона нужно голосовому режиму в целом; перечисление «голос: да»
не означает отдельного grant на каждый инструмент.

## Дополнения этапа 3: desktop-инструменты

Все десять регистрируются `backend/tools/mac.py:mac_tools` в существующей группе
`system` через `build_registry`. Реализация — `backend/mac.py:MacDesktop`,
`backend/media/ytmusic_ax.swift`; путь LLM schemas общий с прежними tools.
Доступны тексту и голосу. Обязателен авторизованный адресный Mac Agent;
capabilities ниже не выдаются клиентским JSON. Тесты: `tests/test_mac.py`,
`tests/test_mac_check.py`, прежние `test_security.py` и живое собственное fixture.

### `get_apps`

Список запущенных приложений или active (`scope`). Чтение, `mac.apps`;
NSWorkspace/NSRunningApplication, AX/TCC не нужен. Риск — метаданные приложений.
Обе операции проверены реально. Это не список всех установленных программ.

### `manage_app`

Switch/обычный quit конкретного запущенного приложения по имени. `mac.apps`;
NSRunningApplication, повторная проверка bundle/PID, без forceTerminate.
Switch требует просьбу; quit — отдельный код. Риск — фокус, закрытие и диалог
сохранения документа. Исполнение подтверждается состоянием приложения;
живые switch/quit проверены только на собственном временном fixture.

### `get_windows`

Список AX-окон конкретного либо активного приложения; `mac.windows` и
Accessibility. Возвращает title/frame/состояние и непрозрачный window ID.
Риск — приватные заголовки окон; диагностический отчёт их не печатает.
Чтение проверено на Mac. ID — короткоживущая ссылка, не стабильный системный номер.

### `manage_window`

Focus, minimize/restore, left/right/maximize, move/resize, fullscreen/exit.
`mac.windows`, Accessibility, явная просьба. ID связан с устройством/сессией,
живёт 120 секунд и одно действие; PID/окно/положение проверяются повторно.
Геометрия ограничена видимой областью; операция проверяется чтением состояния.
Риск — неверный фокус/размер, частично выполненное действие при отказе приложения.
Spaces возвращает отказ без обходных клавиш. Сворачивание fixture не подтвердилось
даже его собственным AppKit-вызовом; живое восстановление свёрнутого окна этим
прогоном не доказано. Остальные перечисленные преобразования fixture проверены.

### `get_brightness` / `set_brightness`

Чтение/установка 0–100 через публичный IOKit; `mac.system`. Set требует просьбу
и проверяет read-back. Риск — изменение видимости экрана/неоднозначный дисплей.
На текущем дисплее подходящий публичный API не найден: возвращается честный отказ.
Частные DisplayServices API, драйверы и утилиты яркости не устанавливаются.

### `mac_power`

Lock/sleep/shutdown; `mac.system`, всегда отдельный код, не голосовой go_to_sleep.
Фиксированные Control-Command-Q (Accessibility), `pmset sleepnow` и адресный
AppleScript System Events для shutdown (Automation). Shell/force shutdown отсутствуют.
Риск — прерывание работы/соединения; приложения могут остановить shutdown из-за
документов. Возвращается отправка команды, а не проверенный факт выключения.
Реальные питание/блокировка не тестировались; исполнение проверено подстановками.

### `take_screenshot`

По прямой просьбе PNG основного экрана, локальный путь/mime/размер/Agent ID;
`mac.screen`, Screen Recording. Путь создаёт код, каталог 0700, файл 0600;
модель не выбирает destination. Внешней отправки и анализа изображения нет.
Риск — чувствительное изображение/хранение файла; политика очистки остаётся явной.
Реального снимка нет из-за отсутствующего разрешения; success/error проверены
на синтетическом изображении и подставном capture.

### `find_files`

Поиск части имени в настроенных `mac.file_roots`; `mac.files`, чтение имён.
По умолчанию Documents/Desktop/Downloads; клиент выбирает только ключ разрешённого
места. Нет чтения содержимого, обхода скрытых файлов, симлинков и app-пакетов;
лимиты глубины/элементов/результатов, признак неполного поиска.
Риск — раскрытие имён/неполный поиск/TCC файловых папок. Проверены реальные
изолированные файлы в тестовом каталоге, без сканирования пользовательских документов.

### `open_path`

Открытие разрешённого файла или Finder в папке, показ файла в Finder;
`mac.files`, явный адресный запрос. Относительный путь проверяется после resolve:
скрытые файлы, выход через symlink/`..`, пакеты, скрипты и executable запрещены.
Файлы ограничены форматами документов/изображений/медиа. Возвращается факт передачи
пути macOS, без утверждения о просмотре содержимого. Риск — приложение по умолчанию,
неоднозначное имя, смена фокуса. Проверено с подставным open и временными файлами;
это не sandbox против вредоносного локального процесса, меняющего FS одновременно.

### Диагностика вне LLM schemas

`python -m backend mac-check [--json]`: разрешения, компоненты, реестр и безопасные
чтения. Не записывает в рабочую память, не вызывает desktop-изменения/музыку/аудио.
`ok` чтения не означает проверку state-changing handler. Если AX плеера неизвестен,
диагностика явно помечает music status непроверенным. Логи живого fixture и полная
матрица результатов находятся в [отчёте этапа 3](CHUSHPAN_STAGE3_REPORT.md).

## Системные инструменты macOS

### `get_system_status`

- Назначение: батарея, оценка нагрузки CPU, память, диск, uptime, системный звук;
  `topic` ограничивает отчёт. CPU — нормализованный load average, не точный CPU usage.
- Реализация: `backend/tools/system.py:system_status`; регистрация: `system_tools`.
- Разрешения: обычные права процесса; фиксированные `pmset`, `memory_pressure`,
  `sysctl`, `osascript get volume settings`, Python API для диска/CPU.
- Голос: да. Тесты: `tests/test_tools.py` — парсеры и topic с подставным `run`;
  `tests/test_hud.py` — преобразование результата для HUD.
- Риски/границы: сведения о машине; совместимость вывода утилит; `all` вызывает
  несколько процессов. В этапе 3 реально прочитаны батарея и системный звук;
  остальные показатели проверены автотестами.

### `set_volume`

- Назначение: громкость всего Mac, mute/unmute, восстановление прежнего уровня;
  русская просьба о музыке маршрутизируется в YouTube Music отдельно.
- Реализация: `backend/tools/system.py:set_volume`, `system_tools.set_requested_volume`;
  регистрация: `system_tools`.
- Разрешения: выполнение фиксированного AppleScript в контексте запуска;
  отдельный Screen Recording не требуется. При перенаправлении музыки нужен AX.
- Голос: да. Тесты: `tests/test_tools.py` — mute/restore с fake Mac;
  `tests/test_youtube_music.py` — защита от путаницы системного/музыкального звука.
- Риски: изменение слышимости всего устройства, включая уведомления. Нужна явная
  текущая просьба; отдельного кода для обычной команды нет. Уровень проверяется в диапазоне 0–100.

### `open_app`

- Назначение: открыть/активировать установленное приложение; специальный путь
  YouTube Music проверяет Safari Web App и делает AX snapshot.
- Реализация: `backend/tools/system.py:open_app`, `system_tools.open_requested`;
  регистрация: `system_tools`.
- Разрешения: запуск `open -a` от текущего пользователя; для специального пути
  YouTube Music нужен Accessibility. Общего списка разрешённых приложений нет.
- Голос: да. Тесты: регистрация в `tests/test_tools.py`, музыкальные схемы/обнаружение
  web app в `tests/test_youtube_music.py`. Системный open проверен на собственном
  fixture; LLM gate отдельно проверен автотестами.
- Риски: запуск любого установленного приложения по имени и его стартовых
  действий, смена фокуса; allowlist пока нет. Пустое имя, пути, URL и ведущий
  `-` отклоняются; shell не используется. Общая защита проверена в `tests/test_security.py`.

## Данные, время и память

### `get_weather`

- Назначение: текущая погода и прогноз 1–7 дней, домашнее место либо указанный город.
- Реализация: `backend/tools/weather.py:WeatherService.forecast`, геокодирование
  `backend/tools/geocode.py`; регистрация: `weather_tools`.
- Разрешения: интернет Open-Meteo; для обычного инструмента TCC location не нужен.
- Голос: да. Тесты: `tests/test_tools.py`, `test_geocode.py`, `test_russian.py`,
  `test_briefing.py`, `test_morning.py` — MockTransport/синтетические координаты.
- Риски: передача города/координат внешнему сервису, неверный одноимённый город,
  временная недоступность API. Домашняя география сейчас не настроена.

### `get_ai_news`

- Назначение: свежие новости ИИ из заданных RSS, фильтр topic, count, кэш.
- Реализация: `backend/tools/news.py:NewsService.headlines`;
  регистрация: `news_tools`.
- Разрешения: интернет к настроенным feeds и их redirect-целям.
- Голос: да. Тесты: `tests/test_tools.py`, `test_briefing.py`, `test_russian.py`.
- Риски: недоверенное содержание/инъекции в модель, устаревший кэш, доступ к
  произвольным адресам при изменении доверенной конфигурации feeds. Это не общий поиск.

### `set_timer`

- Назначение: таймер до 24 часов; замена таймера с тем же label; объявление в голосе/HUD.
- Реализация: `backend/tools/timers.py:TimerManager.set_timer`;
  регистрация: `timer_tools`.
- Разрешения: обычные права процесса, без сети; звук требует голосового аудиовыхода.
- Голос: да. Тесты: `tests/test_tools.py`, `test_hud.py`, `test_russian.py`.
- Риски: неожиданное объявление; теперь проверяется текущая явная просьба;
  таймеры не сохраняются между перезапусками.

### `change_timer`

- Назначение: перезапустить выбранный таймер с новой длительностью.
- Реализация: `backend/tools/timers.py:TimerManager.change_timer`;
  регистрация: `timer_tools`.
- Разрешения: обычные права процесса. Голос: да.
- Тесты: `tests/test_tools.py` — изменение длительности и сохранение label.
- Риски: потеря прежнего deadline при неверном выборе; таймеры хранятся в RAM.

### `list_timers`

- Назначение: активные таймеры и оставшееся время.
- Реализация: `backend/tools/timers.py:TimerManager.list_timers`;
  регистрация: `timer_tools`.
- Разрешения: обычные права процесса. Голос: да.
- Тесты: `tests/test_tools.py`, `test_hud.py` — жизненный цикл и snapshot.
- Риски: раскрытие названий текущих задач через ответ/транскрипт.

### `cancel_timer`

- Назначение: отмена по id/label; при нескольких таймерах требуется выбор.
- Реализация: `backend/tools/timers.py:TimerManager.cancel_timer`;
  регистрация: `timer_tools`.
- Разрешения: обычные права процесса. Голос: да.
- Тесты: `tests/test_tools.py`, `test_hud.py` — выбор/отмена/валидация.
- Риски: потеря напоминания; отмена разрешается по текущей явной просьбе без лишнего кода.

### `time_until`

- Назначение: точный расчёт времени/дней до времени суток или даты.
- Реализация: `backend/tools/clock.py:Clock.time_until`; регистрация: `clock_tools`.
- Разрешения: обычные права процесса, без сети. Голос: да.
- Тесты: `tests/test_clock.py`, `test_russian.py`.
- Риски: неоднозначная дата/часовой пояс; время берётся из настроек/системной зоны.
  Отдельного `get_current_time` нет: текущее время входит в служебный context note.

### `remember`

- Назначение: сохранить факт либо заменить существующий по `replaces`.
- Реализация: `backend/tools/memory.py:LongTermMemory.remember`,
  `backend/memory/store.py`; регистрация: `memory_tools` при подключённом store.
- Разрешения: запись SQLite от пользователя. Голос: да.
- Тесты: `tests/test_memory.py`, `test_store.py`, `test_russian.py`, `test_personality.py`.
- Теперь запись/исправление требуют одноразового подтверждения; замена факта атомарна.
  Риски: чувствительные данные, инъекции через факты и ограничения разбора намерения;
  факты по-прежнему входят в prompt.

### `forget`

- Назначение: удалить один выбранный факт по id/подстроке; неоднозначный выбор отклоняется.
- Реализация: `backend/tools/memory.py:LongTermMemory.forget`;
  регистрация: `memory_tools` при подключённом store.
- Разрешения: запись SQLite. Голос: да.
- Тесты: `tests/test_memory.py`, `tests/test_security.py` — явная просьба, выбор,
  подтверждение, инъекции, старые/чужие/повторные коды, изменение факта до подтверждения.
- Исправлено отсутствие intent guard: требуется конкретная просьба и отдельный код.
  Прежние сообщения, экспорты и логи по-прежнему не очищаются этим инструментом.

## Текущий провайдер YouTube Music и Safari

Регистрация семи схем: `backend/media/youtube.py:YouTubeMusic.tools()` →
`backend/tools/media.py:media_tools()` → `_route_playback_tools()` → `build_registry`.
Routing меняет handler `play_music` и `media_control`, добавляет `target`.
`media.youtube_music.enabled=false` убирает музыкальные схемы при выбранном провайдере.

### `play_music`

- Назначение: песня/исполнитель/альбом/плейлист, сохранённый default или явно
  названный сценарий; `open_only` открывает страницу.
- Реализация: `backend/tools/media.py:MediaService.play_music` →
  `backend/media/youtube.py:YouTubeMusic.play` → `backend/media/safari.py`.
- Регистрация: `YouTubeMusic.tools` с переназначением handler в routing.
- Разрешения: интернет, доступ к web app, Accessibility для приложения запуска;
  авторизованный аккаунт в web app для личной библиотеки. Screen Recording не нужен.
- Голос: да. Тесты: `tests/test_youtube_music.py`, `test_youtube_video.py`, `test_morning.py`.
- Риски: воспроизведение/смена фокуса, ошибочный выбор; предусмотрены уточнение
  неоднозначности и проверка прогресса; действует общий gate текущей просьбы.

### `music_search`

- Назначение: найти музыку/альбом/исполнителя или собственный плейлист без запуска.
- Реализация/регистрация: `backend/media/youtube.py:YouTubeMusic.search` / `tools`.
- Разрешения: интернет; для библиотечного/fallback поиска — web app + Accessibility.
- Голос: да. Тесты: `tests/test_youtube_music.py` — API, fallback, выбор, корректные ID.
- Риски: запрос уходит Google; fallback вводит текст в проверенное поле web app,
  может активировать окно; чтение библиотеки может быть неполным.

### `music_playlist`

- Назначение: list/tracks/get/assign, назначение default/сценарного плейлиста.
- Реализация/регистрация: `backend/media/youtube.py:YouTubeMusic.playlist` / `tools`.
- Разрешения: web app + Accessibility/интернет либо необязательная auth API;
  assign пишет предпочтение в SQLite, требует явную просьбу по regex.
- Голос: да. Тесты: `tests/test_youtube_music.py` — библиотека, tracks, intent,
  неоднозначность и сохранение в общей памяти.
- Риски: личная библиотека, смена назначения и навигация; большой список неполон;
  assign теперь требует отдельного подтверждения; редактирование/удаление самих плейлистов
  не поддерживается.

### `media_control`

- Назначение: музыкальные pause/resume/stop/next/previous/repeat/like/open;
  `target=video` — только pause/resume/stop для обычного YouTube в Safari.
- Реализация: `backend/tools/media.py:MediaService.media_control`,
  `backend/media/safari.py:SafariMusic.control`,
  `backend/media/youtube_video.py:SafariYouTube.control`.
- Регистрация: `YouTubeMusic.tools` → routing с добавлением `target=auto|music|video`.
- Разрешения: Accessibility, открытая нужная страница; сеть для самого контента.
- Голос: да. Тесты: `tests/test_youtube_music.py`, `test_youtube_video.py`.
- Риски: неверная цель/побочный лайк; проверяются контекст, AX-элемент и состояние,
  лайк требует прямой просьбы. `shuffle_on/off` объявлены, но возвращают отказ;
  stop — пауза. Видео не получает музыкальные next/volume/repeat через этот handler.

### `now_playing`

- Назначение: состояние/название/артист/альбом/позиция/громкость плеера YouTube Music.
- Реализация: `backend/media/safari.py:SafariMusic.now_playing`;
  регистрация: `YouTubeMusic.tools`.
- Разрешения: Accessibility и доступный web app; отдельный HTTP API не требуется.
- Голос: да. Тесты: `tests/test_youtube_music.py` — разбор и подтверждение состояния.
- Риски: личные метаданные; snapshot может повторно открыть/активировать dormant
  web app, если AX-дерево недоступно. Не сообщает состояние обычного Safari-видео.

### `media_seek`

- Назначение по схеме: абсолютная/относительная перемотка трека.
- Реализация: `backend/media/youtube.py:YouTubeMusic.seek` →
  `backend/media/safari.py:SafariMusic.seek`; регистрация: `YouTubeMusic.tools`.
- Разрешения: текущий handler отказывает до реального управления.
- Голос: схема доступна, **действие не поддерживается**.
- Тесты: `tests/test_youtube_music.py::test_repeat_is_verified_and_unsupported_controls_do_not_mutate`.
- Риски: модель видит неподдерживаемую возможность; handler возвращает честную ошибку.

### `media_volume`

- Назначение: громкость только YouTube Music, абсолютный уровень или шаг up/down.
- Реализация: `backend/media/youtube.py:YouTubeMusic.volume` →
  `backend/media/safari.py:SafariMusic.set_volume`; регистрация: `YouTubeMusic.tools`.
- Разрешения: Accessibility и ползунок плеера; результат перечитывается.
- Голос: да. Тесты: `tests/test_youtube_music.py` — границы, направление,
  маршрутизация ошибочного вызова set_volume, подтверждение AX-уровня.
- Риски: слишком высокая/нулевая громкость; есть диапазон 0–100 и эвристика
  направления; обычная явно запрошенная команда не требует отдельного кода.

## Голосовой инструмент с отдельной регистрацией

### `go_to_sleep`

- Назначение: закончить беседу и вернуться к ожиданию активации.
- Реализация/регистрация: `backend/voice.py:sleep_tool()` → параметр `extra`
  `build_registry`; только voice с включённой активацией.
- Разрешения: изменение внутреннего состояния; микрофон нужен для следующей активации.
- Голос: да, в обычном текстовом реестре отсутствует.
- Тесты: `tests/test_tools.py`, `test_voice_loop.py`, `test_hud.py`, `test_wakeword.py`.
- Риски: это **не отключение микрофона**, не завершение процесса и не системный сон Mac.
  В `Conversation` имя инструмента, ошибочно выданное моделью обычным текстом,
  также может быть преобразовано в вызов без аргументов.

## Резервный Spotify: те же имена, другие handler

При `media.provider=spotify` `media_tools()` регистрирует только четыре
музыкальных инструмента, остальные семь-схемные возможности YouTube Music
не добавляются. Голосовой доступ сохраняется. Routing видео остаётся общим.

- `play_music`: `backend/tools/media.py:MediaService.play_music` → `Spotify.search_and_play`.
  Нужны Spotify, Apple Events/Automation; для поиска также Accessibility и
  Screen Recording. Тесты `tests/test_media.py` проверяют fake Spotify и BMP/button
  detection, `test_youtube_video.py` — routing. Риски: первый результат поиска,
  screenshot окна, координатный клик, пауза старого трека перед поиском.
  `quiet=true`; отдельного API-поиска/выбора кандидатов как у YouTube Music нет.
- `media_control`: `MediaService.media_control`/`_music_control` → `Spotify.command`,
  либо Safari video routing. Для музыки — Automation; для видео — Accessibility.
  Тесты `test_media.py`, `test_youtube_video.py`; риск неверного музыкального действия.
- `now_playing`: `MediaService.now_playing` → `Spotify.now_playing`.
  Нужна Automation, тест `test_media.py`; риск раскрытия музыкальных метаданных.
- `media_volume`: `MediaService.media_volume`/`Ducker` → `Spotify.set_volume`.
  Нужна Automation, тесты `test_media.py`; риск громкости и восстановления
  ducked-уровня. Ducking/`play_volume` относятся именно к этому провайдеру.

В этом аудите провайдер не переключался и Spotify не запускался.

## Возможности вне LLM-реестра

Это тоже существующие средства ассистента, но модель не может произвольно
вызывать их по имени через tool schema.

- **Утренний сценарий:** `backend/brain/morning.py:MorningRoutine`,
  `backend/brain/conversation.py:_morning_reply`, вызов по распознанному приветствию.
  Погода/Delfi/календарь/плейлист; голос и текст. Calendar/Location TCC и сеть,
  затем AX музыки. Тесты `test_morning.py`. Риски: персональные события,
  запрос разрешения `fullAccess` календаря, автоматически запланированная музыка.
- **Личные данные Mac:** `backend/personal.py`/`personal.swift`, только утренний
  сценарий, не отдельный tool. Читаются события двух дней и разовая геопозиция;
  helper компилируется/подписывается/запускается. Тесты `test_morning.py`,
  Swift typecheck; TCC live не проверен. Временные результаты удаляются,
  но произнесённый календарь может попасть в историю утреннего ответа.
- **Стартовая сводка:** `backend/brain/briefing.py`, голос/CLI briefing,
  weather/news/LLM и маркер `data/last_briefing`. Тесты `test_briefing.py`;
  нужна сеть для актуальных данных, риск неуместного объявления при запуске.
- **AX-мост:** `backend/media/safari.py:AccessibilityApp` и `ytmusic_ax.swift`.
  JSON stdin/stdout, status/snapshot/press/set/search, `xcrun swiftc` при первом
  использовании. Не универсальная схема LLM. Accessibility, проверка identity
  элементов; тесты `test_youtube_music.py`, `test_youtube_video.py` и Swift typecheck.
- **HUD wake/sleep:** `backend/hud/server.py`/`voice.py`, WebSocket, без LLM;
  голосовой режим. Тесты `test_hud.py`; локальный клиент без auth может активировать слушание.
- **HUD autostart:** `backend/voice.py:autostart_switch`, `backend/autostart.py`.
  WebSocket boolean; записывает/паркует LaunchAgent для следующего входа.
  CLI install/start/stop/uninstall отдельно вызывает launchctl. Тесты
  `test_autostart.py`, `test_hud.py`; риск закрепления фонового запуска.
- **App bundle/окно:** `backend/app_bundle.py`, `backend/hud/window.py`.
  CLI/старт voice, не LLM; копирование Python, codesign/LaunchServices,
  нативное окно. Тесты `test_autostart.py`, `test_hud.py`; риск жизненного цикла/TCC identity.
- **Экспорт:** `backend/voice.py:export_button` и `backend/memory/export.py`.
  HUD WebSocket, не LLM; запись Downloads/`data/exports`, `open -R`.
  Тесты `test_export.py`, `test_russian.py`, `test_hud.py`; риск открытой копии диалога.
- **Буфер обмена/fullscreen/новостная ссылка:** `_Api` в `backend/hud/window.py`,
  `frontend/src/clipboard.ts`/`panels.ts`; кнопки HUD, не LLM и не отдельная голосовая
  команда. Обычные GUI-права, AppKit/pbcopy/default browser. Автоматического
  E2E-покрытия нет; ссылки ограничены http/https, риск перезаписи clipboard/выхода в сеть.
- **Панели мониторинга:** `backend/hud/feeds.py`, `backend/footprint.py`;
  fixed polling системных показателей, `pgrep`/`top`, Ollama `/api/ps`, weather/news.
  Не LLM, вывод HUD голосового режима. Тесты `test_hud.py`, `test_resources.py`;
  риски фоновых запросов/процессов и метаданных.
- **CLI памяти:** `backend/memory/cli.py`, list/forget/clear без LLM;
  `test_memory.py`. Права SQLite; удаления не требуют интерактивного подтверждения.
- **Диагностика/установка/bench:** `backend/doctor.py`, `backend/media/cli.py`,
  `backend/wake_test.py`, `backend/echo_test.py`, `backend/bench*.py`, `scripts/setup.sh`.
  CLI, не LLM. Могут читать личную библиотеку, открывать микрофон/звук,
  генерировать ответы, скачивать модели, запускать сервисы или писать файлы.
  Нельзя считать их все безопасными read-only командами только по названию.

## Что повторно использовать на этапе 2

`Tool`/`build_registry`, разделение схем и handler, `ToolError`, единый
`user_text`, существующие `system_tools`, `AccessibilityApp`, верификацию
AX identity/scope, безопасный запуск процессов без shell, музыкальное
разделение Mac/music/video, state machine/HudBus и изолированные тесты.
Внедрение контроля разрешений должно охватить этот общий путь и кодовые
сценарии вне реестра. Добавление произвольного shell tool не требуется.


## Дополнение этапа 4: Gemini, изображения и Shortcuts

Появились ещё четыре инструмента: в текущем YouTube Music-профиле 33 инструмента
с памятью, в voice с go_to_sleep — 34. Gemini получает те же схемы и вызывает
тот же Tool.invoke/валидацию/Agent/confirmation manager. SDK не исполняет функции.

- `analyze_screen`: `backend/vision.py`, регистрация `vision_tools` в system-группе.
  Требует mac.screen, доступный адресный Agent и Screen Recording. Только явная
  просьба; Gemini-профиль требует свежего кода перед захватом/передачей Google.
  Существующий MacDesktop.screenshot переиспользуется; временный PNG удаляется
  при успехе и ошибке. Ответ без снимка не выдаётся за анализ. Голос поддержан
  общим Conversation; живой микрофон не проверен. Тесты: test_vision/test_providers.
  Риски: личные пиксели, неполный экран, ошибки модели, облачная передача.
- `analyze_image`: тот же файл/регистрация, mac.files; выбранный PNG/JPEG/WebP
  внутри разрешённого root, имя в текущей просьбе, без URL/скрытых/обхода root.
  До 8 МиБ/16 млн пикселей; проверка формата, EXIF удаляется. В Gemini — код,
  внутренний hash содержимого включён в fingerprint и перепроверяется перед отправкой.
  Исходный файл сохраняется, bytes не пишутся в SQLite. Тесты: test_vision.
  Риски: секреты на изображении не скрываются автоматически, декодирование и неверное чтение.
- `list_shortcuts`: `backend/shortcuts.py`, shortcut_tools в system, mac.shortcuts
  и Agent. Читает список/UUID через поддерживаемый CLI; запуск этим не разрешается.
  Голосовой путь общий. Тесты: test_shortcuts. Риск: частные названия/недоверенный текст.
- `run_shortcut`: тот же файл/регистрация, mac.shortcuts; только вручную разрешённый
  точный UUID и имя, явная просьба и свежий device/session код. Вход none/text/url
  проверяется; передаётся в приватном файле, без shell/произвольных CLI-флагов.
  Вывод недоверенный, не вызывает новые инструменты; лимиты/тайм-аут/ошибки.
  Требования TCC зависят от действий самой команды. Тесты: test_shortcuts,
  test_providers и прежние регрессии security/voice. Живой запуск существующей
  команды не выполнялся. Риски: изменения внутри Shortcut с прежним UUID,
  его собственные shell/сеть/удаление, частичное выполнение после тайм-аута.

Анализ изображения выполняется отдельным LLM-запросом **без tools**. Полученный
анализ выдаётся напрямую, а не запускает новый цикл действий. Общий системный prompt
также считает текст изображения и вывод Shortcut недоверенными данными.
Фильтрация известных ключей/помеченных текстовых секретов — дополнительная защита,
а не гарантия обнаружить любой пароль или скрыть секретные пиксели.

Рецепты/ручная настройка и реальное состояние: [Shortcuts](CHUSHPAN_SHORTCUTS.md),
[отчёт этапа 4](CHUSHPAN_STAGE4_REPORT.md). Общего shell, кликов по совету модели,
публичного API, автоматического создания команд и мобильного запуска здесь нет.

## Дополнение этапа 5: профиль и ночной сценарий

- `manage_memory`: `backend/memory/profile.py`, memory-группа `build_registry`.
  Общий текстовый/голосовой Conversation распознаёт поддержанные команды до LLM.
  Просмотр/источник/поиск — read; изменение/удаление/очистка/характер/opt-in —
  dangerous и свежий device/session код. Даже read требует аутентифицированной
  сессии и соответствия текущей просьбе. Аргументы/ID/revision проверяются,
  prepared fingerprint блокирует устаревшее согласие. Тесты test_profile_memory,
  прежние memory/security/providers/voice. Риски: неполный локальный словарь,
  ошибочная релевантность, непомеченная чувствительная информация, старые экспорты.
- `night_mode`: `backend/night.py`, system-группа, только при наличии общей SQLite.
  Нужны mac.system и mac.shortcuts, адресный Mac Agent, явная фраза и код.
  Встроенный дисплей определяется CoreGraphics; HISENSE — по заданному UUID,
  группы и посторонние экраны не выбираются. BetterDisplay имеет только фиксированные
  get/set яркости; LLM не получает CLI-флаги/shell. Запуск Youtube sleep — через
  проверенный ShortcutsService, отдельный закрытый порт ожидания. Предыдущие значения,
  lease и статусы шагов сохраняются до побочных эффектов. Тесты test_night,
  включая голосовой путь с fake speaker, конкурентные экземпляры и частичные отказы.
  Живыми были только чтения; реальное изменение/запуск требует ручной приёмки.

Полные ограничения и команды проверки: [этап 5](CHUSHPAN_STAGE5_REPORT.md).

## Дополнение этапа 6: youtube_sleep

Реализация: `backend/media/sleep_video.py`; официальное API/SQLite —
`backend/media/youtube_api.py`, Safari — `youtube_playback.py`, `youtube_safari.swift`.
Регистрация в `build_registry` в группе `media`, если передан общий `MemoryStore`.
Регистрируется отдельно от существующих `play_music`, `music_playlist`, `media_control`.
LLM видит схему, но параметры должны совпадать с текущим детерминированным intent;
текст/распознанный голос также попадают в этот путь напрямую.

Все действия требуют авторизованной сессии, `mac.media` и адресного Mac Agent.
Чтение/счётчик/current — READ; запуск/пауза/метки/подключение/refresh — CHANGE;
reset — DANGEROUS, код привязан к устройству/сессии/плейлисту/версии истории.
API только читает; облако получает ID, не память. Safari требует Accessibility,
Automation и разрешения JavaScript из Apple Events; экран не снимается.
Неоднозначные вкладки, autoplay и отключённый TV сообщаются без выдуманного успеха.
Все новые тесты — без реальной сети/браузера/звука; ручная приёмка описана в
[CHUSHPAN_YOUTUBE_API.md](CHUSHPAN_YOUTUBE_API.md).
