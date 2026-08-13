/* Витрина к агенту. Никакой логики диалога здесь нет и быть не должно:
   всё, что решает, что происходит дальше, живёт в ядре и покрыто тестами.
   Этот файл только показывает состояние, которое отдал API. */

const STAGES = {
  greeting: "знакомимся",
  asking: "выявляем потребности",
  proposing: "подбираем программу",
  confirming: "проверяем собранное",
  registration: "ждём подтверждения записи",
  done: "передан администратору",
  rejected: "отсеян правилом",
  escalated: "передан человеку",
};

const TEMP_CLASS = { "горячий": "chip--hot", "тёплый": "chip--warm", "холодный": "chip--cold" };

const el = (id) => document.getElementById(id);
const chat = el("chat");
let sessionId = null;
let busy = false;

async function api(path, payload) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
  if (!response.ok) {
    const error = new Error("api");
    error.status = response.status;
    throw error;
  }
  return response.json();
}

function addMessage(text, kind) {
  const node = document.createElement("div");
  node.className = `msg msg--${kind}`;
  node.textContent = text;
  chat.append(node);
  chat.scrollTop = chat.scrollHeight;
}

/* ------------------------------------------------------------------ */
/* Профайл                                                             */
/* ------------------------------------------------------------------ */

function renderProfile(state) {
  const list = el("profile");
  list.replaceChildren();

  state.profile.forEach((field) => {
    const row = document.createElement("div");
    row.className = "field" + (field.value ? "" : " field--empty");

    const label = document.createElement("dt");
    label.className = "field__label";
    label.textContent = field.label;

    const value = document.createElement("dd");
    value.className = "field__value" + (field.value ? "" : " field__value--empty");
    value.textContent = field.value || "—";

    const badges = document.createElement("div");
    badges.className = "field__badges";

    // Метка источника — главное, что должно быть видно в этой колонке.
    if (field.value) {
      const source = document.createElement("span");
      const derived = field.source === "выведено";
      source.className = `badge badge--${derived ? "derived" : "said"}`;
      source.textContent = derived ? "посчитано" : "сказал";
      badges.append(source);
    } else if (!field.required) {
      const optional = document.createElement("span");
      optional.className = "badge badge--optional";
      optional.textContent = "желательное";
      badges.append(optional);
    }

    if (field.masked) {
      const masked = document.createElement("span");
      masked.className = "badge badge--masked";
      masked.textContent = "скрыто в демо";
      badges.append(masked);
    }

    row.append(label, value, badges);

    if (field.note) {
      const note = document.createElement("p");
      note.className = "field__note";
      note.textContent = field.note;
      row.append(note);
    }

    list.append(row);
  });

  el("stage-chip").textContent = STAGES[state.stage] || state.stage;

  const temp = el("temp-chip");
  temp.textContent = state.temperature;
  temp.className = "chip chip--temp " + (TEMP_CLASS[state.temperature] || "");

  const done = state.progress.completed_fields;
  const total = state.progress.total_fields;
  el("progress-bar").style.width = total ? `${Math.round((done / total) * 100)}%` : "0%";
  el("progress-text").textContent = `Заполнено полей: ${done} из ${total}`;
}

/* ------------------------------------------------------------------ */
/* Карточка администратора                                             */
/* ------------------------------------------------------------------ */

function renderHandoff(card) {
  const box = el("handoff");
  box.replaceChildren();
  box.hidden = false;

  const title = document.createElement("h3");
  title.textContent = "Уйдёт администратору";
  box.append(title);

  const list = document.createElement("dl");
  const rows = [
    ["Статус", STAGES[card.stage] || card.stage],
    ["Температура", `${card.temperature} (балл ${card.score})`],
    ["Подобрано", card.offer || "—"],
  ];
  card.fields.forEach((field) => {
    rows.push([field.label, field.value + (field.source === "выведено" ? " · посчитано" : "")]);
  });
  rows.forEach(([term, description]) => {
    const dt = document.createElement("dt");
    dt.textContent = term;
    const dd = document.createElement("dd");
    dd.textContent = description;
    list.append(dt, dd);
  });
  box.append(list);

  // Флаги едут в карточку всегда: догадка, о которой знает только профайл,
  // менеджеру не помогает.
  const flagsTitle = document.createElement("h3");
  flagsTitle.textContent = card.flags.length ? "Пометки для администратора" : "Пометок нет";
  box.append(flagsTitle);

  if (card.flags.length) {
    const flags = document.createElement("ul");
    card.flags.forEach((flag) => {
      const item = document.createElement("li");
      item.className = "flag";
      item.textContent = flag;
      flags.append(item);
    });
    box.append(flags);
  }
}

/* ------------------------------------------------------------------ */
/* Управление                                                          */
/* ------------------------------------------------------------------ */

function setBusy(value) {
  busy = value;
  el("send").disabled = value;
  el("input").disabled = value;
  if (!value) el("input").focus();
}

async function start() {
  chat.replaceChildren();
  el("handoff").hidden = true;
  setBusy(true);
  try {
    const body = await api("/api/session");
    sessionId = body.session_id;
    addMessage(body.reply, "bot");
    renderProfile(body.state);
  } catch {
    addMessage("Не удалось начать диалог. Обновите страницу.", "system");
  } finally {
    setBusy(false);
  }
}

async function send(text) {
  addMessage(text, "me");
  setBusy(true);
  try {
    const body = await api("/api/message", { session_id: sessionId, text });
    addMessage(body.reply, "bot");
    renderProfile(body.state);
    if (!el("handoff").hidden) await showHandoff();
    if (body.state.finished) {
      addMessage("Диалог завершён. Можно начать заново.", "system");
      el("input").disabled = true;
      el("send").disabled = true;
    }
  } catch (error) {
    if (error.status === 404) {
      addMessage("Сессия истекла — начинаю заново.", "system");
      await start();
    } else if (error.status === 429) {
      addMessage("Слишком длинный диалог для демонстрации. Начните заново.", "system");
    } else {
      addMessage("Что-то пошло не так. Попробуйте ещё раз.", "system");
    }
  } finally {
    if (!el("input").disabled) setBusy(false);
    else busy = false;
  }
}

async function showHandoff() {
  if (!sessionId) return;
  try {
    renderHandoff(await api("/api/handoff", { session_id: sessionId }));
  } catch {
    addMessage("Карточка недоступна — сессия истекла.", "system");
  }
}

el("composer").addEventListener("submit", (event) => {
  event.preventDefault();
  const input = el("input");
  const text = input.value.trim();
  if (!text || busy) return;
  input.value = "";
  send(text);
});

el("show-handoff").addEventListener("click", showHandoff);

el("restart").addEventListener("click", async () => {
  if (sessionId) {
    // Просим сервер забыть диалог сразу, не дожидаясь таймаута.
    try { await api("/api/reset", { session_id: sessionId }); } catch { /* уже забыт */ }
  }
  await start();
});

(async function boot() {
  try {
    const config = await (await fetch("/api/config")).json();
    document.getElementById("client-name").textContent = config.client;
    document.getElementById("client-business").textContent = config.business;
    document.getElementById("demo-banner").textContent = config.demo_notice;
    document.title = `${config.client} — демонстрация AI-квалификатора`;
  } catch { /* заголовки останутся стандартными */ }
  await start();
})();
