"""Проводка расширения: протокол до демона, разрешения и поля настроек.

Демон с сертификатом ([server] tls_cert в config.toml) слушает только https.
Расширение обязано уметь оба протокола: жёсткий http:// молча получал пустой
ответ от TLS-порта, и зритель читал «демон не отвечает» при живом демоне.
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXTENSION = ROOT / "extension"


def ids_the_options_page_looks_for():
    source = (EXTENSION / "options.js").read_text(encoding="utf-8")

    return set(re.findall(r'getElementById\("([^"]+)"\)', source)) | set(
        re.findall(r'querySelector\("#([^"]+)"\)', source)
    )


def test_the_options_page_looks_for_something():
    assert ids_the_options_page_looks_for()


def test_every_element_the_options_page_looks_for_exists():
    html = (EXTENSION / "options.html").read_text(encoding="utf-8")
    present = set(re.findall(r'id="([^"]+)"', html))

    assert ids_the_options_page_looks_for() - present == set()


def test_the_extension_may_speak_https():
    # Без разрешения на https://127.0.0.1 расширение не вправе туда ходить,
    # даже когда демон сам предлагает CORS для своего источника.
    manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))
    permissions = set(manifest["host_permissions"])

    assert {"http://127.0.0.1/*", "https://127.0.0.1/*"} <= permissions
    assert {"http://localhost/*", "https://localhost/*"} <= permissions


def defaults(path):
    source = path.read_text(encoding="utf-8")
    block = source[source.index("const DEFAULTS = {"):]
    block = block[: block.index("};")]

    return dict(
        re.findall(r'^\s*(\w+):\s*("(?:[^"]*)"|-?\d+|true|false),', block, re.M)
    )


def test_the_scheme_is_the_same_on_both_sides():
    # Фон и страница настроек читают одни и те же настройки из хранилища;
    # разойдись умолчания — проверка связи говорила бы одно, а опрос делал другое.
    background = defaults(EXTENSION / "background.js")
    options = defaults(EXTENSION / "options.js")

    assert background.get("scheme") == options.get("scheme") == '"http"'


def function_body(path, name):
    source = path.read_text(encoding="utf-8")
    start = source.index(f"function {name}(")

    return source[start : source.index("\n}", start)]


def test_the_extension_sends_no_credentials():
    # Токена установки больше нет: расширение не хранит его и не шлёт
    # Authorization, иначе старый ключ жил бы в хранилище и в заголовках.
    background = (EXTENSION / "background.js").read_text(encoding="utf-8")
    options = (EXTENSION / "options.js").read_text(encoding="utf-8")

    assert "Authorization" not in background
    assert "Authorization" not in options
    assert "token" not in defaults(EXTENSION / "background.js")
    assert "token" not in defaults(EXTENSION / "options.js")


def test_the_options_page_asks_for_no_token():
    html = (EXTENSION / "options.html").read_text(encoding="utf-8")

    assert 'id="token"' not in html
    assert "токен" not in html.lower()


def test_the_address_is_built_with_the_chosen_scheme():
    body = function_body(EXTENSION / "background.js", "baseUrl")

    assert "scheme" in body
    assert "https" in body


def test_the_connection_check_uses_the_chosen_scheme():
    body = function_body(EXTENSION / "options.js", "check")

    assert "scheme" in body
    assert "https" in body


def test_every_request_can_fall_back_to_the_other_scheme():
    # Протокол у временного дополнения сбрасывается вместе с браузером, поэтому
    # настройке верить нельзя: демон с сертификатом слушает только https, а
    # умолчание — http, и зритель видел «демон не отвечает» при живом демоне.
    source = (EXTENSION / "background.js").read_text(encoding="utf-8")

    assert "function call(" in source
    # Запрос в файле один: все остальные идут через него.
    assert source.count("fetch(") == 1

    for name in ("pollLoop", "waitForDaemon", "restore", "retry", "sendPlayhead"):
        body = function_body(EXTENSION / "background.js", name)

        assert "call(" in body, name
        assert "fetch(" not in body, name


def test_the_fallback_tries_both_schemes():
    body = function_body(EXTENSION / "background.js", "schemes")

    assert '"https"' in body
    assert '"http"' in body
