/* Проверки логики интерфейса.
 *
 * Появились после настоящего бага: поле ввода и кнопка оставались выключенными
 * после первой же реплики, потому что признак «диалог закончен» спрашивался
 * у самого элемента — а тот выключен и во время запроса тоже. Ни один тест
 * ядра или API этого поймать не мог: там всё было зелёным.
 *
 * Браузер здесь не запускается. Вместо него минимальный DOM — ровно тот
 * набор свойств, которым пользуется app.js. Это не замена живой проверке
 * глазами, но регресс такого рода ловит.
 *
 *     node tests/run_ui_tests.js
 */

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.resolve(__dirname, "..");
const GREEN = "[32m", RED = "[31m", BOLD = "[1m", RESET = "[0m";

// ------------------------------------------------------------------ //
// Мини-DOM
// ------------------------------------------------------------------ //

class Node {
  constructor(tag = "div") {
    this.tag = tag;
    this.children = [];
    this.className = "";
    this.textContent = "";
    this.style = {};
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.listeners = {};
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  focus() { this.focused = true; }
  addEventListener(type, handler) { this.listeners[type] = handler; }
  fire(type, event = {}) { return this.listeners[type]?.({ preventDefault() {}, ...event }); }
  get text() {
    return [this.textContent, ...this.children.map((child) => child.text || "")].join(" ");
  }
}

function makeDocument(ids) {
  const nodes = Object.fromEntries(ids.map((id) => [id, new Node()]));
  return {
    nodes,
    title: "",
    getElementById: (id) => nodes[id] || (nodes[id] = new Node()),
    createElement: (tag) => new Node(tag),
  };
}

const IDS = [
  "chat", "profile", "stage-chip", "temp-chip", "progress-bar", "progress-text",
  "handoff", "send", "input", "composer", "show-handoff", "restart",
  "client-name", "client-business", "demo-banner",
];

// ------------------------------------------------------------------ //
// Заготовленные ответы API
// ------------------------------------------------------------------ //

const STATE = (finished = false) => ({
  stage: finished ? "done" : "asking",
  temperature: finished ? "горячий" : "холодный",
  score: 3,
  offer: "course_ege",
  profile: [
    { key: "grade", label: "Класс", value: "10", masked: false, source: "сказано", note: "", required: true, computed: false },
    { key: "level", label: "Уровень", value: "", masked: false, source: "", note: "", required: false, computed: false },
    { key: "months_left", label: "До экзамена", value: "21", masked: false, source: "выведено", note: "посчитано из класса", required: false, computed: true },
    { key: "contact", label: "Телефон", value: "+7 900 ***-**-67", masked: true, source: "сказано", note: "", required: true, computed: false },
  ],
  progress: { completed_fields: 3, total_fields: 4, percent: 75 },
  flags: ["срок до экзамена посчитан из класса"],
  finished,
});

function makeFetch(plan) {
  const calls = [];
  return {
    calls,
    fetch: async (url, options) => {
      calls.push(url);
      const answer = plan(url, options, calls);
      if (answer.status && answer.status >= 400) {
        return { ok: false, status: answer.status, json: async () => ({}) };
      }
      return { ok: true, status: 200, json: async () => answer.body };
    },
  };
}

async function boot(plan) {
  const document = makeDocument(IDS);
  const { fetch, calls } = makeFetch(plan);
  const sandbox = { document, fetch, console, setTimeout, window: {} };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(ROOT, "web", "static", "app.js"), "utf8"), sandbox);
  await settle();
  return { document, calls };
}

// Мини-DOM синхронный, но app.js асинхронный: даём микрозадачам догореть.
const settle = () => new Promise((resolve) => setTimeout(resolve, 5));

async function type(document, text) {
  document.getElementById("input").value = text;
  document.getElementById("composer").fire("submit");
  await settle();
}

const DEFAULT_PLAN = (finished) => (url) => {
  if (url === "/api/config") return { body: { client: "Парабола", business: "центр", demo_notice: "Демонстрация" } };
  if (url === "/api/session") return { body: { session_id: "s1", reply: "Здравствуйте!", state: STATE() } };
  if (url === "/api/message") return { body: { reply: "Какие предметы?", source: "rules", handoff: false, state: STATE(finished) } };
  if (url === "/api/handoff") return { body: { client: "Парабола", manager_contact: "@x", stage: "asking", temperature: "тёплый", score: 3, offer: "course_ege", fields: STATE().profile.filter((f) => f.value), derived_fields: ["months_left"], flags: ["срок до экзамена посчитан из класса"], questions_asked: [], reject_reason: "", note: "" } };
  return { body: {} };
};

// ------------------------------------------------------------------ //
// Проверки
// ------------------------------------------------------------------ //

const checks = [];
const check = (title, fn) => checks.push([title, fn]);

check("Страница стартует с приветствия и готова к вводу", async () => {
  const { document } = await boot(DEFAULT_PLAN(false));
  const problems = [];
  if (!document.getElementById("chat").text.includes("Здравствуйте")) problems.push("приветствие не показано");
  if (document.getElementById("input").disabled) problems.push("поле ввода выключено на старте");
  if (document.getElementById("demo-banner").textContent !== "Демонстрация") problems.push("плашка демонстрации не заполнена");
  return problems;
});

check("После ответа бота поле ввода снова активно", async () => {
  // Ровно тот баг, ради которого этот файл и появился.
  const { document } = await boot(DEFAULT_PLAN(false));
  await type(document, "10 класс");
  const problems = [];
  if (document.getElementById("input").disabled) problems.push("поле ввода осталось выключенным");
  if (document.getElementById("send").disabled) problems.push("кнопка «Отправить» осталась выключенной");
  if (!document.getElementById("chat").text.includes("Какие предметы")) problems.push("ответ бота не отрисован");
  return problems;
});

check("Диалог можно продолжать несколько реплик подряд", async () => {
  const { document, calls } = await boot(DEFAULT_PLAN(false));
  await type(document, "10 класс");
  await type(document, "математика");
  await type(document, "средне");
  const sent = calls.filter((url) => url === "/api/message").length;
  return sent === 3 ? [] : [`до сервера дошло ${sent} реплики из 3`];
});

check("Завершённый диалог блокирует ввод", async () => {
  const { document } = await boot(DEFAULT_PLAN(true));
  await type(document, "да");
  const problems = [];
  if (!document.getElementById("input").disabled) problems.push("после завершения ввод остался открытым");
  if (!document.getElementById("chat").text.includes("завершён")) problems.push("нет пометки о завершении");
  return problems;
});

check("Профайл показывает источник значения", async () => {
  const { document } = await boot(DEFAULT_PLAN(false));
  await type(document, "10 класс");
  const text = document.getElementById("profile").text;
  const problems = [];
  if (!text.includes("сказал")) problems.push("нет метки «сказал»");
  if (!text.includes("посчитано")) problems.push("нет метки «посчитано»");
  if (!text.includes("желательное")) problems.push("желательное поле не помечено");
  if (!text.includes("скрыто в демо")) problems.push("замаскированное поле не помечено");
  return problems;
});

check("Карточка администратора показывает флаги", async () => {
  const { document } = await boot(DEFAULT_PLAN(false));
  document.getElementById("show-handoff").fire("click");
  await settle();
  const box = document.getElementById("handoff");
  const problems = [];
  if (box.hidden) problems.push("карточка не раскрылась");
  if (!box.text.includes("посчитан из класса")) problems.push("флаг о вычисленном поле не доехал до карточки");
  return problems;
});

check("Истёкшая сессия начинает диалог заново", async () => {
  let expired = true;
  const { document, calls } = await boot((url, options, all) => {
    if (url === "/api/config") return { body: { client: "Парабола", business: "", demo_notice: "" } };
    if (url === "/api/session") return { body: { session_id: "s2", reply: "Здравствуйте!", state: STATE() } };
    if (url === "/api/message" && expired) { expired = false; return { status: 404 }; }
    return { body: { reply: "ок", source: "rules", handoff: false, state: STATE() } };
  });
  await type(document, "привет");
  const problems = [];
  if (calls.filter((url) => url === "/api/session").length !== 2) problems.push("диалог не перезапустился");
  if (document.getElementById("input").disabled) problems.push("после перезапуска ввод выключен");
  return problems;
});

check("Кнопка «Начать заново» просит сервер забыть диалог", async () => {
  const { document, calls } = await boot(DEFAULT_PLAN(false));
  document.getElementById("restart").fire("click");
  await settle();
  return calls.includes("/api/reset") ? [] : ["сброс сессии на сервере не запрошен"];
});

// ------------------------------------------------------------------ //

(async function main() {
  console.log(`\n${BOLD}Проверки интерфейса — ${checks.length} штук${RESET}\n`);
  let passed = 0;
  for (const [title, fn] of checks) {
    let problems;
    try {
      problems = await fn();
    } catch (error) {
      problems = [`упало с ошибкой: ${error.message}`];
    }
    console.log(` ${problems.length ? RED + "✗" + RESET : GREEN + "✓" + RESET}  ${title}`);
    problems.forEach((problem) => console.log(`      ${RED}→ ${problem}${RESET}`));
    passed += problems.length === 0;
  }
  const failed = checks.length - passed;
  console.log(`\n${failed ? RED : GREEN}${passed} из ${checks.length} проверок прошли${RESET}\n`);
  process.exit(failed ? 1 : 0);
})();
