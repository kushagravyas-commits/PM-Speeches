# scheduler.py
import os
import sys
import subprocess
from pathlib import Path
from datetime import datetime, timedelta

from dotenv import load_dotenv
from prefect import flow, task, get_run_logger

# Load .env once for the scheduler (the scripts also load it themselves, harmless)
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

URL_COLLECTOR_SCRIPT = BASE_DIR / "extract_speech_url_range_playwright.py"
SPEECH_EXTRACTOR_SCRIPT = BASE_DIR / "extract_speech_mongodb.py"


def _run_python_script(script_path: Path, env_extra: dict | None = None) -> dict:
    """
    Run a python script in a subprocess and stream its stdout/stderr to Prefect logs.
    Returns a small run summary dict.
    """
    logger = get_run_logger()

    if not script_path.exists():
        raise FileNotFoundError(f"Script not found: {script_path}")

    env = os.environ.copy()
    if env_extra:
        env.update({k: str(v) for k, v in env_extra.items()})

    cmd = [sys.executable, "-u", str(script_path)]  # -u = unbuffered stdout
    logger.info(f"Running: {' '.join(cmd)}")
    start = datetime.utcnow()

    proc = subprocess.Popen(
        cmd,
        cwd=str(BASE_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
        errors="replace",
    )

    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\n")
        if line:
            logger.info(line)

    rc = proc.wait()
    end = datetime.utcnow()

    summary = {
        "script": script_path.name,
        "return_code": rc,
        "started_utc": start.isoformat(),
        "finished_utc": end.isoformat(),
        "duration_seconds": (end - start).total_seconds(),
    }

    if rc != 0:
        raise RuntimeError(f"{script_path.name} failed with exit code {rc} | summary={summary}")

    logger.info(f"Finished: {script_path.name} | {summary}")
    return summary


@task(name="Collect new speech URLs", retries=2, retry_delay_seconds=300)
def collect_new_urls() -> dict:
    """
    Updated for new extract_speech_url_range_playwright.py:
    Passes FROM_DATE / TO_DATE / LANGUAGE / KEYWORD / CHUNK_DAYS / URLS_COLLECTION / HEADLESS via env.
    """

    # Default: last 2 days to today (inclusive range in script)
    # today = datetime.now().date()
    # from_date = today - timedelta(days=2)
    today = datetime.now().date()
    from_date =today - timedelta(days=10)
    env_extra = {
        "FROM_DATE": from_date.strftime("%Y-%m-%d"),   # already a string
        "TO_DATE": today.strftime("%Y-%m-%d"),
        "LANGUAGE": "en",
        "KEYWORD": "",
        "CHUNK_DAYS": "30",
        "HEADLESS": "true",
        "URLS_COLLECTION": "en_urls",
    }


    return _run_python_script(URL_COLLECTOR_SCRIPT, env_extra=env_extra)


@task(name="Extract new speeches", retries=2, retry_delay_seconds=300)
def extract_new_speeches() -> dict:
    env_extra = {}
    return _run_python_script(SPEECH_EXTRACTOR_SCRIPT, env_extra=env_extra)


@flow(name="PM Speech Pipeline")
def pm_speech_pipeline():
    """
    1) Update urls collection with new speech URLs
    2) Extract speeches for any urls not yet in speeches collection
    """
    logger = get_run_logger()
    logger.info("Starting PM Speech Pipeline")

    url_step = collect_new_urls()
    speech_step = extract_new_speeches()

    logger.info(f"Pipeline complete. URL step: {url_step} | Speech step: {speech_step}")
    return {"urls": url_step, "speeches": speech_step}


if __name__ == "__main__":
    pm_speech_pipeline()

    # Optional scheduling block (unchanged)
    # pm_speech_pipeline.serve(
    #     name="pm-speech-daily",
    #     cron="0 3 * * *",
    #     timezone="Asia/Kolkata",
    # )
