"""Проводка страницы: каждый элемент, который ищет app.js, есть в разметке."""

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
