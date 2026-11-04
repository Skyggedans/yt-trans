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
    # Своей кнопки «назад» больше нет, а список эпизодов — тот же адрес: без
    # pushState и popstate браузерная история уводила бы со страницы целиком.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    html = (ROOT / "page" / "index.html").read_text(encoding="utf-8")

    assert "history.pushState" in source
    assert "popstate" in source
    assert 'id="back"' not in html


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
