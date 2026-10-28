"""Прогон моделей OpenRouter через бенчмарк AgentDojo (ETH Zurich).

Все задачи, атаки и проверки берутся из самого AgentDojo; скрипт только подключает модели
OpenRouter, выбирает подмножество задач и сводит метрики в таблицу:

  utility            — доля задач пользователя, решённых без атаки;
  utility_under_attack — доля задач, решённых, когда в данные подложена инъекция;
  asr                — доля случаев, когда агент выполнил задачу атакующего (targeted ASR).

Примеры:
  python run_bench.py --dry-run
  python run_bench.py --models weak strong --suites banking --user-tasks 5 --injection-tasks 3
  python run_bench.py --models weak --attacks important_instructions_no_model_name injecagent \
      --defenses none spotlighting_with_delimiting
"""
import argparse
import csv
import json
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent

DEFAULT_MODELS = {
    "weak": "meta-llama/llama-3.1-8b-instruct",
    "medium": "openai/gpt-4.1-mini",
    "strong": "anthropic/claude-sonnet-4.5",
}
# Атаки, которым не нужно «знать» имя модели (иначе AgentDojo требует модель из своего списка).
DEFAULT_ATTACKS = ["important_instructions_no_model_name"]
# атаки на стенде в браузере, от грубой к изощрённой
STAND_ATTACKS = ["direct", "ignore_previous", "injecagent", "important_instructions_no_model_name"]
DEFENSES = ["none", "spotlighting_with_delimiting", "repeat_user_prompt", "tool_filter"]
SUITES = ["banking", "slack", "travel", "workspace"]


def load_env(path=ROOT / ".env"):
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def resolve_model(name):
    if name in DEFAULT_MODELS:
        return os.getenv(f"MODEL_{name.upper()}", "").strip() or DEFAULT_MODELS[name]
    return name


def safe_name(model_id):
    # AgentDojo кладёт логи в папку с именем пайплайна; «:» в имени папки Windows не допускает.
    return model_id.replace("/", "__").replace(":", "_")


def _clean_arguments(raw):
    """Аргументы вызова инструмента → строка с JSON-объектом, которую AgentDojo точно разберёт.

    Слабые модели присылают "", "null", не объект или несколько объектов подряд
    ('{"a":1}{"b":2}'). Берём первый JSON-объект; если его нет — "{}": инструмент тогда
    вернёт модели ошибку про аргументы, и эпизод продолжится, а не обрушит весь прогон."""
    text = (raw or "").strip()
    try:
        parsed = json.loads(text or "null")
    except ValueError:
        start = text.find("{")
        try:
            parsed, _ = json.JSONDecoder().raw_decode(text[start:]) if start >= 0 else (None, 0)
        except ValueError:
            parsed = None
    return json.dumps(parsed, ensure_ascii=False) if isinstance(parsed, dict) else "{}"


def _split_parallel_calls(messages):
    """Ход ассистента с несколькими вызовами инструментов → последовательность
    «вызов → результат» по одному. По смыслу история та же, но её принимают и провайдеры,
    которые не поддерживают несколько вызовов в одном ходе."""
    out, i = [], 0
    while i < len(messages):
        m = messages[i]
        calls = (m.get("tool_calls") or []) if m.get("role") == "assistant" else []
        if len(calls) <= 1:
            out.append(m)
            i += 1
            continue
        results, j = {}, i + 1
        while j < len(messages) and messages[j].get("role") == "tool":
            results[messages[j].get("tool_call_id")] = messages[j]
            j += 1
        for k, call in enumerate(calls):
            out.append(dict(m, content=m.get("content") if k == 0 else None, tool_calls=[call]))
            if call.get("id") in results:
                out.append(results.pop(call["id"]))
        out.extend(results.values())  # результаты без пары — оставляем как были
        i = j
    return out


class _Completions:
    """Прослойка между AgentDojo и OpenRouter.

    * AgentDojo шлёт системное сообщение с ролью developer (так требует OpenAI). Не все
      модели на OpenRouter её понимают, поэтому превращаем её обратно в system.
    * Некоторые модели вызывают инструмент без аргументов и присылают arguments = "" или
      "null". AgentDojo ждёт словарь и падает, останавливая весь прогон, — подставляем "{}".
    """

    def __init__(self, inner):
        self.inner = inner

    def create(self, **kwargs):
        import openai
        kwargs["messages"] = [dict(m, role="system") if m.get("role") == "developer" else m
                              for m in kwargs["messages"]]
        # Сбои провайдеров повторяем с паузой: пустой ответ 200 без choices, лимит (429),
        # ошибки 5xx. Отдельный случай — «This model only supports single tool-calls»: когда
        # основной провайдер упёрся в лимит, OpenRouter отдаёт запрос другому, который не
        # принимает несколько вызовов инструментов в одном ходе. Ошибка приходит то как 400,
        # то как ответ 200 без choices. Тогда раскладываем такие ходы истории по одному,
        # запрещаем параллельные вызовы и просим OpenRouter больше не слать запрос этому провайдеру.
        def error_of(response):
            return (getattr(response, "model_extra", None) or {}).get("error")

        def single_calls_only(problem):
            kwargs["messages"] = _split_parallel_calls(kwargs["messages"])
            kwargs["parallel_tool_calls"] = False
            provider = ((problem.get("metadata") or {}).get("provider_name")
                        if isinstance(problem, dict) else None)
            if provider:
                extra = dict(kwargs.get("extra_body") or {})
                routing = dict(extra.get("provider") or {})
                routing["ignore"] = sorted(set(routing.get("ignore") or []) | {provider})
                extra["provider"] = routing
                kwargs["extra_body"] = extra

        response, problem = None, None
        for attempt, pause in enumerate((3, 10, 30, None)):
            try:
                response = self.inner.create(**kwargs)
                problem = None if response.choices else error_of(response)
            except openai.BadRequestError as error:
                if "single tool-call" not in f"{error} {error.body}":
                    raise
                response, problem = None, error.body
            except (openai.RateLimitError, openai.InternalServerError, openai.APIConnectionError):
                if pause is None:
                    raise
                time.sleep(pause)
                continue
            if response is not None and response.choices:
                break
            if "single tool-call" in str(problem):
                single_calls_only(problem)
            if pause is None:
                raise RuntimeError(f"OpenRouter не вернул ответ для {kwargs.get('model')} "
                                   f"после {attempt + 1} попыток: {problem or 'без описания'}. "
                                   "Запустите ту же конфигурацию ещё раз: готовые эпизоды возьмутся из кэша.")
            time.sleep(pause)
        for choice in response.choices or []:
            for call in (choice.message.tool_calls or []) if choice.message else []:
                call.function.arguments = _clean_arguments(call.function.arguments)
            # Если финальный ответ пустой (content = None), AgentDojo перезапускает агента до трёх раз
            # на той же, уже изменённой среде, а в журнал пишет только последнюю попытку. Тогда
            # действия первых попыток (в том числе атаки) в журнале не видны. Пустая строка вместо
            # None отключает перезапуск: в журнале остаётся ровно то, что произошло.
            if choice.message and choice.message.content is None and not choice.message.tool_calls:
                choice.message.content = ""
        return response


def openrouter_client():
    import openai
    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not key:
        sys.exit("Нет ключа: скопируйте .env.example в .env и заполните OPENROUTER_API_KEY.")
    real = openai.OpenAI(base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
                         api_key=key, max_retries=2, timeout=120,
                         default_headers={"HTTP-Referer": "http://127.0.0.1", "X-Title": "injection-bench"})
    return SimpleNamespace(chat=SimpleNamespace(completions=_Completions(real.chat.completions)))


def build_pipeline(client, model_id, defense):
    from agentdojo.agent_pipeline import AgentPipeline, OpenAILLM, PipelineConfig
    llm = OpenAILLM(client, model_id, temperature=0.0)
    llm.name = safe_name(model_id)
    pipeline = AgentPipeline.from_config(PipelineConfig(
        llm=llm, model_id=None, defense=None if defense == "none" else defense,
        system_message_name=None, system_message=None))
    # tool_filter создаёт отдельный вызов модели и берёт её id из имени пайплайна,
    # а имя мы поменяли ради путей к журналам — возвращаем настоящий id.
    for element in pipeline.elements:
        if type(element).__name__ == "OpenAILLMToolFilter":
            element.model = model_id
    return pipeline


def make_attack(load_attack, attack_name, suite, pipeline):
    """Атаки AgentDojo при создании ищут «прозаическое» имя модели по имени пайплайна и падают на
    моделях вне своего списка. Подставляем нейтральное имя только на время создания атаки:
    шаблоны с именем модели обратятся к «Local model», варианты *_no_model_name имя не используют.
    Имя пайплайна (по нему лежат журналы) затем возвращаем."""
    real_name = pipeline.name
    pipeline.name = "local"
    try:
        return load_attack(attack_name, suite, pipeline)
    finally:
        pipeline.name = real_name


def pick(ids, spec, rng):
    """spec: None — все; число — случайная выборка (фиксированный seed); иначе список id."""
    ids = sorted(ids, key=lambda s: int(s.rsplit("_", 1)[-1]))
    if not spec:
        return ids
    if len(spec) == 1 and spec[0].isdigit():
        n = int(spec[0])
        return sorted(rng.sample(ids, min(n, len(ids))), key=lambda s: int(s.rsplit("_", 1)[-1]))
    unknown = set(spec) - set(ids)
    if unknown:
        raise ValueError(f"Неизвестные задачи: {sorted(unknown)}")
    return list(spec)


def mean(values):
    values = list(values)
    return round(sum(values) / len(values), 4) if values else None


def main():
    load_env()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--models", nargs="+", default=["weak", "strong"],
                        help="weak / medium / strong или явные id моделей OpenRouter")
    parser.add_argument("--suites", nargs="+", default=["banking"],
                        choices=SUITES)
    parser.add_argument("--attacks", nargs="+", default=DEFAULT_ATTACKS,
                        help="атаки из AgentDojo; см. --list")
    parser.add_argument("--defenses", nargs="+", default=["none"],
                        choices=DEFENSES)
    parser.add_argument("--user-tasks", nargs="*", help="число (случайная выборка) или список id; по умолчанию все")
    parser.add_argument("--injection-tasks", nargs="*", help="число или список id; по умолчанию все")
    parser.add_argument("--benchmark-version", default="v1.2.2")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--logdir", default=str(ROOT / "runs"))
    parser.add_argument("--force-rerun", action="store_true", help="не брать готовые результаты из logdir")
    parser.add_argument("--dry-run", action="store_true", help="показать план и число прогонов, без вызовов API")
    parser.add_argument("--list", action="store_true", help="показать доступные атаки и задачи")
    args = parser.parse_args()

    from agentdojo.attacks.attack_registry import ATTACKS
    from agentdojo.task_suite.load_suites import get_suite

    if args.list:
        print("Атаки:", ", ".join(sorted(a for a in ATTACKS if not a.endswith("_dos") and a not in ("dos", "manual"))))
        for name in ["banking", "slack", "travel", "workspace"]:
            s = get_suite(args.benchmark_version, name)
            print(f"{name}: {len(s.user_tasks)} задач пользователя, {len(s.injection_tasks)} задач атакующего")
        return

    try:
        plan = make_plan(args.models, args.suites, args.attacks, args.defenses, args.user_tasks,
                         args.injection_tasks, args.seed, args.benchmark_version)
    except ValueError as error:
        sys.exit(str(error))
    print("План прогона:")
    for label, model_id in plan["models"]:
        print(f"  модель {label}: {model_id}")
    for suite_name, _, user_ids, inj_ids in plan["suites"]:
        print(f"  {suite_name}: задачи пользователя {user_ids}")
        print(f"  {' ' * len(suite_name)}  задачи атакующего {inj_ids}")
    print(f"  атаки: {plan['attacks'] or ['(без атак)']}; защиты: {plan['defenses']}")
    print(f"  всего эпизодов агента: {plan['episodes']} (каждый — несколько вызовов модели)")
    if args.dry_run:
        return
    rows, stamp = execute(plan, args.logdir, args.force_rerun, args.benchmark_version,
                          meta={"args": vars(args)}, on_row=print_table)
    print(f"\nГотово. Таблица: results/summary-{stamp}.csv, журналы эпизодов: {args.logdir}")


def make_plan(models, suites, attacks, defenses, user_tasks, injection_tasks, seed=0, version="v1.2.2"):
    """Выбор задач и подсчёт объёма. user_tasks/injection_tasks: None, ["N"] или список id."""
    from agentdojo.attacks.attack_registry import ATTACKS
    from agentdojo.task_suite.load_suites import get_suite

    bad = [a for a in attacks if a not in ATTACKS and a != "none"]
    if bad:
        raise ValueError(f"Неизвестные атаки: {bad}. Список: python run_bench.py --list")
    unknown_defenses = set(defenses) - set(DEFENSES)
    if unknown_defenses:
        raise ValueError(f"Неизвестные защиты: {sorted(unknown_defenses)}")
    rng = random.Random(seed)
    plan_suites = []
    for suite_name in suites:
        suite = get_suite(version, suite_name)
        plan_suites.append((suite_name, suite, pick(suite.user_tasks.keys(), user_tasks, rng),
                            pick(suite.injection_tasks.keys(), injection_tasks, rng)))
    attacks = [a for a in attacks if a != "none"]
    models = [(name, resolve_model(name)) for name in models]
    # без атаки: задачи пользователя; задачи атакующего как обычные — один раз на модель и
    # защиту (дальше из кэша); плюс все пары «задача пользователя × задача атакующего» на атаку
    episodes = sum(len(u) + (len(i) if attacks else 0) + len(attacks) * len(u) * len(i)
                   for _, _, u, i in plan_suites)
    episodes *= len(models) * len(defenses)
    return {"models": models, "suites": plan_suites, "attacks": attacks, "defenses": list(defenses),
            "episodes": episodes, "units": len(models) * len(defenses) * len(plan_suites)}


def execute(plan, logdir, force_rerun=False, version="v1.2.2", meta=None, on_row=None, client=None):
    """Прогон плана. После каждого блока (модель × защита × среда) вызывает on_row(rows)
    и сохраняет промежуточную таблицу в results/."""
    from agentdojo.attacks.attack_registry import load_attack
    from agentdojo.benchmark import benchmark_suite_with_injections, benchmark_suite_without_injections
    from agentdojo.logging import OutputLogger

    client = client or openrouter_client()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = ROOT / "results"
    out_dir.mkdir(exist_ok=True)
    rows = []
    for label, model_id in plan["models"]:
        for defense in plan["defenses"]:
            pipeline = build_pipeline(client, model_id, defense)
            for suite_name, suite, user_ids, inj_ids in plan["suites"]:
                started = time.time()
                row = {"model_label": label, "model": model_id, "pipeline": pipeline.name, "defense": defense,
                       "suite": suite_name, "n_user_tasks": len(user_ids), "n_injection_tasks": len(inj_ids)}
                with OutputLogger(str(logdir)):
                    benign = benchmark_suite_without_injections(
                        pipeline, suite, logdir=Path(logdir), force_rerun=force_rerun,
                        user_tasks=user_ids, benchmark_version=version)
                    row["utility"] = mean(benign["utility_results"].values())
                    for attack_name in plan["attacks"]:
                        attack = make_attack(load_attack, attack_name, suite, pipeline)
                        res = benchmark_suite_with_injections(
                            pipeline, suite, attack, logdir=Path(logdir), force_rerun=force_rerun,
                            user_tasks=user_ids, injection_tasks=inj_ids, verbose=False,
                            benchmark_version=version)
                        rows.append({**row, "attack": attack_name,
                                     "utility_under_attack": mean(res["utility_results"].values()),
                                     # security_results в AgentDojo = True, если цель атакующего достигнута
                                     "asr": mean(res["security_results"].values()),
                                     "n_attack_episodes": len(res["security_results"]),
                                     # может ли модель выполнить задачу атакующего, если попросить напрямую;
                                     # если нет — низкий ASR может объясняться неумением, а не устойчивостью
                                     "attacker_task_doable": mean(res["injection_tasks_utility_results"].values()),
                                     "seconds": round(time.time() - started)})
                    if not plan["attacks"]:
                        rows.append({**row, "attack": "none", "utility_under_attack": None, "asr": None,
                                     "n_attack_episodes": 0, "attacker_task_doable": None,
                                     "seconds": round(time.time() - started)})
                save(rows, out_dir, stamp, meta or {})
                if on_row:
                    on_row(rows)
    return rows, stamp


def print_table(rows):
    pct = lambda v: "—" if v is None else f"{v * 100:5.1f}%"
    print(f"\n{'модель':<34} {'защита':<16} {'среда':<9} {'атака':<38} {'utility':>8} {'u/attack':>9} "
          f"{'ASR':>7} {'атака выполнима':>16}")
    for r in rows:
        print(f"{r['model'][:34]:<34} {r['defense'][:16]:<16} {r['suite']:<9} {r['attack'][:38]:<38} "
              f"{pct(r['utility']):>8} {pct(r['utility_under_attack']):>9} {pct(r['asr']):>7} "
              f"{pct(r['attacker_task_doable']):>16}")


def save(rows, out_dir, stamp, meta):
    fields = list(rows[0].keys())
    with open(out_dir / f"summary-{stamp}.csv", "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    data = {**meta, "rows": rows}
    (out_dir / f"summary-{stamp}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
