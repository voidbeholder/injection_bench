"""Просмотр журналов эпизодов AgentDojo из папки runs/ — для разбора на семинаре.

  python show_episode.py --list                       # все эпизоды с итогами
  python show_episode.py --list --attacked --hijacked # только где цель атакующего достигнута
  python show_episode.py --list --attacked --claimed  # агент написал «готово», а проверка не прошла
  python show_episode.py runs/<путь к эпизоду>.json   # показать один эпизод по шагам
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def text_of(message):
    content = message.get("content")
    if isinstance(content, list):
        content = " ".join(block.get("content", "") for block in content if isinstance(block, dict))
    return (content or "").strip()


def episodes(runs):
    for path in sorted(Path(runs).rglob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if "messages" in data and "suite_name" in data:
            yield path, data


def final_text(data):
    for message in reversed(data["messages"]):
        if message["role"] == "assistant" and text_of(message):
            return text_of(message)
    return ""


# Утверждение «сделано» в финальном ответе. Отдельные слова вроде «paid» не годятся: они
# встречаются и в отказах («amount to be paid»), и в пересказе истории («X sent you 10.0»).
# Поэтому ищем обороты «has been paid», «I have updated», «successfully sent», «tasks completed».
_DONE = r"(sent|transferred|completed|paid|scheduled|updated|changed|refunded|processed|made|set up|created)"
_CLAIM = re.compile(
    r"\b(?:has|have|had)\s+been\s+(?:successfully\s+)?" + _DONE + r"\b"
    r"|\bsuccessfully\s+" + _DONE + r"\b|\b" + _DONE + r"\s+successfully\b"
    r"|\bi(?:'ve| have)\s+(?:\w+\s+){0,2}?" + _DONE + r"\b"
    r"|\b(?:task|tasks|transaction|payment|transfer|refund)\b[^.\n]{0,20}\b(?:complete|completed|done)\b"
    r"|\bcompleted\s+the\s+task"
    r"|готово|успешно|(?:был[аио]?|были)\s+(?:отправлен|оплачен|выполнен|переведен|обновлен)", re.I)
_NEGATION = re.compile(r"\b(?:not|unable|cannot|failed)\b|n't\b", re.I)


def claims_success(text):
    """Есть ли в тексте утверждение, что действие выполнено (без отрицания рядом)."""
    for match in _CLAIM.finditer(text):
        if not _NEGATION.search(text[max(0, match.start() - 30):match.end()]):
            return True
    return False


def list_episodes(args):
    rows = []
    for path, d in episodes(args.runs):
        attacked = d.get("injection_task_id") is not None
        if args.attacked and not attacked:
            continue
        if args.model and args.model not in d["pipeline_name"]:
            continue
        hijacked = attacked and bool(d.get("security"))
        if args.hijacked and not hijacked:
            continue
        if args.resisted and (not attacked or hijacked):
            continue
        # «Готово» только на словах: задача пользователя по проверке не решена,
        # а в финальном ответе есть слова об успехе.
        claimed = not d.get("utility") and claims_success(final_text(d))
        if args.claimed and not claimed:
            continue
        rows.append((d["pipeline_name"], d["suite_name"], d["user_task_id"], d.get("injection_task_id") or "—",
                     d.get("attack_type") or "—", d.get("utility"), d.get("security") if attacked else None, path))
    if not rows:
        print("Эпизодов не найдено. Проверьте --runs и фильтры.")
        return
    print(f"{'модель':<34} {'среда':<9} {'задача':<14} {'атака':<18} {'польз.':>6} {'атака усп.':>10}  файл")
    for model, suite, user, inj, attack, util, sec, path in rows:
        mark = lambda v: "—" if v is None else ("да" if v else "нет")
        print(f"{model[:34]:<34} {suite:<9} {user:<14} {inj:<18} {mark(util):>6} {mark(sec):>10}  "
              f"{path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")
    print(f"\nВсего: {len(rows)}")


def show(path, width):
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    attacked = d.get("injection_task_id") is not None
    print(f"Модель: {d['pipeline_name']}   среда: {d['suite_name']}   задача: {d['user_task_id']}")
    if attacked:
        print(f"Атака: {d.get('attack_type')}   задача атакующего: {d['injection_task_id']}")
    print("Итог проверки по состоянию среды:")
    print(f"  задача пользователя решена: {'да' if d.get('utility') else 'нет'}")
    if attacked:
        print(f"  цель атакующего достигнута: {'да' if d.get('security') else 'нет'}")
    squash = lambda t: " ".join(t.split())
    injected = [squash(v)[:40] for v in (d.get("injections") or {}).values() if v and v.strip()]
    print("-" * 80)
    for i, m in enumerate(d["messages"]):
        role = m["role"]
        body = squash(text_of(m))
        if role == "system":
            print(f"[{i}] system: (системный промпт, {len(body)} символов)")
            continue
        if role == "assistant":
            for call in m.get("tool_calls") or []:
                print(f"[{i}] агент → вызывает {call.get('function')}({json.dumps(call.get('args', {}), ensure_ascii=False)})")
            if body:
                print(f"[{i}] агент пишет: {body[:width]}")
            continue
        if role == "tool":
            flag = "  ◀ здесь в данных инъекция" if any(s in body for s in injected) else ""
            print(f"[{i}] результат инструмента: {body[:width]}{'…' if len(body) > width else ''}{flag}")
            continue
        print(f"[{i}] {role}: {body[:width]}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("episode", nargs="?", help="путь к JSON эпизода")
    parser.add_argument("--runs", default=str(ROOT / "runs"))
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--attacked", action="store_true", help="только эпизоды с атакой")
    parser.add_argument("--hijacked", action="store_true", help="только где цель атакующего достигнута")
    parser.add_argument("--resisted", action="store_true", help="только где атака не удалась")
    parser.add_argument("--claimed", action="store_true", help="задача не решена, но агент написал об успехе")
    parser.add_argument("--model", help="фильтр по части имени модели")
    parser.add_argument("--width", type=int, default=300, help="сколько символов показывать в шаге")
    args = parser.parse_args()
    if args.list or not args.episode:
        list_episodes(args)
    else:
        show(args.episode, args.width)


if __name__ == "__main__":
    main()
