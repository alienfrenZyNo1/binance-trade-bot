#!/usr/bin/env python3
"""Smoke-test Telegram command handlers without executing mutating commands.

Default mode is local-only: import scripts/telegram_bot.py, execute safe command
handler paths, and fail on exceptions, non-string responses, malformed Telegram
HTML, or messages longer than Telegram's 4096-character limit.

Optional live mode (--send) sends each locally-valid response to Telegram with
parse_mode=HTML and disable_notification=True, then immediately deletes it.  A
short send/delete probe runs first; if deletion does not work, the script aborts
live sends to avoid leaving a pile of test messages behind.

Safe paths covered:
  /status /trades /coins /price /futures /health /config /regime /profit /hop
  /deposit /help, plus /addcoin, /removecoin, /swap usage responses and /kill
  preview only.  It never runs /kill confirm, never adds/removes/swaps real
  coins, and never records a positive deposit.
"""

from __future__ import annotations

import argparse
import html.parser
import importlib.util
import os
from pathlib import Path
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

try:
    import requests
except ImportError:  # pragma: no cover - requests is already a runtime dep here
    requests = None  # type: ignore[assignment]


TELEGRAM_LIMIT = 4096
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV = REPO_ROOT / ".env.telegram"
TARGET = REPO_ROOT / "scripts" / "telegram_bot.py"

# Telegram Bot API HTML tags. The target bot currently only emits b/i/code/pre,
# but keeping the full common allow-list makes the validator useful after edits.
ALLOWED_TAGS = {
    "b",
    "strong",
    "i",
    "em",
    "u",
    "ins",
    "s",
    "strike",
    "del",
    "span",  # class="tg-spoiler" only
    "tg-spoiler",
    "a",
    "code",
    "pre",
    "blockquote",
    "tg-emoji",
}
ALLOWED_NAMED_ENTITIES = {"lt", "gt", "amp", "quot"}
TAG_TOKEN_RE = re.compile(r"</?\s*([A-Za-z][A-Za-z0-9-]*)(?:\s+[^<>]*?)?\s*>")
RAW_AMP_RE = re.compile(r"&(?!lt;|gt;|amp;|quot;|#[0-9]+;|#x[0-9A-Fa-f]+;)")


@dataclass(frozen=True)
class Case:
    label: str
    func_name: str
    args: tuple[Any, ...] = ()
    live_send: bool = True


@dataclass
class Result:
    label: str
    ok: bool = True
    chars: int = 0
    bytes_: int = 0
    elapsed_ms: float = 0.0
    send_status: str = "-"
    errors: list[str] = field(default_factory=list)
    response: str = ""

    def fail(self, message: str) -> None:
        self.ok = False
        self.errors.append(message)


SAFE_CASES: list[Case] = [
    Case("/status", "cmd_status"),
    Case("/trades", "cmd_trades"),
    Case("/coins", "cmd_coins"),
    Case("/price", "cmd_price"),
    Case("/futures", "cmd_futures"),
    Case("/health", "cmd_health"),
    Case("/config", "cmd_config"),
    Case("/regime", "cmd_regime"),
    Case("/profit", "cmd_profit"),
    Case("/hop", "cmd_hop"),
    Case("/deposit", "cmd_deposit", ("",)),  # list deposits only; no write
    Case("/help", "cmd_help"),
    Case("/addcoin usage", "cmd_addcoin", ("",)),
    Case("/removecoin usage", "cmd_removecoin", ("",)),
    Case("/swap usage", "cmd_swap", ("",)),
    Case("/kill preview", "cmd_kill", ("",)),  # never /kill confirm
]


class SafetyTripwire(RuntimeError):
    """Raised if a smoke case accidentally reaches a mutating helper."""


class TelegramHTMLValidator(html.parser.HTMLParser):
    """Small strict validator for Telegram's limited HTML parse mode.

    Python's HTMLParser is forgiving, so this class is paired with raw-character
    scans below. It catches unsupported tags/entities and unbalanced nesting.
    The live --send mode is still the source of truth for Telegram parser
    behavior, but this local check is fast and catches most regressions before
    anything is sent.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in ALLOWED_TAGS:
            self.errors.append(f"unsupported tag <{tag}> at offset {self.getpos()}")
            return
        attr_names = {name for name, _ in attrs}
        if tag == "a":
            bad = attr_names - {"href"}
        elif tag == "code":
            bad = attr_names - {"class"}
        elif tag == "span":
            bad = attr_names - {"class"}
            cls = dict(attrs).get("class")
            if cls not in (None, "tg-spoiler"):
                self.errors.append("<span> only supports class=\"tg-spoiler\" in Telegram HTML")
        elif tag == "tg-emoji":
            bad = attr_names - {"emoji-id"}
        else:
            bad = attr_names
        if bad:
            self.errors.append(f"unsupported attribute(s) on <{tag}>: {sorted(bad)}")
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag not in ALLOWED_TAGS:
            self.errors.append(f"unsupported closing tag </{tag}> at offset {self.getpos()}")
            return
        if not self.stack:
            self.errors.append(f"orphan closing tag </{tag}> at offset {self.getpos()}")
            return
        if self.stack[-1] != tag:
            self.errors.append(f"mismatched closing tag </{tag}>; open stack is {self.stack}")
            # Recover enough to continue finding more errors.
            if tag in self.stack:
                while self.stack and self.stack[-1] != tag:
                    self.stack.pop()
                if self.stack:
                    self.stack.pop()
            return
        self.stack.pop()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.errors.append(f"self-closing tag <{tag}/> is not valid Telegram HTML")

    def handle_entityref(self, name: str) -> None:
        if name not in ALLOWED_NAMED_ENTITIES:
            self.errors.append(f"unsupported named entity &{name};")

    def handle_charref(self, name: str) -> None:
        try:
            if name.lower().startswith("x"):
                int(name[1:], 16)
            else:
                int(name, 10)
        except ValueError:
            self.errors.append(f"malformed numeric entity &#{name};")

    def handle_comment(self, data: str) -> None:
        self.errors.append("HTML comments are not supported by Telegram parse_mode=HTML")

    def unknown_decl(self, data: str) -> None:
        self.errors.append(f"unsupported HTML declaration <!{data}>")

    def close(self) -> None:
        super().close()
        if self.stack:
            self.errors.append(f"unclosed tag stack: {self.stack}")


def load_dotenv(path: Path, *, override: bool = False) -> None:
    """Load KEY=VALUE pairs from .env.telegram without printing secrets."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if override or key not in os.environ:
            os.environ[key] = value


def import_target(env_path: Path) -> Any:
    load_dotenv(env_path)
    # telegram_bot.py parses TELEGRAM_CHAT_IDS during import; make import safe
    # for local-only smoke runs even when .env.telegram is absent.
    os.environ.setdefault("TELEGRAM_CHAT_IDS", "0")
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")

    os.chdir(REPO_ROOT)
    sys.path.insert(0, str(REPO_ROOT))
    spec = importlib.util.spec_from_file_location("telegram_bot_smoke_target", TARGET)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not import {TARGET}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def install_mutation_tripwires(tb: Any) -> None:
    """Block known mutating helpers if a future smoke case reaches them."""

    def blocked(name: str) -> Callable[..., None]:
        def _blocked(*args: Any, **kwargs: Any) -> None:
            raise SafetyTripwire(
                f"unsafe mutating helper {name} was called; smoke suite only allows read-only/usage paths"
            )

        return _blocked

    for name in ("_enable_coin", "_disable_coin", "_execute_kill"):
        if hasattr(tb, name):
            setattr(tb, name, blocked(name))


def find_raw_angle_errors(text: str) -> list[str]:
    """Flag raw < or > characters that are not part of an HTML tag token."""
    errors: list[str] = []
    tag_spans: list[range] = []
    for match in TAG_TOKEN_RE.finditer(text):
        tag_spans.append(range(match.start(), match.end()))

    def in_tag_span(idx: int) -> bool:
        return any(idx in span for span in tag_spans)

    for idx, ch in enumerate(text):
        if ch == "<" and not in_tag_span(idx):
            errors.append(f"raw '<' at char {idx}; escape as &lt;")
        elif ch == ">" and not in_tag_span(idx):
            errors.append(f"raw '>' at char {idx}; escape as &gt;")
    return errors


def validate_html(text: str) -> list[str]:
    errors: list[str] = []
    errors.extend(find_raw_angle_errors(text))
    for match in RAW_AMP_RE.finditer(text):
        errors.append(f"raw or unsupported '&' at char {match.start()}; escape as &amp;")
    parser = TelegramHTMLValidator()
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:
        errors.append(f"HTML parser exception: {exc}")
    errors.extend(parser.errors)
    return errors


def run_case(tb: Any, case: Case) -> Result:
    result = Result(label=case.label)
    handler = getattr(tb, case.func_name, None)
    if handler is None:
        result.fail(f"missing handler {case.func_name}")
        return result

    start = time.perf_counter()
    try:
        response = handler(*case.args)
    except Exception as exc:
        result.elapsed_ms = (time.perf_counter() - start) * 1000
        result.fail(f"exception: {type(exc).__name__}: {exc}")
        result.errors.append(traceback.format_exc(limit=8).rstrip())
        return result

    result.elapsed_ms = (time.perf_counter() - start) * 1000
    if not isinstance(response, str):
        result.fail(f"handler returned {type(response).__name__}, expected str")
        response = "" if response is None else str(response)
    result.response = response
    result.chars = len(response)
    result.bytes_ = len(response.encode("utf-8"))

    if not response.strip():
        result.fail("empty response")
    if result.chars > TELEGRAM_LIMIT:
        result.fail(f"message too long: {result.chars} chars > {TELEGRAM_LIMIT}")
    html_errors = validate_html(response)
    for err in html_errors:
        result.fail(f"HTML: {err}")
    return result


def parse_chat_id(raw: str | None) -> int | None:
    if not raw:
        raw = os.environ.get("TELEGRAM_CHAT_IDS", "")
    if not raw:
        return None
    first = raw.split(",", 1)[0].strip()
    if not first:
        return None
    return int(first)


def telegram_api(token: str, method: str, payload: dict[str, Any], timeout: int = 20) -> dict[str, Any]:
    if requests is None:
        raise RuntimeError("requests is not installed")
    url = f"https://api.telegram.org/bot{token}/{method}"
    resp = requests.post(url, json=payload, timeout=timeout)
    try:
        data = resp.json()
    except ValueError:
        data = {"ok": False, "description": resp.text[:300]}
    if resp.status_code != 200 or not data.get("ok"):
        raise RuntimeError(f"{method} failed: HTTP {resp.status_code}: {data.get('description', data)}")
    return data


def send_then_delete(token: str, chat_id: int, text: str, *, delete_delay: float = 0.0) -> str:
    data = telegram_api(
        token,
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_notification": True,
        },
    )
    msg = data.get("result", {})
    message_id = msg.get("message_id")
    if not message_id:
        raise RuntimeError(f"sendMessage succeeded but returned no message_id: {data}")
    if delete_delay > 0:
        time.sleep(delete_delay)
    telegram_api(token, "deleteMessage", {"chat_id": chat_id, "message_id": message_id})
    return str(message_id)


def live_send_results(results: list[Result], *, chat_id: int | None, delete_delay: float) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set; cannot use --send")
    if chat_id is None:
        raise RuntimeError("No chat id supplied and TELEGRAM_CHAT_IDS is empty; cannot use --send")

    probe = "🧪 <b>Telegram smoke probe</b> <code>send/delete check</code>"
    send_then_delete(token, chat_id, probe, delete_delay=delete_delay)

    for res in results:
        if not res.ok:
            res.send_status = "SKIP(local-fail)"
            continue
        try:
            msg_id = send_then_delete(token, chat_id, res.response, delete_delay=delete_delay)
            res.send_status = f"sent+deleted:{msg_id}"
        except Exception as exc:
            res.send_status = "FAIL"
            res.fail(f"Telegram live send/delete failed: {type(exc).__name__}: {exc}")
            # Stop immediately; if deletion is broken, do not risk more spam.
            break


def print_report(results: list[Result], *, sent: bool) -> None:
    print("\nTelegram command smoke report")
    print(f"Target: {TARGET}")
    print(f"Limit:  {TELEGRAM_LIMIT} characters")
    print("")
    send_col = " SEND" if sent else ""
    print(f"{'CASE':24} {'OK':3} {'CHARS':>5} {'BYTES':>6} {'MS':>8}{send_col}")
    print("-" * (54 + (24 if sent else 0)))
    for res in results:
        ok = "OK" if res.ok else "BAD"
        send_status = f" {res.send_status[:23]:23}" if sent else ""
        print(f"{res.label:24} {ok:3} {res.chars:5d} {res.bytes_:6d} {res.elapsed_ms:8.1f}{send_status}")
    print("")

    failures = [r for r in results if not r.ok]
    if failures:
        print("Failures:")
        for res in failures:
            print(f"\n[{res.label}]")
            for err in res.errors:
                print(f"  - {err}")
    else:
        live = " + Telegram send/delete" if sent else ""
        print(f"PASS: {len(results)} command paths passed local validation{live}.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV, help="env file to load before importing telegram_bot.py")
    parser.add_argument("--send", action="store_true", help="also send/delete each locally-valid HTML response via Telegram")
    parser.add_argument("--chat-id", type=int, default=None, help="chat id for --send; default first TELEGRAM_CHAT_IDS")
    parser.add_argument("--delete-delay", type=float, default=0.0, help="seconds to wait between sendMessage and deleteMessage")
    args = parser.parse_args(argv)

    tb = import_target(args.env)
    install_mutation_tripwires(tb)

    results = [run_case(tb, case) for case in SAFE_CASES]

    if args.send:
        try:
            live_send_results(results, chat_id=args.chat_id or parse_chat_id(None), delete_delay=args.delete_delay)
        except Exception as exc:
            # Mark the suite failed, but do not print secrets.
            marker = Result(label="--send setup", ok=False)
            marker.fail(f"Telegram live validation setup failed: {type(exc).__name__}: {exc}")
            results.append(marker)

    print_report(results, sent=args.send)
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
