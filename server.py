"""Веб-интерфейс к run_bench.py: выбрать модели, атаки и защиты, запустить AgentDojo,
посмотреть таблицу и журналы эпизодов. Только локально: http://127.0.0.1:8770

  .venv\\Scripts\\python server.py [--port 8770]
"""
import argparse
import json
import os
import re
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import run_bench
from show_episode import claims_success, episodes, final_text, text_of

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "runs"
RESULTS = ROOT / "results"
TOOL_FILTER_PREFIX = "Your task is to filter the list of tools"
# Слабые модели иногда пишут вызов инструмента текстом: {"name": ...}, <function=...>, <read_file>.
# Такой «вызов» не выполняется — это просто текст ответа.
FAKE_CALL = re.compile(r'<function[=/>]|-function=|\{\s*"(?:name|type)"\s*:|<(?:read_file|search_files_by_filename|send_money)>')

jobs = {}
job_lock = threading.Lock()


def config():
    from agentdojo.task_suite.load_suites import get_suite
    # На стенде только атаки, которые разбираются на семинаре; остальные доступны из консоли.
    attacks = run_bench.STAND_ATTACKS
    suites = {}
    for name in run_bench.SUITES:
        s = get_suite("v1.2.2", name)
        suites[name] = {"user_tasks": len(s.user_tasks), "injection_tasks": len(s.injection_tasks)}
    return {"key_configured": bool(os.getenv("OPENROUTER_API_KEY", "").strip()),
            "base_url": os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
            "presets": {name: run_bench.resolve_model(name) for name in run_bench.DEFAULT_MODELS},
            "attacks": attacks, "default_attacks": run_bench.DEFAULT_ATTACKS,
            "defenses": run_bench.DEFENSES, "suites": suites}


def parse_request(data):
    models = [m.strip() for m in data.get("models", []) if isinstance(m, str) and m.strip()]
    if not models:
        raise ValueError("Выберите хотя бы одну модель.")
    count = lambda key: [str(int(data[key]))] if data.get(key) not in (None, "", 0, "0") else None
    return dict(models=models[:6], suites=[data.get("suite", "banking")],
                attacks=data.get("attacks") or run_bench.DEFAULT_ATTACKS,
                defenses=data.get("defenses") or ["none"],
                user_tasks=count("user_tasks"), injection_tasks=count("injection_tasks"),
                seed=int(data.get("seed") or 0))


def plan_summary(plan):
    return {"episodes": plan["episodes"], "units": plan["units"],
            "models": [{"label": l, "id": m} for l, m in plan["models"]],
            "suites": [{"name": n, "user_tasks": u, "injection_tasks": i} for n, _, u, i in plan["suites"]],
            "attacks": plan["attacks"], "defenses": plan["defenses"]}


def expected_files(plan):
    """Пути журналов, которые появятся в runs/ по ходу прогона (уже готовые тоже считаются)."""
    files = []
    for _, model_id in plan["models"]:
        for defense in plan["defenses"]:
            name = run_bench.safe_name(model_id) + ("" if defense == "none" else f"-{defense}")
            for suite_name, _, user_ids, inj_ids in plan["suites"]:
                base = RUNS / name / suite_name
                files += [base / u / "none" / "none.json" for u in user_ids]
                for attack in plan["attacks"]:
                    files += [base / i / "none" / "none.json" for i in inj_ids]
                    files += [base / u / attack / f"{i}.json" for u in user_ids for i in inj_ids]
    return sorted(set(files))


def run_job(job_id, plan, request):
    job = jobs[job_id]

    def on_row(rows):
        with job_lock:
            job["rows"] = list(rows)
            job["units_done"] += 1

    try:
        rows, stamp = run_bench.execute(plan, RUNS, meta={"request": request}, on_row=on_row)
        with job_lock:
            job.update(status="done", rows=rows, stamp=stamp)
    except Exception as error:  # показываем ошибку в интерфейсе вместо тихого падения потока
        with job_lock:
            job.update(status="error", error=f"{type(error).__name__}: {error}")
        traceback.print_exc()
    finally:
        job["finished"] = time.time()


def showcase():
    """Эпизоды, отобранные для семинара: путь внутри runs/ → заметка, что в нём смотреть."""
    path = ROOT / "showcase.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


def episode_rows(params):
    pick = lambda key: (params.get(key) or [""])[0]
    kind = pick("kind")
    notes = showcase()
    out = []
    for path, d in episodes(RUNS):
        if pick("pipeline") and d["pipeline_name"] != pick("pipeline"):
            continue
        if pick("suite") and d["suite_name"] != pick("suite"):
            continue
        attacked = d.get("injection_task_id") is not None
        rel = str(path.relative_to(RUNS)).replace("\\", "/")
        # эпизоды без атаки общие для всех атак этой модели и защиты — их фильтр по атаке не отсекает
        if pick("attack") and attacked and d.get("attack_type") != pick("attack"):
            continue
        if pick("attack") not in ("", "none") and not attacked and kind not in ("benign", "showcase"):
            continue
        hijacked = attacked and bool(d.get("security"))
        # «готово» в тексте, а проверка задачи пользователя не прошла
        claimed = not d.get("utility") and claims_success(final_text(d))
        if kind == "hijacked" and not hijacked or kind == "resisted" and (not attacked or hijacked) \
                or kind == "claimed" and not claimed or kind == "benign" and attacked \
                or kind == "showcase" and rel not in notes:
            continue
        out.append({"path": rel, "pipeline": d["pipeline_name"],
                    "suite": d["suite_name"], "user_task": d["user_task_id"],
                    "injection_task": d.get("injection_task_id"), "attack": d.get("attack_type"),
                    "utility": d.get("utility"), "hijacked": hijacked if attacked else None, "claimed": claimed,
                    "note": notes.get(rel)})
    if kind == "showcase":
        order = list(notes)
        out.sort(key=lambda e: order.index(e["path"]))  # в порядке showcase.json: удалась, не удалась, интересное
    return out[:500]


def episode_detail(rel):
    path = (RUNS / rel).resolve()
    if RUNS.resolve() not in path.parents or path.suffix != ".json" or not path.exists():
        raise ValueError("Эпизод не найден.")
    d = json.loads(path.read_text(encoding="utf-8"))
    squash = lambda t: " ".join(t.split())
    injected = [squash(v)[:40] for v in (d.get("injections") or {}).values() if v and v.strip()]
    steps, query, after_filter = [], None, False
    for m in d["messages"]:
        body = text_of(m)
        if m["role"] == "assistant":
            # текст и вызовы приходят одним сообщением; текст модель пишет до вызова
            if body:
                say = {"kind": "say", "text": body}
                if after_filter:
                    say["filter_answer"] = True
                elif FAKE_CALL.search(body):
                    # есть настоящий вызов — текст его дублирует; нет — «вызов» остался текстом и не выполнялся
                    say["echo_call" if m.get("tool_calls") else "fake_call"] = True
                steps.append(say)
            for call in m.get("tool_calls") or []:
                steps.append({"kind": "call", "tool": call.get("function"), "args": call.get("args", {})})
            after_filter = False
        elif m["role"] == "tool":
            steps.append({"kind": "result", "text": body, "injected": any(s in squash(body) for s in injected),
                          "tool": (m.get("tool_call") or {}).get("function"), "error": m.get("error")})
        elif m["role"] == "user":
            # защиты добавляют свои сообщения с ролью user — отделяем их от слов пользователя
            if body.startswith(TOOL_FILTER_PREFIX):
                steps.append({"kind": "filter", "text": body})
                after_filter = True
            elif query is not None and body == query:
                steps.append({"kind": "repeat", "text": body})
            else:
                query = query if query is not None else body
                steps.append({"kind": "user", "text": body})
        else:
            steps.append({"kind": "system", "text": body})
    attacked = d.get("injection_task_id") is not None
    return {"pipeline": d["pipeline_name"], "suite": d["suite_name"], "user_task": d["user_task_id"],
            "injection_task": d.get("injection_task_id"), "attack": d.get("attack_type"),
            "utility": d.get("utility"), "hijacked": bool(d.get("security")) if attacked else None,
            "error": d.get("error"), "steps": steps}


def history():
    items = []
    for f in sorted(RESULTS.glob("summary-*.json"), reverse=True)[:30]:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        items.append({"name": f.stem, "rows": data.get("rows", [])})
    return items


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, value, mime="application/json; charset=utf-8"):
        body = json.dumps(value, ensure_ascii=False).encode() if mime.startswith("application/json") else value
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def allowed(self):
        hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if self.headers.get("Host") not in hosts:
            self.send(403, {"error": "Только localhost."})
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://" + h for h in hosts}:
            self.send(403, {"error": "Чужой origin."})
            return False
        return True

    def do_GET(self):
        if not self.allowed():
            return
        url = urlparse(self.path)
        params = parse_qs(url.query)
        try:
            if url.path == "/api/config":
                return self.send(200, config())
            if url.path.startswith("/api/jobs/"):
                job = jobs.get(url.path.rsplit("/", 1)[-1])
                if not job:
                    return self.send(404, {"error": "Нет такого запуска."})
                with job_lock:
                    view = {k: v for k, v in job.items() if k != "files"}
                view["elapsed"] = round((job.get("finished") or time.time()) - job["started"])
                view["episodes_done"] = sum(1 for f in job["files"] if f.exists())
                return self.send(200, view)
            if url.path == "/api/episodes":
                return self.send(200, episode_rows(params))
            if url.path == "/api/episode":
                return self.send(200, episode_detail((params.get("path") or [""])[0]))
            if url.path == "/api/history":
                return self.send(200, history())
        except ValueError as error:
            return self.send(400, {"error": str(error)})
        files = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                 "/style.css": ("style.css", "text/css; charset=utf-8")}
        if url.path not in files:
            return self.send(404, {"error": "Не найдено."})
        name, mime = files[url.path]
        self.send(200, (ROOT / "web" / name).read_bytes(), mime)

    def do_POST(self):
        if not self.allowed():
            return
        if self.headers.get("X-Bench") != "1":
            return self.send(403, {"error": "Ожидается запрос интерфейса."})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 20_000:
                raise ValueError("Недопустимый размер запроса.")
            data = json.loads(self.rfile.read(length))
            request = parse_request(data)
            plan = run_bench.make_plan(**request)
            path = urlparse(self.path).path
            if path == "/api/plan":
                return self.send(200, plan_summary(plan))
            if path == "/api/run":
                if not os.getenv("OPENROUTER_API_KEY", "").strip():
                    raise ValueError("Нет ключа: заполните OPENROUTER_API_KEY в .env и перезапустите сервер.")
                with job_lock:
                    if any(j["status"] == "running" for j in jobs.values()):
                        raise ValueError("Уже идёт запуск. Дождитесь его окончания.")
                    job_id = uuid.uuid4().hex[:10]
                    jobs[job_id] = {"id": job_id, "status": "running", "rows": [], "units_done": 0,
                                    "units": plan["units"], "episodes": plan["episodes"], "started": time.time(),
                                    "plan": plan_summary(plan)}
                    jobs[job_id]["files"] = expected_files(plan)
                    jobs[job_id]["episodes"] = len(jobs[job_id]["files"])
                threading.Thread(target=run_job, args=(job_id, plan, request), daemon=True).start()
                return self.send(201, {"id": job_id})
            return self.send(404, {"error": "Неизвестный маршрут."})
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            self.send(400, {"error": str(error)})


def main():
    run_bench.load_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args()
    RUNS.mkdir(exist_ok=True)
    RESULTS.mkdir(exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Стенд: http://127.0.0.1:{server.server_port} — Ctrl+C для остановки.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
