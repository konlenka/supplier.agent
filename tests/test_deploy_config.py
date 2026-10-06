"""The deploy config is part of how the bot behaves. The Wednesday job and its follow-ups run
inside the app process and everything it remembers is one file on disk, so a wrong hosting
setting stops orders, or doubles them, as surely as a bug in the code does."""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def _setting(text, key):
    """The value of `key = value` in a TOML file, without quotes or a trailing comment."""
    match = re.search(rf'^\s*{key}\s*=\s*"?([^"#\n]+?)"?\s*(#.*)?$', text, re.M)
    return match.group(1).strip() if match else None


def test_the_machine_is_never_stopped_when_idle():
    """A host that sleeps an idle app would sleep through 9:00 on Wednesday."""
    fly = _read("fly.toml")
    assert _setting(fly, "auto_stop_machines") == "off"
    assert _setting(fly, "min_machines_running") == "1"


def test_the_database_sits_on_the_persistent_volume():
    """Off the volume, every deploy wipes the counts, the order history and the record of
    this week's order — and the next run can order a second time."""
    import config

    workdir = re.search(r"^WORKDIR\s+(\S+)", _read("Dockerfile"), re.M).group(1)
    data_folder = os.path.basename(os.path.dirname(config.DB_PATH))
    assert _setting(_read("fly.toml"), "destination") == f"{workdir}/{data_folder}"


def test_the_app_is_started_the_way_that_starts_the_scheduler():
    """The weekly job is only booked under `python app.py`, not under gunicorn or `flask run`."""
    assert 'CMD ["python", "app.py"]' in _read("Dockerfile")


def test_traffic_is_sent_to_the_port_the_app_listens_on():
    port = re.search(r"app\.run\(.*port=(\d+)", _read("app.py")).group(1)
    assert _setting(_read("fly.toml"), "internal_port") == port


def test_the_bot_runs_in_sydney():
    """It stores staff phone numbers; thelo's tool matrix keeps client data in Australia."""
    assert _setting(_read("fly.toml"), "primary_region") == "syd"


def test_secrets_and_client_data_stay_out_of_the_image():
    ignored = _read(".dockerignore").split()
    assert ".env" in ignored and "data/" in ignored


def test_the_image_build_fails_if_the_suite_or_the_cafes_timezone_does():
    docker = _read("Dockerfile")
    assert "pytest" in docker and "Australia/Melbourne" in docker


def test_every_dependency_is_pinned():
    """An unpinned range lets a rebuild months later pull a version nobody tested."""
    lines = [line.strip() for line in _read("requirements.txt").splitlines()]
    lines = [line for line in lines if line and not line.startswith("#")]
    assert lines and all(re.fullmatch(r"[A-Za-z0-9_.-]+==[0-9][A-Za-z0-9_.]*", line) for line in lines)
