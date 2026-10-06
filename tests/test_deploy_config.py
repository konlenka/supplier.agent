"""The deploy config is part of how the bot behaves. The Wednesday job and its follow-ups run
inside the app process and everything it remembers is one file on disk, so a wrong hosting
setting stops orders, or doubles them, as surely as a bug in the code does.

These tests read the files the way the tools that use them do — sections, instructions and
patterns, with comments ignored — so that a setting that is commented out, moved to the wrong
section or overridden lower down does not still look right."""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def _fly():
    """fly.toml as {section: {key: value}}. Enough TOML for this file: `[section]` and
    `[[section]]` headers and `key = value` lines; comments dropped."""
    sections, current = {"": {}}, ""
    for raw in _read("fly.toml").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        header = re.fullmatch(r"\[+([^\]]+)\]+", line)
        if header:
            current = header.group(1).strip()
            sections.setdefault(current, {})
        else:
            key, value = line.split("=", 1)
            sections[current][key.strip()] = value.strip().strip('"')
    return sections


def _dockerfile():
    """The Dockerfile as (INSTRUCTION, arguments) pairs: comments dropped, continued lines joined."""
    joined = re.sub(r"\\\s*\n", " ", "\n".join(
        line for line in _read("Dockerfile").splitlines() if not line.lstrip().startswith("#")
    ))
    pairs = [line.strip().split(None, 1) for line in joined.splitlines() if line.strip()]
    return [(pair[0].upper(), pair[1] if len(pair) > 1 else "") for pair in pairs]


def _dockerignore():
    return [line.strip() for line in _read(".dockerignore").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def test_the_machine_is_never_stopped_when_idle():
    """A host that sleeps an idle app would sleep through 9:00 on Wednesday."""
    service = _fly()["http_service"]
    assert service["auto_stop_machines"] == "off"
    assert service["min_machines_running"] == "1"


def test_a_machine_that_exits_is_always_started_again():
    """Fly's default gives up after ten crashes in five minutes, and leaves a machine that
    exits cleanly stopped. Either way the bot would be down until someone noticed."""
    restart = _fly()["restart"]
    assert restart["policy"] == "always"
    assert "processes" not in restart  # for every process, not a named few


def test_the_database_sits_on_the_persistent_volume():
    """Off the volume, every deploy wipes the counts, the order history and the record of
    this week's order — and the next run can order a second time."""
    import config

    import storage

    # The database is <the app's folder>/data/stock.db, and storage writes where config says.
    app_folder = os.path.dirname(os.path.abspath(config.__file__))
    assert os.path.dirname(os.path.abspath(config.DB_PATH)) == os.path.join(app_folder, "data")
    assert storage.DB_PATH == config.DB_PATH
    # In the image the app's folder is WORKDIR, and the volume is mounted over its data folder.
    workdir = dict(_dockerfile())["WORKDIR"]
    mounts = _fly()["mounts"]
    assert mounts["destination"] == f"{workdir}/data"
    assert "processes" not in mounts  # mounted for every process, not a named few


def test_the_volume_the_readme_creates_is_the_one_that_gets_mounted():
    volume, region = _fly()["mounts"]["source"], _fly()[""]["primary_region"]
    # A volume in another region is never attached: the machine would get a new, empty one.
    assert f"fly volumes create {volume} --region {region} " in _read("README.md")


def test_the_app_is_started_the_way_that_starts_the_scheduler():
    """The weekly job is only booked under `python app.py`, not under gunicorn or `flask run`.
    The last CMD is the one that runs, and a [processes] section in fly.toml would replace it."""
    instructions = _dockerfile()
    assert [args for instruction, args in instructions if instruction == "CMD"] == ['["python", "app.py"]']
    assert "ENTRYPOINT" not in [instruction for instruction, _ in instructions]
    assert len([1 for instruction, _ in instructions if instruction == "FROM"]) == 1
    fly = _fly()
    assert "processes" not in fly and "experimental" not in fly


def test_traffic_reaches_the_app():
    """Fly sends traffic to internal_port on the machine's network address; an app listening
    on another port, or only on 127.0.0.1, never sees a staff text."""
    host, port, debug = re.search(
        r'app\.run\(host="([^"]+)", port=(\d+), debug=(\w+)\)', _read("app.py")
    ).groups()
    assert _fly()["http_service"]["internal_port"] == port
    assert host == "0.0.0.0"
    assert debug == "False"  # debug mode runs the app twice, and with it the scheduler


def test_the_bot_runs_in_sydney():
    """The machine and the database sit in Sydney: it stores staff phone numbers, and thelo's
    tool matrix keeps client data in Australia. (Logs are another matter: see CLAUDE.md.)"""
    assert _fly()[""]["primary_region"] == "syd"


def test_secrets_and_client_data_stay_out_of_the_image():
    patterns = _dockerignore()
    assert ".env" in patterns and "data/" in patterns
    # A later "!pattern" puts a file back in. Only the template may come back.
    assert [p for p in patterns if p.startswith("!")] == ["!.env.example"]


def test_the_image_build_fails_if_the_suite_or_the_cafes_timezone_does():
    runs = [args for instruction, args in _dockerfile() if instruction == "RUN"]
    checks = [args for args in runs if "pytest" in args]
    assert len(checks) == 1
    steps = [" ".join(step.split()) for step in checks[0].split("&&")]
    # Exactly these two commands, joined so that either failing stops the build: the whole
    # suite, with nothing after it that could swallow its exit code.
    assert steps == [
        """python -c "from zoneinfo import ZoneInfo; ZoneInfo('Australia/Melbourne')\"""",
        "python -m pytest -q -p no:cacheprovider",
    ]


def test_the_container_runs_as_the_user_that_can_write_to_the_volume():
    """The volume is mounted owned by root. A USER line would leave the bot unable to save
    a stock count or its own run log."""
    assert "USER" not in [instruction for instruction, _ in _dockerfile()]


def test_every_dependency_is_pinned():
    """An unpinned range lets a rebuild months later pull a version nobody tested."""
    lines = [line.strip() for line in _read("requirements.txt").splitlines()]
    lines = [line for line in lines if line and not line.startswith("#")]
    assert lines and all(re.fullmatch(r"[A-Za-z0-9_.-]+==[0-9][A-Za-z0-9_.]*", line) for line in lines)
