import argparse
import json
import logging
import os
import sys


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key in ("novel", "author", "segment", "operation", "status"):
            value = record.__dict__.get(key)
            if value is not None:
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


def add_log_format_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--log-format",
        choices=["text", "json"],
        default=None,
        help="日志输出格式（默认从环境变量 INDEX_LOG_FORMAT 读取，否则为 text）",
    )


def configure_logging(*, fmt: str | None = None, level: int = logging.INFO) -> None:
    if fmt is None:
        fmt = (os.environ.get("INDEX_LOG_FORMAT", "text").strip().lower() or "text")
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    if fmt == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
