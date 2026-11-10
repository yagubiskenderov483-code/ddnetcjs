"""CLI локального агента.

Использование:
  tde-agent pair <CODE> --api-id <ID> --api-hash <HASH>

Опции:
  --api-id / --api-hash  — реквизиты my.telegram.org (или env TG_API_ID / TG_API_HASH)
  --api-base             — URL бэкенда (env TDE_API_BASE), default http://localhost:8000
  --no-media             — не скачивать медиафайлы
  --limit N              — N последних сообщений на диалог (0 = без лимита)
  --max-file-mb MB       — пропускать медиафайлы крупнее (default 50)
  --session PATH         — путь к файлу сессии Telethon (default ~/.tde-agent/session)

Коды авторизации и пароль 2FA вводятся локально в этой консоли и НИКУДА
кроме серверов Telegram не отправляются.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import os
import platform
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn, TimeRemainingColumn

from . import __version__
from .exporter import ExportOptions, export_account
from .relay import Relay
from .tg_auth import interactive_login


console = Console()


def _env_or(args_val: Optional[str], key: str) -> Optional[str]:
    return args_val or os.environ.get(key)


def _agent_info() -> str:
    return f"{platform.system()} {platform.release()} · tde_agent {__version__} · py{platform.python_version()}"


def _make_zip(src_dir: Path, out_path: Path, on_progress=None) -> None:
    files = []
    for root, _, names in os.walk(src_dir):
        for n in names:
            files.append(Path(root) / n)
    total = max(1, len(files))
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for i, f in enumerate(files, 1):
            arc = f.relative_to(src_dir)
            zf.write(f, arcname=str(arc))
            if on_progress:
                on_progress(i, total)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


async def _run_export(args, api_id: int, api_hash: str, session_path: Path,
                      export_dir: Path, relay: Relay) -> None:
    def _ask_phone() -> str:
        return input("Номер телефона Telegram (в формате +79991234567): ").strip()

    def _ask_code() -> str:
        return input("Код из Telegram (только у вас в клиенте): ").strip()

    def _ask_password() -> str:
        return getpass.getpass("Пароль 2FA (локально, не отправляется на сервер): ")

    client = await interactive_login(
        api_id, api_hash, session_path,
        phone_cb=_ask_phone, code_cb=_ask_code, password_cb=_ask_password,
    )
    try:
        me = await client.get_me()
        console.print(f"[green]Авторизация OK[/green]: {me.first_name or ''} @{me.username or '—'}")

        opts = ExportOptions(
            include_messages=not args.no_messages,
            include_media=not args.no_media,
            message_limit_per_chat=args.limit,
            media_max_bytes_per_file=args.max_file_mb * 1024 * 1024,
        )

        async def on_progress(pct: int, stage: str) -> None:
            relay.progress("exporting", pct, stage)
            console.print(f"  [dim]{pct:3}% · {stage}[/dim]")

        await export_account(client, export_dir, opts, on_progress)
    finally:
        await client.disconnect()


def cmd_pair(args: argparse.Namespace) -> int:
    api_id = _env_or(args.api_id, "TG_API_ID")
    api_hash = _env_or(args.api_hash, "TG_API_HASH")
    api_base = _env_or(args.api_base, "TDE_API_BASE") or "http://localhost:8000"

    if not api_id or not api_hash:
        console.print("[red]Нужны api_id и api_hash от my.telegram.org[/red]")
        console.print("Передайте --api-id / --api-hash или задайте TG_API_ID / TG_API_HASH.")
        return 2
    try:
        api_id_i = int(api_id)
    except ValueError:
        console.print("[red]api_id должен быть числом[/red]")
        return 2

    session_path = Path(args.session).expanduser()
    session_path.parent.mkdir(parents=True, exist_ok=True)

    relay = Relay(api_base)
    console.print(f"[cyan]Подключение к бэкенду[/cyan] {api_base} …")
    try:
        relay.claim(args.pairing_code, _agent_info())
    except Exception as e:
        console.print(f"[red]Pairing отклонён:[/red] {e}")
        return 1
    console.print(f"[green]Pairing OK[/green] · pairing_id={relay.pairing_id}")

    workdir = Path(tempfile.mkdtemp(prefix="tde_export_"))
    export_dir = workdir / "export"
    zip_path = workdir / f"telegram_export_{int(time.time())}.zip"

    try:
        asyncio.run(_run_export(args, api_id_i, api_hash, session_path, export_dir, relay))

        relay.progress("exporting", 92, "упаковка ZIP")
        with Progress(
            TextColumn("[bold]Упаковка[/bold]"),
            BarColumn(bar_width=None),
            "[progress.percentage]{task.percentage:>3.0f}%",
            TimeElapsedColumn(),
        ) as bar:
            task = bar.add_task("zip", total=1.0)
            def on_zip(i, total):
                bar.update(task, completed=i / total)
            _make_zip(export_dir, zip_path, on_progress=on_zip)

        size_mb = zip_path.stat().st_size / 1024 / 1024
        console.print(f"[cyan]Архив готов:[/cyan] {zip_path.name} ({size_mb:.1f} MB)")

        relay.progress("uploading", 1, "подсчёт sha256")
        console.print(f"sha256: {_sha256(zip_path)}")

        total = zip_path.stat().st_size
        with Progress(
            TextColumn("[bold]Передача на сервер[/bold]"),
            BarColumn(bar_width=None),
            "[progress.percentage]{task.percentage:>3.0f}%",
            TimeRemainingColumn(),
        ) as bar:
            task = bar.add_task("upload", total=total)
            last_ping = 0.0
            def on_up(sent, total_):
                nonlocal last_ping
                bar.update(task, completed=sent)
                now = time.time()
                if now - last_ping > 1.0:
                    pct = int(sent * 100 / max(1, total_))
                    relay.progress("uploading", pct, f"{sent // 1024 // 1024} MB / {total_ // 1024 // 1024} MB")
                    last_ping = now
            relay.upload(zip_path, on_progress=on_up)

        console.print("[green]Готово.[/green] Сервер проверит контрольную сумму и перешлёт архив в Telegram-бот владельца.")
        console.print("Можно закрыть агент.")
        return 0
    except KeyboardInterrupt:
        console.print("[yellow]Прервано пользователем.[/yellow]")
        relay.report_error("interrupted_by_user")
        return 130
    except Exception as e:
        console.print(f"[red]Ошибка:[/red] {type(e).__name__}: {e}")
        relay.report_error(f"{type(e).__name__}: {e}")
        return 1
    finally:
        # удаляем временный экспорт целиком
        try:
            if zip_path.exists():
                zip_path.unlink()
        except Exception:
            pass
        try:
            import shutil
            if export_dir.exists():
                shutil.rmtree(export_dir, ignore_errors=True)
            if workdir.exists():
                shutil.rmtree(workdir, ignore_errors=True)
        except Exception:
            pass


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="tde-agent", description="Telegram Data Extractor — local agent")
    ap.add_argument("--version", action="version", version=f"tde-agent {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("pair", help="Привязаться к веб-сессии по pairing-коду и выполнить экспорт")
    p.add_argument("pairing_code", help="6-значный код с сайта")
    p.add_argument("--api-id", help="Telegram api_id (my.telegram.org)")
    p.add_argument("--api-hash", help="Telegram api_hash")
    p.add_argument("--api-base", help="URL бэкенда TDE (по умолчанию http://localhost:8000)")
    p.add_argument("--no-media", action="store_true", help="Не скачивать медиа")
    p.add_argument("--no-messages", action="store_true", help="Только профиль + список диалогов")
    p.add_argument("--limit", type=int, default=0, help="Лимит сообщений на диалог (0 = без лимита)")
    p.add_argument("--max-file-mb", type=int, default=50, help="Пропускать медиа крупнее N MB (default 50)")
    p.add_argument("--session", default="~/.tde-agent/session", help="Путь к файлу сессии Telethon")
    p.set_defaults(func=cmd_pair)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
