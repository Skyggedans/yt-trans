from pathlib import Path


def test_cache_paths_are_isolated():
    from helper import env

    assert Path.home() not in env.CACHE_DIR.parents
    assert Path.home() not in env.CONFIG_DIR.parents


def test_manager_fixture_builds(manager):
    assert manager.stats()["jobs"] == 0


def test_manager_registers_a_job_without_launching_the_pipeline(manager):
    # Именно обычный def, не async: настоящий Job.start зовёт asyncio.create_task,
    # и вне цикла событий это падение «no running event loop». От этого контракта
    # зависят все остальные тесты реестра.
    job = manager.get_or_create("abcdefghijk", "ru", True)

    assert job.video_id == "abcdefghijk"
    assert manager.started == ["abcdefghijk"]


def test_manager_records_a_resume_without_launching_it(manager):
    # Тот же контракт для возобновления: настоящий Job.resume тоже создаёт задачу,
    # и без заглушки async-тест на приостановленном задании запустил бы конвейер.
    from helper.jobs import Stage

    job = manager.get_or_create("abcdefghijk", "ru", True)
    job.stage = Stage.PAUSED

    assert manager.get_or_create("abcdefghijk", "ru", True) is job
    # Два вызова, а не два запуска: start при создании и resume при повторном
    # обращении к приостановленному заданию. Так и задумано — см. get_or_create.
    assert manager.started == ["abcdefghijk", "abcdefghijk"]


async def test_client_and_manager_see_one_registry(manager, client):
    # Обработчики читают модульный helper.app.manager. Если фикстура не подменила
    # его тем же объектом, что вернула тесту, созданное запросом задание осталось
    # бы в чужом реестре, а проверка через manager его не нашла.
    response = await client.post("/v1/jobs", json={"videoId": "abcdefghijk"})

    assert response.status_code == 200
    assert response.json()["videoId"] == "abcdefghijk"
    assert manager.get("abcdefghijk") is not None
    assert manager.started == ["abcdefghijk"]
