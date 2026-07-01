import logging
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.init_db import get_setting
from app.db.models import RequestLog, SystemLog


def setup_logging() -> None:
    settings = get_settings()
    Path(settings.log_dir).mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.FileHandler(Path(settings.log_dir) / "app.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def system_log(db: Session, level: str, module: str, message: str) -> None:
    if get_setting(db, "enable_system_logs", "true").lower() != "true":
        return
    db.add(SystemLog(level=level, module=module, message=message))
    db.commit()


def traced_system_log(db: Session, level: str, module: str, trace_id: str, message: str) -> None:
    prefix = f"trace={trace_id} " if trace_id else ""
    system_log(db, level, module, f"{prefix}{message}")


@contextmanager
def request_timer(
    db: Session,
    endpoint: str,
    model: str,
    source_ip: str,
    api_key_name: str = "",
    prompt_chars: int = 0,
) -> Iterator[dict]:
    state = {"request_id": uuid.uuid4().hex, "completion_chars": 0, "status_code": 200, "error": ""}
    start = time.perf_counter()
    try:
        yield state
    except Exception as exc:
        state["status_code"] = 500
        state["error"] = str(exc)
        raise
    finally:
        if get_setting(db, "enable_request_logs", "true").lower() != "true":
            return
        duration_ms = int((time.perf_counter() - start) * 1000)
        db.add(
            RequestLog(
                request_id=state["request_id"],
                api_key_name=api_key_name,
                source_ip=source_ip,
                endpoint=endpoint,
                model=model,
                status_code=state["status_code"],
                duration_ms=duration_ms,
                prompt_chars=prompt_chars,
                completion_chars=state.get("completion_chars", 0),
                error=state.get("error", ""),
            )
        )
        db.commit()
