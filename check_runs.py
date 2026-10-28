"""Сверка журналов эпизодов с тем, что в них записано.

Каждый эпизод из runs/ переигрывается офлайн, без вызовов модели: среда загружается с теми же
инъекциями, на ней выполняются ровно те вызовы инструментов, что есть в журнале, и заново
считаются проверки AgentDojo. Если итог не совпадает с записанным или финальный ответ пустой,
журнал не показывает всего, что произошло (AgentDojo перезапускал агента на той же среде и
сохранил только последнюю попытку).

    python check_runs.py            # только отчёт
    python check_runs.py --move     # перенести такие эпизоды в runs_excluded/

После --move запустите ту же конфигурацию ещё раз: недостающие эпизоды прогонятся заново,
остальные возьмутся из кэша.
"""
import argparse
import json
import shutil
import warnings
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def final_output(messages):
    last = messages[-1] if messages else {}
    return last.get("content") if last.get("role") == "assistant" else None


def check(path, suites):
    from agentdojo.functions_runtime import FunctionsRuntime
    from agentdojo.task_suite.load_suites import get_suite
    from agentdojo.task_suite.task_suite import functions_stack_trace_from_messages

    d = json.loads(path.read_text(encoding="utf-8"))
    output = final_output(d["messages"])
    if output is None:
        return "пустой финальный ответ"
    key = (d["benchmark_version"], d["suite_name"])
    if key not in suites:
        suites[key] = get_suite(*key)
    suite = suites[key]
    task_id = d["user_task_id"]
    is_user_task = task_id.startswith("user_task")
    task = suite.get_user_task_by_id(task_id) if is_user_task else suite.get_injection_task_by_id(task_id)
    env = suite.load_and_inject_default_environment(d["injections"])
    if is_user_task:
        env = task.init_environment(env)
    pre = env.model_copy(deep=True)
    runtime = FunctionsRuntime(suite.tools)
    for message in d["messages"]:
        for call in message.get("tool_calls") or []:
            try:
                runtime.run_function(env, call["function"], call["args"])
            except Exception:
                pass
    trace = functions_stack_trace_from_messages(d["messages"])
    if suite._check_task_result(task, output, pre, env, trace) != d["utility"]:
        return "utility не совпадает с журналом"
    if d["injection_task_id"]:
        injection = suite.get_injection_task_by_id(d["injection_task_id"])
        if suite._check_task_result(injection, output, pre, env, trace) != d["security"]:
            return "успех атаки не совпадает с журналом"
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--logdir", default=str(ROOT / "runs"))
    parser.add_argument("--move", action="store_true", help="перенести несовпавшие эпизоды в runs_excluded/")
    args = parser.parse_args()
    warnings.filterwarnings("ignore")

    logdir = Path(args.logdir)
    excluded = logdir.parent / "runs_excluded"
    suites, problems = {}, []
    files = sorted(logdir.glob("*/*/*/*/*.json"))
    for path in files:
        reason = check(path, suites)
        if reason:
            problems.append((path, reason))

    print(f"Эпизодов: {len(files)}, не совпадают с журналом: {len(problems)}")
    for (pipeline, reason), n in sorted(Counter((p.relative_to(logdir).parts[0], r) for p, r in problems).items()):
        print(f"  {pipeline}: {reason} — {n}")
    if args.move:
        for path, _ in problems:
            target = excluded / path.relative_to(logdir)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(target))
        if problems:
            print(f"Перенесено в {excluded}. Запустите ту же конфигурацию ещё раз, чтобы прогнать их заново.")


if __name__ == "__main__":
    main()
