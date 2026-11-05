"""Проводка страницы: каждый элемент, который ищет app.js, есть в разметке."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def ids_the_page_looks_for():
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    return set(re.findall(r'getElementById\("([^"]+)"\)', source))


def test_the_page_looks_for_something():
    assert ids_the_page_looks_for()


def test_every_element_the_page_looks_for_exists():
    html = (ROOT / "page" / "index.html").read_text(encoding="utf-8")
    present = set(re.findall(r'id="([^"]+)"', html))

    assert ids_the_page_looks_for() - present == set()


def test_the_video_carries_its_own_controls():
    # Браузер не даёт запустить звук вне нажатия: автозапуск после ответа демона
    # отклоняется молча, и без встроенных кнопок зритель остаётся с чёрным кадром
    # и без способа что-либо сделать.
    html = (ROOT / "page" / "index.html").read_text(encoding="utf-8")
    tag = re.search(r"<video[^>]*>", html)

    assert tag is not None
    assert "controls" in tag.group(0)


def test_the_layer_repaints_when_cues_arrive():
    # Пауза там, где ждёшь перевода, — главный сценарий страницы. Пока кадры
    # не идут, timeupdate молчит, поэтому новый перевод сам должен перерисовать слой.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("function handleMessage"):source.index("function takeSnapshot")]

    assert "paint()" in body


def test_the_viewer_can_switch_each_line_off():
    html = (ROOT / "page" / "index.html").read_text(encoding="utf-8")

    assert 'id="toggle-target"' in html
    assert 'id="toggle-study"' in html


def test_the_layer_obeys_the_switches():
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("function paint"):source.index("function startClock")]

    assert "visibleLines" in body


def test_the_stage_is_left_by_the_browser_history():
    # Список эпизодов — тот же адрес: без pushState и popstate браузерная
    # история уводила бы со страницы целиком.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    assert "history.pushState" in source
    assert "popstate" in source


def test_the_stage_goes_back_to_the_list_by_its_own_arrow():
    # С планшета браузерную кнопку «назад» искать негде, а уход со сцены — самое
    # частое действие. Стрелка стоит первой в полосе, перед «во весь экран».
    html = (ROOT / "page" / "index.html").read_text(encoding="utf-8")
    bar = re.search(r'<div id="stage-bar">(.*?)</div>', html, re.S)

    assert bar is not None
    assert 'id="back"' in bar.group(1)
    assert bar.group(1).index('id="back"') < bar.group(1).index('id="fullscreen"')

    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    # Возврат идёт той же дорогой, что и браузерная кнопка: шаг истории,
    # положенный при входе в ролик, снимается, а не остаётся висеть.
    assert 'els.back.addEventListener("click"' in source
    assert "history.back()" in source


def test_the_players_fullscreen_turns_into_the_whole_stage():
    # Родной полноэкранный режим разворачивает сам <video>, а слой строк — соседний
    # элемент: без переключения на сцену титры там не рисуются вовсе.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    assert "fullscreenchange" in source
    assert "requestFullscreen" in source


def test_the_players_own_fullscreen_button_is_hidden():
    # Он разворачивает голый <video>, а слой строк — соседний элемент и в этом
    # режиме не рисуется. Своя кнопка разворачивает сцену целиком.
    html = (ROOT / "page" / "index.html").read_text(encoding="utf-8")
    tag = re.search(r"<video[^>]*>", html)

    assert tag is not None
    assert 'controlslist="nofullscreen"' in tag.group(0)


def test_the_stage_goes_fullscreen_by_the_pages_own_button():
    html = (ROOT / "page" / "index.html").read_text(encoding="utf-8")
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    assert 'id="fullscreen"' in html
    assert "els.stage.requestFullscreen" in source


def test_the_page_is_installable_as_an_app():
    # Android показывает приложение в share-диалоге только установленным, а
    # установка требует манифеста с именем, экраном запуска и иконками.
    html = (ROOT / "page" / "index.html").read_text(encoding="utf-8")
    manifest = json.loads((ROOT / "page" / "manifest.json").read_text(encoding="utf-8"))

    assert 'rel="manifest"' in html
    assert manifest["start_url"] == "/"
    assert manifest["display"] == "standalone"
    assert {"192x192", "512x512"} <= {icon["sizes"] for icon in manifest["icons"]}


def test_the_manifest_icons_are_really_there():
    manifest = json.loads((ROOT / "page" / "manifest.json").read_text(encoding="utf-8"))

    for icon in manifest["icons"]:
        assert (ROOT / "page" / icon["src"].lstrip("/")).is_file(), icon["src"]


def test_the_share_sheet_opens_the_page_itself():
    manifest = json.loads((ROOT / "page" / "manifest.json").read_text(encoding="utf-8"))
    target = manifest["share_target"]

    assert target["action"] == "/"
    assert target["method"] == "GET"
    assert target["params"] == {"title": "title", "text": "text", "url": "url"}


def test_a_shared_link_is_added_like_a_typed_one():
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    assert "sharedUrl(new URLSearchParams(location.search))" in source
    assert 'api("/v1/links", { method: "POST"' in source
    # Иначе перезагрузка страницы добавила бы ту же ссылку второй раз.
    assert "history.replaceState" in source


def test_the_saved_links_can_be_searched_by_name():
    html = (ROOT / "page" / "index.html").read_text(encoding="utf-8")
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    assert 'id="links-filter"' in html
    assert "filterLinks(" in source
    assert 'els.linksFilter.addEventListener("input"' in source


def test_the_saved_links_scroll_while_the_fields_stay_in_place():
    # Поле ссылки и поиск должны стоять на месте: на планшете с сотней
    # сохранённых прокрутка страницы уносила бы их с экрана.
    html = (ROOT / "page" / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "page" / "style.css").read_text(encoding="utf-8")

    library = re.search(r'<div id="library">(.*?)</div>', html, re.S)

    assert library is not None
    assert 'id="links"' in library.group(1)
    assert 'id="episodes"' in library.group(1)
    assert 'id="links-filter"' not in library.group(1)
    assert re.search(r"#home \{[^}]*height: 100dvh", css, re.S)
    assert re.search(r"#library \{[^}]*overflow-y: auto", css, re.S)


def test_the_viewer_picks_the_subtitle_size():
    # С планшета в другой комнате 22px не читаются, а на телефоне 34px не
    # помещаются. Размер выбирается из нескольких вариантов, а не угадывается
    # по ширине экрана.
    html = (ROOT / "page" / "index.html").read_text(encoding="utf-8")

    assert 'id="font-size"' in html
    for size in ("tiny", "small", "normal", "large"):
        assert f'value="{size}"' in html
    assert 'value="huge"' not in html


def test_the_chosen_size_survives_a_reload_and_shapes_the_layer():
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "page" / "style.css").read_text(encoding="utf-8")

    assert "readFontSize(" in source
    assert "fontSize" in source
    assert 'addEventListener("change"' in source
    # Размер задаётся на сцене признаком: сам слой в shadow root, и правила
    # для него лежат в style.css рядом с остальным видом строк.
    assert re.search(r"#stage\[data-font=", css)
    assert re.search(r'#stage\[data-font="tiny"\]', css)
    assert 'data-font="huge"' not in css
    # Строки рисуются в shadow root: внешние правила внутрь не проходят,
    # поэтому множитель должен стоять в LAYER_CSS рядом с var(--font).
    layer = source[source.index("const LAYER_CSS"):source.index("function layerNodes")]
    assert "var(--font" in layer
    assert "--overlay-scale" in layer


def test_the_stage_has_a_cache_switch_next_to_fullscreen():
    html = (ROOT / "page" / "index.html").read_text(encoding="utf-8")
    bar = re.search(r'<div id="stage-bar">(.*?)</div>', html, re.S)

    assert bar is not None
    assert 'id="cache"' in bar.group(1)
    assert 'id="fullscreen"' in bar.group(1)
    # По умолчанию кеш выключен: без нажатия на устройстве не оседает ничего.
    assert 'aria-pressed="false"' in bar.group(1)


def test_the_cache_switch_is_an_icon_that_says_only_what_changes():
    # На сцене надпись «кеш» спорила с кадром. Скачивание — иконка; когда копия
    # лежит на устройстве, рядом остаются только галочка и размер.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("function drawCacheButton"):source.index("function toggleCache")]

    assert "ICON_DOWNLOAD" in body
    assert "ICON_CHECK" in body
    # Готовая копия — галочка, иначе — скачивание; слов на кнопке нет.
    assert "saved ? ICON_CHECK : ICON_DOWNLOAD" in body
    # Надпись по-прежнему приходит из logic.js: проценты, размер, ожидание.
    assert "copyState(" in body
    # Иконка без слова: имя для пальца и подсказки меняется вместе с копией.
    assert "копия на устройстве" in body
    assert "отменить копирование" in body


def test_the_cache_switch_copies_the_rolled_video():
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    logic = (ROOT / "page" / "logic.js").read_text(encoding="utf-8")

    assert 'els.cache.addEventListener("click"' in source
    for name in (
        "copyState(", "copyVerdict(", "formatBytes(", "percent(", "mergeCues(",
        "SPARE_BYTES", "downloadVideo(", "putVideo(", "freeBytes(", "askPersist(",
    ):
        assert name in source, name
    # Иконку рисует страница, а подпись к ней — по-прежнему logic.js.
    assert "export function copyState" in logic
    # Повторное нажатие во время копирования отменяет его и убирает недокачанное.
    assert "controller.abort()" in source
    assert "copy = null" in source


def test_the_base_is_named_in_one_place():
    # Имя базы и таблиц живёт в cache.js; страница их не повторяет, иначе одно
    # переименование пришлось бы искать в двух файлах.
    cache = (ROOT / "page" / "cache.js").read_text(encoding="utf-8")
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    assert 'DB_NAME = "yt-trans"' in cache
    assert '"yt-trans"' not in source
    assert "openDb(" in source


def test_the_copy_keeps_the_rolled_video_subscribed():
    # Ушёл со сцены с идущим копированием — титры должны дописываться дальше,
    # иначе сохранённое оборвётся на моменте ухода.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("function leaveStage"):source.index("// --- Запуск")]

    assert body.index("copy") < body.index("unwatchMessage")


def test_the_copy_catches_cues_its_own_video():
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("function handleMessage"):source.index("function takeSnapshot")]

    assert "rememberCues(" in body
    assert body.index("rememberCues(") < body.index("isForVideo(")


def test_the_saved_cues_keep_arriving_after_the_picture_is_stored():
    # Перевод идёт дольше скачивания: к концу перевода на устройстве должны
    # лежать и картинка, и все строки, а не те, что успели к концу загрузки.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    assert "let follow = null" in source
    assert "followSoon(" in source
    assert "FOLLOW_MS" in source


def test_the_stage_bar_holds_both_buttons():
    css = (ROOT / "page" / "style.css").read_text(encoding="utf-8")

    assert re.search(r"#stage-bar \{[^}]*display: flex", css, re.S)


def test_a_saved_video_plays_from_the_device():
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    assert "URL.createObjectURL" in source
    assert "URL.revokeObjectURL" in source
    assert "switchToCopy(" in source
    assert "playLocal(" in source
    assert "playSource(" in source


def test_the_copy_carries_its_cues_without_the_daemon():
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    assert "seedCues(" in source
    assert "mergeCues(" in source
    assert "демона нет — играю сохранённое" in source


def test_offline_says_why_a_rolled_video_does_not_play():
    # Спека: без копии офлайн сцена не открывается вовсе — сообщение остаётся
    # на домашней странице, чтобы чёрный экран не притворялся поломкой.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("function play("):source.index("async function createJob")]

    assert "демона нет, этого ролика на устройстве нет" in body
    assert body.index("playSource(") < body.index("els.home.hidden = true")


def test_an_evicted_copy_does_not_lie_in_the_list():
    # Браузер вправе вытеснить Blob: запись в базе есть, играть нечего.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("async function playLocal"):source.index("function switchToCopy")]

    assert "savedVideos.delete(" in body
    assert "deleteVideo(" in body


def test_the_home_stays_with_the_saved_copy_when_the_daemon_is_gone():
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("async function loadLinks"):source.index("function savedForLink")]

    # Молчащий демон — не отказ: прошлый список рисуется сразу, а запрос
    # к демону получает короткий срок и заменяет его, когда ответит.
    assert "AbortSignal.timeout(LINKS_TIMEOUT_MS)" in body
    assert 'getMeta(db, "links")' in body
    assert body.index("getMeta") < body.index("api(")
    assert "offline = true" in body
    assert "savedForLink(" in source
    assert "на устройстве" in source


def test_a_saved_video_is_marked_in_the_list_and_can_be_deleted():
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    assert "cacheLabel(" in source
    assert "deleteVideo(" in source
    assert "dropCopy(" in source
    # Удаление — корзина: слово на каждой строке отнимало место у того, ради
    # чего строку читают (метраж, статус, размер).
    assert "ICON_TRASH" in source
    assert 'iconButton(ICON_TRASH, "удалить копию", () => dropCopy(' in source


def test_a_saved_row_does_not_repeat_the_title():
    # Строка копии стоит под своей ссылкой, и заголовок уже нарисован выше.
    # Повторять его — значит отнимать место у метража, статуса и размера.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    rows = source[source.index("function savedRows"):source.index("async function dropCopy")]
    label = source[source.index("function savedLabel"):source.index("async function dropCopy")]

    assert "episodeLabel(" not in rows
    assert "savedLabel(" in rows
    assert "formatTime(" in label
    assert "cacheLabel(" in label
    assert "title" not in label


def test_a_shared_link_does_not_wipe_the_saved_home():
    # При мёртвом демоне шаринг из YouTube не должен оставлять пустой экран:
    # сохранённое с пометками важнее неудавшегося добавления.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("async function consumeShare"):source.index("function row(")]

    assert "drawLinks(knownLinks" in body


def test_saved_episodes_stand_under_their_link():
    css = (ROOT / "page" / "style.css").read_text(encoding="utf-8")

    assert re.search(r"#links li \{[^}]*flex-wrap: wrap", css, re.S)
    assert re.search(r"#links li > ul \{[^}]*flex-basis: 100%", css, re.S)


def test_the_shell_is_kept_for_the_day_the_computer_is_off():
    worker = (ROOT / "page" / "sw.js").read_text(encoding="utf-8")
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    assert 'serviceWorker.register("/sw.js")' in source
    assert "registerWorker()" in source
    for name in ("/app.js", "/logic.js", "/cache.js", "/style.css", "/manifest.json"):
        assert name in worker
    # Живые данные демона и сертификат — не оболочка.
    assert 'startsWith("/v1/")' in worker
    assert "/ca.crt" in worker


def test_a_browser_without_a_worker_says_so_once():
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    assert "workerMissing = true" in source
    assert '"yt-trans-no-worker"' in source
    assert "не сохранит страницу для офлайна" in source


def test_a_broken_answer_is_not_taken_for_an_empty_daemon():
    # Тело ответа может оборваться (демон умер, вышел срок), и тогда страница
    # не должна принять обрыв за «ничего нет»: пустой список затёр бы
    # сохранённые ссылки на устройстве.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    cache = (ROOT / "page" / "cache.js").read_text(encoding="utf-8")

    assert ".json().catch" not in source
    assert "readJson(" in source
    assert "export async function readJson" in cache

    body = source[source.index("async function loadLinks"):source.index("function savedForLink")]

    assert "Array.isArray(data.links)" in body


def test_an_offline_home_does_not_wait_for_a_sleeping_daemon():
    # Уснувший компьютер не отвечает минутами: действие с известным мёртвым
    # демоном должно сразу говорить, что демона нет, а не висеть.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    for name in ("addLink", "dropLink", "openLink", "consumeShare"):
        start = source.index(f"async function {name}(")
        body = source[start:source.index("await api(", start)]

        assert "!daemonAlive" in body, name


def test_the_copy_stays_subscribed_after_a_reconnect():
    # Копия и её титры переживают уход со сцены: после переподключения
    # подписка нужна и им, иначе запись оборвётся на переподключении.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[
        source.index('opening.addEventListener("open"'):source.index('opening.addEventListener("message"')
    ]

    assert "copy && copy.videoId" in body
    assert "follow && follow.videoId" in body


def test_an_evicted_copy_disappears_from_the_list():
    # Запись есть, а Blob браузер вытеснил: строка списка не должна больше
    # обещать «на устройстве».
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("async function playLocal"):source.index("function switchToCopy")]

    assert "drawLinks(knownLinks" in body


def test_a_video_with_nothing_to_play_closes_the_stage():
    # Копию вытеснили, демона нет: сцена открылась бы пустой, поэтому
    # сообщение остаётся на домашней странице.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("async function prepareVideo"):source.index("// --- Кеш роликов")]

    assert "leaveStage()" in body


def test_the_saved_copy_is_marked_while_the_daemon_is_alive():
    # Иначе копию нельзя ни увидеть, ни удалить, пока компьютер работает.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("function drawLinks"):source.index("async function loadLinks")]

    assert "if (offline)" not in body
    assert "savedForLink(link.id)" in body


def test_a_saved_copy_keeps_collecting_cues_while_the_daemon_is_alive():
    # Копия могла быть снята до конца перевода: её запись должна расти и при
    # обычном просмотре, иначе офлайн останутся только титры момента копии.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("async function playLocal"):source.index("function switchToCopy")]

    assert "follow = {" in body
    assert "daemonAlive" in body


def test_opening_a_video_rereads_the_copies_from_the_database():
    # Копию могли удалить в другом окне (установленное приложение и вкладка
    # браузера — это два экземпляра страницы). Память этого окна тогда врёт:
    # строка обещает «на устройстве», а играть нечего, и офлайн остаётся
    # только сообщение «этого ролика на устройстве нет».
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("async function play("):source.index("async function createJob")]

    assert "await openCache()" in body
    assert body.index("await openCache()") < body.index("playSource(")


def test_the_copies_list_is_rebuilt_and_not_only_extended():
    # openCache вызывается при каждом открытии ролика: доливка оставила бы в
    # памяти то, чего в базе уже нет.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("async function openCache("):source.index("function drawCacheButton")]

    assert "savedVideos.clear()" in body


def test_deleting_a_copy_redraws_the_cache_switch():
    # Переключатель на сцене живёт своей надписью: без перерисовки он
    # продолжал бы показывать размер удалённой копии.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("async function dropCopy"):source.index("async function addLink")]

    assert "drawCacheButton()" in body


def test_returning_to_the_window_refreshes_the_copies():
    # Вкладку прячут и возвращаются к ней: пока её не было, копию могли удалить.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    assert 'document.addEventListener("visibilitychange"' in source


def test_the_token_screen_does_not_wait_for_the_service_worker():
    # Раздел токена виден в разметке по умолчанию, а приложение — спрятано.
    # Регистрация воркера без сети может не ответить вовсе: дождавшись её до
    # решения об экране, страница оставляла зрителя перед экраном ввода токена,
    # который тот давно сохранил, — и офлайн оттуда не выбраться.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("async function boot("):source.index("boot();", source.index("async function boot("))]

    assert body.index("els.app.hidden = false") < body.index("await registerWorker()")


def test_a_missing_worker_is_reported_over_the_drawn_list():
    # Регистрация идёт после экрана, поэтому список ссылок мог быть нарисован
    # раньше её ответа. Предупреждение обязано появиться поверх уже
    # нарисованного, иначе о нём не узнают никогда.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("async function boot("):source.index("boot();", source.index("async function boot("))]

    assert "drawLinks(knownLinks, warning)" in body
    # Список при загрузке тоже несёт это предупреждение: регистрация могла
    # ответить раньше, чем пришёл ответ демона.
    links = source[source.index("async function loadLinks"):source.index("function savedForLink")]
    assert "workerNote()" in links
    # Упавшая без сети регистрация — не приговор браузеру: оболочка могла уже
    # стоять, и страница офлайн открылась именно из неё.
    worker = source[source.index("async function registerWorker"):source.index("function workerNote")]
    assert "!navigator.serviceWorker.controller" in worker


def test_the_page_imports_every_helper_it_uses():
    # Пропущенное имя в импорте видно только в браузере: node --check имён не
    # знает, а разметка и текст остаются на месте. Поэтому имена сверяются
    # списком: всё, что страница зовёт из logic.js и cache.js, должно быть
    # у неё в импорте.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    for module in ("logic.js", "cache.js"):
        text = (ROOT / "page" / module).read_text(encoding="utf-8")
        block = re.search(rf'import \{{([^}}]*)\}} from "\./{re.escape(module)}";', source)
        names = {name.strip() for name in block.group(1).replace("\n", " ").split(",") if name.strip()}
        exported = set(re.findall(r"export (?:async )?function (\w+)", text))
        exported |= set(re.findall(r"export const (\w+)", text))

        for name in sorted(exported):
            if re.search(rf"\b{name}\(", source):
                assert name in names, f"{module}: {name}"
