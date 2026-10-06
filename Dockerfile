# The supplier bot as one always-on container.
#
# The weekly job and its follow-ups run inside the app process (APScheduler), so the app
# must be started with `python app.py`, and exactly one copy of it may run: two copies
# are two bots, each with its own database, and both would text the supplier.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Fail the build, not the Wednesday run: the cafe's timezone must resolve in this image,
# and the suite must pass on the Python and the OS that will run in production. The suite
# sends no SMS, makes no model call and uses a throwaway database.
RUN python -c "from zoneinfo import ZoneInfo; ZoneInfo('Australia/Melbourne')" \
 && python -m pytest -q -p no:cacheprovider

EXPOSE 5000
CMD ["python", "app.py"]
