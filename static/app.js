/* Newbiegrader SPA — hash-routed views (home / tasks / task / history), no build step. */
"use strict";

const $ = (id) => document.getElementById(id);

/* Split-deploy support: static host + separate backend.
 * Set window.GRADER_API_BASE in config.js ("" = same origin). */
const API = (window.GRADER_API_BASE || "").replace(/\/$/, "");

const state = {
  problems: [],
  stats: {},
  statsLoaded: false,
  homeSummary: null,
  authReturnTo: "#/tasks",
  homeLevel: "easy",
  homeSelected: null,
  homeOffset: 0,
  myBest: {},
  myBestLoaded: false,
  detailCache: {},
  samples: {},           // "<pid>/<sample name>" -> {input, output} (fetched on click)
  contests: [],
  contestsConfigured: null,
  contestCache: {},
  contest: null,         // {id,title} while the task page submits into a contest
  current: null,
  jobResults: {},        // pid -> {result, secs} last verdict rendered this session
  lang: "python",
  taskTab: "statement",
  file: null,
  user: null,
  authReady: false,      // initial /me + remember-me restoration have completed
  authError: false,
  is_admin: false,
  isAdmin: false,        // alias of is_admin (same value)
  filter: { q: "", diff: "", tag: "", status: "all", sort: "no" },
};

const _sampleInflight = new Map();   // "<pid>/<name>" -> Promise

/* ---------------- theme ---------------- */
function icon(n, cls) {
  return `<svg class="ic${cls ? " " + cls : ""}" aria-hidden="true"><use href="#i-${n}"/></svg>`;
}

function scoreCls(s) {
  s = Number(s) || 0;
  return s >= 100 ? "s100" : s > 0 ? "smid" : "s0";
}

/* Toasts: transient bottom-right notes for completed actions. Click or
 * timeout dismisses; at most a few stack up. */
function toast(msg, kind) {
  const stack = $("toast-stack");
  if (!stack) return;
  const el = document.createElement("div");
  el.className = "toast" + (kind ? " " + kind : "");
  el.textContent = msg;
  el.onclick = () => el.remove();
  stack.appendChild(el);
  while (stack.children.length > 4) stack.firstChild.remove();
  setTimeout(() => el.remove(), 3500);
}

/* Skeleton shimmer shown while a list loads instead of bare "Loading…". */
function skel(n) {
  const w = [92, 78, 85, 64, 88, 71];
  let html = `<div class="skel" aria-hidden="true">`;
  for (let i = 0; i < (n || 4); i++) html += `<div class="skel-line" style="width:${w[i % w.length]}%"></div>`;
  return html + `</div>`;
}

/* Command palette (Ctrl/⌘K): fuzzy-jump to any problem by number, id or
 * title. Fully keyboard driven; Esc closes. */
let paletteIdx = 0;

function paletteMatches(q) {
  const qq = (q || "").trim().toLowerCase();
  const list = state.problems || [];
  if (!qq) return list.slice(0, 8);
  return list.filter((p) =>
    String(p.no || "") === qq || `#${p.no || ""}` === qq ||
    (p.id || "").toLowerCase().includes(qq) ||
    (p.title || "").toLowerCase().includes(qq)).slice(0, 8);
}

function renderPalette() {
  const q = $("palette-input").value;
  const items = paletteMatches(q);
  if (paletteIdx >= items.length) paletteIdx = 0;
  $("palette-list").innerHTML = items.length ? items.map((p, i) =>
    `<button class="palette-item${i === paletteIdx ? " active" : ""}" role="option" data-pid="${esc(p.id)}">` +
    `${noLabel(p)}<span>${esc(p.title)}</span> <span class="dim">${esc(p.id)}</span></button>`
  ).join("") : `<div class="dim" style="padding:12px 14px">ไม่เจอโจทย์ที่ตรง</div>`;
  $("palette-list").querySelectorAll("[data-pid]").forEach((b) => {
    b.onclick = () => { const pid = b.dataset.pid; closePalette(); openTask(pid); };
  });
}

function openPalette() {
  if (!state.user) { _nav("#/tasks", "push"); return; }
  $("palette-overlay").classList.remove("hidden");
  $("palette-input").value = "";
  paletteIdx = 0;
  const ensure = (state.problems && state.problems.length)
    ? Promise.resolve()
    : apiGet("/api/problems").then((d) => { state.problems = problemRowsForIdentity(d.problems); }).catch(() => {});
  ensure.then(() => { renderPalette(); $("palette-input").focus(); });
  $("palette-input").oninput = () => { paletteIdx = 0; renderPalette(); };
  $("palette-overlay").onclick = (e) => {
    if (e.target.id === "palette-overlay") closePalette();
  };
}

function closePalette() {
  $("palette-overlay").classList.add("hidden");
}

document.addEventListener("keydown", (e) => {
  const open = !$("palette-overlay").classList.contains("hidden");
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    if (open) closePalette();
    else openPalette();
    return;
  }
  if (!open) return;
  if (e.key === "Escape") { closePalette(); return; }
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    const n = paletteMatches($("palette-input").value).length;
    if (!n) return;
    paletteIdx = e.key === "ArrowDown" ? (paletteIdx + 1) % n : (paletteIdx + n - 1) % n;
    renderPalette();
    return;
  }
  if (e.key === "Enter") {
    const items = paletteMatches($("palette-input").value);
    const pick = items[Math.min(paletteIdx, items.length - 1)];
    if (pick) { const pid = pick.id; closePalette(); openTask(pid); }
  }
});

function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("ng_theme", t); } catch (e) { /* ignore */ }
  const b = $("theme-btn");
  if (b) b.innerHTML = icon(t === "dark" ? "sun" : "moon");
  document.querySelectorAll("[data-setting-theme]").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.settingTheme === t));
  });
}
try { applyTheme(localStorage.getItem("ng_theme") || "light"); } catch (e) { applyTheme("light"); }
$("theme-btn").onclick = () =>
  applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
document.querySelectorAll("[data-setting-theme]").forEach((button) => {
  button.onclick = () => applyTheme(button.dataset.settingTheme);
});

/* ---------------- drafts, last language, last job (localStorage) ----------------
 * Judging is async, so wandering off mid-run is normal — a student who clicks
 * away must not lose typed code or the verdict. One draft per problem, the
 * language choice remembered separately, and the last job per problem kept so
 * the Submit tab can pick a run back up (the server owns the job; we keep the id).
 */
const DRAFT_KEY = "ng_draft_v1";
const JOB_KEY = "ng_job_v1";
const LANG_KEY = "ng_lang";
const JOB_RESUME_MS = 30 * 60 * 1000;   // older than this: read it in History
const DRAFT_MAX = 40;                   // newest N problems
const JOB_MAX = 20;

function lsJson(key) {
  try { return JSON.parse(localStorage.getItem(key)) || {}; } catch (e) { return {}; }
}
function lsPut(key, obj) {
  try { localStorage.setItem(key, JSON.stringify(obj)); } catch (e) { /* quota: fine */ }
}
function keepNewest(map, n) {
  Object.keys(map)
    .sort((a, b) => (map[b].t || 0) - (map[a].t || 0))
    .slice(n)
    .forEach((k) => delete map[k]);
  return map;
}
function setDraftHint(saved) {
  const el = $("draft-hint");
  if (el) el.textContent = saved ? "บันทึกร่างอัตโนมัติแล้ว" : "";
}

function saveDraft() {
  const ed = $("editor");
  if (!ed || !state.current) return;
  const d = lsJson(DRAFT_KEY);
  if (ed.value.trim()) d[state.current] = { code: ed.value, t: Date.now() };
  else delete d[state.current];
  lsPut(DRAFT_KEY, keepNewest(d, DRAFT_MAX));
  setDraftHint(!!ed.value.trim());
}
function draftFor(pid) {
  return (lsJson(DRAFT_KEY)[pid] || {}).code || "";
}
function rememberJob(pid, jid) {
  const m = lsJson(JOB_KEY);
  m[pid] = { id: jid, t: Date.now() };
  lsPut(JOB_KEY, keepNewest(m, JOB_MAX));
}
function forgetJob(pid) {
  const m = lsJson(JOB_KEY);
  if (m[pid]) { delete m[pid]; lsPut(JOB_KEY, m); }
}
function lastJobFor(pid) {
  const e = lsJson(JOB_KEY)[pid];
  return e && Date.now() - (e.t || 0) < JOB_RESUME_MS ? e : null;
}
function clearLocalWorkspace() {
  try { [DRAFT_KEY, JOB_KEY].forEach((k) => localStorage.removeItem(k)); } catch (e) { /* ignore */ }
}
function lastLang() {
  try { return localStorage.getItem(LANG_KEY) || ""; } catch (e) { return ""; }
}

/* ---------------- editor keys (plain textarea, no dependency) ----------------
 * Tab has to indent instead of moving focus: losing the caret mid-line is the
 * fastest way to make a beginner give up. Shift+Tab outdents, Enter keeps the
 * current indentation (and opens a block after `:` or `{`), Cmd/Ctrl+Enter
 * submits, Cmd/Ctrl+/ comments the selection out.
 *
 * The line numbers live in a gutter beside the textarea: a textarea cannot
 * render one element per line. The gutter is positioned rather than laid out,
 * so it always covers exactly the textarea's box, and it scrolls with it.
 * The editor does not soft-wrap: a wrapped line occupies two visual rows and
 * the numbers would drift out of step. Code scrolls sideways instead.
 */
const INDENT = "    ";

function syncEditorGutter() {
  const ed = $("editor"), g = $("editor-gutter");
  if (!ed || !g || typeof ed.value !== "string" || !g.dataset) return;
  const lines = (ed.value.match(/\n/g) || []).length + 1;
  if (g.dataset.lines !== String(lines)) {
    g.dataset.lines = String(lines);
    g.textContent = Array.from({ length: lines }, (_, i) => i + 1).join("\n");
  }
  g.scrollTop = ed.scrollTop;
}

/* Every write to the editor goes through here so the gutter never drifts:
 * a programmatic value change does not fire `input`. */
function setEditorValue(value, ed) {
  const target = ed || $("editor");
  if (!target) return;
  target.value = value;
  syncEditorGutter();
}

function commentToken() {
  return state.lang === "python" ? "#" : "//";
}

function lineRange(value, start, end) {
  const from = value.lastIndexOf("\n", start - 1) + 1;
  const nl = value.indexOf("\n", end);
  return [from, nl === -1 ? value.length : nl];
}

function applyIndent(ed, outdent) {
  const v = ed.value, s = ed.selectionStart, e = ed.selectionEnd;
  if (s === e && !outdent) {                    // Tab with no selection: 4 spaces
    setEditorValue(v.slice(0, s) + INDENT + v.slice(s), ed);
    ed.selectionStart = ed.selectionEnd = s + INDENT.length;
    return;
  }
  const [from, to] = lineRange(v, s, e);
  const block = v.slice(from, to).split("\n").map((line) => {
    if (outdent) {
      return line.startsWith(INDENT) ? line.slice(INDENT.length)
        : line.replace(/^[ \t]{1,4}/, "");
    }
    return INDENT + line;
  }).join("\n");
  setEditorValue(v.slice(0, from) + block + v.slice(to), ed);
  if (s === e) {
    // Caret only: keep the caret where the student left it (no selection — a
    // selected block would be replaced by the very next keystroke).
    const delta = block.length - (to - from);
    const caret = Math.max(from, Math.min(from + block.length, s + delta));
    ed.selectionStart = ed.selectionEnd = caret;
  } else {
    ed.selectionStart = from;
    ed.selectionEnd = from + block.length;
  }
}

function toggleComment(ed) {
  const v = ed.value, s = ed.selectionStart, e = ed.selectionEnd;
  const tok = commentToken();
  const [from, to] = lineRange(v, s, e);
  const lines = v.slice(from, to).split("\n");
  const allOn = lines.every((l) => !l.trim() || l.trimStart().startsWith(tok));
  const block = lines.map((line) => {
    if (!line.trim()) return line;
    if (allOn) {
      const i = line.indexOf(tok);
      return line.slice(0, i) + line.slice(i + tok.length).replace(/^ /, "");
    }
    const pad = (line.match(/^\s*/) || [""])[0];
    return pad + tok + " " + line.slice(pad.length);
  }).join("\n");
  setEditorValue(v.slice(0, from) + block + v.slice(to), ed);
  ed.selectionStart = from;
  ed.selectionEnd = from + block.length;
}

function newlineIndent(ed) {
  const v = ed.value, s = ed.selectionStart;
  const line = v.slice(v.lastIndexOf("\n", s - 1) + 1, s);
  let pad = (line.match(/^[ \t]*/) || [""])[0];
  if (/[:{]\s*$/.test(line)) pad += INDENT;      // new block: indent one level
  const ins = "\n" + pad;
  setEditorValue(v.slice(0, s) + ins + v.slice(ed.selectionEnd), ed);
  ed.selectionStart = ed.selectionEnd = s + ins.length;
}

function bindEditorKeys() {
  const ed = $("editor");
  if (!ed) return;
  let pending = null;
  ed.addEventListener("input", () => {           // autosave, debounced
    syncEditorGutter();                          // numbers first: no visible lag
    clearTimeout(pending);
    pending = setTimeout(saveDraft, 400);
  });
  ed.addEventListener("scroll", syncEditorGutter);
  ed.addEventListener("focus", syncEditorGutter);
  ed.addEventListener("blur", saveDraft);
  ed.addEventListener("keydown", (ev) => {
    const meta = ev.metaKey || ev.ctrlKey;
    if (ev.key === "Tab") {
      ev.preventDefault(); applyIndent(ed, ev.shiftKey); saveDraft();
    } else if (ev.key === "Enter" && !meta) {
      ev.preventDefault(); newlineIndent(ed); saveDraft();
    } else if (ev.key === "Enter" && meta) {
      ev.preventDefault(); doSubmit();
    } else if (meta && (ev.key === "/" || ev.key === "?")) {
      ev.preventDefault(); toggleComment(ed); saveDraft();
    }
  });
  syncEditorGutter();
  // A resized editor (the student drags the handle) changes the gutter's box.
  if (typeof ResizeObserver === "function") {
    try {
      new ResizeObserver(syncEditorGutter).observe(ed);
    } catch (e) { /* no observer: input/scroll/focus still keep it right */ }
  }
}

/* ---------------- task tabs ---------------- */
/* The code editor (#code-shared: dropzone + file chip + textarea) is ONE DOM
 * node shared by the Submit and Run tabs — it is moved between their slots on
 * tab switch, never duplicated. */
function shareEditor(where) {
  const wrap = $("code-shared");
  const slot = where === "run" ? $("run-editor-slot") : $("submit-editor-slot");
  if (wrap && slot && wrap.parentElement !== slot) slot.appendChild(wrap);
}

function setTaskTab(tab) {
  state.taskTab = tab;
  document.querySelectorAll(".tab-btn").forEach((b) => {
    const on = b.dataset.tab === tab;
    b.classList.toggle("active", on);
    if (b.hasAttribute("aria-selected")) b.setAttribute("aria-selected", on ? "true" : "false");
  });
  document.querySelectorAll(".tab-pane").forEach((p) =>
    p.classList.toggle("active", p.id === "pane-" + tab));
  if (tab === "run" || tab === "submit") shareEditor(tab);
  if (tab === "subs") loadTaskSubs();
  if (tab === "mine") loadTaskMine();
  if (tab === "report") loadMyReports();
}
document.querySelectorAll(".tab-btn").forEach((b) => {
  b.onclick = () => setTaskTab(b.dataset.tab);
});

function esc(s) {
  // One pass instead of five regexes; callers run this on every table cell.
  return String(s).replace(/[&<>"']/g, (c) => ESC_MAP[c]);
}
const ESC_MAP = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"};

function inlineMd(s) {
  s = esc(s);
  s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  return s;
}

/* Small escaped renderer for the statement format; no raw HTML or scripts. */
function renderMd(src) {
  const blocks = [];
  src = src.replace(/```(\w*)\n([\s\S]*?)```/g, (_m, _lang, code) => {
    blocks.push(`<div class="code-copy-wrap"><button class="icon-btn code-copy" type="button" data-copy title="คัดลอกโค้ดตัวอย่าง" aria-label="คัดลอกโค้ดตัวอย่าง">${icon("copy")}</button><pre><code>${esc(code.replace(/\n$/, ""))}</code></pre></div>`);
    return `\u0000${blocks.length - 1}\u0000`;
  });
  const labels = { Input: "ข้อมูลนำเข้า", Output: "ข้อมูลส่งออก", Limits: "ข้อจำกัด",
    Subtask: "การให้คะแนน", Subtasks: "การให้คะแนน" };
  const lines = [], lists = [];
  const closeList = () => { const list = lists.pop(); lines.push(`</li></${list.type}>`); };
  for (const line of src.split("\n")) {
    const item = line.match(/^(\s*)(-|\d+\.)\s+(.*)$/);
    if (item) {
      const indent = item[1].length, type = item[2] === "-" ? "ul" : "ol";
      while (lists.length && indent < lists[lists.length - 1].indent) closeList();
      if (lists.length && indent === lists[lists.length - 1].indent && type !== lists[lists.length - 1].type) closeList();
      if (!lists.length || indent > lists[lists.length - 1].indent) {
        lines.push(`<${type}>`);
        lists.push({ indent, type });
      } else lines.push("</li>");
      lines.push(`<li>${inlineMd(item[3])}`);
      continue;
    }
    while (lists.length) closeList();
    const ph = line.match(/^\u0000(\d+)\u0000$/);
    if (ph) { lines.push(blocks[Number(ph[1])] || ""); continue; }
    const heading = line.match(/^(#{1,3})\s+(.*)$/);
    if (heading) {
      const level = heading[1].length;
      lines.push(`<h${level}>${inlineMd(labels[heading[2]] || heading[2])}</h${level}>`);
    } else if (line.trim()) lines.push(`<p>${inlineMd(line)}</p>`);
  }
  while (lists.length) closeList();
  return lines.join("\n");
}

function badge(text, cls) {
  cls = cls || text;
  return `<span class="badge b-${esc(cls)}">${esc(text)}</span>`;
}

/* Tiny offline syntax highlighter (Python/C/C++) for the code viewer. */
const KEYWORDS = {
  python: new Set(("False None True and as assert async await break class continue " +
    "def del elif else except finally for from global if import in is lambda " +
    "nonlocal not or pass raise return try while with yield").split(" ")),
  c: new Set(("auto break case char const continue default do double else enum " +
    "extern float for goto if inline int long register return short signed " +
    "sizeof static struct switch typedef union unsigned void volatile while").split(" ")),
  cpp: new Set(("alignas and and_eq asm auto bitand bitor bool break case catch " +
    "char class compl concept const constexpr consteval constinit const_cast " +
    "continue decltype default delete do double else enum explicit export extern " +
    "false float for friend goto if inline int long mutable namespace new " +
    "noexcept not nullptr operator or private protected public return short " +
    "signed sizeof static struct switch template this throw true try typedef " +
    "typename union unsigned using virtual void volatile while").split(" ")),
};

function highlightCode(src, lang) {
  const key = KEYWORDS[lang] || KEYWORDS.cpp;
  const com = lang === "python" ? "#[^\\n]*" : "\\/\\/[^\\n]*|\\/\\*[\\s\\S]*?\\*\\/";
  const pp = lang === "python" ? "(?!)" : "#[^\\n]*";
  const re = new RegExp(
    `(?<com>${com})|(?<pp>${pp})|` +
    `(?<str>"(?:[^"\\\\\\n]|\\\\.)*"|'(?:[^'\\\\\\n]|\\\\.)*')|` +
    `(?<num>\\b\\d[\\w.]*(?:[eE][+-]?\\d+)?[uUlLfF]*)|` +
    `(?<word>\\b[A-Za-z_]\\w*)`,
    "g");
  let out = "", last = 0, m;
  while ((m = re.exec(src)) !== null) {
    out += esc(src.slice(last, m.index));
    const t = m[0];
    if (m.groups.com) out += `<span class="tk-c">${esc(t)}</span>`;
    else if (m.groups.pp) out += `<span class="tk-p">${esc(t)}</span>`;
    else if (m.groups.str) out += `<span class="tk-s">${esc(t)}</span>`;
    else if (m.groups.num) out += `<span class="tk-n">${esc(t)}</span>`;
    else if (key.has(t)) out += `<span class="tk-k">${esc(t)}</span>`;
    else {
      let j = re.lastIndex;
      while (src[j] === " " || src[j] === "\t") j++;
      out += src[j] === "("
        ? `<span class="tk-f">${esc(t)}</span>`
        : esc(t);
    }
    last = re.lastIndex;
  }
  return out + esc(src.slice(last));
}

function diffBadge(d) {
  if (!d) return `<span class="dim">—</span>`;
  return `<span class="diff-tag diff-${esc(d)}">${esc(d)}</span>`;
}

/* ---------------- starter templates (all correct) ---------------- */
const TEMPLATES = {
  aplusb: {
    python:
`import sys

def main():
    data = sys.stdin.read().strip().split()
    if not data:
        return
    a, b = int(data[0]), int(data[1])
    print(a + b)

main()
`,
    c:
`#include <stdio.h>

int main(void) {
    long long a, b;
    if (scanf("%lld %lld", &a, &b) != 2) return 0;
    printf("%lld\\n", a + b);
    return 0;
}
`,
    cpp:
`#include <iostream>

int main() {
    std::ios::sync_with_stdio(false);
    std::cin.tie(nullptr);
    long long a, b;
    if (!(std::cin >> a >> b)) return 0;
    std::cout << a + b << '\\n';
    return 0;
}
`,
  },
  fizzbuzz: {
    python:
`import sys

def main():
    n = int(sys.stdin.read().strip())
    out = []
    for i in range(1, n + 1):
        if i % 15 == 0:
            out.append("FizzBuzz")
        elif i % 3 == 0:
            out.append("Fizz")
        elif i % 5 == 0:
            out.append("Buzz")
        else:
            out.append(str(i))
    sys.stdout.write("\\n".join(out) + "\\n")

main()
`,
    c:
`#include <stdio.h>

int main(void) {
    int n;
    if (scanf("%d", &n) != 1) return 0;
    for (int i = 1; i <= n; i++) {
        if (i % 15 == 0) printf("FizzBuzz\\n");
        else if (i % 3 == 0) printf("Fizz\\n");
        else if (i % 5 == 0) printf("Buzz\\n");
        else printf("%d\\n", i);
    }
    return 0;
}
`,
    cpp:
`#include <iostream>
#include <string>

int main() {
    std::ios::sync_with_stdio(false);
    std::cin.tie(nullptr);
    int n;
    if (!(std::cin >> n)) return 0;
    std::string out;
    out.reserve((size_t)n * 8);
    for (int i = 1; i <= n; i++) {
        if (i % 15 == 0) out += "FizzBuzz\\n";
        else if (i % 3 == 0) out += "Fizz\\n";
        else if (i % 5 == 0) out += "Buzz\\n";
        else { out += std::to_string(i); out += '\\n'; }
    }
    std::cout << out;
    return 0;
}
`,
  },
  palindrome: {
    python:
`import sys

def main():
    data = sys.stdin.read().splitlines()
    s = data[0] if data else ""
    print("YES" if s == s[::-1] else "NO")

main()
`,
    c:
`#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int main(void) {
    char *line = NULL;
    size_t cap = 0;
    if (getline(&line, &cap, stdin) < 0) {
        printf("YES\\n");
        free(line);
        return 0;
    }
    long len = (long)strlen(line);
    while (len > 0 && (line[len - 1] == '\\n' || line[len - 1] == '\\r'))
        line[--len] = '\\0';
    for (long i = 0, j = len - 1; i < j; i++, j--) {
        if (line[i] != line[j]) {
            printf("NO\\n");
            free(line);
            return 0;
        }
    }
    printf("YES\\n");
    free(line);
    return 0;
}
`,
    cpp:
`#include <iostream>
#include <string>

int main() {
    std::ios::sync_with_stdio(false);
    std::cin.tie(nullptr);
    std::string s;
    if (!std::getline(std::cin, s)) s = "";
    if (!s.empty() && s.back() == '\\r') s.pop_back();
    std::string r(s.rbegin(), s.rend());
    std::cout << (s == r ? "YES\\n" : "NO\\n");
    return 0;
}
`,
  },
};

/* ---------------- api ---------------- */
/* One fetch wrapper: JSON in/out, and errors carry the server `detail`
 * verbatim plus `status` so callers can special-case 401/403/409/429. */
async function apiFetch(path, opts) {
  const identity = state.user;
  const r = await fetch(API + path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    if (r.status === 401 && !path.startsWith("/api/auth/") && state.user && state.user === identity) {
      state.user = null;
      state.is_admin = state.isAdmin = false;
      state.problems = [];
      state.stats = {};
      state.statsLoaded = false;
      clearGetCache();
      try { localStorage.removeItem(PROBLEMS_CACHE_KEY); } catch (e) { /* ignore */ }
      router();
      void refreshMe();
    }
    const err = new Error(data.detail || `HTTP ${r.status} (${path})`);
    err.status = r.status;
    const retry = r.headers.get("Retry-After");
    if (retry) err.retryAfter = retry;
    throw err;
  }
  return data;
}

function apiGet(path) {
  return apiFetch(path, { credentials: "include" });
}

/* Several views ask for the same history slice while rendering (the task list,
 * "your score", the My Submissions tab). Deduplicate in-flight requests and
 * keep the answer for a moment so one render is not four round trips.
 * Cleared after a submission, when the answers are known to be stale. */
const _getCache = new Map();
const _getInflight = new Map();
let _getGen = 0;   // bumped on logout: late responses must not repopulate the cache

function apiGetCached(path, ttlMs, force = false) {
  if (force) { _getCache.delete(path); _getInflight.delete(path); }
  const hit = _getCache.get(path);
  if (hit && Date.now() - hit.t < ttlMs) return Promise.resolve(hit.v);
  if (_getInflight.has(path)) return _getInflight.get(path);
  const gen = _getGen;
  const p = apiGet(path).then((v) => {
    if (gen === _getGen && _getInflight.get(path) === p) {
      const pending = (v.submissions || []).some(r => ["queued", "running"].includes(r.status));
      if (!pending) _getCache.set(path, { t: Date.now(), v: v });
      if (_getCache.size > 100) _getCache.delete(_getCache.keys().next().value);
      _getInflight.delete(path);
    }
    return v;
  }, (e) => {
    if (_getInflight.get(path) === p) _getInflight.delete(path);
    throw e;
  });
  _getInflight.set(path, p);
  return p;
}

function clearGetCache() {
  // Identity invalidation must also remove source already displayed on a
  // shared computer. Ordinary submission completion only expires GET caches.
  state.inspectionIdentityGen = (state.inspectionIdentityGen || 0) + 1;
  if (state.inspectionSeq || state.inspectedSubmission) clearSubmissionInspection();
  invalidateGetCache();
}

function invalidateGetCache() {
  _getGen++;
  _getCache.clear();
  _getInflight.clear();
  state.detailCache = {};
  _detailInflight.clear();
  state.samples = {};
  _sampleInflight.clear();
  state.taskSeq = (state.taskSeq || 0) + 1;
}

function apiPostJson(path, body) {
  return apiFetch(path, {
    method: "POST", credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

function apiPostForm(path, form) {
  return apiFetch(path, { method: "POST", credentials: "include", body: form });
}

function apiDelete(path) {
  return apiFetch(path, { method: "DELETE", credentials: "include" });
}

async function fetchStats() {
  const gen = _getGen;
  try {
    const s = await apiGet("/api/stats");
    if (gen !== _getGen) return;
    state.stats = (s && s.configured && s.stats) || {};
    state.statsLoaded = !!(s && s.configured);
  } catch (e) {
    if (gen !== _getGen) return;
    state.stats = {};
    state.statsLoaded = false;
  }
}

/* ---------------- router ---------------- */
function show(view) {
  if (view !== "contest") state.contestSeq = (state.contestSeq || 0) + 1;
  if (view !== "submission" && (state.inspectionSeq || state.inspectedSubmission)) clearSubmissionInspection();
  // Leaving the task list remembers where you were: coming back from a
  // problem restores the scroll instead of dumping you at the top.
  try {
    if (document.querySelector("#view-tasks.active") && view !== "tasks") {
      state.tasksScrollY = window.scrollY || 0;
    }
  } catch (e) { /* ignore */ }
  document.querySelectorAll(".view").forEach((v) =>
    v.classList.toggle("active", v.id === "view-" + view));
  document.querySelectorAll(".nav-link").forEach((a) => {
    const on = a.dataset.route === view ||
      (view === "task" && a.dataset.route === "tasks") ||
      (view === "contest" && a.dataset.route === "contests");
    a.classList.toggle("active", on);
    if (on) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  window.scrollTo(0, 0);
  // Move screen-reader/keyboard focus to the new view's heading.
  try {
    const h = document.querySelector(`#view-${view} h1, #view-${view} h2`);
    if (h) {
      if (!h.hasAttribute("tabindex")) h.setAttribute("tabindex", "-1");
      h.focus({ preventScroll: true });
    }
  } catch (e) { /* ignore */ }
}

const TITLES = {
  home: "Newbiegrader — ฝึกเขียนโปรแกรมด้วยโจทย์ภาษาไทย",
  tasks: "โจทย์ทั้งหมด · Newbiegrader",
  archive: "หมวดโจทย์ · Newbiegrader",
  contests: "การแข่ง · Newbiegrader",
  contest: "การแข่ง · Newbiegrader",
  history: "ประวัติการส่ง · Newbiegrader",
  submission: "รายละเอียดการส่ง · Newbiegrader",
  settings: "ตั้งค่า · Newbiegrader",
  admin: "ผู้ดูแล · Newbiegrader",
  login: "เข้าสู่ระบบ · Newbiegrader",
};

function setTitle(view, extra) {
  document.title = (extra ? `${extra} · ` : "") + (TITLES[view] || TITLES.home);
}

// Re-render whatever view is already on screen. Used after the identity fetch
// resolves: a page opened directly on #/settings (or #/contests, #/task/<id>)
// renders before /api/auth/me answers, so admin cards and "your score" would
// otherwise stay stale until the next navigation. Never navigates.
function resolveTaskToken(token) {
  const byId = state.problems.find((p) => p.id === token);
  if (byId) return byId.id;
  if (/^\d+$/.test(token)) {
    const byNo = state.problems.find((p) => String(p.no) === token);
    if (byNo) return byNo.id;
  }
  return null;
}

/* ---------------- history hygiene ----------------
 * The task list is the hub users bounce back to, so a link pointing at the
 * route sitting directly behind the current entry must *go back* instead of
 * pushing a second copy — otherwise the problem that was open before stays in
 * the stack and one Back press lands on it. The shortcut is only taken when we
 * know what is behind us: either we pushed the entry ourselves, or the entry
 * recorded its neighbour in history.state (survives reloads and Forward). */
let _currentRoute = null;     // route of the entry we are on
let _prevEntryRoute = null;   // route of the entry directly behind it, or null
let _pendingNav = null;       // {kind, to} for a navigation we started

function _nav(url, kind) {
  if (!url || url === (location.hash || "#/")) return;
  _pendingNav = { kind: kind, to: url };
  if (kind === "replace") location.replace(url);
  else location.hash = url;
}

function _syncHistoryEntry(kind) {
  if (kind === "push") {
    _prevEntryRoute = _currentRoute;      // we just left it: it is right behind us
  } else if (kind !== "replace") {
    _prevEntryRoute = (history.state && history.state.prev) || null;   // Back/Forward
  }
  try {
    history.replaceState({ prev: _prevEntryRoute }, "", location.href);
  } catch (e) { /* history disabled: navigation still works, only the hint is lost */ }
}

/* Task-to-task jumps replace the history entry instead of pushing: opening
 * problem B from problem A must not stack. From anywhere else (list, palette,
 * history, contest) navigation pushes normally. */
function openTask(pid) {
  const url = "#/task/" + encodeURIComponent(pid);
  _nav(url, (location.hash || "").startsWith("#/task/") ? "replace" : "push");
}

// Cards are plain <a> for keyboard/SEO: intercept only the two cases that need
// custom history handling and let every other hash link push on its own.
document.addEventListener("click", (e) => {
  if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.shiftKey ||
      (e.button !== undefined && e.button !== 0)) return;
  const a = e.target && e.target.closest ? e.target.closest('a[href^="#/"]') : null;
  if (!a) return;
  const target = a.getAttribute("href") || "";
  if (_prevEntryRoute && target === _prevEntryRoute) {   // retracing: reuse the entry
    e.preventDefault();
    history.back();
    return;
  }
  if (target.startsWith("#/task/") && (location.hash || "").startsWith("#/task/")) {
    e.preventDefault();
    openTask(decodeURIComponent(target.slice("#/task/".length)));
    return;
  }
  if (target !== (location.hash || "#/")) _pendingNav = { kind: "push", to: target };
});

async function openTaskByToken(token) {
  const route = location.hash, gen = state.inspectionIdentityGen || 0;
  const current = () => route === location.hash && gen === (state.inspectionIdentityGen || 0);
  const pid = resolveTaskToken(token);
  if (pid) { show("task"); selectProblem(pid); return; }
  try {   // contest-only problems are absent from the public list: ask the API
    const d = await apiGet(`/api/problems/${encodeURIComponent(token)}`);
    if (!current()) return;
    if (d && d.id) { show("task"); selectProblem(d.id); return; }
  } catch (e) { /* fall through */ }
  if (current()) _nav("#/tasks", "push");
}

function currentRouteRender() {
  const h = location.hash || "#/";
  if (h.startsWith("#/submission/")) {
    const sid = decodeURIComponent(h.slice("#/submission/".length));
    return () => loadSubmission(sid);
  }
  if (h.startsWith("#/task/")) {
    const pid = resolveTaskToken(decodeURIComponent(h.slice("#/task/".length)));
    if (!pid) return null;
    return () => selectProblem(pid);
  }
  if (h === "#/tasks") return () => renderTasksTable();
  if (h === "#/archive") return () => renderArchive();
  if (h === "#/contests") return () => renderContests();
  if (h.startsWith("#/contests/")) {
    const cid = decodeURIComponent(h.slice("#/contests/".length));
    return () => renderContest(cid);
  }
  if (h === "#/history") return () => loadHistory();
  if (h === "#/settings") return () => renderSettings();
  if (h === "#/admin") return () => renderAdmin();
  return null;
}

function publicRoute(hash) {
  return hash === "#/" || hash === "#/login" || hash.startsWith("#/login?") || hash === "#/login/register";
}

function loginDestination() {
  const route = state.authReturnTo || "#/tasks";
  return /^#\/(?:tasks|archive|history|settings|admin|task\/[^?#]+|submission\/[^?#]+|contests(?:\/[^?#]+)?)$/.test(route)
    ? route : "#/tasks";
}

function router() {
  const pend = _pendingNav;
  _pendingNav = null;
  const h = location.hash || "#/";
  // Record where this entry sits before rendering: a navigation we started is
  // trustworthy, anything else is a Back/Forward traversal.
  _syncHistoryEntry(pend && pend.to === h ? pend.kind : null);
  _currentRoute = h;
  if (!publicRoute(h) && !state.user) {
    stopContestTicker();
    if (!state.authReady) { show("home"); return; }
    state.authReturnTo = h;
    _nav("#/login", "replace");
    return;
  }
  if (!h.startsWith("#/contests/")) stopContestTicker();
  if (h.startsWith("#/submission/")) {
    show("submission");
    setTitle("submission");
    loadSubmission(decodeURIComponent(h.slice("#/submission/".length)));
    return;
  }
  if (h.startsWith("#/task/")) {
    openTaskByToken(decodeURIComponent(h.slice("#/task/".length)));
    return;
  }
  if (h === "#/tasks") { show("tasks"); setTitle("tasks"); state.restoreTasksScroll = true; renderTasksTable(); return; }
  if (h === "#/archive") { show("archive"); setTitle("archive"); renderArchive(); return; }
  if (h === "#/leaderboard") { _nav("#/tasks", "replace"); return; }
  if (h === "#/contests") { show("contests"); setTitle("contests"); renderContests(); return; }
  if (h.startsWith("#/contests/")) {
    const cid = decodeURIComponent(h.slice("#/contests/".length));
    show("contest");
    setTitle("contest");
    renderContest(cid);
    return;
  }
  if (h === "#/history") { show("history"); setTitle("history"); loadHistory(); return; }
  if (h === "#/settings") { show("settings"); setTitle("settings"); renderSettings(); return; }
  if (h === "#/admin") {
    show("admin");
    setTitle("admin");
    renderAdmin();
    return;
  }
  if (h === "#/login" || h.startsWith("#/login?")) {
    show("login"); setTitle("login"); setAuthMode("login");
    if (state.authError) {
      $("auth-err").textContent = "ตรวจสอบการเข้าสู่ระบบไม่สำเร็จ กรุณาโหลดหน้าอีกครั้ง";
      $("auth-err").classList.remove("hidden");
    }
    if (!state.authError && state.authReturnTo !== "#/tasks") {
      $("auth-err").textContent = "เข้าสู่ระบบก่อนเพื่อเปิดหน้าที่เลือกไว้";
      $("auth-err").classList.remove("hidden", "bad");
    }
    if (h.includes("email_proof=required")) {
      $("auth-err").textContent = "กรุณาเข้าสู่ระบบด้วยรหัสผ่าน หรือยืนยันอีเมลเพื่อตั้งรหัสผ่านใหม่ บัญชี Google จะไม่ถูกผูกกับบัญชีเดิมโดยอัตโนมัติ";
      $("auth-err").classList.remove("hidden");
    }
    return;
  }
  if (h === "#/login/register") { show("login"); setTitle("login"); setAuthMode("register"); return; }
  show("home");
  setTitle("home");
  renderHome();
}
window.addEventListener("hashchange", router);

/* ---------------- home ---------------- */
function renderHome(animateSelection = false) {
  let subs = 0, acc = 0;
  Object.values(state.stats).forEach((s) => { subs += s.subs || 0; acc += s.accepted || 0; });
  if (!state.user && state.homeSummary) {
    subs = state.homeSummary.subs || 0;
    acc = state.homeSummary.accepted || 0;
  }
  $("stat-subs").textContent = state.statsLoaded ? subs : "—";
  $("stat-accepted").textContent = state.statsLoaded ? acc : "—";
  $("stat-mine").textContent = state.user
    ? `${Object.values(state.myBest || {}).filter((v) => v === 100).length}`
    : "—";
  const ledger = document.querySelector(".home-ledger");
  if (ledger) ledger.classList.toggle("hidden", !state.statsLoaded && !state.user);
  const pool = state.problems.filter((p) => !p.hidden && !p.contest_only && !p.config_hidden && p.difficulty === state.homeLevel)
    .sort((a, b) => (a.no || 9999) - (b.no || 9999));
  state.homeOffset = pool.length ? state.homeOffset % pool.length : 0;
  const starters = Array.from({ length: Math.min(3, pool.length) }, (_, i) => pool[(state.homeOffset + i) % pool.length]);
  $("home-next").disabled = pool.length <= 3;
  const selected = starters.find((p) => p.id === state.homeSelected) || starters[0];
  state.homeSelected = selected ? selected.id : null;
  document.querySelectorAll("[data-home-level]").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.homeLevel === state.homeLevel));
  });
  const box = $("home-problems");
  const focusedProblem = document.activeElement && document.activeElement.dataset.homeProblem;
  box.innerHTML = starters.map((p) =>
    `<button type="button" class="home-problem" data-home-problem="${esc(p.id)}" aria-pressed="${p.id === state.homeSelected}">` +
    `<span class="home-problem-no">${esc(String(p.no || "—").padStart(2, "0"))}</span>` +
    `<span>${problemNameHtml(p.title)}</span><span class="selection-mark" aria-hidden="true"></span></button>`).join("");
  box.querySelectorAll("[data-home-problem]").forEach((button) => {
    button.onclick = (event) => {
      state.homeSelected = button.dataset.homeProblem;
      renderHome(true);
      revealHomePreview(event);
    };
    if (button.dataset.homeProblem === focusedProblem) button.focus({ preventScroll: true });
  });
  $("home-featured").innerHTML = selected
    ? `<div class="featured-meta"><div class="featured-number"><span>โจทย์</span><strong>${esc(String(selected.no || "—").padStart(2, "0"))}</strong></div>${diffBadge(selected.difficulty)}</div>` +
      `<div class="featured-content"><h3>${problemNameHtml(selected.title)}</h3>` +
      `<div class="featured-tags">${(selected.tags || []).slice(0, 3).map((tag) => `<span>${esc(tag)}</span>`).join("")}</div></div>` +
      `<div class="featured-bottom"><dl class="featured-limits"><div><dt>เวลา</dt><dd>${esc(String(selected.time_limit))} วินาที</dd></div><div><dt>หน่วยความจำ</dt><dd>${esc(String(selected.memory_limit))} MB</dd></div></dl>` +
      `<a class="featured-open" href="#/task/${encodeURIComponent(selected.id)}"><span>เปิดโจทย์</span>${icon("back")}</a></div>`
    : `<p>ยังไม่มีโจทย์ในระดับนี้ <a href="#/tasks">ดูโจทย์ทั้งหมด</a></p>`;
  if (animateSelection && selected && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
    $("home-featured").querySelector(".featured-content").animate(
      [{ opacity: .35, transform: "translateY(4px)" }, { opacity: 1, transform: "translateY(0)" }],
      { duration: 180, easing: "ease-out" }
    );
  }
  renderResumeCard();
}

function revealHomePreview(event) {
  const preview = $("home-featured");
  const bounds = preview.getBoundingClientRect();
  const headerBottom = document.querySelector(".topbar").getBoundingClientRect().bottom;
  if (event.detail > 0 && (bounds.top < headerBottom || bounds.bottom > window.innerHeight)) {
    preview.scrollIntoView({ block: "start", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth" });
  }
}

function emptyStateHtml(title, message, href, label) {
  return `<div class="empty-state"><h3>${esc(title)}</h3><p>${esc(message)}</p>` +
    (href ? `<a class="btn-ghost" href="${esc(href)}">${esc(label)}</a>` : "") + `</div>`;
}

function relTime(ts) {
  const s = Math.max(0, Math.round((Date.now() - ts) / 1000));
  if (s < 60) return "เมื่อสักครู่";
  const m = Math.round(s / 60);
  if (m < 60) return `${m} นาทีที่แล้ว`;
  const h = Math.round(m / 60);
  return h < 24 ? `${h} ชั่วโมงที่แล้ว` : `${Math.round(h / 24)} วันที่แล้ว`;
}

/* "pick up where you left off": the newest draft this browser kept, plus a note
 * when a judge run for it is still in flight. */
function renderResumeCard() {
  const box = $("resume-card");
  if (!box) return;
  const drafts = lsJson(DRAFT_KEY);
  const newest = Object.entries(drafts).sort((a, b) => (b[1].t || 0) - (a[1].t || 0))[0];
  if (!newest) {
    box.classList.add("hidden");
    box.innerHTML = "";
    return;
  }
  const pid = newest[0];
  const p = state.problems.find((x) => x.id === pid);
  const title = p ? `${noLabel(p)} ${esc(p.title)}` : esc(pid);
  const pending = lastJobFor(pid) ? " · งานตรวจยังไม่จบ" : "";
  box.classList.remove("hidden");
  box.innerHTML = `<div class="resume">
    <div class="resume-main">
      <div class="resume-title">ทำต่อจากที่ค้างไว้</div>
      <div class="dim">${title} — บันทึกร่างไว้ ${esc(relTime(newest[1].t || Date.now()))}${esc(pending)}</div>
    </div>
    <a class="btn-primary" href="#/task/${encodeURIComponent(pid)}">เปิดโจทย์นี้</a>
  </div>`;
}

/* ---------------- tasks (sidebar + cards) ---------------- */
function noLabel(p) {
  return p && p.no ? `<span class="pno">#${esc(String(p.no))}</span>` : "";
}

function problemNameHtml(title) {
  const name = String(title || "");
  const match = name.match(/^(.*?)\s*\(([^()]*)\)\s*$/);
  return `<span class="problem-name">${esc(match ? match[1] : name)}</span>` +
    (match ? `<span class="problem-english">${esc(match[2])}</span>` : "");
}

function problemRowsForIdentity(rows) {
  const list = Array.isArray(rows) ? rows : [];
  if (state.is_admin) return list;
  // A late admin response must never repopulate a logged-out user's task list.
  return list.filter(p => !p.hidden && !p.contest_only && !p.config_hidden).map(p => {
    const { hidden, contest_only, config_hidden, ...publicRow } = p;
    return publicRow;
  });
}

const _byPidIndex = new WeakMap();
function byPidOf(list, pid) {
  if (!list) return null;
  let index = _byPidIndex.get(list);
  if (!index) {
    index = new Map();
    list.forEach((p) => {
      if (p && p.id != null && !index.has(p.id)) index.set(p.id, p);
    });
    _byPidIndex.set(list, index);
  }
  return index.get(pid) || null;
}

function problemCell(pid) {
  const p = byPidOf(state.problems, pid);
  const num = p && p.no ? `<span class="record-number">#${esc(String(p.no))}</span>` : "";
  return `<a class="tlink problem-record" href="#/task/${encodeURIComponent(pid)}">${num}<span>${p ? problemNameHtml(p.title) : esc(pid)}</span></a>`;
}

function filteredProblems() {
  const q = state.filter.q.trim().toLowerCase();
  const list = state.problems.filter((p) => {
    if (state.filter.diff && (p.difficulty || "") !== state.filter.diff) return false;
    if (state.filter.tag && !(p.tags || []).includes(state.filter.tag)) return false;
    if (q) {
      const qq = q.startsWith("#") ? q.slice(1) : q;
      const hay = `${p.no || ""} ${p.id} ${p.title} #${p.no || ""}`.toLowerCase();
      if (!hay.includes(q)) {
        // numeric search: "19" should find #19 even if the slug contains 19 too
        if (!(/^\d+$/.test(qq) && String(p.no) === qq)) return false;
      }
    }
    if (state.filter.status === "tried" && !(state.myBest && p.id in state.myBest)) return false;
    if (state.filter.status === "solved" && !(state.myBest && state.myBest[p.id] === 100)) return false;
    return true;
  });
  return sortProblemList(list);
}

const DIFF_RANK = { easy: 0, medium: 1, hard: 2 };

function sortProblemList(list) {
  const byNo = (a, b) => ((a.no || 9999) - (b.no || 9999)) || a.id.localeCompare(b.id);
  const mode = state.filter.sort || "no";
  const copy = [...list];
  if (mode === "difficulty") {
    copy.sort((a, b) => ((DIFF_RANK[a.difficulty] ?? 9) - (DIFF_RANK[b.difficulty] ?? 9)) || byNo(a, b));
  } else if (mode === "title") {
    copy.sort((a, b) => (a.title || "").localeCompare(b.title || "", "th") || byNo(a, b));
  } else {
    copy.sort(byNo);
  }
  return copy;
}

function taskScoreHtml(pid) {
  if (state.user && !state.myBestLoaded) return `<span class="score-untried">กำลังโหลด…</span>`;
  const tried = state.myBest && state.myBest[pid] != null;
  const s = tried ? state.myBest[pid] : 0;
  if (!tried) return `<span class="score-untried"><span class="status-ring" aria-hidden="true"></span>ยังไม่ได้ลอง</span>`;
  if (s === 100) return `<span class="score-solved">${icon("check")}ผ่านแล้ว</span>`;
  return `<span class="${scoreCls(s)}">${esc(String(s))} <span class="dim">/ 100</span></span>`;
}

function renderTasksTable() {
  try {
    const f0 = state.filter;
    localStorage.setItem("ng_filter", JSON.stringify({ q: f0.q, diff: f0.diff, tag: f0.tag, status: f0.status }));
  } catch (e) { /* ignore */ }
  const list = filteredProblems();
  const hiddenCount = state.problems.filter((p) => p.hidden).length;
  // The total problem count is not published: show how many matched only while a
  // filter/search is active (otherwise the number is just the total in disguise).
  const f = state.filter;
  const filtering = !!(f.q.trim() || f.diff || f.tag || (f.status && f.status !== "all"));
  $("filter-count").textContent = (filtering ? `พบ ${list.length} ข้อ` : "") +
    (state.is_admin && hiddenCount ? ` (มี ${hiddenCount} ข้อซ่อนจากผู้ใช้ทั่วไป)` : "");
  const box = $("task-cards");
  box.innerHTML = "";
  $("tasks-empty").classList.toggle("hidden", list.length > 0);
  if (!state.problems.length) {
    $("tasks-empty").textContent = "ยังไม่มีโจทย์ให้แสดง ลองกลับมาใหม่อีกครั้ง";
    $("tasks-empty").classList.remove("hidden");
    return;
  }
  if (!state.user && state.filter.status !== "all") {
    $("tasks-empty").innerHTML = `<div class="empty-state"><p>เข้าสู่ระบบเพื่อดูโจทย์ที่คุณเคยลองทำ</p><a class="btn-ghost" href="#/login">เข้าสู่ระบบ</a></div>`;
    $("tasks-empty").classList.remove("hidden");
    return;
  }
  if (!list.length && filtering) {
    $("tasks-empty").innerHTML = `<div class="empty-state">
      <p>ไม่เจอโจทย์ที่ตรงเงื่อนไข</p>
      <button class="btn-ghost" type="button" id="filter-reset">ล้างตัวกรองทั้งหมด</button>
    </div>`;
    $("tasks-empty").classList.remove("hidden");
    $("filter-reset").onclick = () => {
      state.filter.q = "";
      $("q").value = "";
      state.filter.diff = "";
      const fd = $("f-diff");
      if (fd) fd.value = "";
      setStatusFilter("all");
      if (state.filter.tag) setTagFilter(state.filter.tag);
      else renderTasksTable();
    };
    return;
  }
  const showTag = $("show-tag").checked;
  list.forEach((p) => {
    const st = state.stats[p.id] || {};
    const tags = (p.tags || []).map((t) => `<span class="tag-chip">${esc(t)}</span>`).join("");
    // A real link: keyboard users can reach and open a problem (Tab + Enter).
    const card = document.createElement("a");
    card.className = "task-card";
    card.href = "#/task/" + encodeURIComponent(p.id);
    card.innerHTML = `
      <span class="tc-number">${esc(String(p.no || "—").padStart(2, "0"))}</span>
      <div class="tc-main">
        <div class="tc-title">${problemNameHtml(p.title)}</div>
        ${showTag ? `<div class="tc-tags">${tags || '<span class="dim">—</span>'}</div>` : ""}
        ${st.solvers > 0 || p.hidden ? `<div class="tc-meta">${st.solvers > 0 ? `<span>ผ่านแล้ว ${esc(String(st.solvers))} คน</span>` : ""}${p.hidden ? badge("ซ่อนจาก Task", "hidden") : ""}</div>` : ""}
      </div>
      <div class="tc-difficulty">${diffBadge(p.difficulty)}</div>
      <div class="tc-side score-wrap">${taskScoreHtml(p.id)}<span class="row-arrow" aria-hidden="true">↗</span></div>`;
    card.onmouseenter = () => {
      prefetchDetail(p.id);
    };
    card.onfocus = () => {
      prefetchDetail(p.id);
    };
    box.appendChild(card);
  });
  if (state.user && !state.myBestLoaded) {
    fetchMyBest().then(() => {
      if (document.querySelector("#view-tasks.active")) renderTasksTable();
    });
  }
  if (state.restoreTasksScroll) {
    state.restoreTasksScroll = false;
    window.scrollTo(0, state.tasksScrollY || 0);
  }
}

async function fetchMyBest() {
  const gen = _getGen, pid = state.current;
  state.myBestLoaded = true;
  try {
    const h = await apiGetCached("/api/history?limit=100&mine=1", 15000);
    if (gen !== _getGen) return;
    const best = {};
    ((h.configured && h.submissions) || []).forEach((r) => {
      if (r.contest_id) return;   // contest scores belong to the contest board
      best[r.problem] = Math.max(best[r.problem] || 0, r.score || 0);
    });
    state.myBest = best;
  } catch (e) {
    if (gen !== _getGen) return;
    state.myBest = state.myBest || {};
    state.myBestLoaded = false;  // retry on next view render
  }
}
/* ---------------- archive ---------------- */
function renderArchive() {
  const tagCount = {};
  state.problems.forEach((p) => (p.tags || []).forEach((t) => {
    tagCount[t] = (tagCount[t] || 0) + 1;
  }));
  const box = $("archive-tags");
  const tags = Object.keys(tagCount).sort((a, b) => a.localeCompare(b));
  box.classList.remove("dim");
  box.innerHTML = tags.length
    ? tags.map((t) => `<a class="archive-topic" href="#/tasks" data-tag="${esc(t)}"><span>${esc(t)}<small>${tagCount[t]} ข้อในหมวดนี้</small></span>${icon("back")}</a>`).join("")
    : emptyStateHtml("ยังไม่มีหมวดโจทย์", "กลับมาเลือกหัวข้อได้เมื่อมีโจทย์พร้อมให้ฝึก", "#/tasks", "ดูโจทย์ทั้งหมด");
  box.querySelectorAll("[data-tag]").forEach((link) => {
    link.onclick = () => applyHomeFilter("", link.dataset.tag);
  });
  const labels = { easy: "เริ่มต้น", medium: "ปานกลาง", hard: "ท้าทาย", unrated: "ยังไม่จัดระดับ" };
  const keys = [...new Set(state.problems.map((p) => p.difficulty || "unrated"))]
    .sort((a, b) => (DIFF_RANK[a] ?? 9) - (DIFF_RANK[b] ?? 9));
  const progressReady = state.user && state.myBestLoaded;
  $("archive-groups").innerHTML = `<div class="archive-grid">` + keys.map((d) => {
    const list = state.problems.filter((p) => (p.difficulty || "unrated") === d);
    const solved = list.filter((p) => state.myBest && state.myBest[p.id] === 100).length;
    const pct = list.length ? Math.round(100 * solved / list.length) : 0;
    return `<a class="archive-card" data-diff="${esc(d)}" href="#/tasks">` +
      `<div class="arch-top"><h4>${esc(labels[d] || d)}</h4>${diffBadge(d)}</div>` +
      (progressReady ? `<div class="arch-sub"><span>ทำผ่าน ${solved} / ${list.length} ข้อ</span><span>${pct}%</span></div><span class="arch-bar"><span class="arch-fill" style="width:${pct}%"></span></span>` :
        `<p class="arch-description">${state.user ? "กำลังโหลดผลการฝึก…" : "เปิดดูโจทย์ในระดับนี้"}</p>`) +
      `<span class="archive-level-open">ดูโจทย์ ${icon("back")}</span></a>`;
  }).join("") + `</div>`;
  $("archive-groups").querySelectorAll("[data-diff]").forEach((link) => {
    link.onclick = () => applyHomeFilter(link.dataset.diff === "unrated" ? "" : link.dataset.diff);
  });
  if (state.user && !state.myBestLoaded) {
    fetchMyBest().then(() => {
      if (document.querySelector("#view-archive.active")) renderArchive();
    });
  }
}

/* ---------------- task detail ---------------- */
const DETAIL_CACHE_TTL = 60 * 1000;
const _detailInflight = new Map();

function showPdfFrame(url) {
  const current = document.getElementById("pdf-frame");
  if (current && current.dataset.src === url) {
    current.classList.remove("hidden");
    return;
  }
  // Replacing the frame avoids adding nested PDF navigations to Back history.
  const frame = document.createElement("iframe");
  frame.id = "pdf-frame";
  frame.title = "เอกสารโจทย์ PDF";
  frame.dataset.src = url;
  frame.src = url;
  if (current) current.replaceWith(frame);
  else $("pane-statement").appendChild(frame);
}

function fillTaskDetail(p) {
  setTitle("tasks", `#${p.no || "?"} ${p.title}`);
  $("task-title").innerHTML = `<span class="task-number">โจทย์ ${esc(String(p.no || "—").padStart(2, "0"))} <span>/ ${esc(p.id)}</span></span>${problemNameHtml(p.title)}`;
  $("task-meta").innerHTML =
    `${diffBadge(p.difficulty)}` +
    `<span class="meta-chip">เวลา ${esc(String(p.time_limit))} วินาที</span>` +
    `<span class="meta-chip">หน่วยความจำ ${esc(String(p.memory_limit))} MB</span>` +
    `<span class="meta-chip">${p.tests} เทสต์ / ตัวตรวจ ${esc(String(p.checker))}</span>` +
    ((p.tags || []).map((t) => `<span class="meta-chip">${esc(t)}</span>`).join(""));
  const nSamples = (p.samples || []).length;
  $("task-counts").textContent = `ตัวอย่างสาธารณะ ${nSamples} ข้อ`;
  renderSampleButtons(p);
  prefetchSamples(p);
  const pdfUrl = `${API}/api/problems/${encodeURIComponent(p.id)}/pdf`;
  $("statement-open").classList.toggle("hidden", !p.has_pdf);
  if (p.has_pdf) $("statement-open").href = pdfUrl;
  else $("statement-open").removeAttribute("href");
  const tb = $("task-body");
  if (p.has_pdf) {
    showPdfFrame(pdfUrl);
    tb.classList.add("hidden");
  } else {
    const frame = document.getElementById("pdf-frame");
    if (frame) frame.classList.add("hidden");
    tb.classList.remove("hidden");
    tb.innerHTML = renderMd(p.statement || "เนื้อหาโจทย์ยังไม่พร้อมใช้งาน");
    bindCopyButtons(tb);
  }
}

/* ---------------- public sample viewer ---------------- */
function loadSample(pid, name) {
  const key = `${pid}/${name}`;
  if (state.samples[key]) return Promise.resolve(state.samples[key]);
  if (_sampleInflight.has(key)) return _sampleInflight.get(key);
  const gen = _getGen;
  const p = apiGet(
    `/api/problems/${encodeURIComponent(pid)}/sample/${encodeURIComponent(name)}`)
    .then((d) => {
      if (gen === _getGen) state.samples[key] = d;
      return d;
    })
    .finally(() => {
      if (_sampleInflight.get(key) === p) _sampleInflight.delete(key);
    });
  _sampleInflight.set(key, p);
  return p;
}

function prefetchSamples(p) {
  // Public samples are small and there are at most a few per problem. Warming
  // them after detail render makes the first click instant without delaying
  // the statement itself.
  (p.samples || []).slice(0, 4).forEach((name) => {
    loadSample(p.id, name).catch(() => { /* click retries or reports */ });
  });
}

function renderSampleButtons(p) {
  const box = $("sample-btns");
  const names = p.samples || [];
  if (!names.length) {
    box.innerHTML = `<span class="dim">ข้อนี้ไม่มีตัวอย่างสาธารณะ</span>`;
    $("sample-view").innerHTML = "";
    return;
  }
  box.innerHTML = names.map((n) =>
    `<button class="chip-btn" type="button" data-sample="${esc(n)}">ตัวอย่าง ${esc(n)}</button>`
  ).join("");
  box.querySelectorAll("[data-sample]").forEach((b) => {
    b.onclick = () => showSample(p.id, b.dataset.sample, b);
  });
  $("sample-view").innerHTML = `<span class="dim">เลือกตัวอย่างด้านบนเพื่อดู input / output</span>`;
}

async function showSample(pid, name, btn) {
  const view = $("sample-view");
  const gen = _getGen;
  document.querySelectorAll("#sample-btns [data-sample]").forEach((b) =>
    b.classList.toggle("active", b === btn));
  const key = `${pid}/${name}`;
  if (state.samples[key]) {
    renderSample(state.samples[key]);
    return;
  }
  view.innerHTML = `<span class="dim">กำลังโหลดตัวอย่าง ${esc(name)}...</span>`;
  try {
    const d = await loadSample(pid, name);
    if (gen !== _getGen || state.current !== pid) return;
    renderSample(d);
  } catch (e) {
    view.innerHTML = `<span class="dim">โหลดตัวอย่างไม่ได้: ${esc(e.message)}</span>`;
  }
}

function ioBlockHtml(title, text) {
  const t = text == null ? "" : String(text);
  return `<div class="io-block">` +
    `<div class="io-head"><span class="io-title">${esc(title)}</span>` +
    `<button class="icon-btn" type="button" data-copy title="คัดลอก">${icon("copy")}</button></div>` +
    `<pre class="io-pre">${esc(t)}</pre></div>`;
}

function renderSample(d) {
  const view = $("sample-view");
  view.innerHTML = `<div class="io-grid">` +
    ioBlockHtml(`input (${d.test})`, d.input) +
    ioBlockHtml("output", d.output) + `</div>`;
  bindCopyButtons(view);
}

async function copyText(text, btn) {
  let done = false;
  try {
    await navigator.clipboard.writeText(text);
    done = true;
  } catch (e) {
    done = false;
  }
  if (!btn) return;
  const prev = btn.title;
  btn.title = done ? "คัดลอกแล้ว" : "คัดลอกไม่ได้";
  btn.classList.toggle("copied", done);
  setTimeout(() => {
    btn.title = prev;
    btn.classList.remove("copied");
  }, 1200);
}

function bindCopyButtons(root) {
  root.querySelectorAll("button[data-copy]").forEach((btn) => {
    btn.onclick = () => {
      const box = btn.closest(".io-block,.code-copy-wrap");
      const pre = box && box.querySelector("pre");
      copyText(pre ? pre.textContent : "", btn);
    };
  });
}

function getProblemDetail(pid) {
  const hit = state.detailCache[pid];
  if (hit && Date.now() - hit.t < DETAIL_CACHE_TTL) return Promise.resolve(hit.data);
  if (_detailInflight.has(pid)) return _detailInflight.get(pid);
  const gen = _getGen;
  const pending = apiGet(`/api/problems/${encodeURIComponent(pid)}`).then((p) => {
    if (gen === _getGen) state.detailCache[pid] = { t: Date.now(), data: p };
    return p;
  }).finally(() => {
    if (_detailInflight.get(pid) === pending) _detailInflight.delete(pid);
  });
  _detailInflight.set(pid, pending);
  return pending;
}

function prefetchDetail(pid) {
  getProblemDetail(pid).catch(() => { /* navigation will retry */ });
}

async function selectProblem(pid) {
  saveDraft();                      // keep the draft of the problem we are leaving
  state.current = pid;
  setTaskTab("statement");
  // The list already knows whether a PDF exists. Start the frame immediately
  // instead of waiting for the detail round trip; detail still has the final
  // say and hides the frame if the row was stale.
  const listed = state.problems.find((p) => p.id === pid);
  const oldFrame = document.getElementById("pdf-frame");
  if (listed && listed.has_pdf) {
    showPdfFrame(`${API}/api/problems/${encodeURIComponent(pid)}/pdf`);
    $("task-body").classList.add("hidden");
  } else if (oldFrame) {
    oldFrame.classList.add("hidden");
    $("task-body").classList.remove("hidden");
  }
  // Per-problem workspace: this problem's draft code, no leftover attached file.
  const draft = draftFor(pid);
  setEditorValue(draft);
  setDraftHint(!!draft.trim());
  state.file = null;
  fi.value = "";
  $("file-chip").classList.add("hidden");
  $("submit-status").textContent = "";
  shareEditor("submit");
  $("sample-btns").innerHTML = "";
  $("sample-view").innerHTML = `<span class="dim">เลือกตัวอย่างด้านบนเพื่อดู input / output</span>`;
  renderContestBanner();
  const hit = state.detailCache[pid];
  const fresh = hit && Date.now() - hit.t < DETAIL_CACHE_TTL;
  if (fresh) {
    fillTaskDetail(hit.data);
  } else {
    $("task-title").textContent = "Loading...";
    $("task-body").textContent = "Loading problem...";
  }
  if (state.jobResults[pid]) {      // verdict of this session's last run on this problem
    renderResults(state.jobResults[pid].result, state.jobResults[pid].secs);
  } else {
    $("result-body").innerHTML = `<div class="feedback-empty"><svg class="ic" aria-hidden="true"><use href="#i-check"/></svg><h3>พร้อมตรวจคำตอบ</h3><p>ส่งโค้ดแล้วดูคะแนน เวลา และผลรายเทสต์ได้ตรงนี้</p></div>`;
    $("score-line").textContent = "";
  }
  $("your-score-num").textContent = "—";
  $("report-status").textContent = "";
  $("report-msg").value = "";
  const mySeq = (state.taskSeq = (state.taskSeq || 0) + 1);
  const pending = lastJobFor(pid);
  if (pending) resumeJob(pid, pending.id);   // keep waiting for an in-flight run
  try {
    const p = await getProblemDetail(pid);
    if (mySeq !== state.taskSeq) return;   // user moved to another problem: stale response
    if (!fresh) fillTaskDetail(p);
    refreshYourScore();
  } catch (e) {
    if (mySeq !== state.taskSeq) return;
    if (!fresh) $("task-body").textContent = "โหลดโจทย์ไม่สำเร็จ: " + e.message;
  }
}

async function refreshYourScore() {
  const gen = _getGen, pid = state.current;
  if (!state.current || !state.user) {
    $("your-score-num").textContent = state.user ? "0" : "—";
    return;
  }
  try {
    const h = await apiGetCached(`/api/history?limit=100&problem=${encodeURIComponent(state.current)}&mine=1`, 10000);
    if (gen !== _getGen || pid !== state.current) return;
    const rows = (h.configured && h.submissions) || [];
    const best = rows.reduce((m, r) => Math.max(m, r.score || 0), 0);
    const ysn = $("your-score-num");
    ysn.textContent = rows.length ? `${best}/100` : "0";
    ysn.className = scoreCls(best);
  } catch (e) {
    if (gen !== _getGen || pid !== state.current) return;
    $("your-score-num").textContent = "?";
  }
}
async function loadTaskSubs() {
  const gen = _getGen, pid = state.current;
  const body = $("task-all-body");
  if (!state.current) return;
  body.innerHTML = skel(4);
  try {
    // Public per-problem feed: safe to reuse briefly; clearGetCache() runs after
    // this user submits, so their own new row is never hidden by the cache.
    const h = await apiGetCached(`/api/history?limit=50&problem=${encodeURIComponent(state.current)}`, 10000);
    if (gen !== _getGen || pid !== state.current) return;
    if (!h.configured) {
      body.innerHTML = `<span class="dim">ยังไม่เปิด submissions (ตั้งค่า Supabase ที่ backend)</span>`;
      $("task-top-body").innerHTML = `<span class="dim">—</span>`;
      return;
    }
    const rows = h.submissions || [];
    renderTopScores(rows);
    const accessNote = `<p class="dim">${esc(peerAccessMessage(h.peer_access))}</p>`;
    if (!rows.length) {
      body.innerHTML = accessNote + `<span class="dim">ข้อนี้ยังไม่มีใครส่ง — เป็นคนแรกเลย</span>`;
      return;
    }
    body.classList.remove("dim");
    body.innerHTML = accessNote + historyTable(rows, false);
    bindViewButtons(body);
  } catch (e) {
    if (gen !== _getGen || pid !== state.current) return;
    body.innerHTML = `<span class="dim">ดูไม่ได้: ${esc(e.message)}</span>`;
  }
}
function renderTopScores(rows) {
  const body = $("task-top-body");
  const best = new Map();
  rows.forEach((r) => {
    const key = (r.name || "—") + "\u0000" + r.language;
    const cur = best.get(key);
    if (!cur || (r.score || 0) > cur.score) {
      best.set(key, { name: r.name || "—", language: r.language, score: r.score || 0 });
    }
  });
  const top = [...best.values()].sort((a, b) => b.score - a.score).slice(0, 10);
  if (!top.length) {
    body.innerHTML = `<span class="dim">—</span>`;
    return;
  }
  let html = `<table class="tests"><thead><tr>
    <th>#</th><th>ชื่อ</th><th>ภาษา</th><th>ดีสุด</th>
  </tr></thead><tbody>`;
  top.forEach((t, i) => {
    const medal = i < 3 ? `<span class="rank rank-${i + 1}">${i + 1}</span>` : `${i + 1}`;
    html += `<tr><td>${medal}</td><td>${esc(t.name)}</td><td>${esc(t.language)}</td><td><strong class="${scoreCls(t.score)}">${t.score}/100</strong></td></tr>`;
  });
  body.classList.remove("dim");
  body.innerHTML = html + `</tbody></table>`;
}

async function loadTaskMine() {
  const gen = _getGen, pid = state.current;
  const body = $("task-mine-body");
  if (!state.current) return;
  if (!state.user) {
    body.innerHTML = `<div class="empty-state"><h3>ดูการส่งของคุณ</h3><p>เข้าสู่ระบบเพื่อดูประวัติและผลตรวจของโจทย์ข้อนี้</p><a class="btn-ghost" href="#/login">เข้าสู่ระบบ</a></div>`;
    return;
  }
  body.innerHTML = skel(4);
  try {
    const h = await apiGetCached(`/api/history?limit=100&problem=${encodeURIComponent(state.current)}&mine=1`, 10000);
    if (gen !== _getGen || pid !== state.current) return;
    const rows = (h.configured && h.submissions) || [];
    if (!rows.length) {
      body.innerHTML = `<div class="empty-state"><h3>ยังไม่เคยส่งข้อนี้</h3><p>เปิดแท็บส่งโค้ดเพื่อส่งคำตอบแรก แล้วกลับมาดูผลตรวจได้ที่นี่</p></div>`;
      return;
    }
    body.classList.remove("dim");
    body.innerHTML = historyTable(rows, false);
    bindViewButtons(body);
  } catch (e) {
    if (gen !== _getGen || pid !== state.current) return;
    body.innerHTML = `<span class="dim">ดูไม่ได้: ${esc(e.message)}</span>`;
  }
}
/* ---------------- auth ---------------- */
async function refreshMe() {
  const seq = state.meSeq = (state.meSeq || 0) + 1;
  const previousIdentity = JSON.stringify([state.user && state.user.email, !!state.is_admin]);
  let embedded = null;
  let restoreAvailable;
  try {
    const me = await apiGet("/api/auth/me");
    if (seq !== state.meSeq) return;
    state.authError = false;
    state.user = (me && me.user) || null;
    state.is_admin = state.isAdmin = !!(me && me.is_admin);
    embedded = me && me.profile ? me : null;
    restoreAvailable = me && me.restore_available;
  } catch (e) {
    if (seq !== state.meSeq) return;
    state.authError = true;
    state.user = null;
    state.is_admin = state.isAdmin = false;
  }
  if (previousIdentity !== JSON.stringify([state.user && state.user.email, !!state.is_admin])) {
    clearGetCache();
    state.problems = problemRowsForIdentity(state.problems);
    state.contests = [];
    state.contestCache = {};
    if (state.user && state.authReady) await Promise.all([refreshProblems(), fetchStats()]);
  }
  state.myBestLoaded = false;
  state.myBest = {};
  $("login-btn").classList.toggle("hidden", !!state.user);
  $("signup-btn").classList.toggle("hidden", !!state.user);
  $("logout-btn").classList.toggle("hidden", !state.user);
  const navAdmin = $("nav-admin");
  if (navAdmin) navAdmin.classList.toggle("hidden", !state.is_admin);
  if (state.authReady && !state.is_admin && (location.hash || "") === "#/admin") renderAdmin();
  const chip = $("user-chip");
  chip.classList.toggle("hidden", !state.user);
  if (state.user) {
    let display = state.user.name || state.user.email;
    // profile rides along with /me (one round trip); fall back to the
    // dedicated endpoint only for stale backends without it.
    let prof = embedded && embedded.profile ? embedded.profile : null;
    if (!prof) {
      try {
        const data = await apiGet("/api/profile");
        prof = data.profile || null;
      } catch (e) { /* ignore */ }
    }
    if (seq !== state.meSeq) return;
    if (prof) {
      if (prof.display_name) display = prof.display_name;
      if (prof.default_language) setLang(prof.default_language);
    }
    chip.textContent = display;
    chip.title = state.user.email || "";
  }
  return { restore_available: restoreAvailable };
}

async function restoreInitialSession() {
  const me = await refreshMe();
  // A missing session cookie is not a final denial until remember-me is checked.
  // Older backends omit the hint: retain their existing restoration flow.
  if (!state.user && (!me || me.restore_available !== false)) {
    try {
      const r = await apiGet("/api/auth/restore");
      if (r && r.restored) await refreshMe();
    } catch (e) { state.authError = true; }
  }
  state.authReady = true;
}

$("login-btn").onclick = () => {
  state.authReturnTo = "#/tasks";
  location.hash = "#/login";
};

$("signup-btn").onclick = () => {
  location.hash = "#/login/register";
};

async function signOut() {
  const button = $("logout-btn");
  button.disabled = true;
  try {
    await apiPostJson("/api/auth/logout", {});
  } catch (e) {
    toast("ออกจากระบบไม่สำเร็จ กรุณาลองอีกครั้ง", "t-err");
    button.disabled = false;
    return;
  }
  button.disabled = false;
  // Identity changed: the cached problem list (and the solved marks) must not
  // survive into the next session on a shared machine. Same for drafts and
  // remembered job ids — a school computer is shared.
  try { localStorage.removeItem(PROBLEMS_CACHE_KEY); } catch (e) { /* ignore */ }
  clearGetCache();   // GET cache holds per-user answers (history?mine=1): must not leak to the next login
  clearLocalWorkspace();
  setEditorValue("");
  $("run-input").value = "";
  fi.value = "";
  state.file = null;
  state.current = null;
  $("file-chip").classList.add("hidden");
  setDraftHint(false);
  state.jobResults = {};
  await refreshMe();
  state.problems = [];
  state.stats = {};
  state.statsLoaded = false;
  location.hash = "#/";
  await loadPublicHome();
  renderHome();
  toast("ออกจากระบบแล้ว");
}
$("logout-btn").onclick = signOut;

/* ---------------- admin overview ---------------- */
function uptimeText(s) {
  s = Number(s) || 0;
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return (d ? `${d}d ` : "") + (h ? `${h}h ` : "") + `${m}m`;
}

function overviewKpi(iconName, label, value, note, cls) {
  return `<div class="kpi${cls ? " " + cls : ""}">` +
    `<div class="kpi-top"><span class="kpi-icon">${icon(iconName)}</span>` +
    `<span class="kpi-label">${esc(label)}</span></div>` +
    `<div class="kpi-value">${esc(String(value))}</div>` +
    (note ? `<div class="kpi-note">${esc(note)}</div>` : "") +
    `</div>`;
}

function setDashCount(id, value) {
  const el = $(id);
  if (el) el.textContent = value;
}

async function loadOverview() {
  const stats = $("overview-stats"), probs = $("overview-problems"), recent = $("overview-recent");
  const hiddenBox = $("overview-hidden"), health = $("overview-health"), meta = $("overview-meta");
  [stats, probs, recent, hiddenBox].forEach((el) => {
    if (!el) return;
    el.classList.add("dim");
    el.innerHTML = skel(3);
  });
  if (health) health.innerHTML = "";
  try {
    const d = await apiGet("/api/admin/overview");
    const j = d.jobs || {};
    const sys = d.system || {};
    const depth = j.queue_depth ?? j.queued ?? 0;
    const queueFull = !!j.queue_full;
    const queueAlert = !!j.queue_alert || queueFull;
    const reportsOpen = Number(d.reports_open) || 0;
    const subs = Number(d.submissions) || 0;
    const accepted = Number(d.accepted) || 0;
    const rate = subs ? Math.round((100 * accepted) / subs) : 0;
    const contestOnly = (d.problems_contest_only || []).length;

    if (meta) {
      meta.textContent = `อัปเดต ${new Date().toLocaleTimeString("th-TH", { hour: "2-digit", minute: "2-digit" })}` +
        ` · uptime ${uptimeText(sys.uptime_s)}`;
    }

    if (health) {
      const banners = [];
      if (queueAlert) {
        banners.push(`<div class="dash-banner dash-banner-warn" role="alert">${icon("alert")}` +
          `<div><strong>คิวตรวจ${queueFull ? "เต็ม" : "ค้าง"}</strong>` +
          `<span>${esc(String(depth))} งานค้าง${queueFull ? " — ผู้ใช้ใหม่จะได้ 503" : " — งานใหม่อาจรอนาน"}</span>` +
          `</div></div>`);
      } else {
        banners.push(`<div class="dash-banner dash-banner-ok">${icon("check")}` +
          `<div><strong>คิวตรวจทำงานปกติ</strong>` +
          `<span>${esc(String(depth))} งานรอ · ${esc(String(j.running ?? 0))} กำลังตรวจ · ${esc(String(j.workers ?? 0))} workers</span>` +
          `</div></div>`);
      }
      if (reportsOpen) {
        banners.push(`<div class="dash-banner dash-banner-info">${icon("flag")}` +
          `<div><strong>มีรีพอร์ตค้าง ${reportsOpen} รายการ</strong><span>ตรวจสอบและปิดรายการได้ที่แท็บ “รีพอร์ต”</span></div>` +
          `<button class="ghost-btn" type="button" data-goto-tab="reports">เปิดรีพอร์ต</button></div>`);
      }
      health.innerHTML = banners.join("");
      health.querySelectorAll("[data-goto-tab]").forEach((b) => {
        b.onclick = () => showAdminTab(b.dataset.gotoTab);
      });
    }

    stats.classList.remove("dim");
    stats.innerHTML =
      overviewKpi("tasks", "โจทย์", d.problems, contestOnly ? `${contestOnly} ข้อซ่อนในคอนเทสต์` : "ทั้งหมดในระบบ") +
      overviewKpi("users", "ผู้ใช้", d.users, "บัญชีที่ลงทะเบียน") +
      overviewKpi("send", "คำตอบ", d.submissions, "ทุกการส่งในระบบ") +
      overviewKpi("check", "ผ่าน", d.accepted, `${rate}% อัตราผ่าน`, rate >= 50 ? "is-ok" : "") +
      overviewKpi("queue", "งานรอคิว", j.queued ?? 0, "queued") +
      overviewKpi("refresh", "กำลังตรวจ", j.running ?? 0, "running") +
      overviewKpi("alert", "คิวค้าง", depth, queueFull ? "คิวเต็ม" : "รอตรวจ", queueAlert ? "is-warn" : "") +
      overviewKpi("server", "Workers", j.workers ?? 0, "ตัวช่วยตรวจ");

    const pp = d.per_problem || {};
    const entries = Object.entries(pp).sort((a, b) =>
      (Number(b[1].subs) || 0) - (Number(a[1].subs) || 0));
    probs.classList.remove("dim");
    setDashCount("overview-problems-count", `${entries.length} ข้อ`);
    probs.innerHTML = entries.length
      ? `<table class="tests dash-table"><thead><tr>
          <th>ข้อ</th><th class="num">ส่ง</th><th class="num">ผ่าน</th><th class="num">ดีสุด</th><th>อัตราผ่าน</th>
        </tr></thead><tbody>` + entries.map(([pid, v]) => {
          const nSubs = Number(v.subs) || 0;
          const nAccepted = Number(v.accepted) || 0;
          const pct = nSubs ? Math.round((100 * nAccepted) / nSubs) : 0;
          const cls = pct >= 60 ? "" : pct >= 30 ? " mid" : " low";
          return `<tr><td>${problemCell(pid)}</td><td class="num">${nSubs}</td><td class="num">${nAccepted}</td>` +
            `<td class="num">${esc(String(v.best ?? "—"))}</td>` +
            `<td><div class="rate-cell"><span class="rate-track"><span class="rate-fill${cls}" style="width:${pct}%"></span></span>` +
            `<span class="rate-num">${pct}%</span></div></td></tr>`;
        }).join("") + `</tbody></table>`
      : `<p class="dash-empty">ยังไม่มีข้อมูลการส่ง — สถิติจะขึ้นหลังมีคนส่งคำตอบ</p>`;

    const rows = d.recent || [];
    setDashCount("overview-recent-count", `${rows.length} รายการ`);
    recent.classList.remove("dim");
    recent.innerHTML = rows.length ? `<table class="tests dash-table"><thead><tr>
        <th>เวลา</th><th>ข้อ</th><th>ผู้ส่ง</th><th>ผล</th><th class="num">คะแนน</th>
      </tr></thead><tbody>` + rows.map((r) =>
      `<tr><td class="dim">${esc(shortTime(r.created_at))}</td><td>${problemCell(r.problem)}</td>` +
      `<td>${esc(r.name || "—")}</td><td>${badgeVerdict(rowVerdictCode(r))}</td>` +
      `<td class="num"><span class="${scoreCls(r.score)}">${esc(String(r.score ?? ""))}</span></td></tr>`
    ).join("") + `</tbody></table>` : `<p class="dash-empty">ยังไม่มีการส่ง</p>`;

    const hidList = d.problems_contest_only || [];
    setDashCount("overview-hidden-count", `${hidList.length} ข้อ`);
    hiddenBox.classList.remove("dim");
    hiddenBox.innerHTML = hidList.length
      ? `<table class="tests dash-table"><thead><tr>
          <th>ข้อ</th><th>ชื่อ</th><th>ที่มา</th><th></th>
        </tr></thead><tbody>` + hidList.map((h) => {
          const src = [];
          if (h.config_hidden) src.push("config hidden");
          if (h.contest_hidden) src.push(h.in_contest ? "ในคอนเทสต์" : "คอนเทสต์ (ไม่มีแล้ว?)");
          return `<tr><td>${esc(h.id)}</td><td>${esc(h.title)}</td>` +
            `<td class="dim">${esc(src.join(" · ") || "—")}</td>` +
            `<td><a class="tlink" href="#/task/${encodeURIComponent(h.id)}">เปิด</a></td></tr>`;
        }).join("") + `</tbody></table>` +
        `<p class="dash-note">โจทย์เหล่านี้ไม่ขึ้นในหน้า Task ของผู้ใช้ทั่วไป — เข้าถึงได้จากหน้าคอนเทสต์และจากตรงนี้</p>`
      : `<p class="dash-empty">ไม่มีโจทย์ที่ซ่อนอยู่ — โจทย์ทุกข้อแสดงในหน้า Task ปกติ</p>`;
  } catch (e) {
    const err = `<p class="dash-empty dash-error">โหลดไม่ได้: ${esc(e.message)}</p>`;
    [stats, probs, recent, hiddenBox].forEach((el) => {
      if (!el) return;
      el.classList.remove("dim");
      el.innerHTML = err;
    });
    if (meta) meta.textContent = "โหลดข้อมูลภาพรวมไม่สำเร็จ";
    if (health) {
      health.innerHTML = `<div class="dash-banner dash-banner-err" role="alert">${icon("alert")}` +
        `<div><strong>เชื่อมต่อข้อมูลภาพรวมไม่ได้</strong><span>${esc(e.message)}</span></div></div>`;
    }
  }
}

/* ---------------- report ---------------- */
async function loadMyReports() {
  const box = $("report-mine");
  if (!box || !state.current) return;
  if (!state.user) {
    box.innerHTML = `<span class="dim">login แล้วจะเห็นรีพอร์ตที่เคยส่งข้อนี้</span>`;
    return;
  }
  try {
    const d = await apiGet(`/api/reports?mine=1&problem=${encodeURIComponent(state.current)}&limit=10`);
    const rows = (d.configured && d.reports) || [];
    box.innerHTML = rows.length
      ? rows.map((r) => `<div class="report-item">[${esc(r.kind)}] ${esc(r.message)} — <span class="dim">${esc(r.status)} · ${esc(new Date(r.created_at).toLocaleString())}</span></div>`).join("")
      : `<span class="dim">ยังไม่เคยรีพอร์ตข้อนี้</span>`;
  } catch (e) {
    box.innerHTML = `<span class="dim">โหลดไม่ได้</span>`;
  }
}

async function loadAdminReports() {
  const box = $("admin-reports");
  if (!box) return;
  box.classList.add("dim");
  box.innerHTML = skel(3);
  try {
    const d = await apiGet("/api/reports?all=1&limit=50");
    const rows = (d.configured && d.reports) || [];
    setDashCount("admin-reports-count", `${rows.length} รายการ`);
    box.classList.remove("dim");
    if (!rows.length) {
      box.innerHTML = `<p class="dash-empty">ไม่มีรีพอร์ตค้าง — ทุกอย่างเรียบร้อย</p>`;
      return;
    }
    box.innerHTML = rows.map((r) => {
      const open = r.status === "open";
      return `<article class="admin-report">` +
        `<div class="admin-report-head">` +
        `<span class="admin-report-problem">${esc(r.problem)}</span>` +
        `<span class="diff-tag">${esc(r.kind)}</span>` +
        badge(open ? "ค้าง" : "ปิดแล้ว", open ? "upcoming" : "OK") +
        (open ? `<button class="ghost-btn" type="button" data-close="${esc(r.id)}">ปิดรายการ</button>` : "") +
        `</div>` +
        `<p class="admin-report-msg">${esc(r.message)}</p>` +
        `<div class="admin-report-meta">` +
        `<span>${esc(r.name || "ไม่ระบุผู้ส่ง")}</span>` +
        `<span>${esc(shortTime(r.created_at))}</span><span>status: ${esc(r.status)}</span>` +
        `</div></article>`;
    }).join("");
    box.querySelectorAll("[data-close]").forEach((b) => {
      b.onclick = async () => {
        b.disabled = true;
        try {
          const res = await fetch(API + `/api/reports/${encodeURIComponent(b.dataset.close)}`, {
            method: "PUT", credentials: "include",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ status: "closed" }),
          });
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          toast("ปิดรีพอร์ตแล้ว", "t-ok");
          loadAdminReports();
        } catch (e) {
          b.disabled = false;
          toast("ปิดรีพอร์ตไม่สำเร็จ: " + e.message, "t-err");
        }
      };
    });
  } catch (e) {
    box.classList.remove("dim");
    setDashCount("admin-reports-count", "—");
    box.innerHTML = `<p class="dash-empty dash-error">โหลดไม่ได้: ${esc(e.message)}</p>`;
  }
}

/* ---------------- password auth ---------------- */
let authMode = "login";
let authPendingEmail = "";
let authBusy = false;

function renderAuthStep() {
  const verifying = !!authPendingEmail;
  $("auth-heading").textContent = verifying ? "ยืนยันอีเมล" : authMode === "login" ? "เข้าสู่ระบบ" : "สร้างบัญชี";
  $("auth-description").textContent = verifying ? "กรอกรหัสที่ได้รับทางอีเมล แล้วตั้งรหัสผ่านสำหรับบัญชีของคุณ" :
    authMode === "login" ? "เข้าสู่ระบบเพื่อเปิดโจทย์ ส่งคำตอบ และดูประวัติการฝึกของคุณ" : "ยืนยันอีเมลของคุณเพื่อเริ่มส่งคำตอบและเก็บประวัติการฝึก";
  $("auth-fields").setAttribute("aria-labelledby", authMode === "login" ? "auth-tab-login" : "auth-tab-register");
  $("row-name").style.display = verifying ? "" : "none";
  $("row-auth-code").classList.toggle("hidden", !verifying);
  $("row-auth-pass").classList.toggle("hidden", authMode !== "login" && !verifying);
  $("auth-email").readOnly = verifying;
  $("auth-pass").autocomplete = authMode === "login" ? "current-password" : "new-password";
  $("auth-go").textContent = verifying ? "ยืนยันอีเมลและตั้งรหัสผ่าน" :
    (authMode === "login" ? "เข้าสู่ระบบ" : "ส่งรหัสยืนยันอีเมล");
  $("auth-resend").classList.toggle("hidden", !verifying);
  $("auth-recover").classList.toggle("hidden", authMode !== "login");
  globalThis.GraderBotVerification?.prepare(authMode === "register" && !verifying);
}

function setAuthMode(m) {
  if (authBusy) return;
  authMode = m;
  authPendingEmail = "";
  $("auth-pass").value = "";
  $("auth-code").value = "";
  document.querySelectorAll("[data-authtab]").forEach((b) => {
    const active = b.dataset.authtab === m;
    b.classList.toggle("active", active);
    b.setAttribute("aria-selected", String(active));
    b.tabIndex = active ? 0 : -1;
  });
  renderAuthStep();
  $("auth-err").classList.add("hidden");
}
document.querySelectorAll("[data-authtab]").forEach((b) => {
  b.onclick = () => setAuthMode(b.dataset.authtab);
  b.onkeydown = (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    if (authBusy) return;
    const mode = event.key === "Home" ? "login" : event.key === "End" ? "register" : authMode === "login" ? "register" : "login";
    setAuthMode(mode);
    document.querySelector(`[data-authtab="${mode}"]`).focus();
  };
});
$("auth-recover").onclick = () => setAuthMode("register");

async function doPasswordAuth(resend = false) {
  if (authBusy) return;
  const email = (authPendingEmail || $("auth-email").value).trim().toLowerCase();
  const password = $("auth-pass").value;
  const verifying = !!authPendingEmail && !resend;
  const err = $("auth-err");
  err.classList.remove("hidden");
  err.classList.remove("bad");
  if (!email || ((verifying || authMode === "login") && !password)) {
    err.classList.add("bad");
    err.textContent = !email ? "กรอกอีเมลของคุณ" : "กรอกรหัสผ่านของคุณ";
    return;
  }
  if (verifying && !/^[0-9]{8}$/.test($("auth-code").value.trim())) {
    err.classList.add("bad");
    err.textContent = "กรอกรหัสยืนยัน 8 หลักจากอีเมล";
    return;
  }
  const payload = { email };
  let path = "/api/auth/email/request";
  if (verifying) {
    path = "/api/auth/email/verify";
    Object.assign(payload, { code: $("auth-code").value.trim(), password,
      name: $("auth-name").value.trim(), remember: $("auth-remember").checked });
  } else if (authMode === "login") {
    path = "/api/auth/password";
    Object.assign(payload, { password, remember: $("auth-remember").checked });
  }
  authBusy = true;
  for (const id of ["auth-go", "auth-resend", "auth-recover", "google-btn"]) $(id).disabled = true;
  err.textContent = "กำลังดำเนินการ...";
  try {
    if (path === "/api/auth/email/request") {
      if (typeof globalThis.GraderBotVerification?.token !== "function") {
        throw new Error("ระบบตรวจสอบผู้ใช้งานยังไม่พร้อม กรุณาโหลดหน้าเว็บใหม่");
      }
      const token = await globalThis.GraderBotVerification.token();
      if (token) payload["cf-turnstile-response"] = token;
    }
    const r = await fetch(API + path, {
      method: "POST", credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      if (r.status === 403 && r.headers.get("X-Email-Verification-Required") === "true") {
        authBusy = false;
        setAuthMode("register");
        err.textContent = "บัญชีเดิมต้องยืนยันอีเมลและตั้งรหัสผ่านใหม่ กดส่งรหัสยืนยันเพื่อดำเนินการ";
        err.classList.remove("hidden");
        return;
      }
      if (r.status === 429) {
        err.classList.add("bad");
        err.textContent = "พยายามบ่อยเกินไป กรุณารอ " + (r.headers.get("Retry-After") || "60") + " วินาที";
        return;
      }
      throw new Error(r.status === 503 ? "ระบบยืนยันอีเมลไม่พร้อม กรุณาลองภายหลัง" :
        (d.detail || `HTTP ${r.status}`));
    }
    if (d.verification_required) {
      authPendingEmail = email;
      $("auth-email").value = email;
      $("auth-pass").value = "";
      $("auth-code").value = "";
      renderAuthStep();
      err.textContent = "ส่งคำขอรหัสยืนยันแล้ว กรุณาตรวจ Inbox และ Spam จากนั้นตั้งรหัสผ่านใหม่";
      $("auth-code").focus();
      return;
    }
    $("auth-pass").value = "";
    $("auth-code").value = "";
    authPendingEmail = "";
    await refreshMe();
    location.hash = loginDestination();
  } catch (e) {
    err.classList.add("bad");
    err.textContent = e.message;
  } finally {
    authBusy = false;
    if (path === "/api/auth/email/request") {
      globalThis.GraderBotVerification?.reset();
      renderAuthStep();
    }
    for (const id of ["auth-go", "auth-resend", "auth-recover", "google-btn"]) $(id).disabled = false;
  }
}
$("auth-resend").onclick = () => doPasswordAuth(true);

/* A versioned notice for essential cookies, not a fake consent toggle.
 * Acknowledgement is not an authentication requirement or tracking consent. */
const COOKIE_NOTICE_KEY = "ng_cookie_notice_v2";
let cookieNoticeAcknowledged = false;

function cookieNoticeSeen() {
  if (cookieNoticeAcknowledged) return true;
  try { return localStorage.getItem(COOKIE_NOTICE_KEY) === "acknowledged"; }
  catch (e) { return false; }
}

function fitCookieNotice() {
  const bar = $("cookie-banner");
  document.body.style.paddingBottom = bar.classList.contains("hidden")
    ? "" : `${bar.getBoundingClientRect().height + 32}px`;
}

function acknowledgeCookieNotice() {
  cookieNoticeAcknowledged = true;
  try { localStorage.setItem(COOKIE_NOTICE_KEY, "acknowledged"); } catch (e) { /* session-only fallback */ }
  $("cookie-banner").classList.add("hidden");
  fitCookieNotice();
  const source = $("cookie-settings");
  if (document.querySelector("#view-settings.active")) source.focus();
}

function maybeShowCookieBanner(force = false) {
  if (!force && cookieNoticeSeen()) return;
  const bar = $("cookie-banner");
  if (!bar) return;
  bar.classList.remove("hidden");
  $("cookie-ack").onclick = acknowledgeCookieNotice;
  $("cookie-more").onclick = () => {
    const details = $("cookie-details");
    const open = details.classList.toggle("hidden") === false;
    $("cookie-more").setAttribute("aria-expanded", String(open));
    $("cookie-more").textContent = open ? "ย่อรายละเอียด" : "รายละเอียด";
    fitCookieNotice();
  };
  fitCookieNotice();
  if (force) $("cookie-more").focus();
}
window.addEventListener("resize", fitCookieNotice);
window.addEventListener("storage", (event) => {
  if (event.key === COOKIE_NOTICE_KEY && event.newValue === "acknowledged") acknowledgeCookieNotice();
});

/* ---------------- language + file ---------------- */
document.querySelectorAll(".lang-btn").forEach((b) => {
  b.onclick = () => setLang(b.dataset.lang);
});

const dz = $("dropzone");
const fi = $("file-input");
dz.onclick = () => fi.click();
dz.onkeydown = (e) => {
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fi.click(); }
};
["dragover", "dragenter"].forEach((ev) =>
  dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("over"); })
);
["dragleave", "drop"].forEach((ev) =>
  dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("over"); })
);
dz.addEventListener("drop", (e) => {
  if (e.dataTransfer.files.length) setFile(e.dataTransfer.files[0]);
});
fi.onchange = () => { if (fi.files.length) setFile(fi.files[0]); };

function setFile(f) {
  state.file = f;
  $("file-name").textContent = `${f.name} (${f.size} bytes)`;
  $("file-chip").classList.remove("hidden");
}

$("file-clear").onclick = () => {
  state.file = null;
  fi.value = "";
  $("file-chip").classList.add("hidden");
};

$("file-load").onclick = async () => {
  if (!state.file) return;
  setEditorValue(await state.file.text());
  $("submit-status").textContent = "โหลดเนื้อหาไฟล์ลง editor แล้ว";
};

$("template-btn").onclick = () => {
  const t = (TEMPLATES[state.current] || {})[state.lang];
  if (t) {
    setEditorValue(t);
    $("submit-status").textContent = `โหลด template แล้ว (${state.current} / ${state.lang})`;
  } else {
    $("submit-status").textContent = "ข้อนี้ยังไม่มี template";
  }
};

/* ---------------- judging jobs (async submit) ---------------- */
// Keep active jobs responsive; deep queues still back off to reduce API load.
const POLL_MS = 700;
const POLL_MIN_MS = 600;
const POLL_CAP_MS = 4000;
const POLL_ACTIVE_CAP_MS = 1000;
const POLL_MAX_MS = 4 * 60 * 1000;
const POLL_NET_RETRIES = 8;   // transient network blips: retry, don't drop the job id
const sleep = (ms) => new Promise((res) => setTimeout(res, ms));

/* Poll one job until it is done/error. onTick sees every state so the caller
 * can keep the status area live. No cancel: the job keeps running server-side
 * either way, so the UI just waits (with a hard 4 minute cap). */
async function pollJob(jid, onTick) {
  const t0 = Date.now();
  let delay = POLL_MS;
  let previous = null;
  let netFails = 0;
  const timeout = () => {
    const err = new Error(
      "รอผลนานเกิน 4 นาที — งานยังตรวจอยู่เบื้องหลัง กลับมาดูผลได้ที่ History");
    err.timeout = true;
    err.job_id = jid;
    return err;
  };
  for (;;) {
    if (Date.now() - t0 >= POLL_MAX_MS) throw timeout();
    let job;
    try {
      job = await apiGet(`/api/jobs/${encodeURIComponent(jid)}`);
      netFails = 0;
    } catch (e) {
      if (e && e.status !== undefined) throw e;   // HTTP error: contract unchanged
      if (++netFails > POLL_NET_RETRIES) {
        const lost = new Error(
          "เครือข่ายขัดข้อง — งานยังตรวจอยู่เบื้องหลัง กลับมาดูผลได้ที่ History");
        lost.keepJob = true;
        lost.job_id = jid;
        throw lost;
      }
      await sleep(Math.min(POLL_MAX_MS - (Date.now() - t0),
        Math.min(POLL_CAP_MS, 1000 * 2 ** (netFails - 1))));
      continue;
    }
    if (onTick) onTick(job);
    if (job.status === "done" || job.status === "error") return job;
    const remaining = POLL_MAX_MS - (Date.now() - t0);
    if (remaining <= 0) throw timeout();
    // Back off while unchanged; react promptly to execution or queue progress.
    // A deep queue means each submission waits minutes: poll slower so status
    // checks don't add load to the judging host.
    const progressed = previous && (job.status !== previous.status ||
      (job.status === "queued" && Number(job.queued_ahead) < Number(previous.queued_ahead)));
    const cap = job.status === "running" ||
      (job.status === "queued" && !(Number(job.queued_ahead) > 0))
      ? POLL_ACTIVE_CAP_MS : POLL_CAP_MS;
    delay = !previous || progressed ? POLL_MS : Math.min(cap, delay * 1.5);
    if (job.status === "queued" && (Number(job.queued_ahead) || 0) > 0) {
      delay = Math.max(delay, 1500);
      if ((Number(job.queued_ahead) || 0) >= 5) delay = Math.max(delay, 3000);
    }
    previous = job;
    // Jitter prevents clients submitted together from polling in lockstep.
    const jittered = Math.max(POLL_MIN_MS, Math.min(cap, delay * (0.8 + Math.random() * 0.4)));
    await sleep(Math.min(remaining, jittered));
  }
}

/* ---- reattach to a job that was already submitted ----
 * A reload or a walk to another page must not hide a verdict: the server keeps
 * the job, so the UI only has to keep the id and go back to waiting on it. */
const polling = new Set();

async function resumeJob(pid, jid) {
  if (polling.has(jid)) return;
  polling.add(jid);
  // Never yank the user to the submit tab: they may have opened this problem
  // just to read the statement. The status line only shows on the submit tab.
  const onSubmitTab = () => state.current === pid && state.taskTab === "submit";
  if (onSubmitTab()) {
    setJudgeStatus("กำลังดึงผลงานที่ค้างอยู่...", true);
  }
  try {
    const t0 = performance.now();
    const done = await pollJob(jid, (j) => {
      if (onSubmitTab()) {
        setJudgeStatus(`${jobStateText(j) || "กำลังตรวจโค้ด"} — ${waitedText(t0)}`, true);
      }
    });
    if (done.status === "error") throw new Error(done.error || "ตรวจไม่สำเร็จ");
    state.jobResults[pid] = { result: done.result || {}, secs: null };
    forgetJob(pid);
    if (onSubmitTab()) {
      renderResults(state.jobResults[pid].result, null);
      setJudgeStatus("ผลล่าสุดของข้อนี้", false);
    }
  } catch (e) {
    if (e.timeout || e.keepJob) rememberJob(pid, jid);
    else forgetJob(pid);       // stale id: server restarted, or the job aged out
    if (onSubmitTab()) {
      setJudgeStatus(e.timeout || e.keepJob ? (e.message || "รอดูผลได้ที่ History")
        : "ดึงผลงานเดิมไม่ได้แล้ว — ดูผลย้อนหลังได้ที่ History", false);
    }
  } finally {
    polling.delete(jid);
  }
}

function jobStateText(job) {
  if (job.status === "queued") {
    if (job.queued_ahead == null || !Number.isFinite(Number(job.queued_ahead))) return "อยู่ในคิวตรวจ";
    const n = Number(job.queued_ahead) || 0;
    return n > 0 ? `อยู่ในคิวตรวจ (มีงานก่อนหน้า ${n} งาน)` : "อยู่ในคิวตรวจ (ถึงคิวเป็นคนแรก)";
  }
  if (job.status === "running") return "กำลังตรวจโค้ด";
  return "";
}

/* Judging takes a few seconds on a shared host: show that time is passing, so
 * nobody thinks the page hung and hits submit again. */
function waitedText(t0) {
  const s = Math.max(0, Math.round((performance.now() - t0) / 1000));
  return `รอมาแล้ว ${s} วินาที`;
}

function setJudgeStatus(msg, busy) {
  const el = $("submit-status");
  el.innerHTML = (busy ? `<span class="spinner" aria-hidden="true"></span>` : "") +
    `<span>${esc(msg)}</span>`;
}

/* ---------------- submit + verdict ---------------- */
$("submit-btn").onclick = doSubmit;

async function doSubmit() {
  if (!state.current) return;
  const pid = state.current;   // judging is async: the student may navigate away
  const btn = $("submit-btn");
  const code = $("editor").value;
  if (!state.user) {
    setJudgeStatus("login ก่อนส่ง");
    location.hash = "#/login";
    return;
  }
  if (!state.file && !code.trim()) {
    setJudgeStatus("วางโค้ดหรือแนบไฟล์ก่อนส่ง");
    return;
  }
  const fd = new FormData();
  fd.append("problem", state.current);
  fd.append("language", state.lang);
  fd.append("code", code);
  fd.append("name", state.user.name || "");
  if (state.contest && state.contest.id) fd.append("contest", state.contest.id);
  if (state.file) fd.append("file", state.file, state.file.name);

  btn.disabled = true;
  setJudgeStatus(`กำลังส่ง ${state.current} (${state.lang})...`, true);
  $("result-body").innerHTML = `<span class="dim">กำลังเข้าคิวตรวจ...</span>`;
  const t0 = performance.now();
  try {
    // 202 {job_id, status:"queued", ...} — judging happens in the background.
    const job = await apiPostForm("/api/submit", fd);
    rememberJob(pid, job.job_id);   // survives reload and navigation
    setJudgeStatus(jobStateText(job) || "อยู่ในคิว", true);
    const done = await pollJob(job.job_id, (j) => {
      setJudgeStatus(`${jobStateText(j) || "กำลังตรวจโค้ด"} — ${waitedText(t0)}`, true);
    });
    if (done.status === "error") {
      forgetJob(pid);
      if (state.current === pid) {
        $("result-body").innerHTML =
          `<div class="score-big bad">ตรวจไม่สำเร็จ</div>` +
          `<pre class="dim">${esc(done.error || "unknown error")}</pre>`;
        setJudgeStatus(`error: ${done.error || "unknown"}`, false);
      }
      return;
    }
    const secs = (performance.now() - t0) / 1000;
    const result = done.result || {};
    // Keep the verdict for this problem: coming back must not lose it.
    state.jobResults[pid] = { result: result, secs: secs };
    forgetJob(pid);
    invalidateGetCache();     // history/stats changed, but the identity did not
    if (state.current === pid && document.querySelector("#view-task.active")) {
      renderResults(result, secs);
      $("result-body").scrollIntoView({ behavior: "smooth", block: "start" });
      setJudgeStatus(`เสร็จใน ${secs.toFixed(2)}s — ${result.score}/100 คะแนน`, false);
      toast(`ตรวจเสร็จ: ${result.score}/100 คะแนน`, result.score === 100 ? "t-ok" : "");
    }
    await fetchStats();
    state.myBestLoaded = false;
    renderTasksTable();
    refreshYourScore();
    if (state.taskTab === "subs") loadTaskSubs();
    if (state.taskTab === "mine") loadTaskMine();
    if (document.querySelector("#view-history.active")) loadHistory();
  } catch (e) {
    // A timeout (or a lost network) means the job is still queued/running
    // server-side: keep the id so returning to this problem picks it back up.
    if ((e.timeout || e.keepJob) && e.job_id) rememberJob(pid, e.job_id);
    else forgetJob(pid);
    if (state.current !== pid) return;   // student moved on: nothing to show here
    if (e.timeout || e.keepJob) {
      $("result-body").innerHTML =
        `<div class="score-big bad">หมดเวลารอผล</div>` +
        `<pre class="dim">${esc(e.message)}${e.job_id ? `\njob: ${esc(String(e.job_id))}` : ""}</pre>`;
      setJudgeStatus(e.message, false);
    } else if (e.status === 401) {
      // server detail verbatim ("login first")
      setJudgeStatus(e.message, false);
      $("result-body").innerHTML =
        `<div class="score-big bad">ส่งไม่สำเร็จ</div><pre class="dim">${esc(e.message)}</pre>` +
        `<p class="dim"><a href="#/login">เข้าสู่ระบบก่อนส่ง</a></p>`;
    } else {
      // 409 (job in flight) / 429 (quota) / 400 / 403: show detail verbatim
      $("result-body").innerHTML =
        `<div class="score-big bad">ส่งไม่สำเร็จ</div><pre class="dim">${esc(e.message)}</pre>`;
      const extra = (e.status === 409 || e.status === 429) && e.retryAfter
        ? ` (ลองใหม่ใน ${esc(String(e.retryAfter))} วินาที)` : "";
      setJudgeStatus(`error: ${e.message}${extra}`, false);
    }
  } finally {
    btn.disabled = false;
  }
}

/* The verdict label is the judge's English abbreviation. The constant name is
 * historical; do not replace these values with Thai words. */
const VERDICT_TH = {
  AC: "AC", WA: "WA", TLE: "TLE", MLE: "MLE",
  OLE: "OLE", RE: "RE", CE: "CE",
  OK: "OK", SKIP: "SKIP", "": "—",
};

function badgeVerdict(v) {
  return `<span class="badge b-${esc(v)}" title="${esc(v)}">${esc(VERDICT_TH[v] || v)}</span>`;
}

/* Colour the unified diff ourselves: the server strips ANSI now, and a student
 * scanning for the first wrong line should not have to read the whole block. */
function diffHtml(text) {
  const lines = String(text || "").split("\n").map((line) => {
    const cls = line.startsWith("+") && !line.startsWith("+++") ? "d-add"
      : line.startsWith("-") && !line.startsWith("---") ? "d-del"
      : line.startsWith("@@") ? "d-hunk" : "d-ctx";
    return `<span class="${cls}">${esc(line)}</span>`;
  });
  return `<pre class="diff-pre">${lines.join("\n")}</pre>`;
}

/* One sentence that answers "แล้วฉันต้องแก้ตรงไหน" before the test table. */
function verdictSummary(d) {
  const rows = d.results || [];
  if (d.verdict === "CE") {
    return `<p class="verdict-sum bad">คอมไพล์ไม่ผ่าน — ดูข้อความ error ข้างล่างนี้ก่อนส่งใหม่</p>`;
  }
  const bad = rows.filter((r) => r.verdict !== "AC");
  if (!bad.length) return `<p class="verdict-sum ok">ผ่านครบทุกเทสต์</p>`;
  const first = bad[0];
  const where = `เทสต์ ${rows.indexOf(first) + 1}`;
  const kinds = Object.keys(d.counts || {}).filter((k) => k !== "AC");
  const scope = kinds.length > 1
    ? kinds.map((k) => `${VERDICT_TH[k] || k} ${d.counts[k]} ข้อ`).join(" · ")
    : `${bad.length} จาก ${rows.length} ข้อ`;
  return `<p class="verdict-sum">เริ่มแก้ที่ <strong>${esc(where)}</strong> — ` +
    `${esc(VERDICT_TH[first.verdict] || first.verdict)}` +
    ` <span class="dim">(${esc(scope)})</span>` +
    `<br><span class="dim">${esc(verdictHint(first))}</span></p>`;
}

function renderResults(d, secs) {
  $("score-line").textContent = `${d.passed}/${d.total} — ${d.score}/100` +
    (secs == null ? "" : ` — ${secs.toFixed(2)}s`);
  let html = "";
  if (d.author || d.language) {
    html += `<div class="meta">โดย ${esc(d.author || "—")} · ${esc(d.language || "")}${d.filename ? ` · ${esc(d.filename)}` : ""}</div>`;
  }
  if (d.verdict === "CE") {
    html += `<div class="score-big bad">คอมไพล์ไม่ผ่าน</div>`;
    html += `<div class="detail-cell"><pre>${esc(d.compile_error || "(ไม่มีข้อความ)")}</pre></div>`;
  } else {
    const ok = d.accepted;
    html += `<div class="score-big ${ok ? "ok" : "bad"}">${ok ? "ผ่านทั้งหมด" : "ยังไม่ผ่าน"}</div>`;
    html += verdictSummary(d);
    const counts = Object.entries(d.counts || {})
      .map(([k, n]) => `${VERDICT_TH[k] || k}:${n}`).join("  ");
    html += `<div class="meta">${esc(counts)} &nbsp;|&nbsp; ${limitsMeta(d)} ตัวตรวจคำตอบ ${esc(d.checker)}</div>`;
    if (d.build_warnings) {
      html += `<div class="detail-cell"><p class="dim">คำเตือนตอนคอมไพล์:</p><pre>${esc(d.build_warnings)}</pre></div>`;
    }
    // Tests are listed by their display order only. The internal test name
    // (e.g. 01/1001) is never surfaced: the numbered run reads as 1..N so the
    // public/hidden split stays invisible.
    html += `<table class="tests"><thead><tr>
      <th>เทสต์</th><th>ผล</th>
      <th title="เวลาที่ใช้จริง (วินาที)">เวลา</th>
      <th title="เวลา CPU (วินาที)">ซีพียู</th>
      <th title="หน่วยความจำสูงสุด">แรม</th>
    </tr></thead><tbody>`;
    (d.results || []).forEach((r, i) => {
      html += `<tr class="test-row" data-i="${i}" tabindex="0" role="button" aria-expanded="false">
        <td>${String(i + 1).padStart(2, "0")}</td>
        <td>${badgeVerdict(r.verdict)}</td><td>${r.wall_s}s</td><td>${r.cpu_s}s</td><td>${r.peak_mb}MB</td>
      </tr>`;
      html += `<tr class="detail-row hidden" data-i="${i}"><td colspan="5" class="detail-cell">${detailHtml(r)}</td></tr>`;
    });
    html += `</tbody></table>`;
    html += `<p class="dim">คลิกแถวเพื่อดูรายละเอียดของเทสต์นั้น</p>`;
  }
  if (d.code) {
    html += `<details class="code-view"><summary>ดูโค้ดที่ส่ง (${esc(String(d.code).split("\n").length)} บรรทัด)</summary><pre>${highlightCode(d.code, d.language)}</pre></details>`;
  }
  const body = $("result-body");
  body.classList.remove("dim");
  body.innerHTML = html;
  bindCopyButtons(body);
  body.querySelectorAll("tr.test-row").forEach((tr) => {
    const toggle = () => {
      const det = body.querySelector(`tr.detail-row[data-i="${tr.dataset.i}"]`);
      if (!det) return;
      const open = det.classList.toggle("hidden");
      tr.setAttribute("aria-expanded", open ? "false" : "true");
    };
    tr.onclick = toggle;
    tr.onkeydown = (ev) => {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); toggle(); }
    };
  });
  const firstFail = Array.from(body.querySelectorAll("tr.test-row")).find((tr) => {
    const b = tr.querySelector("td:nth-child(2) .badge");
    return b && !b.classList.contains("b-AC") && !b.classList.contains("b-OK");
  });
  if (firstFail) {
    const det = body.querySelector(`tr.detail-row[data-i="${firstFail.dataset.i}"]`);
    if (det) {
      det.classList.remove("hidden");
      firstFail.setAttribute("aria-expanded", "true");
    }
  }
}

function detailHtml(r) {
  if (r.hidden) return hiddenDetailHtml(r);
  if (r.verdict === "AC") return `<span class="dim">ผ่านเทสต์นี้</span>`;
  let html = `<p class="dim">${esc(verdictHint(r))}</p>`;
  if (r.detail) html += `<pre>${esc(r.detail)}</pre>`;
  if (r.verdict === "WA" && r.diff) {
    html += `<p class="io-title">ส่วนต่าง (· = ช่องว่าง, → = แท็บ) — <span class="d-del">บรรทัดสีแดงคือคำตอบที่ถูก</span> / <span class="d-add">สีเขียวคือที่โปรแกรมพิมพ์</span></p>${diffHtml(r.diff)}`;
  }
  // Older history rows may lack diagnostics. Do not present missing data as
  // empty input/output; never fetch any hidden fixture to fill the gap.
  if (typeof r.input === "string" && typeof r.expected === "string") {
    html += `<div class="io-grid">
      ${ioBlockHtml(r.practice_feedback ? "input (เคสที่ผิด)" : "input (เคสสาธารณะ)", r.input)}
      ${ioBlockHtml("expected output (คำตอบที่ถูก)", r.expected)}
    </div>`;
    if (typeof r.actual === "string") {
      html += ioBlockHtml("actual output (ที่โปรแกรมพิมพ์ก่อนหยุด)", r.actual);
    }
    html += `<p class="dim">แสดงข้อมูลแต่ละช่องไม่เกิน 2,000 ตัวอักษร</p>`;
  }
  return html;
}

/* Hidden input/output stay private, including when a payload carries them. */
function hiddenDetailHtml(r) {
  return `<p class="dim">${esc(verdictHint(r))}</p>` +
    `<p class="dim">เคสนี้ไม่เปิดเผย input/output เพื่อรักษาชุดทดสอบลับ ลองเทียบผลด้วย input ของคุณในแท็บรันโค้ด</p>`;
}

function verdictHint(r) {
  if (r.verdict === "AC") return "ผ่านเทสต์นี้";
  if (r.verdict === "OK") return "รันจบโดยไม่มี error";
  if (r.verdict === "TLE") return `ใช้เวลา ${r.wall_s}s เกิน limit — มักเกิดจากอัลกอริทึมช้า (O(n²)) หรืออ่าน input ช้า`;
  if (r.verdict === "MLE") return `ใช้หน่วยความจำสูงสุด ${r.peak_mb}MB เกิน limit — ลดขนาด array หรือเก็บข้อมูลน้อยลง`;
  if (r.verdict === "OLE") return "พิมพ์ผลลัพธ์ยาวเกินกำหนด";
  if (r.verdict === "RE") return "โปรแกรมพังตอนรัน — เช็ก index ของ array, การหารด้วยศูนย์, ชนิดข้อมูล";
  if (r.verdict === "WA") return "คำตอบไม่ตรงกับเทสต์นี้ — เทียบทีละบรรทัดกับ expected";
  return String(r.verdict || "");
}

/* ---------------- playground (Run tab; never recorded) ---------------- */
$("run-btn").onclick = doRun;

async function doRun() {
  const btn = $("run-btn");
  const status = $("run-status");
  const box = $("run-result");
  if (!state.user) {
    status.textContent = "login ก่อนใช้ playground";
    box.innerHTML = `<span class="dim"><a href="#/login">เข้าสู่ระบบ</a> ก่อนรันโค้ด (playground คิด quota ต่อบัญชี)</span>`;
    return;
  }
  const code = $("editor").value;
  if (!state.file && !code.trim()) {
    status.textContent = "วางโค้ดในช่อง editor ก่อนรัน";
    return;
  }
  const fd = new FormData();
  fd.append("language", state.lang);
  fd.append("code", code);
  fd.append("input", $("run-input").value);
  if (state.current) fd.append("problem", state.current);
  if (state.file) fd.append("file", state.file, state.file.name);

  btn.disabled = true;
  status.textContent = `กำลังรัน ${state.current || "โค้ด"} (${state.lang})...`;
  box.classList.add("dim");
  box.innerHTML = `<span class="dim">กำลังรัน...</span>`;
  const t0 = performance.now();
  try {
    const d = await apiPostForm("/api/run", fd);
    renderRunResult(d);
    status.textContent =
      `รันเสร็จใน ${((performance.now() - t0) / 1000).toFixed(2)}s — playground ไม่ถูกบันทึกเป็นประวัติ`;
  } catch (e) {
    box.innerHTML = `<span class="dim">รันไม่สำเร็จ: ${esc(e.message)}</span>`;
    status.textContent = "error: " + e.message;
  } finally {
    btn.disabled = false;
  }
}

function limitsMeta(d) {
  const lim = d.limits || {};
  let out = `TL ${esc(String(lim.time_s))}s MEM ${esc(String(lim.memory_mb))}MB`;
  if (lim.capped) {
    out += ` <span class="dim">(เครื่องตัดที่ ${esc(String(lim.memory_mb))}MB จาก ${esc(String(lim.declared_mb))}MB ที่โจทย์ประกาศ — แรมของเซิร์ฟเวอร์มี ${esc(String(lim.host_memory_mb))}MB)</span>`;
  }
  return out;
}

function renderRunResult(d) {
  const box = $("run-result");
  box.classList.remove("dim");
  let html = `<div class="run-head">${badgeVerdict(d.verdict)}` +
    `<span class="meta">wall ${esc(String(d.wall_s))}s · cpu ${esc(String(d.cpu_s))}s · peak ${esc(String(d.peak_mb))}MB` +
    (d.reason ? ` · ${esc(d.reason)}` : "") + `</span></div>`;
  if (d.verdict !== "OK") {
    html += `<p class="dim">${esc(verdictHint(d))}</p>`;
  }
  if (d.limits) {
    html += `<div class="meta">${limitsMeta(d)}</div>`;
  }
  html += `<div class="io-grid">` +
    ioBlockHtml("stdout", d.stdout) + ioBlockHtml("stderr", d.stderr) + `</div>`;
  if (d.truncated) {
    html += `<p class="dim">เอาต์พุตยาวเกินเพดาน — ถูกตัดให้เห็นเฉพาะส่วนแรก (truncated)</p>`;
  }
  if (d.build_warnings) {
    html += `<div class="detail-cell"><p class="dim">build warnings:</p><pre>${esc(d.build_warnings)}</pre></div>`;
  }
  box.innerHTML = html;
  bindCopyButtons(box);
}

/* ---------------- history ---------------- */
/* A stored row only has AC / a failure verdict / counts: pick the label the
 * student can act on instead of painting every failure as WA. */
function rowVerdictCode(r) {
  if (r.accepted) return "AC";
  if (r.verdict && r.verdict !== "AC" && VERDICT_TH[r.verdict]) return r.verdict;
  const counts = r.counts || {};
  return ["CE", "RE", "MLE", "TLE", "OLE", "WA"].find((k) => counts[k]) || "WA";
}

function peerAccessMessage(access) {
  if (access && access.allowed) return "เข้าสู่ระบบแล้ว เปิดดูโค้ดและผลรายเทสต์ของเพื่อนได้ทุกข้อที่เปิดอยู่ โดยไม่เปิดเผยข้อมูลทดสอบ";
  const reason = access && access.reason;
  if (reason === "login") return "เข้าสู่ระบบก่อนดูโค้ดของเพื่อน";
  if (reason === "contest") return "ยังดูโค้ดของเพื่อนไม่ได้: ข้อนี้อยู่ในการแข่งขันที่กำลังจะเริ่มหรือกำลังแข่ง";
  if (reason === "unavailable") return "ยังตรวจสอบสิทธิ์ดูโค้ดของเพื่อนไม่ได้ กรุณาลองอีกครั้ง";
  if (reason === "hidden") return "ข้อนี้ยังไม่เปิดให้ดูโค้ดของเพื่อน";
  if (reason === "incomplete") return "การส่งนี้ยังตรวจไม่เสร็จ ลองใหม่ภายหลัง";
  return "ยังดูโค้ดของเพื่อนไม่ได้ในขณะนี้";
}

const SHORT_TIME_OPTS = { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" };
const _shortTimeFmt = new Intl.DateTimeFormat("th-TH", SHORT_TIME_OPTS);
const _shortTimeFmtYear = new Intl.DateTimeFormat("th-TH", { ...SHORT_TIME_OPTS, year: "2-digit" });

function shortTime(iso) {
  const t = Date.parse(iso);
  if (!t) return iso || "";
  const d = new Date(t);
  const fmt = d.getFullYear() !== new Date().getFullYear() ? _shortTimeFmtYear : _shortTimeFmt;
  return fmt.format(d);
}

function historyTable(rows, showProblem) {
  let html = `<table class="tests history-table ${showProblem ? "history-with-problem" : "history-single-problem"}" role="table"><thead role="rowgroup"><tr role="row">
    <th scope="col">เวลา</th>${showProblem ? '<th scope="col">ข้อ</th>' : ""}<th class="history-author" scope="col">ชื่อ</th><th scope="col">ภาษา</th><th scope="col">ผล</th><th scope="col">คะแนน</th><th scope="col"><span class="sr-only">ดูรายละเอียด</span></th>
  </tr></thead><tbody role="rowgroup">`;
  rows.forEach((r) => {
    const language = { python: "Python", c: "C", cpp: "C++" }[r.language] || r.language || "—";
    html += `<tr role="row">
      <td class="history-time" role="cell" title="${esc(r.created_at || "")}">${esc(shortTime(r.created_at))}</td>${showProblem ? `<td class="history-problem" role="cell">${problemCell(r.problem)}</td>` : ""}<td class="history-author" role="cell">${esc(r.name || "—")}</td><td class="history-language" role="cell">${esc(language)}</td>
      <td class="history-verdict" role="cell">${badgeVerdict(rowVerdictCode(r))}</td><td class="history-score" role="cell"><strong class="${scoreCls(r.score)}">${esc(String(r.score ?? "—"))}<small> / 100</small></strong><span>${esc(String(r.passed ?? "—"))} / ${esc(String(r.total ?? "—"))} เทสต์</span></td>
      <td class="history-action" role="cell"><button class="icon-btn" data-view="${esc(r.id)}" title="ดูผลการตรวจ" aria-label="ดูผลการตรวจ">${icon("eye")}</button></td>
    </tr>`;
  });
  return html + `</tbody></table>`;
}

function bindViewButtons(root) {
  root.querySelectorAll("button[data-view]").forEach((b) => {
    b.onclick = () => viewSubmission(b.dataset.view);
  });
}

async function loadHistory(force) {
  const gen = _getGen, seq = state.historySeq = (state.historySeq || 0) + 1;
  const body = $("hist-body");
  const filt = $("hist-filter").value;
  if (!state.user) {
    $("hist-stats").textContent = "";
    body.innerHTML = `<div class="empty-state"><h3>ประวัติการฝึกของคุณอยู่ที่นี่</h3><p>เข้าสู่ระบบเพื่อย้อนดูโค้ด คะแนน และผลตรวจของแต่ละครั้ง</p><a class="btn-primary" href="#/login">เข้าสู่ระบบ</a></div>`;
    return;
  }
  body.innerHTML = skel(6);
  try {
    const path = `/api/history?limit=100${filt ? `&problem=${encodeURIComponent(filt)}` : ""}&mine=1`;
    // The refresh button must bypass the short cache (force === true); change
    // handlers receive an Event object and keep using the cached read.
    const h = await apiGetCached(path, 10000, force === true);
    if (gen !== _getGen || seq !== state.historySeq) return;
    if (!h.configured) {
      body.innerHTML = `<div class="empty-state"><h3>ประวัติยังไม่พร้อมใช้งาน</h3><p>กลับมาดูอีกครั้งภายหลัง หรือเลือกโจทย์ที่อยากฝึกต่อได้เลย</p><a class="btn-ghost" href="#/tasks">เลือกโจทย์</a></div>`;
      $("hist-stats").textContent = "";
      return;
    }
    const rows = h.submissions || [];
    const vf = ($("hist-verdict") || {}).value || "";
    const shown = rows.filter((r) =>
      vf === "pass" ? r.accepted : vf === "fail" ? !r.accepted : true);
    $("hist-stats").innerHTML = shown.length ? `<div class="history-summary">` +
      `<div><span>รายการล่าสุดที่แสดง</span><strong>${shown.length}</strong></div>` +
      `<div><span>ผ่านแล้ว</span><strong>${shown.filter((r) => r.accepted).length}</strong></div>` +
      `<div><span>คะแนนดีที่สุดในรายการ</span><strong>${Math.max(0, ...shown.map((r) => Number(r.score) || 0))}<small> / 100</small></strong></div></div>` : "";
    if (!rows.length) {
      body.innerHTML = `<div class="empty-state"><h3>เริ่มจากคำตอบแรก</h3><p>เมื่อส่งโค้ด ประวัติและผลตรวจจะเก็บไว้ให้กลับมาดูได้ที่นี่</p><a class="btn-primary" href="#/tasks">เลือกโจทย์</a></div>`;
      return;
    }
    if (!shown.length) {
      body.innerHTML = `<div class="empty-state"><h3>ไม่พบการส่งที่ตรงกัน</h3><p>ลองเลือก “ทุกผล” หรือเปลี่ยนโจทย์ในตัวกรองด้านบน</p></div>`;
      return;
    }
    body.classList.remove("dim");
    if ($("hist-group") && $("hist-group").checked) {
      const byProblem = new Map();
      shown.forEach((r) => {
        if (!byProblem.has(r.problem)) byProblem.set(r.problem, []);
        byProblem.get(r.problem).push(r);
      });
      body.innerHTML = Array.from(byProblem.entries()).map(([pid, list]) => {
        const best = list.reduce((m, r) => Math.max(m, r.score || 0), 0);
        const nPass = list.filter((r) => r.accepted).length;
        return `<h3 class="pane-h">${problemCell(pid)} — ส่ง ${list.length} ครั้ง · ผ่าน ${nPass} · ดีสุด ${best}/100</h3>` +
          historyTable(list, false);
      }).join("");
    } else {
      body.innerHTML = historyTable(shown, true);
    }
    bindViewButtons(body);
  } catch (e) {
    if (gen !== _getGen || seq !== state.historySeq) return;
    body.innerHTML = `<span class="dim">ดูประวัติไม่ได้: ${esc(e.message)}</span>`;
  }
}
async function viewSubmission(sid) {
  const route = "#/submission/" + encodeURIComponent(sid);
  if (location.hash === route) return loadSubmission(sid);
  _nav(route, "push");
}

function clearSubmissionInspection() {
  state.inspectionSeq = (state.inspectionSeq || 0) + 1;
  state.inspectedSubmission = false;
  $("submission-body").innerHTML = "";
  $("submission-status").textContent = "";
  $("submission-body").removeAttribute("aria-busy");
}

async function loadSubmission(sid) {
  const gen = state.inspectionIdentityGen || 0;
  const seq = state.inspectionSeq = (state.inspectionSeq || 0) + 1;
  const route = location.hash;
  const current = () => gen === (state.inspectionIdentityGen || 0) &&
    seq === state.inspectionSeq && location.hash === route;
  const body = $("submission-body"), status = $("submission-status");
  state.inspectedSubmission = false;
  body.innerHTML = "";
  body.setAttribute("aria-busy", "true");
  status.textContent = "กำลังโหลดการส่งและตรวจสอบสิทธิ์…";
  try {
    const r = await apiGet(`/api/history/${encodeURIComponent(sid)}`);
    if (!current()) return;
    renderSubmission(r, sid);
    state.inspectedSubmission = true;
    status.textContent = "";
    setTitle("submission", r.name ? `การส่งของ ${r.name}` : "");
  } catch (e) {
    if (!current()) return;
    const msg = e.status === 403
      ? peerAccessMessage({ reason: /contest/.test(e.message) ? "contest" : /hidden/.test(e.message) ? "hidden" : "attempt" })
      : e.status === 401 ? peerAccessMessage({ reason: "login" })
      : e.status === 503 ? peerAccessMessage({ reason: "unavailable" })
      : e.status === 404 ? "ไม่พบการส่งนี้ หรือการส่งนี้ยังไม่พร้อมให้ดู" : `โหลดการส่งไม่สำเร็จ: ${e.message}`;
    status.textContent = msg;
    body.innerHTML = `<div class="inspection-error"><p>เปิดรายละเอียดการส่งนี้ไม่ได้</p>` +
      (e.status === 401 ? `<a class="ghost-btn" href="#/login">เข้าสู่ระบบ</a>` :
        `<button class="ghost-btn" id="submission-retry" type="button">ลองอีกครั้ง</button>`) + `</div>`;
    const retry = $("submission-retry");
    if (retry) retry.onclick = () => loadSubmission(sid);
  } finally {
    if (current()) body.removeAttribute("aria-busy");
  }
}

function submissionSourceHtml(code, language) {
  // Highlight once so multiline C/C++ comments retain their colour. Close and
  // reopen the highlighter's fixed spans at line boundaries for valid markup.
  let continuation = "";
  return highlightCode(code, language).split("\n").map((line, i) => {
    const prefix = continuation;
    for (const tag of line.matchAll(/<span class="tk-[a-z]">|<\/span>/g)) {
      continuation = tag[0] === "</span>" ? "" : tag[0];
    }
    return `<span class="source-line"><span class="source-line-number" aria-hidden="true">${i + 1}</span>` +
      `<span class="source-line-code">${prefix}${line || " "}${continuation ? "</span>" : ""}</span></span>`;
  }).join("\n");
}

function renderSubmission(r, sid) {
  const code = typeof r.code === "string" ? r.code : "";
  const peer = r.peer_inspection === true;
  const problem = state.problems.find((p) => p.id === r.problem);
  const language = { python: "Python", c: "C", cpp: "C++" }[r.language] || r.language || "—";
  const rows = r.results || [];
  const measure = (value, unit) => value == null ? "—" : `${esc(String(value))} ${unit}`;
  let html = `<div class="inspection-summary" id="submission-meta">` +
    `<div class="inspection-author"><span class="dim">ผู้ส่ง</span><h2>${esc(r.name || "ไม่ระบุชื่อ")}</h2></div>` +
    `<div class="inspection-verdict">${badgeVerdict(rowVerdictCode(r))}<strong class="${scoreCls(r.score)}">${esc(String(r.score ?? "—"))}<span class="dim"> / 100</span></strong></div>` +
    `<dl class="inspection-facts"><div><dt>โจทย์</dt><dd><a id="submission-problem" href="#/task/${encodeURIComponent(r.problem)}">${esc(problem ? problem.title : r.problem)}</a></dd></div>` +
    `<div><dt>รหัสการส่ง</dt><dd class="inspection-id">${esc(sid)}</dd></div>` +
    `<div><dt>ภาษา</dt><dd>${esc(language)}</dd></div>` +
    `<div><dt>ส่งเมื่อ</dt><dd>${esc(shortTime(r.created_at) || "—")}</dd></div>` +
    `<div><dt>เทสต์ที่ผ่าน</dt><dd>${esc(String(r.passed ?? "—"))} / ${esc(String(r.total ?? "—"))}</dd></div></dl></div>`;
  html += `<section class="card inspection-source-card" aria-labelledby="submission-code-heading"><div class="card-head">` +
    `<div><h2 id="submission-code-heading">โค้ดที่ส่ง</h2><span class="dim inspection-file">${esc(r.filename || language)} · ${code.split("\n").length} บรรทัด</span></div>` +
    `<div class="inspection-code-tools"><button class="ghost-btn" id="submission-wrap" type="button" aria-pressed="false" aria-controls="submission-source">ตัดบรรทัดยาว</button>` +
    `<button class="ghost-btn" id="submission-copy" type="button">${icon("copy")}คัดลอกโค้ด</button></div></div>` +
    (code ? `<pre class="inspection-source" id="submission-source" tabindex="0" aria-label="โค้ดที่ส่ง ภาษา ${esc(language)}"><code>${submissionSourceHtml(code, r.language)}</code></pre>` :
      `<p class="dim" id="submission-source">การส่งนี้ไม่มีโค้ดที่บันทึกไว้</p>`) + `</section>`;
  html += `<section class="card inspection-tests-card" id="submission-tests" aria-labelledby="submission-tests-heading">` +
    `<div class="card-head"><h2 id="submission-tests-heading">ผลการตรวจรายเทสต์</h2><span class="dim">${rows.length} เทสต์</span></div>`;
  if (peer) html += `<p class="dim">แสดงผล เวลา และหน่วยความจำ โดยไม่เปิดเผยข้อมูลทดสอบหรือข้อความวินิจฉัยส่วนตัว</p>`;
  if (!peer && r.verdict === "CE" && r.detail) html += `<div class="detail-cell"><pre>${esc(r.detail)}</pre></div>`;
  if (rows.length) {
    html += `<div class="inspection-table-scroll" tabindex="0" role="region" aria-label="ตารางผลการตรวจรายเทสต์"><table class="tests"><thead><tr>` +
      `<th scope="col">เทสต์</th><th scope="col">ผล</th><th scope="col">เวลา (วินาที)</th><th scope="col">CPU (วินาที)</th><th scope="col">แรม (MB)</th></tr></thead><tbody>`;
    rows.forEach((row, i) => {
      html += `<tr><th scope="row">${String(i + 1).padStart(2, "0")}</th><td>${badgeVerdict(row.verdict)}</td>` +
        `<td>${measure(row.wall_s, "")}</td><td>${measure(row.cpu_s, "")}</td><td>${measure(row.peak_mb, "")}</td></tr>`;
      // Peers never receive detailed feedback. Keep the browser boundary just
      // as narrow even if an older or malformed response includes extra fields.
      if (!peer && row.verdict !== "AC") html += `<tr><td colspan="5" class="detail-cell"><details><summary>รายละเอียดเทสต์ ${i + 1}</summary>${detailHtml(row)}</details></td></tr>`;
    });
    html += `</tbody></table></div>`;
  } else html += `<p class="dim">ไม่มีผลรายเทสต์ที่บันทึกไว้${r.verdict === "CE" ? " เนื่องจากคอมไพล์ไม่ผ่าน" : ""}</p>`;
  html += `</section>`;
  $("submission-body").innerHTML = html;
  bindCopyButtons($("submission-body"));
  const copy = $("submission-copy"), wrap = $("submission-wrap");
  copy.disabled = wrap.disabled = !code;
  copy.onclick = async () => {
    try { await navigator.clipboard.writeText(code); toast("คัดลอกโค้ดแล้ว"); }
    catch (e) { toast("คัดลอกไม่สำเร็จ เลือกโค้ดแล้วคัดลอกด้วยแป้นพิมพ์ได้"); }
  };
  wrap.onclick = () => {
    const on = $("submission-source").classList.toggle("source-wrapped");
    wrap.setAttribute("aria-pressed", on ? "true" : "false");
  };
}

/* ---------------- contests ---------------- */
const CONTEST_STATUS_TH = {
  upcoming: "กำลังจะมาถึง", running: "กำลังแข่ง", ended: "จบแล้ว",
};

function contestStatusBadge(st) {
  const key = st || "ended";
  return badge(CONTEST_STATUS_TH[key] || key, key);
}

const _thaiTimeFmt = new Intl.DateTimeFormat("th-TH", {
  day: "2-digit", month: "short", year: "numeric",
  hour: "2-digit", minute: "2-digit",
});

function thaiTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso);
  return _thaiTimeFmt.format(d);
}

function contestWindowText(c) {
  return `${thaiTime(c.starts_at)} — ${thaiTime(c.ends_at)}`;
}

/* Contests are stored in UTC; the datetime-local inputs are Thai local time. */
function localToUtcIso(v) {
  if (!v) return "";
  const d = new Date(v);
  if (isNaN(d.getTime())) return "";
  return d.toISOString();  // ISO-8601 UTC, e.g. 2026-11-01T02:00:00.000Z
}

/* Reverse of localToUtcIso: fill a datetime-local input from a UTC ISO string
 * (used when pre-filling the contest form for mid-round edits). */
function utcToLocalInput(iso) {
  const d = new Date(iso || "");
  if (isNaN(d.getTime())) return "";
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}` +
    `T${p(d.getHours())}:${p(d.getMinutes())}`;
}

const CONTESTS_UNCONFIGURED = "ยังไม่ได้ตั้งค่าคอนเทสต์";

function isUnconfiguredMessage(msg) {
  return /not configured|configured/i.test(String(msg || ""));
}

async function renderContests() {
  const body = $("contests-body");
  body.classList.add("dim");
  body.innerHTML = skel(5);
  try {
    const d = await apiGet("/api/contests");
    state.contests = d.contests || [];
    state.contestsConfigured = !!d.configured;
    if (!d.configured) {
      body.innerHTML = emptyStateHtml("สนามแข่งขันยังไม่พร้อมใช้งาน", "ระหว่างนี้เลือกโจทย์ฝึกด้วยตัวเองได้ เมื่อมีการแข่งขันจะดูรายละเอียดได้จากหน้านี้", "#/tasks", "ไปฝึกโจทย์");
      return;
    }
    const rows = state.contests;
    if (!rows.length) {
      body.innerHTML = emptyStateHtml("ยังไม่มีการแข่งขัน", "เมื่อมีการแข่งขันใหม่ วันเวลาและรายละเอียดจะแสดงที่นี่", "#/tasks", "ไปฝึกโจทย์ระหว่างรอ");
      return;
    }
    body.classList.remove("dim");
    body.innerHTML = `<div class="contest-list">` + rows.map((c) =>
      `<a class="contest-event" href="#/contests/${encodeURIComponent(c.id)}">` +
      `<div class="contest-event-main"><div class="contest-event-status">${contestStatusBadge(c.status)}${c.registered ? badge("ลงทะเบียนแล้ว", "OK") : ""}</div>` +
      `<h3>${esc(c.title || c.id)}</h3><p>${c.status === "upcoming" && !state.is_admin ? "โจทย์เปิดเมื่อเริ่มแข่ง" : `${(c.problems || []).length} โจทย์`}<span>ผู้ลงทะเบียน ${Number(c.participants) || 0} คน</span></p></div>` +
      `<dl class="contest-event-dates"><div><dt>เริ่ม / เวลาไทย</dt><dd>${esc(thaiTime(c.starts_at))}</dd></div><div><dt>สิ้นสุด</dt><dd>${esc(thaiTime(c.ends_at))}</dd></div></dl>` +
      `<span class="contest-event-arrow" aria-hidden="true">${icon("back")}</span></a>`).join("") + `</div>`;
  } catch (e) {
    body.innerHTML = `<div style="padding:4px">โหลดคอนเทสต์ไม่ได้: ${esc(e.message)}</div>`;
  }
}

async function renderContest(cid) {
  const seq = state.contestSeq = (state.contestSeq || 0) + 1;
  const gen = _getGen;
  const title = $("contest-title"), meta = $("contest-meta");
  title.textContent = "Loading...";
  meta.textContent = "";
  $("contest-status-chip").innerHTML = "";
  $("contest-freeze").classList.add("hidden");
  $("contest-problems").innerHTML = skel(4);
  $("contest-scoreboard").innerHTML = skel(3);
  $("contest-annc").innerHTML = skel(2);
  // Announcements only need the contest id, so let them travel in parallel with
  // the (larger) detail payload instead of adding a second serial round trip.
  const anncPromise = apiGet(`/api/contests/${encodeURIComponent(cid)}/announcements`)
    .catch((e) => ({ __error: e }));
  try {
    const d = await apiGet(`/api/contests/${encodeURIComponent(cid)}`);
    if (seq !== state.contestSeq || gen !== _getGen) return;
    state.contestCache[cid] = d;
    setTitle("contest", d.title || d.id);
    title.textContent = d.title || d.id;
    meta.textContent =
      `${d.id} · ${contestWindowText(d)} · แช่แข็ง ${d.freeze_minutes || 0} นาที · ` +
      (d.show_samples ? "เปิดตัวอย่าง" : "ปิดตัวอย่างระหว่างแข่ง") +
      (d.hide_from_tasks ? " · โจทย์ชุดนี้ไม่โชว์ในหน้า Task" : "");
    $("contest-status-chip").innerHTML = contestStatusBadge(d.status);
    if (d.frozen) $("contest-freeze").classList.remove("hidden");
    renderContestJoin(d);
    renderContestProblems(d);
    renderContestAnnc(d, anncPromise, seq, gen);
    renderContestScoreboard(d);
    startContestTicker(d.id || cid, d);
  } catch (e) {
    if (seq !== state.contestSeq || gen !== _getGen) return;
    const msg = isUnconfiguredMessage(e.message) ? CONTESTS_UNCONFIGURED : e.message;
    title.textContent = "โหลดคอนเทสต์ไม่สำเร็จ";
    meta.textContent = "";
    $("contest-problems").innerHTML = `<span class="dim">${esc(msg)}</span>`;
    $("contest-scoreboard").innerHTML = `<span class="dim">—</span>`;
  }
}

function renderContestJoin(d) {
  const box = $("contest-join");
  if (!box) return;
  box.classList.remove("dim");
  const n = d.participants || 0;
  const parts = [`<span class="meta">ผู้ลงทะเบียน ${n} คน</span>`];
  if (d.starts_in_s != null && d.starts_in_s > 0) {
    parts.push(`<span class="badge b-upcoming" id="contest-countdown">เริ่มในอีก ${durationText(d.starts_in_s)}</span>`);
  } else if (d.ends_in_s != null && d.ends_in_s > 0 && d.status === "running") {
    parts.push(`<span class="badge b-running" id="contest-countdown">เหลืออีก ${durationText(d.ends_in_s)}</span>`);
  }
  if (!d.registrations_supported) {
    parts.push(`<span class="dim">ระบบลงทะเบียนยังไม่พร้อม (รัน supabase/schema.sql)</span>`);
  } else if (d.registered) {
    parts.push(`<span class="badge b-OK">ลงทะเบียนแล้ว</span>`);
    if (d.status !== "running") {
      parts.push(`<button class="ghost-btn" type="button" id="contest-unregister">ยกเลิกลงทะเบียน</button>`);
    }
  } else {
    parts.push(`<button class="submit-btn" type="button" id="contest-register">${icon("send")}<span>Register</span></button>`);
  }
  if (!d.problems_open) {
    parts.push(`<span class="dim">โจทย์จะเปิดให้อ่านตอนเริ่มแข่ง</span>`);
  } else if (d.status === "running" && !d.can_submit && d.registrations_supported) {
    parts.push(`<span class="dim">ต้องลงทะเบียนก่อนจึงส่งได้</span>`);
  }
  const t0 = Date.parse(d.starts_at || ""), t1 = Date.parse(d.ends_at || "");
  if (t0 && t1 && t1 > t0) {
    const pct = Math.min(100, Math.max(0, Math.round((100 * (Date.now() - t0)) / (t1 - t0))));
    parts.push(`<span style="flex-basis:100%"><span class="ct-track" style="display:block">` +
      `<span class="ct-fill" id="ct-fill" data-t0="${t0}" data-t1="${t1}" style="display:block;width:${pct}%"></span></span></span>`);
  }
  box.innerHTML = parts.join(" ");
  const reg = $("contest-register");
  if (reg) reg.onclick = () => contestRegister(d.id, true);
  const unreg = $("contest-unregister");
  if (unreg) unreg.onclick = () => contestRegister(d.id, false);
}

function durationText(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (d) return `${d} วัน ${h} ชม.`;
  if (h) return `${h} ชม. ${m} นาที`;
  if (m) return `${m} นาที ${sec} วิ`;
  return `${sec} วิ`;
}

function stopContestTicker() {
  if (state.contestTimer) { clearInterval(state.contestTimer); state.contestTimer = null; }
  state.contestPhase = null;
}

/* Live countdown on the contest page: ticks every second from the server
 * timestamps captured at fetch, and reloads the page data the moment the
 * round starts or ends — problems unlock without a manual refresh. */
function startContestTicker(cid, d) {
  stopContestTicker();
  const phase = d.status;
  let target = null;
  if (phase === "upcoming" && d.starts_in_s != null) target = Date.now() + d.starts_in_s * 1000;
  else if (phase === "running" && d.ends_in_s != null) target = Date.now() + d.ends_in_s * 1000;
  if (target == null) return;
  state.contestPhase = phase;
  const wantHash = "#/contests/" + encodeURIComponent(cid);
  state.contestTimer = setInterval(() => {
    if (location.hash !== wantHash) { stopContestTicker(); return; }   // navigated away mid-fetch
    if (document.hidden) return; // hidden tab: skip DOM writes, keep phase guard on return
    const left = Math.max(0, Math.round((target - Date.now()) / 1000));
    const el = $("contest-countdown");
    if (el) el.textContent = (phase === "upcoming" ? "เริ่มในอีก " : "เหลืออีก ") + durationText(left);
    const fill = $("ct-fill");
    if (fill && fill.dataset.t0 && fill.dataset.t1) {
      const a = Number(fill.dataset.t0), b = Number(fill.dataset.t1);
      if (b > a) fill.style.width = Math.min(100, Math.max(0, (100 * (Date.now() - a)) / (b - a))) + "%";
    }
    if (left <= 0 && state.contestPhase === phase) {
      state.contestPhase = null;   // refire guard while refetching
      renderContest(cid);          // restarts the ticker for the new phase
    }
  }, 1000);
}

async function contestRegister(cid, register) {
  const box = $("contest-join");
  if (box) box.innerHTML = `<span class="dim">${register ? "กำลังลงทะเบียน..." : "กำลังยกเลิก..."}</span>`;
  try {
    if (register) await apiPostJson(`/api/contests/${encodeURIComponent(cid)}/register`, {});
    else await apiDelete(`/api/contests/${encodeURIComponent(cid)}/register`);
    toast(register ? "ลงทะเบียนสำเร็จ" : "ยกเลิกการลงทะเบียนแล้ว", "t-ok");
    await renderContest(cid);
    if (document.querySelector("#view-contests.active")) renderContests();
  } catch (e) {
    toast("ไม่สำเร็จ: " + e.message, "t-err");
    if (box) box.innerHTML = `<span class="dim">${esc(e.message)}</span>`;
  }
}

function renderContestProblems(d) {
  const box = $("contest-problems");
  const list = d.problem_details || [];
  if (!list.length) {
    box.innerHTML = d.problems_open
      ? `<span class="dim">ยังไม่มีโจทย์ในคอนเทสต์นี้</span>`
      : `<span class="dim">ยังไม่เปิดเผยโจทย์ — จะแสดงเมื่อเริ่มแข่ง</span>`;
    return;
  }
  const running = d.status === "running";
  box.classList.remove("dim");
  box.innerHTML = `<table class="tests"><thead><tr>
    <th>#</th><th>ข้อ</th><th>ระดับ</th><th>เทสต์</th><th></th>
  </tr></thead><tbody>` + list.map((p, i) => {
    const nS = p.samples || 0;
    const label = esc((p.no ? "#" + p.no + " " : "") + (p.title || p.id));
    const link = d.problems_open
      ? `<a class="tlink contest-open" href="#/task/${encodeURIComponent(p.id)}">${label}</a>`
      : `<span class="dim" title="โจทย์จะเปิดตอนเริ่มแข่ง">${label} <span class="badge b-hidden">ล็อก</span></span>`;
    const action = !d.problems_open
      ? `<span class="dim">รอเริ่มแข่ง</span>`
      : running && d.can_submit
      ? `<button class="ghost-btn" type="button" data-enter="${esc(p.id)}">${icon("send")}<span>ส่งข้อนี้เข้าคอนเทสต์</span></button>`
      : `<a class="ghost-btn contest-open" href="#/task/${encodeURIComponent(p.id)}">${icon("eye")}<span>ดูโจทย์</span></a>`;
    return `<tr>
      <td>${String(i + 1).padStart(2, "0")}</td>
      <td class="contest-problem-name">${link}</td>
      <td>${diffBadge(p.difficulty)}</td>
      <td>${nS} ตัวอย่าง</td>
      <td>${action}</td>
    </tr>`;
  }).join("") + `</tbody></table>`;
  box.querySelectorAll(".contest-open").forEach((el) => {
    el.onclick = () => enterContestFrom(d);
  });
  box.querySelectorAll("button[data-enter]").forEach((b) => {
    b.onclick = () => {
      enterContestFrom(d);
      openTask(b.dataset.enter);
    };
  });
}

async function renderContestAnnc(d, pending, seq = state.contestSeq, gen = _getGen) {
  const box = $("contest-annc");
  const form = $("contest-annc-form");
  box.classList.add("dim");
  box.innerHTML = skel(2);
  form.classList.add("hidden");
  try {
    const a = pending
      ? await pending
      : await apiGet(`/api/contests/${encodeURIComponent(d.id)}/announcements`);
    if (seq !== state.contestSeq || gen !== _getGen) return;
    if (a && a.__error) throw a.__error;
    const list = a.announcements || [];
    box.classList.remove("dim");
    box.innerHTML = list.length
      ? list.map((m) =>
          `<div class="report-item"><div>${esc(m.body)}</div>` +
          `<div class="dim">${esc(shortTime(m.created_at))}</div></div>`).join("")
      : (a.locked
        ? `<span class="dim">ประกาศจะแสดงเมื่อเริ่มแข่ง</span>`
        : `<span class="dim">ยังไม่มีประกาศ</span>`);
  } catch (e) {
    if (seq !== state.contestSeq || gen !== _getGen) return;
    box.innerHTML = `<span class="dim">โหลดประกาศไม่ได้: ${esc(e.message)}</span>`;
  }
  if (state.is_admin) {
    form.classList.remove("hidden");
    $("annc-status").textContent = "";
    $("annc-post").onclick = async () => {
      const st = $("annc-status"), body = $("annc-body").value.trim();
      if (!body) { st.textContent = "พิมพ์ข้อความก่อน"; return; }
      st.textContent = "กำลังโพสต์...";
      try {
        await apiPostJson(`/api/contests/${encodeURIComponent(d.id)}/announcements`, { body });
        $("annc-body").value = "";
        st.textContent = "โพสต์แล้ว";
        toast("โพสต์ประกาศแล้ว", "t-ok");
        renderContestAnnc(d);
      } catch (e) {
        if (seq !== state.contestSeq || gen !== _getGen) return;
        st.textContent = "โพสต์ไม่สำเร็จ: " + e.message;
      }
    };
  }
}

function renderContestScoreboard(d) {
  const box = $("contest-scoreboard");
  const rows = d.scoreboard || [];
  if (!rows.length) {
    box.innerHTML = `<span class="dim">ยังไม่มีคะแนนในคอนเทสต์นี้</span>`;
    return;
  }
  const pids = d.problems || [];
  const showPer = pids.length > 0 &&
    rows.some((r) => r.per_problem && Object.keys(r.per_problem).length);
  box.classList.remove("dim");
  let html = `<table class="tests"><thead><tr>
    <th>#</th><th>ชื่อ</th><th>แก้ได้</th><th>คะแนน</th>` +
    (showPer ? pids.map((p) => `<th>${esc(p)}</th>`).join("") : "") + `</tr></thead><tbody>`;
  rows.forEach((r, i) => {
    const medal = i < 3 ? `<span class="rank rank-${i + 1}">${i + 1}</span>` : `${i + 1}`;
    html += `<tr><td>${medal}</td><td>${esc(r.name || "—")}</td><td>${r.solved} ข้อ</td>` +
      `<td><strong class="${scoreCls(r.points)}">${r.points}</strong></td>` +
      (showPer
        ? pids.map((p) => `<td>${esc(String((r.per_problem || {})[p] ?? ""))}</td>`).join("")
        : "") + `</tr>`;
  });
  box.innerHTML = html + `</tbody></table>`;
}

/* Arriving from a contest problem link pins state.contest: every submit from
 * that task page then carries contest=<id> until the banner is cleared. */
function enterContestFrom(d) {
  state.contest = { id: d.id, title: d.title || d.id };
  renderContestBanner();
}

function renderContestBanner() {
  const box = $("contest-banner");
  if (!box) return;
  const c = state.contest;
  if (!c) {
    box.classList.add("hidden");
    box.innerHTML = "";
    return;
  }
  box.classList.remove("hidden");
  box.innerHTML =
    `<span>กำลังส่งเข้าคอนเทสต์ <strong>${esc(c.title)}</strong> <span class="dim">(${esc(c.id)})</span>` +
    ` — ทุกครั้งที่กด “ส่งคำตอบ” จะนับคะแนนในคอนเทสต์นี้ · ` +
    `<a href="#/contests/${encodeURIComponent(c.id)}">ดูคอนเทสต์</a></span>` +
    ` <button id="contest-clear" type="button" class="ghost-btn">ออกจากคอนเทสต์</button>`;
  const clear = $("contest-clear");
  if (clear) {
    clear.onclick = () => {
      state.contest = null;
      renderContestBanner();
    };
  }
}

/* ---------------- settings ---------------- */
function setLang(lang) {
  if (!["python", "c", "cpp"].includes(lang)) return;
  state.lang = lang;
  try { localStorage.setItem(LANG_KEY, lang); } catch (e) { /* ignore */ }
  document.querySelectorAll(".lang-btn").forEach((x) => {
    const on = x.dataset.lang === lang;
    x.classList.toggle("active", on);
    if (x.hasAttribute("aria-checked")) x.setAttribute("aria-checked", on ? "true" : "false");
  });
}

async function renderSettings() {
  const body = $("settings-body");
  if (!state.user) {
    body.innerHTML = emptyStateHtml("ตั้งค่าบัญชีของคุณ", "เข้าสู่ระบบเพื่อเปลี่ยนชื่อที่แสดงและภาษาที่ใช้ส่งคำตอบ", "#/login", "เข้าสู่ระบบ");
    return;
  }
  body.innerHTML = skel(3);
  try {
    const data = await apiGet("/api/profile");
    const p = data.profile || {};
    if (!data.configured) {
      body.innerHTML = `<div class="account-identity"><strong>${esc(p.name || "บัญชีของคุณ")}</strong><span>${esc(p.email || "")}</span></div><p class="dim">ยังแก้ไขข้อมูลบัญชีไม่ได้ในขณะนี้</p>`;
      return;
    }
    body.classList.remove("dim");
    body.innerHTML = `
      <div class="account-identity"><strong>${esc(p.name || "บัญชีของคุณ")}</strong><span>${esc(p.email || "")}</span></div>
      <div class="settings-row">
        <label for="set-display">ชื่อที่แสดง</label>
        <input id="set-display" type="text" maxlength="50" value="${esc(p.display_name || "")}" placeholder="${esc(p.name || "")}">
        <p class="field-hint">เว้นว่างเพื่อใช้ชื่อจากบัญชีของคุณ</p>
      </div>
      <div class="settings-row">
        <label for="set-lang">ภาษาที่ใช้เป็นค่าเริ่มต้น</label>
        <select id="set-lang">
          <option value="python">Python</option>
          <option value="c">C</option>
          <option value="cpp">C++</option>
        </select>
      </div>
      <div class="submit-row">
        <button id="settings-save" type="button" class="submit-btn"><svg class="ic"><use href="#i-check"/></svg><span>บันทึกการตั้งค่า</span></button>
      </div>
      <div id="settings-status" class="status dim"></div>`;
    $("set-lang").value = p.default_language || state.lang;
    $("settings-save").onclick = async () => {
      const payload = {
        display_name: $("set-display").value.trim(),
        default_language: $("set-lang").value,
      };
      $("settings-status").textContent = "กำลังบันทึก...";
      try {
        const r = await fetch(API + "/api/profile", {
          method: "PUT", credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
        $("settings-status").textContent = "บันทึกแล้ว";
        toast("บันทึกโปรไฟล์แล้ว", "t-ok");
        setLang(d.profile.default_language || state.lang);
      } catch (e) {
        $("settings-status").textContent = "บันทึกไม่สำเร็จ: " + e.message;
      }
    };
  } catch (e) {
    body.innerHTML = `<span class="dim">โหลดไม่ได้: ${esc(e.message)}</span>`;
  }
}

/* ---------------- admin tools (rejudge / plagiarism / contests) ---------------- */
function buildProblemSelect(sel, multiple) {
  if (!sel) return;
  const prev = multiple ? null : sel.value;
  // Contest-only problems first in the multi-select, and marked, so the admin can
  // tell the "thrown in" set apart from the public Tasks.
  const list = multiple
    ? [...state.problems].sort((a, b) => (b.hidden ? 1 : 0) - (a.hidden ? 1 : 0))
    : state.problems;
  sel.innerHTML = list.map((p) => {
    const tag = p.hidden ? " [ซ่อนจาก Task]" : "";
    const num = p.no ? `#${p.no} ` : "";
    return `<option value="${esc(p.id)}">${esc(num + p.id)} — ${esc(p.title)}${esc(tag)}</option>`;
  }).join("");
  if (!multiple && prev && [...sel.options].some((o) => o.value === prev)) sel.value = prev;
}

/* ---------------- admin page (แยกจาก Settings, เห็นเฉพาะ admin) ---------------- */
const ADMIN_TABS = ["overview", "history", "contests", "reports", "admins", "tools"];

function showAdminTab(name) {
  if (!ADMIN_TABS.includes(name)) name = "overview";
  state.adminTab = name;
  document.querySelectorAll("[data-admin-panel]").forEach((el) => {
    el.classList.toggle("active", el.dataset.adminPanel === name);
  });
  document.querySelectorAll("[data-admin-tab]").forEach((b) => {
    const on = b.dataset.adminTab === name;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", String(on));
  });
  // Load heavy panels on first open instead of on every admin visit. The
  // overview and the contest badge still load immediately; reports and the
  // tool selects wait until the operator actually asks for them.
  if (name === "overview" && !state.adminOverviewLoaded) {
    state.adminOverviewLoaded = true;
    loadOverview();
  }
  if (name === "history" && !state.adminHistoryLoaded) {
    state.adminHistoryLoaded = true;
    loadAdminHistory();
  }
  if (name === "reports" && !state.adminReportsLoaded) {
    state.adminReportsLoaded = true;
    loadAdminReports();
  }
  if (name === "admins" && !state.adminAdminsLoaded) {
    state.adminAdminsLoaded = true;
    loadAdmins();
  }
  if (name === "tools" && !state.adminToolsBuilt) {
    state.adminToolsBuilt = true;
    renderAdminTools();
  }
}

function renderAdmin() {
  const pending = !state.authReady || state.authError;
  $("admin-session-status").classList.toggle("hidden", !pending);
  $("admin-content").classList.toggle("hidden", pending || !state.is_admin);
  if (pending) {
    $("admin-session-status").textContent = state.authError
      ? "ตรวจสอบสิทธิ์ไม่สำเร็จ กรุณาโหลดหน้าใหม่อีกครั้ง"
      : "กำลังตรวจสอบสิทธิ์ผู้ดูแล…";
    return;
  }
  if (!state.is_admin) {
    _nav("#/", "replace");
    return;
  }
  ["overview-card", "admin-history-card", "contest-card", "admin-card", "admins-card", "admin-tools-card"].forEach((id) => {
    const el = $(id);
    if (el) el.classList.remove("hidden");
  });
  document.querySelectorAll("[data-admin-tab]").forEach((b) => {
    b.onclick = () => showAdminTab(b.dataset.adminTab);
  });
  const refresh = $("admin-refresh");
  if (refresh) {
    refresh.onclick = async () => {
      const label = refresh.querySelector("span");
      refresh.disabled = true;
      if (label) label.textContent = "กำลังโหลด…";
      const jobs = [loadOverview(), loadAdminContests(), loadAdminReports()];
      if (state.adminAdminsLoaded) jobs.push(loadAdmins());
      await Promise.all(jobs);
      state.adminReportsLoaded = true;
      state.adminOverviewLoaded = true;
      if (label) label.textContent = "รีเฟรช";
      refresh.disabled = false;
    };
  }
  const repRefresh = $("admin-reports-refresh");
  if (repRefresh) repRefresh.onclick = loadAdminReports;
  const admRefresh = $("admins-refresh");
  if (admRefresh) admRefresh.onclick = loadAdmins;
  const addForm = $("admin-add-form");
  if (addForm) addForm.onsubmit = addAdminByEmail;
  const histRefresh = $("admin-history-refresh");
  if (histRefresh) histRefresh.onclick = () => loadAdminHistory();
  const histSize = $("admin-history-size");
  if (histSize && !histSize.dataset.bound) {
    histSize.dataset.bound = "1";
    histSize.value = String(state.adminHistoryPerPage || 20);
    histSize.onchange = () => {
      state.adminHistoryPage = 1;
      state.adminHistoryPerPage = Number(histSize.value) || 20;
      loadAdminHistory();
    };
  }
  const histProblem = $("admin-history-problem");
  if (histProblem && !state.adminHistoryBuilt) {
    buildProblemSelect(histProblem);
    histProblem.insertAdjacentHTML("afterbegin", '<option value="">ทุกข้อ</option>');
    histProblem.value = state.adminHistoryProblem || "";
    state.adminHistoryBuilt = true;
    histProblem.onchange = () => {
      state.adminHistoryProblem = histProblem.value;
      state.adminHistoryPage = 1;
      loadAdminHistory();
    };
  }
  state.adminReportsLoaded = false;
  state.adminToolsBuilt = false;
  state.adminOverviewLoaded = false;
  state.adminAdminsLoaded = false;
  state.adminHistoryLoaded = false;
  // The contest tab shows a count badge, so its list still starts immediately;
  // only the panel bodies wait until their tab is opened.
  loadAdminContests();
  showAdminTab(state.adminTab || "overview");
}

function setAdminsStatus(text, isError) {
  const el = $("admins-status");
  if (!el) return;
  el.textContent = text || "";
  el.className = "status " + (isError ? "dash-error" : "dim");
}

async function loadAdmins() {
  const box = $("admins-list");
  if (!box) return;
  box.classList.add("dim");
  box.innerHTML = skel(3);
  try {
    const d = await apiGet("/api/admin/admins");
    const rows = d.admins || [];
    setDashCount("admins-count", `${rows.length} คน`);
    const dl = $("admin-candidates");
    if (dl) {
      dl.innerHTML = (d.candidates || []).map((c) =>
        `<option value="${esc(c.email)}">${esc(c.name || c.email)}</option>`).join("");
    }
    box.classList.remove("dim");
    if (!rows.length) {
      box.innerHTML = `<p class="dash-empty">ยังไม่มีผู้ดูแล</p>`;
      return;
    }
    box.innerHTML = rows.map((r) => {
      const env = r.source === "env";
      const removable = !!r.removable && !env;
      const mine = (state.user && (state.user.email || "").toLowerCase() === r.email);
      return `<article class="admin-report">` +
        `<div class="admin-report-head">` +
        `<span class="admin-report-problem">${esc(r.email)}</span>` +
        (env ? badge("ADMIN_EMAILS", "OK") : badge("ฐานข้อมูล", "upcoming")) +
        (mine ? `<span class="diff-tag">คุณ</span>` : "") +
        (removable ? `<button class="ghost-btn" type="button" data-admin-remove="${esc(r.email)}">ถอดสิทธิ์</button>` : "") +
        `</div>` +
        `<div class="admin-report-meta">` +
        `<span>${env ? "ถาวรจาก ADMIN_EMAILS" : "เพิ่มโดย " + esc(r.created_by || "ไม่ระบุ")}</span>` +
        (r.created_at ? `<span>${esc(shortTime(r.created_at))}</span>` : "") +
        `</div></article>`;
    }).join("");
    box.querySelectorAll("[data-admin-remove]").forEach((b) => {
      b.onclick = () => removeAdmin(b.dataset.adminRemove, b);
    });
  } catch (e) {
    box.classList.remove("dim");
    setDashCount("admins-count", "—");
    box.innerHTML = `<p class="dash-empty dash-error">โหลดไม่ได้: ${esc(e.message)}</p>`;
  }
}

async function addAdminByEmail(ev) {
  ev.preventDefault();
  const input = $("admin-add-email");
  const btn = $("admin-add-btn");
  const email = (input && input.value || "").trim().toLowerCase();
  if (!email) return;
  if (btn) btn.disabled = true;
  setAdminsStatus("กำลังเพิ่ม…", false);
  try {
    const d = await apiPostJson("/api/admin/admins", { email });
    if (input) input.value = "";
    setAdminsStatus(`เพิ่ม ${d.email} เป็นผู้ดูแลแล้ว — มีผลทันที`, false);
    toast("เพิ่มผู้ดูแลแล้ว", "t-ok");
    await loadAdmins();
  } catch (e) {
    setAdminsStatus(e.message, true);
    toast("เพิ่มผู้ดูแลไม่สำเร็จ: " + e.message, "t-err");
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function removeAdmin(email, btn) {
  if (!email) return;
  if (!confirm(`ถอดสิทธิ์ผู้ดูแลของ ${email}?`)) return;
  if (btn) btn.disabled = true;
  try {
    await apiDelete(`/api/admin/admins/${encodeURIComponent(email)}`);
    toast("ถอดสิทธิ์แล้ว", "t-ok");
    await loadAdmins();
    if (state.user && (state.user.email || "").toLowerCase() === email) {
      await refreshMe();
    }
  } catch (e) {
    if (btn) btn.disabled = false;
    toast("ถอดสิทธิ์ไม่สำเร็จ: " + e.message, "t-err");
  }
}

function adminHistoryTable(rows) {
  let html = `<table class="tests"><thead><tr>
    <th>เวลา</th><th>ผู้ส่ง</th><th>ข้อ</th><th>ภาษา</th><th>ผล</th><th>คะแนน</th><th></th>
  </tr></thead><tbody>`;
  rows.forEach((r) => {
    html += `<tr>
      <td title="${esc(r.created_at || "")}">${esc(shortTime(r.created_at))}</td>
      <td><div>${esc(r.name || "—")}</div><div class="admin-user-email">${esc(r.user_email || "")}</div></td>
      <td>${problemCell(r.problem)}</td>
      <td>${esc(r.language || "—")}</td>
      <td>${badgeVerdict(rowVerdictCode(r))}</td>
      <td>${r.passed}/${r.total} (<span class="${scoreCls(r.score)}">${r.score}</span>)</td>
      <td>${r.id ? `<button class="icon-btn" data-view="${esc(r.id)}" title="ดูผลการตรวจ" aria-label="ดูผลการตรวจ">${icon("eye")}</button>` : ""}</td>
    </tr>`;
  });
  return html + `</tbody></table>`;
}

function renderAdminHistoryPager(page, pages) {
  const box = $("admin-history-pager");
  if (!box) return;
  const btn = (p, label, disabled) =>
    `<button class="ghost-btn" type="button" data-history-page="${p}" ${disabled ? "disabled" : ""}>${label}</button>`;
  box.innerHTML = btn(1, "« แรก", page <= 1) +
    btn(page - 1, "ก่อนหน้า", page <= 1) +
    `<span class="pager-info">หน้า ${page} / ${pages}</span>` +
    btn(page + 1, "ถัดไป", page >= pages) +
    btn(pages, "สุดท้าย »", page >= pages);
  box.querySelectorAll("button[data-history-page]").forEach((b) => {
    b.onclick = () => {
      const p = Number(b.dataset.historyPage);
      if (!p || p === state.adminHistoryPage) return;
      state.adminHistoryPage = p;
      loadAdminHistory();
    };
  });
}

async function loadAdminHistory() {
  const body = $("admin-history-body");
  if (!body) return;
  const sizes = [20, 50, 100];
  const per = sizes.includes(Number(state.adminHistoryPerPage))
    ? Number(state.adminHistoryPerPage) : 20;
  const page = Math.max(1, Number(state.adminHistoryPage) || 1);
  const filter = $("admin-history-problem");
  const pid = filter ? filter.value : "";
  state.adminHistoryPerPage = per;
  state.adminHistoryPage = page;
  body.classList.add("dim");
  body.innerHTML = skel(8);
  const meta = $("admin-history-meta");
  if (meta) meta.textContent = "กำลังโหลด…";
  const pager = $("admin-history-pager");
  if (pager) pager.innerHTML = "";
  try {
    let path = `/api/admin/submissions?page=${page}&per_page=${per}`;
    if (pid) path += `&problem=${encodeURIComponent(pid)}`;
    const d = await apiGet(path);
    if (!d.configured) {
      body.innerHTML = `<span class="dim">ยังไม่เปิด history — ตั้งค่า SUPABASE_URL / SUPABASE_KEY ที่ backend</span>`;
      if (meta) meta.textContent = "";
      setDashCount("admin-history-count", "—");
      return;
    }
    const total = Number(d.total) || 0;
    const pages = Number(d.pages) || 1;
    state.adminHistoryPage = Number(d.page) || 1;
    state.adminHistoryPerPage = Number(d.per_page) || per;
    setDashCount("admin-history-count", `${total.toLocaleString("th-TH")} รายการ`);
    if (meta) {
      meta.textContent = total
        ? `ทั้งหมด ${total.toLocaleString("th-TH")} รายการ · หน้า ${d.page} / ${pages} · ${d.per_page} รายการต่อหน้า`
        : "ยังไม่มีการส่ง";
    }
    const rows = d.submissions || [];
    if (!rows.length) {
      body.innerHTML = `<span class="dim">ไม่พบประวัติการส่ง${pid ? "ของข้อนี้" : ""}</span>`;
      renderAdminHistoryPager(Number(d.page) || 1, pages);
      return;
    }
    body.classList.remove("dim");
    body.innerHTML = adminHistoryTable(rows);
    bindViewButtons(body);
    renderAdminHistoryPager(Number(d.page) || 1, pages);
  } catch (e) {
    body.classList.remove("dim");
    body.innerHTML = `<span class="dim">โหลดประวัติไม่ได้: ${esc(e.message)}</span>`;
    if (meta) meta.textContent = "";
    setDashCount("admin-history-count", "—");
  }
}

function renderAdminTools() {
  buildProblemSelect($("rejudge-problem"));
  buildProblemSelect($("plag-problem"));
  renderContestProblemPicker();
}

/* ---- contest problem picker (checkbox list beats a ctrl-click multiselect) ---- */
function contestProblemIds() {
  const box = $("ct-problems");
  if (!box || !box.querySelectorAll) return [];
  return [...box.querySelectorAll("input[data-pid]")]
    .filter((input) => input.checked)
    .map((input) => input.dataset.pid);
}

function renderContestProblemPicker() {
  const box = $("ct-problems");
  if (!box) return;
  const checked = new Set(contestProblemIds());
  const list = [...state.problems].sort((a, b) =>
    (b.hidden ? 1 : 0) - (a.hidden ? 1 : 0) || (a.no || 9999) - (b.no || 9999));
  box.innerHTML = list.length ? list.map((p) => {
    const no = p.no ? `<span class="pno">${esc(String(p.no))}</span>` : "";
    const meta = [p.id, p.difficulty, p.hidden ? "ซ่อนจาก Task" : ""].filter(Boolean).join(" · ");
    const on = checked.has(p.id);
    return `<label class="ct-problem-option${p.hidden ? " is-contest-only" : ""}${on ? " on" : ""}">` +
      `<input type="checkbox" data-pid="${esc(p.id)}"${on ? " checked" : ""}>` +
      `<span class="ct-problem-main">` +
      `<span class="ct-problem-name">${no}${esc(p.title || p.id)}</span>` +
      `<span class="ct-problem-meta">${esc(meta)}</span>` +
      `</span></label>`;
  }).join("") : `<p class="dim">ยังไม่มีโจทย์ให้เลือก</p>`;
  box.querySelectorAll("input[data-pid]").forEach((input) => {
    input.onchange = () => {
      const option = input.parentElement;
      if (option) option.classList.toggle("on", input.checked);
      updateContestProblemCount();
    };
  });
  updateContestProblemCount();
}

function setContestProblems(ids) {
  const want = new Set(ids || []);
  const box = $("ct-problems");
  if (!box || !box.querySelectorAll) return;
  box.querySelectorAll("input[data-pid]").forEach((input) => {
    input.checked = want.has(input.dataset.pid);
    const option = input.parentElement;
    if (option) option.classList.toggle("on", input.checked);
  });
  updateContestProblemCount();
}

function updateContestProblemCount() {
  const el = $("ct-problem-count");
  if (el) el.textContent = `เลือก ${contestProblemIds().length} ข้อ`;
  updateContestPreview();
}

/* Live summary under the form: the admin sees the phase and duration before
 * saving, and a bad window is flagged instead of failing server-side later. */
function updateContestPreview() {
  const el = $("ct-preview");
  if (!el) return;
  const startV = $("ct-start") ? $("ct-start").value : "";
  const endV = $("ct-end") ? $("ct-end").value : "";
  const a = startV ? Date.parse(startV) : NaN;
  const b = endV ? Date.parse(endV) : NaN;
  const parts = [];
  let err = "";
  if (startV && endV) {
    if (!(b > a)) err = "เวลาจบต้องอยู่หลังเวลาเริ่ม";
    else {
      const now = Date.now();
      const st = now < a ? "upcoming" : now < b ? "running" : "ended";
      parts.push(`${CONTEST_STATUS_TH[st]} · ยาว ${durationText((b - a) / 1000)}`);
    }
  } else {
    parts.push("ยังไม่ได้กำหนดช่วงเวลา");
  }
  parts.push(`${contestProblemIds().length} ข้อ`);
  const freeze = Math.max(0, Number($("ct-freeze") && $("ct-freeze").value) || 0);
  if (freeze) parts.push(`แช่แข็ง ${freeze} นาที`);
  el.textContent = err || parts.join(" · ");
  el.classList.toggle("is-err", !!err);
}

function nextHalfHourInput() {
  const d = new Date();
  d.setMinutes(d.getMinutes() + (30 - (d.getMinutes() % 30)), 0, 0);
  return utcToLocalInput(d.toISOString());
}

function applyContestDuration(minutes) {
  const startEl = $("ct-start");
  if (!startEl) return;
  if (!startEl.value) startEl.value = nextHalfHourInput();
  const start = new Date(startEl.value);
  if (isNaN(start.getTime())) return;
  $("ct-end").value = utcToLocalInput(new Date(start.getTime() + minutes * 60000).toISOString());
  updateContestPreview();
}

function setContestEditorOpen(open, contest) {
  const editor = $("ct-editor");
  if (!editor) return;
  editor.classList.toggle("hidden", !open);
  if (!open) {
    state.editingContest = "";
    return;
  }
  state.editingContest = (contest && contest.id) || "";
  const isEdit = !!state.editingContest;
  $("ct-editor-title").textContent = isEdit ? `แก้ไขคอนเทสต์ · ${state.editingContest}` : "สร้างคอนเทสต์ใหม่";
  $("ct-editor-note").textContent = isEdit
    ? "เพิ่ม/ลดโจทย์หรือปรับเวลาได้กลางคัน การบันทึกมีผลทันที"
    : "กรอกรหัสและช่วงเวลา แล้วเลือกโจทย์อย่างน้อย 1 ข้อ";
  $("ct-id").readOnly = isEdit;
  $("ct-id").classList.toggle("is-readonly", isEdit);
  $("ct-id").value = isEdit ? state.editingContest : "";
  $("ct-title").value = (contest && contest.title) || "";
  $("ct-start").value = contest ? utcToLocalInput(contest.starts_at) : nextHalfHourInput();
  $("ct-end").value = contest ? utcToLocalInput(contest.ends_at) : "";
  $("ct-freeze").value = (contest && contest.freeze_minutes) || 0;
  $("ct-samples").checked = !contest || contest.show_samples !== false;
  $("ct-hide").checked = !contest || contest.hide_from_tasks !== false;
  if ($("ct-problem-search")) $("ct-problem-search").value = "";
  renderContestProblemPicker();
  setContestProblems(contest ? contest.problems || [] : []);
  if (!contest) applyContestDuration(120);
  $("ct-status").textContent = "";
  if (contest) {
    const card = $("contest-card");
    if (card && card.scrollIntoView) card.scrollIntoView({ behavior: "smooth", block: "start" });
  } else {
    const input = $("ct-id");
    if (input && input.focus) input.focus();
  }
  updateContestPreview();
}

function contestCardHtml(c) {
  const flags = [];
  if (c.hide_from_tasks) flags.push("ซ่อนจาก Task");
  if (c.show_samples === false) flags.push("ปิดตัวอย่าง");
  if (c.freeze_minutes) flags.push(`แช่แข็ง ${c.freeze_minutes} นาที`);
  return `<article class="ct-card">` +
    `<div class="ct-card-top">` +
    `<div class="ct-card-main">` +
    `<div class="ct-card-title">${esc(c.title || c.id)}</div>` +
    `<div class="ct-card-id">${esc(c.id)}</div>` +
    `</div>${contestStatusBadge(c.status)}</div>` +
    `<div class="ct-card-window">${esc(contestWindowText(c))}</div>` +
    `<div class="ct-card-facts">` +
    `<span><strong>${(c.problems || []).length}</strong> ข้อ</span>` +
    `<span><strong>${Number(c.participants) || 0}</strong> ผู้ลงทะเบียน</span>` +
    (flags.length ? `<span>${esc(flags.join(" · "))}</span>` : "") +
    `</div>` +
    `<div class="ct-card-actions">` +
    `<button class="ghost-btn" type="button" data-edit-contest="${esc(c.id)}">${icon("settings")}<span>แก้ไข</span></button>` +
    `<a class="ghost-btn" href="#/contests/${encodeURIComponent(c.id)}">${icon("eye")}<span>เปิดหน้า</span></a>` +
    `<button class="ghost-btn" type="button" data-del-contest="${esc(c.id)}"><span>ลบ</span></button>` +
    `</div></article>`;
}

async function loadAdminContests() {
  const box = $("ct-list");
  if (!box) return;
  box.classList.add("dim");
  box.innerHTML = skel(3);
  try {
    const d = await apiGet("/api/contests");
    state.contests = d.contests || [];
    state.contestsConfigured = !!d.configured;
    const countEl = $("admin-tab-contest-count");
    if (countEl) countEl.textContent = String(state.contests.length);
    if (!d.configured) {
      box.innerHTML = `<div class="ct-empty">${esc(CONTESTS_UNCONFIGURED)} — ตั้งค่าฐานข้อมูลก่อนจึงจะสร้างคอนเทสต์ได้</div>`;
      return;
    }
    if (!state.contests.length) {
      box.innerHTML = `<div class="ct-empty">ยังไม่มีคอนเทสต์ — กด “สร้างคอนเทสต์” เพื่อเริ่มรอบแรก</div>`;
      return;
    }
    box.classList.remove("dim");
    box.innerHTML = state.contests.map(contestCardHtml).join("");
    box.querySelectorAll("[data-edit-contest]").forEach((b) => {
      b.onclick = () => {
        const c = (state.contests || []).find((x) => x.id === b.dataset.editContest);
        if (!c) return;
        showAdminTab("contests");
        setContestEditorOpen(true, c);
      };
    });
    box.querySelectorAll("[data-del-contest]").forEach((b) => {
      b.onclick = async () => {
        const cid = b.dataset.delContest;
        if (!window.confirm(`ลบคอนเทสต์ ${cid}? การส่งที่ผูกไว้จะยังอยู่ใน Scoreboard`)) return;
        try {
          await apiDelete(`/api/contests/${encodeURIComponent(cid)}`);
          toast(`ลบ ${cid} แล้ว`, "t-ok");
          if (state.editingContest === cid) setContestEditorOpen(false);
          await refreshProblems();
          loadAdminContests();
        } catch (e) {
          toast("ลบไม่สำเร็จ: " + e.message, "t-err");
        }
      };
    });
  } catch (e) {
    box.innerHTML = `โหลดไม่ได้: ${esc(e.message)}`;
  }
}

/* rejudge: queue jobs, then poll every returned job id to completion. */
$("rejudge-btn").onclick = async () => {
  const btn = $("rejudge-btn"), st = $("rejudge-status");
  const problem = $("rejudge-problem").value;
  const limit = Math.max(1, Number($("rejudge-limit").value) || 10);
  if (!problem) {
    st.textContent = "เลือกข้อก่อน";
    return;
  }
  btn.disabled = true;
  st.textContent = `กำลังเข้าคิวตรวจซ้ำ ${problem} (${limit} ล่าสุด)...`;
  try {
    const d = await apiPostJson("/api/admin/rejudge", { problem, limit });
    const jobs = d.jobs || [];
    st.textContent = `เข้าคิว ${d.queued} งาน · ข้าม ${d.skipped} — กำลังตรวจ...`;
    for (let i = 0; i < jobs.length; i++) {
      const job = await pollJob(jobs[i], (j) => {
        st.textContent =
          `ตรวจซ้ำ ${i + 1}/${jobs.length} — ${jobStateText(j) || "กำลังตรวจ..."}`;
      });
      st.textContent = `ตรวจซ้ำ ${i + 1}/${jobs.length} — ` +
        (job.status === "done" ? "เสร็จ" : `error: ${job.error || "unknown"}`);
    }
    st.textContent =
      `เสร็จแล้ว — เข้าคิว ${d.queued} · ข้าม ${d.skipped} · ตรวจจนจบ ${jobs.length} งาน`;
    loadOverview();
  } catch (e) {
    st.textContent = "ตรวจซ้ำไม่สำเร็จ: " + e.message;
  } finally {
    btn.disabled = false;
  }
};

function simCls(s) {
  s = Number(s) || 0;
  if (s >= 0.9) return "sim-hi";
  if (s >= 0.75) return "sim-mid";
  return "sim-lo";
}

function pairSideHtml(label, id, name, len) {
  return `<div class="io-block">` +
    `<div class="io-head"><span class="io-title">${esc(label)}: ${esc(name || "—")}</span>` +
    `<button class="icon-btn" type="button" data-copy title="คัดลอก submission id">${icon("copy")}</button></div>` +
    `<pre class="io-pre">${esc(id || "—")}</pre>` +
    `<p class="dim">normalized ${esc(String(len == null ? "?" : len))} ตัวอักษร</p></div>`;
}

function renderPlagiarism(d) {
  const box = $("plag-body"), st = $("plag-status");
  const pairs = d.pairs || [];
  st.textContent =
    `สแกน ${d.scanned} การส่ง · threshold ${Number(d.threshold).toFixed(2)} · พบ ${pairs.length} คู่`;
  if (!pairs.length) {
    box.innerHTML = `<span class="dim">ไม่พบโค้ดที่คล้ายกันถึง threshold นี้</span>`;
    return;
  }
  box.classList.remove("dim");
  box.innerHTML = `<table class="tests"><thead><tr>
    <th>#</th><th>ความคล้าย</th><th>A</th><th>B</th><th>ภาษา</th><th>matches</th>
  </tr></thead><tbody>` + pairs.map((p, i) =>
    `<tr class="test-row" data-pair="${i}">
      <td>${String(i + 1).padStart(2, "0")}</td>
      <td><strong class="${simCls(p.similarity)}">${(100 * (Number(p.similarity) || 0)).toFixed(1)}%</strong></td>
      <td>${esc(p.a_name || "—")}</td>
      <td>${esc(p.b_name || "—")}</td>
      <td>${esc(p.language || "")}</td>
      <td>${esc(String(p.matches == null ? "" : p.matches))}</td>
    </tr>
    <tr class="detail-row hidden" data-pair="${i}"><td colspan="6" class="detail-cell">` +
      `<div class="io-grid">` +
      pairSideHtml("A", p.a_id, p.a_name, p.normalized_len_a) +
      pairSideHtml("B", p.b_id, p.b_name, p.normalized_len_b) +
      `</div></td></tr>`).join("") + `</tbody></table>`;
  box.querySelectorAll("tr.test-row").forEach((tr) => {
    tr.onclick = () => {
      const det = box.querySelector(`tr.detail-row[data-pair="${tr.dataset.pair}"]`);
      if (det) det.classList.toggle("hidden");
    };
  });
  bindCopyButtons(box);
}

$("plag-btn").onclick = async () => {
  const box = $("plag-body"), st = $("plag-status");
  const problem = $("plag-problem").value;
  const threshold = Number($("plag-threshold").value) || 0.6;
  if (!problem) {
    st.textContent = "เลือกข้อก่อน";
    return;
  }
  st.textContent = "กำลังสแกน...";
  box.classList.add("dim");
  box.innerHTML = `<span class="dim">กำลังสแกน ${esc(problem)}...</span>`;
  try {
    const d = await apiGet(
      `/api/admin/plagiarism?problem=${encodeURIComponent(problem)}&threshold=${threshold}&limit=300`);
    renderPlagiarism(d);
  } catch (e) {
    st.textContent = "สแกนไม่สำเร็จ: " + e.message;
    box.innerHTML = `<span class="dim">สแกนไม่สำเร็จ: ${esc(e.message)}</span>`;
  }
};

$("plag-threshold").oninput = () => {
  $("plag-threshold-val").textContent = Number($("plag-threshold").value).toFixed(2);
};

/* contest editor controls: the editor stays closed until a card or “สร้าง” opens it */
const ctSearch = $("ct-problem-search");
if (ctSearch) {
  ctSearch.oninput = () => {
    const needle = ctSearch.value.trim().toLowerCase();
    document.querySelectorAll("#ct-problems .ct-problem-option").forEach((opt) => {
      opt.classList.toggle("hidden", !!needle && !opt.textContent.toLowerCase().includes(needle));
    });
  };
}
const ctNew = $("ct-new");
if (ctNew) ctNew.onclick = () => setContestEditorOpen(true, null);
const ctCancel = $("ct-cancel");
if (ctCancel) ctCancel.onclick = () => setContestEditorOpen(false);
const ctClear = $("ct-problem-clear");
if (ctClear) ctClear.onclick = () => setContestProblems([]);
document.querySelectorAll("[data-duration]").forEach((b) => {
  b.onclick = () => applyContestDuration(Number(b.dataset.duration));
});
["ct-start", "ct-end", "ct-freeze"].forEach((id) => {
  const el = $(id);
  if (el) el.oninput = updateContestPreview;
});

$("ct-save").onclick = async () => {
  const st = $("ct-status");
  const btn = $("ct-save");
  const problems = contestProblemIds();
  const payload = {
    id: $("ct-id").value.trim().toLowerCase(),
    title: $("ct-title").value.trim(),
    starts_at: localToUtcIso($("ct-start").value),
    ends_at: localToUtcIso($("ct-end").value),
    problems,
    freeze_minutes: Math.max(0, Number($("ct-freeze").value) || 0),
    show_samples: $("ct-samples").checked,
    hide_from_tasks: $("ct-hide").checked,
  };
  if (!payload.id || !payload.starts_at || !payload.ends_at) {
    st.textContent = "กรอก id / เริ่มแข่ง / จบแข่ง ให้ครบก่อน";
    return;
  }
  if (Date.parse(payload.ends_at) <= Date.parse(payload.starts_at)) {
    st.textContent = "เวลาจบต้องอยู่หลังเวลาเริ่ม";
    return;
  }
  if (!problems.length) {
    st.textContent = "เลือกโจทย์อย่างน้อย 1 ข้อ";
    return;
  }
  btn.disabled = true;
  st.textContent = "กำลังบันทึก...";
  try {
    const d = await apiPostJson("/api/contests", payload);
    const saved = (d.contest && d.contest.id) || payload.id;
    st.textContent = `บันทึกแล้ว: ${saved}`;
    toast(`บันทึกคอนเทสต์ ${saved} แล้ว`, "t-ok");
    // Visibility can change for the problems in this contest: refresh both lists.
    await refreshProblems();
    renderContestProblemPicker();
    setContestProblems(problems);
    // A newly created round stays open for edits, so adding a problem mid-round
    // is one click instead of finding the card again.
    if (!state.editingContest) {
      state.editingContest = saved;
      $("ct-id").readOnly = true;
      $("ct-id").classList.add("is-readonly");
      $("ct-editor-title").textContent = `แก้ไขคอนเทสต์ · ${saved}`;
      $("ct-editor-note").textContent = "เพิ่ม/ลดโจทย์หรือปรับเวลาได้กลางคัน การบันทึกมีผลทันที";
    }
    loadAdminContests();
    if (document.querySelector("#view-contests.active")) renderContests();
  } catch (e) {
    // server validation messages are shown verbatim
    st.textContent = "บันทึกไม่สำเร็จ: " + e.message;
  } finally {
    btn.disabled = false;
  }
};

/* ---------------- init (cache-first, parallel, non-blocking) ---------------- */
// Catalog numbers changed in the approved difficulty-order migration.
const PROBLEMS_CACHE_KEY = "ng_problems_v5";
const PROBLEMS_CACHE_TTL = 3600 * 1000;

function loadProblemsCache() {
  try {
    const raw = localStorage.getItem(PROBLEMS_CACHE_KEY);
    if (!raw) return false;
    const c = JSON.parse(raw);
    if (!c || !Array.isArray(c.data) || Date.now() - c.t > PROBLEMS_CACHE_TTL) return false;
    state.problems = problemRowsForIdentity(c.data);
    return state.problems.length > 0;
  } catch (e) { return false; }
}

function saveProblemsCache() {
  // The admin list carries contest-only problems (hidden/contest_only flags and
  // the extra rows). Never persist it: browsers are often shared, and a cached
  // admin list would flash those titles at the next user of the machine.
  if (state.is_admin || !state.user) {
    try { localStorage.removeItem(PROBLEMS_CACHE_KEY); } catch (e) { /* ignore */ }
    return;
  }
  try {
    localStorage.setItem(PROBLEMS_CACHE_KEY,
      JSON.stringify({ t: Date.now(), data: state.problems }));
  } catch (e) { /* ignore */ }
}

function problemsCacheAgeMs() {
  try {
    const raw = localStorage.getItem(PROBLEMS_CACHE_KEY);
    if (!raw) return Infinity;
    const c = JSON.parse(raw);
    if (!c || !Array.isArray(c.data) || typeof c.t !== "number") return Infinity;
    return Date.now() - c.t;
  } catch (e) { return Infinity; }
}

async function refreshProblems() {
  const gen = _getGen;
  try {
    const d = await apiGet("/api/problems");
    if (gen !== _getGen || !state.user) return false;
    if (d && Array.isArray(d.problems)) {
      state.problems = problemRowsForIdentity(d.problems);
      saveProblemsCache();
      buildFilterOptions();
      if (document.querySelector("#view-tasks.active")) renderTasksTable();
      return true;
    }
  } catch (e) { /* keep whatever we have */ }
  return false;
}

async function loadPublicHome() {
  const gen = _getGen;
  try {
    const data = await apiGet("/api/home");
    if (gen !== _getGen || state.user) return;
    state.problems = data.problems || [];
    state.homeSummary = data.summary || null;
    state.statsLoaded = !!data.configured;
  } catch (e) { /* the landing shell remains usable during an outage */ }
}

/* Canonical tag taxonomy (mirrors KNOWN_TAGS in grader.py). */
const TAGS = [
  "Implementation", "Math", "Ad-hoc", "String", "Data Structure", "DP",
  "Sweepline", "Greedy", "DFS", "Bitmasks", "Binary Search", "Graph",
  "DSU", "Sorting", "Sliding Window", "Divide and Conquer",
  "Matrix Exponential", "Tree", "Fixed Parameter", "Hashing", "Geometry",
  "NP", "Recursive", "Linear Algebra", "Matching",
];

const TAG_SHOWN = 8;              // tags before the "ดูทั้งหมด" toggle
let tagsExpanded = false;

function renderTagList() {
  const box = $("tag-list");
  if (!box) return;
  $("tag-filter-label").textContent = state.filter.tag ? `หมวดโจทย์ · ${state.filter.tag}` : "หมวดโจทย์";
  // Rank by how many problems carry the tag: the useful ones come first instead
  // of opening the sidebar with 28 equal chips.
  const counts = {};
  state.problems.forEach((p) => (p.tags || []).forEach((t) => {
    counts[t] = (counts[t] || 0) + 1;
  }));
  const ranked = TAGS.slice().sort((a, b) =>
    (counts[b] || 0) - (counts[a] || 0) || a.localeCompare(b));
  const shown = tagsExpanded ? ranked : ranked.slice(0, TAG_SHOWN);
  // never hide the tag that is currently filtering the list
  if (!tagsExpanded && state.filter.tag && !shown.includes(state.filter.tag)) {
    shown.push(state.filter.tag);
  }
  box.innerHTML = shown.map((t) =>
    `<button class="tag-row${state.filter.tag === t ? " on" : ""}" data-tag="${esc(t)}">${esc(t)}</button>`
  ).join("");
  if (ranked.length > TAG_SHOWN) {
    box.insertAdjacentHTML("beforeend",
      `<button class="tag-row tag-more" id="tag-more" type="button">${
        tagsExpanded ? "ย่อรายการ" : `ดูทั้งหมด (${ranked.length})`}</button>`);
    $("tag-more").onclick = () => { tagsExpanded = !tagsExpanded; renderTagList(); };
  }
  box.querySelectorAll("[data-tag]").forEach((b) => {
    b.onclick = () => setTagFilter(b.dataset.tag);
  });
}

function setTagFilter(t) {
  state.filter.tag = state.filter.tag === t ? "" : t;
  renderTagList();
  const disclosure = $("tag-filters");
  if (disclosure && disclosure.open) {
    disclosure.open = false;
    disclosure.querySelector("summary").focus({ preventScroll: true });
  }
  if (document.querySelector("#view-tasks.active")) renderTasksTable();
}

function setStatusFilter(s) {
  state.filter.status = s;
  document.querySelectorAll("[data-status]").forEach((b) => {
    b.classList.toggle("active", b.dataset.status === s);
    b.setAttribute("aria-pressed", String(b.dataset.status === s));
  });
  renderTasksTable();
}

function applyHomeFilter(diff = "", tag = "") {
  state.filter.q = "";
  state.filter.diff = diff;
  state.filter.tag = tag;
  $("q").value = "";
  $("f-diff").value = diff;
  renderTagList();
  setStatusFilter("all");
}

document.querySelectorAll("[data-home-level]").forEach((button) => {
  button.onclick = () => {
    state.homeLevel = button.dataset.homeLevel;
    state.homeSelected = null;
    state.homeOffset = 0;
    renderHome(true);
  };
});
document.querySelectorAll("[data-home-tag]").forEach((link) => {
  link.onclick = () => applyHomeFilter("", link.dataset.homeTag);
});
$("home-level-all").onclick = () => applyHomeFilter(state.homeLevel);
$("home-next").onclick = (event) => {
  state.homeOffset += 3;
  state.homeSelected = null;
  renderHome(true);
  revealHomePreview(event);
};

function buildFilterOptions() {
  const diffs = [...new Set(state.problems.map((p) => p.difficulty).filter(Boolean))].sort();
  const fd = $("f-diff"), hf = $("hist-filter");
  const keep = [fd.value, hf.value];
  fd.querySelectorAll("option:not([value=''])").forEach((o) => o.remove());
  hf.querySelectorAll("option:not([value=''])").forEach((o) => o.remove());
  diffs.forEach((d) => {
    const o = document.createElement("option");
    o.value = d; o.textContent = d;
    fd.appendChild(o);
  });
  state.problems.forEach((p) => {
    const o = document.createElement("option");
    o.value = p.id;
    o.textContent = `${p.id} — ${p.title}`;
    hf.appendChild(o);
  });
  [fd, hf].forEach((sel, i) => {
    if ([...sel.options].some((o) => o.value === keep[i])) sel.value = keep[i];
  });
  renderTagList();
}

/* ---------------- init ---------------- */
async function init() {
  $("quick-search").onclick = openPalette;
  $("cookie-settings").onclick = () => maybeShowCookieBanner(true);
  maybeShowCookieBanner();
  const tagFilters = $("tag-filters");
  tagFilters.open = false;
  document.addEventListener("click", (event) => {
    if (tagFilters.open && !tagFilters.contains(event.target)) tagFilters.open = false;
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && tagFilters.open) {
      tagFilters.open = false;
      tagFilters.querySelector("summary").focus();
    }
  });
  bindEditorKeys();
  setLang(lastLang() || state.lang);   // remember the language, default Python
  window.addEventListener("beforeunload", saveDraft);
  const fd = $("f-diff"), hf = $("hist-filter"), fs = $("f-sort");
  try {
    const saved = localStorage.getItem("ng_sort");
    if (saved) state.filter.sort = saved;
    const ff = JSON.parse(localStorage.getItem("ng_filter") || "null");
    if (ff && typeof ff === "object") {
      for (const k of ["q", "diff", "tag", "status"]) {
        if (typeof ff[k] === "string") state.filter[k] = ff[k];
      }
      if ($("q")) $("q").value = state.filter.q;
      if ($("f-diff")) $("f-diff").value = state.filter.diff || "";
      document.querySelectorAll("[data-status]").forEach((b) =>
        {
        const on = (b.dataset.status || "all") === (state.filter.status || "all");
        b.classList.toggle("active", on);
        b.setAttribute("aria-pressed", String(on));
      });
    }
  } catch (e) { /* ignore */ }
  $("q").oninput = (e) => {
    state.filter.q = e.target.value;
    clearTimeout(state._qTimer);
    state._qTimer = setTimeout(renderTasksTable, 180);
  };
  fd.onchange = (e) => { state.filter.diff = e.target.value; renderTasksTable(); };
  if (fs) {
    fs.value = state.filter.sort || "no";
    fs.onchange = (e) => {
      state.filter.sort = e.target.value;
      try { localStorage.setItem("ng_sort", state.filter.sort); } catch (err) { /* ignore */ }
      renderTasksTable();
    };
  }
  $("show-tag").onchange = () => {
    if (document.querySelector("#view-tasks.active")) renderTasksTable();
  };
  document.querySelectorAll("[data-status]").forEach((b) => {
    b.onclick = () => setStatusFilter(b.dataset.status);
  });
  $("tag-clear-side").onclick = () => setTagFilter(state.filter.tag);
  hf.onchange = loadHistory;
  if ($("hist-verdict")) $("hist-verdict").onchange = loadHistory;
  if ($("hist-group")) $("hist-group").onchange = loadHistory;
  $("hist-refresh").onclick = () => loadHistory(true);
  $("auth-go").onclick = () => doPasswordAuth();
  $("auth-code").onkeydown = (e) => { if (e.key === "Enter") doPasswordAuth(); };
  $("auth-pass").onkeydown = (e) => { if (e.key === "Enter") doPasswordAuth(); };
  $("google-btn").onclick = () => {
    location.href = API + "/api/auth/login?" + new URLSearchParams({ next: loginDestination(), remember: String($("auth-remember").checked) }).toString();
  };
  $("report-send").onclick = async () => {
    if (!state.user) {
      $("report-status").textContent = "login ก่อนส่งรีพอร์ต";
      return;
    }
    if (!state.current) return;
    const msg = $("report-msg").value.trim();
    if (!msg) {
      $("report-status").textContent = "พิมพ์ข้อความก่อน";
      return;
    }
    $("report-status").textContent = "กำลังส่ง...";
    try {
      const r = await fetch(API + "/api/report", {
        method: "POST", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          problem: state.current, kind: $("report-kind").value, message: msg }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
      $("report-status").textContent = "ส่งรีพอร์ตแล้ว ขอบคุณ";
      $("report-msg").value = "";
      loadMyReports();
    } catch (e) {
      $("report-status").textContent = "ส่งไม่สำเร็จ: " + e.message;
    }
  };
  // The landing shell may render immediately; private routes wait for both
  // the session and remember-me checks, without painting cached private views.
  router();

  // Load only landing data until the session has been verified.
  const homeP = loadPublicHome();
  let healthErr = null;
  const healthP = apiGet("/api/health").then(() => {
    $("health").textContent = "online";
    $("health").classList.add("ok");
  }).catch((e) => {
    healthErr = e;
    $("health").textContent = "offline";
  });
  // A remembered session must finish restoring before loading member data.
  await Promise.all([
    homeP,
    restoreInitialSession(),
    healthP,
  ]);
  if (state.user) {
    if (loadProblemsCache()) buildFilterOptions();
    await refreshProblems();
  }
  const statsP = state.user ? fetchStats() : Promise.resolve();
  if (healthErr && !state.problems.length && location.hash !== "#/admin") {
    show("home");
    document.querySelector("#view-home .practice-description").textContent =
      "เชื่อมต่อระบบไม่ได้ในขณะนี้ ลองโหลดหน้าอีกครั้งในอีกสักครู่";
    return;
  }
  maybeShowCookieBanner();
  // The identity arrives after the first route render, so a view opened
  // directly (bookmark/reload on #/settings, #/contests, #/task/<id>) can be
  // stale: admin cards hidden, contest banner missing, "your score" blank.
  // Re-render whatever is on screen now that the session is known.
  router();

  // 4. stats never block rendering
  statsP.then(() => {
    if (document.querySelector("#view-home.active")) renderHome();
    else if (document.querySelector("#view-tasks.active")) renderTasksTable();
  });

  if ((location.hash || "").includes("login=failed")) {
    $("health").textContent = "login failed";
    location.hash = "#/";
  }
}

init();
