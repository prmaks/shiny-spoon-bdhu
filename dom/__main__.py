"""Command line interface: ``python -m dom {ingest,serve,pick,stats}``."""

from __future__ import annotations

import argparse
import json
import sys

from .config import Config, ConfigError
from .db import connect


def _print_json(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_ingest(cfg: Config, args: argparse.Namespace) -> int:
    from .ingest import ingest

    conn = connect(cfg.db_path)
    try:
        report = ingest(cfg, conn, thumbnails=not args.no_thumbs).to_dict()
    finally:
        conn.close()
    if args.json:
        _print_json(report)
        return 0
    print(f"Индексация завершена за {report['duration_s']} с")
    print(
        f"  арты: {report['artworks']} (новых {report['artworks_new']}, "
        f"обновлено {report['artworks_updated']}, удалено {report['artworks_removed']})"
    )
    print(
        f"  каталог: {report['catalogue_entries']} записей, "
        f"связано {report['catalogue_linked']}, без файла {len(report['catalogue_unlinked'])}"
    )
    print(
        f"  контрольные суммы: совпало {report['checksums_ok']}, "
        f"расхождений {len(report['checksum_mismatches'])}"
    )
    print(
        f"  дневник: {report['diary_entries']} записей; рассказы: {report['stories']}; "
        f"аудио: {report['audio']}; гардероб: {'да' if report['wardrobe'] else 'нет'}"
    )
    print(f"  миниатюр создано: {report['thumbnails_created']}")
    if report["missing"]:
        print("  не найдено: " + ", ".join(report["missing"]))
    for p in report["problems"]:
        print(f"  ! {p['path']}: {p['error']}")
    for m in report["checksum_mismatches"]:
        print(f"  ! контрольная сумма не совпадает: {m}")
    return 0


def cmd_serve(cfg: Config, args: argparse.Namespace) -> int:
    import uvicorn

    from .web import create_app

    uvicorn.run(create_app(cfg), host=args.host or cfg.host, port=args.port or cfg.port)
    return 0


def cmd_pick(cfg: Config, args: argparse.Namespace) -> int:
    from .picker import pick

    conn = connect(cfg.db_path)
    try:
        result = pick(conn, args.count, cfg.pick_cooldown_days)
    finally:
        conn.close()
    if args.json:
        _print_json(result)
        return 0
    if not result["artworks"]:
        print("Кладовая пуста — сначала запустите индексацию: python -m dom ingest")
        return 1
    if result["all_on_cooldown"]:
        print("Все арты недавно выпадали — кулдаун проигнорирован.")
    for a in result["artworks"]:
        title = f" — {a['title']}" if a["title"] else ""
        print(f"{a['day']}  {a['path']}{title}")
    return 0


def cmd_stats(cfg: Config, args: argparse.Namespace) -> int:
    from .queries import stats

    conn = connect(cfg.db_path)
    try:
        s = stats(conn)
    finally:
        conn.close()
    if args.json:
        _print_json(s)
        return 0
    print(f"Артов: {s['artworks']} (в каталоге: {s['artworks_catalogued']})")
    print(f"Записей каталога: {s['catalogue_entries']}")
    print(f"Записей дневника: {s['diary_entries']}")
    print(f"Рассказов: {s['stories']}")
    print(f"Аудио: {s['audio']}")
    print(f"Выборок из кладовой: {s['picks']}")
    if s["first_day"]:
        print(f"Период: {s['first_day']} — {s['last_day']}")
    last = s["last_ingest"]
    print(f"Последняя индексация: {last['started_at'] if last else 'ещё не было'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dom", description="Дом Шинобу — личный архив")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("ingest", help="проиндексировать каталог данных")
    p.add_argument("--json", action="store_true", help="вывести отчёт в JSON")
    p.add_argument("--no-thumbs", action="store_true", help="не создавать миниатюры")
    p.set_defaults(func=cmd_ingest)
    p = sub.add_parser("serve", help="запустить веб-сервер")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.set_defaults(func=cmd_serve)
    p = sub.add_parser("pick", help="вытянуть случайные арты из кладовой")
    p.add_argument("--count", "-n", type=int, default=1)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_pick)
    p = sub.add_parser("stats", help="статистика архива")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_stats)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2
    return args.func(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
