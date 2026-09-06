"""Append-only JSONL mailboxes for agent-to-agent messaging."""

import json
import threading
import time

from nexus_agent.config import MAILBOX_DIR, PROMPT, CLI_ACTIVE


MAILBOX_DIR.mkdir(exist_ok=True)
_mailbox_lock = threading.Lock()


def _terminal_print(text: str) -> None:
    """Print without clobbering the readline prompt line."""
    if threading.current_thread() is threading.main_thread() or not CLI_ACTIVE:
        print(text)
        return
    line = ""
    try:
        import readline
        line = readline.get_line_buffer()
    except Exception:
        pass
    print(f"\r\033[K{text}")
    print(PROMPT + line, end="", flush=True)


class MessageBus:
    """Simple append-only inbox store backed by JSONL files."""

    def send(self, from_agent: str, to_agent: str, content: str,
             msg_type: str = "message", metadata: dict | None = None) -> None:
        msg = {
            "from": from_agent,
            "to": to_agent,
            "content": content,
            "type": msg_type,
            "ts": time.time(),
            "metadata": metadata or {},
        }
        inbox = MAILBOX_DIR / f"{to_agent}.jsonl"
        with _mailbox_lock:
            with open(inbox, "a") as f:
                f.write(json.dumps(msg) + "\n")
        _terminal_print(
            f"  \033[33m[bus] {from_agent} → {to_agent}: "
            f"({msg_type}) {content[:50]}\033[0m"
        )

    def read_inbox(self, agent: str) -> list[dict]:
        inbox = MAILBOX_DIR / f"{agent}.jsonl"
        with _mailbox_lock:
            if not inbox.exists():
                return []
            msgs = [
                json.loads(line)
                for line in inbox.read_text().splitlines()
                if line.strip()
            ]
            inbox.unlink()
        return msgs


BUS = MessageBus()
