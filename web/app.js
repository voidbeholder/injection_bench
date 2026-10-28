// Интерфейс к server.py. Тексты из журналов (инъекции, ответы модели) выводятся только как
// textContent: никакого HTML, markdown и автозагрузки картинок — ровно то, о чём говорит лекция.
"use strict";

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
};
const pct = (v) => (v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`);

let cfg = null;
let rows = [];
let scope = {};
let kind = "showcase";
let pollTimer = null;

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST", body: JSON.stringify(body),
    headers: { "Content-Type": "application/json", "X-Bench": "1" },
  };
  const res = await fetch(path, opts);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function checkbox(container, value, label, checked, hint) {
  const lab = el("label");
  const box = el("input");
  box.type = "checkbox";
  box.value = value;
  box.checked = checked;
  box.addEventListener("change", updatePlan);
  lab.append(box, el("span", null, label));
  if (hint) lab.append(el("small", null, hint));
  container.append(lab);
}

const checked = (id) => [...$(id).querySelectorAll("input:checked")].map((b) => b.value);

function request() {
  const models = checked("models");
  const custom = $("custom-model").value.trim();
  if (custom) models.push(custom);
  return {
    models, suite: $("suite").value, attacks: checked("attacks"), defenses: checked("defenses"),
    user_tasks: Number($("user-tasks").value) || null, injection_tasks: Number($("injection-tasks").value) || null,
    seed: Number($("seed").value) || 0,
  };
}

async function updatePlan() {
  try {
    const p = await api("/api/plan", request());
    const s = p.suites[0];
    $("plan").textContent = `${p.episodes} эпизодов агента · задачи пользователя: ${s.user_tasks.join(", ")} · ` +
      `задачи атакующего: ${s.injection_tasks.join(", ")}`;
    showError("");
  } catch (e) {
    $("plan").textContent = "";
    showError(e.message);
  }
}

function showError(text) {
  $("error").hidden = !text;
  $("error").textContent = text;
}

async function run() {
  showError("");
  try {
    const { id } = await api("/api/run", request());
    $("run").disabled = true;
    $("progress").hidden = false;
    poll(id);
  } catch (e) {
    showError(e.message);
  }
}

async function poll(id) {
  clearTimeout(pollTimer);
  const job = await api(`/api/jobs/${id}`);
  const share = job.episodes ? job.episodes_done / job.episodes : 0;
  $("progress-fill").style.width = `${Math.round(share * 100)}%`;
  $("progress-text").textContent =
    `${job.status === "running" ? "идёт прогон" : job.status === "done" ? "готово" : "ошибка"} · ` +
    `эпизодов ${job.episodes_done} из ${job.episodes} · ${job.elapsed} с`;
  if (job.rows && job.rows.length) renderTable(job.rows);
  if (job.status === "running") {
    pollTimer = setTimeout(() => poll(id), 2000);
    return;
  }
  $("run").disabled = false;
  if (job.status === "error") showError(job.error);
  loadHistory();
}

function metric(value, cls) {
  const box = el("div", "metric");
  const track = el("div", "track");
  const fill = el("div", `fill ${cls}`);
  fill.style.width = value === null || value === undefined ? "0" : `${Math.round(value * 100)}%`;
  track.append(fill);
  box.append(track, el("b", null, pct(value)));
  return box;
}

function renderTable(data) {
  rows = data;
  const body = $("table").querySelector("tbody");
  body.replaceChildren();
  $("empty").hidden = data.length > 0;
  for (const r of data) {
    const tr = el("tr");
    tr.append(el("td", "model", r.model), el("td", null, r.defense), el("td", null, r.attack));
    for (const [key, cls] of [["utility", "util"], ["utility_under_attack", "uatt"], ["asr", "asr"], ["attacker_task_doable", "doable"]]) {
      const td = el("td");
      td.append(metric(r[key], cls));
      tr.append(td);
    }
    tr.addEventListener("click", () => {
      body.querySelectorAll("tr").forEach((x) => x.classList.remove("sel"));
      tr.classList.add("sel");
      scope = { pipeline: r.pipeline || pipelineName(r.model, r.defense), suite: r.suite, attack: r.attack };
      $("ep-scope").textContent = `${scope.pipeline} · ${r.suite} · ${r.attack}`;
      loadEpisodes();
    });
    body.append(tr);
  }
}

// Так же, как run_bench.safe_name + суффикс защиты в AgentDojo.
function pipelineName(model, defense) {
  const base = model.replaceAll("/", "__").replaceAll(":", "_");
  return defense && defense !== "none" ? `${base}-${defense}` : base;
}

// Все запуски вместе: по строке на каждую комбинацию модель × защита × среда × атака,
// из самого свежего запуска (история приходит от новых к старым).
function mergeRuns(items) {
  const seen = new Map();
  for (const it of items) {
    for (const r of it.rows) {
      const key = [r.model, r.defense, r.suite, r.attack].join("|");
      if (!seen.has(key)) seen.set(key, r);
    }
  }
  return [...seen.values()].sort((a, b) =>
    (a.model_label || a.model).localeCompare(b.model_label || b.model) ||
    a.defense.localeCompare(b.defense) || a.attack.localeCompare(b.attack));
}

async function loadHistory() {
  const items = await api("/api/history");
  const sel = $("history");
  const current = sel.value || "all";
  sel.replaceChildren();
  const all = el("option", null, `все запуски вместе · ${items.length}`);
  all.value = "all";
  sel.append(all);
  items.forEach((it, i) => {
    const opt = el("option", null, `${it.name.replace("summary-", "")} · ${it.rows.length} строк`);
    opt.value = String(i);
    sel.append(opt);
  });
  sel.onchange = () => {
    if (sel.value === "all") return renderTable(mergeRuns(items));
    const it = items[Number(sel.value)];
    if (it) renderTable(it.rows);
  };
  sel.value = [...sel.options].some((o) => o.value === current) ? current : "all";
  if (items.length) sel.onchange();
}

async function loadEpisodes() {
  const params = new URLSearchParams({ ...scope, kind });
  if (scope.attack === "none" && kind !== "showcase") params.set("kind", "benign");
  const list = await api(`/api/episodes?${params}`);
  const ul = $("ep-list");
  ul.replaceChildren();
  $("ep-detail").replaceChildren(el("p", "hint", "Выберите эпизод слева."));
  if (!list.length) {
    ul.append(el("li", "hint", kind === "showcase"
      ? "Для этой строки эпизоды для показа не отобраны — откройте вкладку «все»."
      : "Нет эпизодов с таким фильтром."));
  }
  for (const ep of list) {
    const li = el("li");
    const head = el("div");
    if (ep.injection_task) {
      head.append(el("span", `tag ${ep.hijacked ? "bad" : "good"}`, ep.hijacked ? "атака удалась" : "атака не удалась"));
    } else {
      head.append(el("span", "tag neutral", "без атаки"));
    }
    head.append(el("span", `tag ${ep.utility ? "good" : "neutral"}`, ep.utility ? "задача решена" : "задача не решена"));
    if (ep.claimed) head.append(el("span", "tag bad", "«готово» только на словах"));
    li.append(head, el("div", "meta", `${ep.user_task}${ep.injection_task ? " × " + ep.injection_task : ""}`));
    if (ep.note) li.append(el("div", "note", ep.note));
    li.addEventListener("click", () => {
      ul.querySelectorAll("li").forEach((x) => x.classList.remove("sel"));
      li.classList.add("sel");
      showEpisode(ep.path, ep.note);
    });
    ul.append(li);
  }
}

function step(cls, who, content) {
  const box = el("div", `step ${cls}`);
  box.append(el("div", "who", who));
  box.append(content);
  return box;
}

function long(text) {
  if (text.length < 600) return el("pre", null, text);
  const det = el("details");
  det.append(el("summary", null, `показать полностью (${text.length} символов)`), el("pre", null, text));
  const wrap = el("div");
  wrap.append(el("pre", null, text.slice(0, 400) + "…"), det);
  return wrap;
}

async function showEpisode(path, note) {
  const d = await api(`/api/episode?path=${encodeURIComponent(path)}`);
  const box = $("ep-detail");
  box.replaceChildren();
  box.append(el("div", "meta", `${d.pipeline} · ${d.suite} · ${d.user_task}${d.injection_task ? " × " + d.injection_task : ""}` +
    (d.attack ? ` · ${d.attack}` : "")));
  if (note) box.append(el("p", "note", note));
  const verdict = el("div", "verdict");
  verdict.append(el("span", `tag ${d.utility ? "good" : "neutral"}`, `задача пользователя: ${d.utility ? "решена" : "не решена"}`));
  if (d.hijacked !== null) {
    verdict.append(el("span", `tag ${d.hijacked ? "bad" : "good"}`, `цель атакующего: ${d.hijacked ? "достигнута" : "не достигнута"}`));
  }
  verdict.append(el("span", "tag neutral", "проверено по состоянию среды"));
  box.append(verdict);
  if (d.error) box.append(el("p", "error", `Ошибка в эпизоде: ${d.error}`));
  for (const s of d.steps) {
    if (s.kind === "system") {
      const det = el("details");
      det.append(el("summary", null, "системный промпт"), el("pre", null, s.text));
      box.append(step("system", "система", det));
    } else if (s.kind === "user") {
      box.append(step("user", "пользователь", el("pre", null, s.text)));
    } else if (s.kind === "repeat") {
      const c = el("div");
      c.append(el("div", "hint", "Защита повторяет исходный запрос после каждого ответа инструмента. Это не новое сообщение пользователя."),
        el("pre", null, s.text));
      box.append(step("service", "повтор запроса · защита repeat_user_prompt", c));
    } else if (s.kind === "filter") {
      const c = el("div");
      c.append(el("div", "hint", "Перед работой защита просит модель назвать нужные инструменты. Остальные будут убраны."),
        el("pre", null, s.text));
      box.append(step("service", "вопрос фильтра · защита tool_filter", c));
    } else if (s.kind === "call") {
      const c = el("div");
      c.append(el("span", "tool", s.tool), el("pre", null, JSON.stringify(s.args, null, 1)));
      box.append(step("call", "агент вызывает инструмент", c));
    } else if (s.kind === "result") {
      const c = el("div");
      if (s.injected) c.append(el("div", "flag", "в этих данных — инъекция"));
      if (s.error) {
        c.append(el("div", "flag", "инструмент вернул ошибку"), el("pre", null, s.error));
      } else if (!(s.text || "").trim()) {
        c.append(el("div", "hint", s.tool === "read_file"
          ? "Пустой ответ: файла по такому пути нет."
          : "Инструмент вернул пустой ответ."));
      } else {
        c.append(long(s.text));
      }
      box.append(step(`result${s.injected ? " injected" : ""}`, s.tool ? `результат ${s.tool}` : "результат инструмента", c));
    } else if (s.filter_answer) {
      const c = el("div");
      c.append(el("div", "hint", "Ответ модели фильтру: остаются только инструменты, чьи точные названия есть в этом тексте."),
        el("pre", null, s.text));
      box.append(step("service", "модель отвечает фильтру · защита tool_filter", c));
    } else if (s.echo_call) {
      const det = el("details");
      det.append(el("summary", null, "модель продублировала вызов текстом (сам вызов — следующий шаг)"), el("pre", null, s.text));
      box.append(step("say", "агент пишет", det));
    } else if (s.fake_call) {
      const c = el("div");
      c.append(el("div", "flag", "вызов написан текстом и не выполнялся"), el("pre", null, s.text));
      box.append(step("say", "агент пишет пользователю", c));
    } else {
      box.append(step("say", "агент пишет пользователю", el("pre", null, s.text)));
    }
  }
}

async function init() {
  cfg = await api("/api/config");
  const key = $("key");
  key.textContent = cfg.key_configured ? `ключ задан · ${cfg.base_url.replace("https://", "")}` : "нет ключа OpenRouter в .env";
  key.className = `badge ${cfg.key_configured ? "ok" : "no"}`;
  for (const [name, id] of Object.entries(cfg.presets)) {
    checkbox($("models"), name, name, name === "weak" || name === "strong", id);
  }
  for (const [name, s] of Object.entries(cfg.suites)) {
    const opt = el("option", null, `${name} · ${s.user_tasks} задач пользователя, ${s.injection_tasks} атакующего`);
    opt.value = name;
    $("suite").append(opt);
  }
  for (const a of cfg.attacks) checkbox($("attacks"), a, a, cfg.default_attacks.includes(a));
  for (const d of cfg.defenses) checkbox($("defenses"), d, d === "none" ? "без защиты" : d, d === "none");
  ["suite", "user-tasks", "injection-tasks", "seed", "custom-model"].forEach((id) => $(id).addEventListener("change", updatePlan));
  $("run").addEventListener("click", run);
  document.querySelectorAll("#tabs button").forEach((b) => {
    if (b.dataset.kind === kind) b.classList.add("on");
    b.addEventListener("click", () => {
      document.querySelectorAll("#tabs button").forEach((x) => x.classList.remove("on"));
      b.classList.add("on");
      kind = b.dataset.kind;
      if (scope.pipeline) loadEpisodes();
    });
  });
  updatePlan();
  await loadHistory();
}

init().catch((e) => showError(e.message));
