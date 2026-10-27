// The whiteboard page: Excalidraw (vendored under ./vendor/) joined to voice-claude's control server.
//   GET  /board/api/scene?project=…          the project's scene (elements, appState, files), its version
//   POST /board/api/scene                     the whole scene after you changed it (debounced)
//   GET  /board/api/events?project&since&wait what a line added ("added"), a clear ("cleared"), a look ("snapshot")
//   POST /board/api/snapshot                  the current view as PNG plus every visible text, for the line
// The project id and the run's token come in the URL (…/board/?project=p-xxx&token=…); the window process puts
// them there from daemon.json.
import * as Ex from "./vendor/esm/@excalidraw/excalidraw@0.18.1/index.mjs";

const params = new URLSearchParams(location.search);
const PROJECT = params.get("project") || "";
const TOKEN = params.get("token") || "";
const NAME = params.get("name") || "";
const headers = { "Content-Type": "application/json", "X-Board-Token": TOKEN };

let api = null;           // Excalidraw's imperative API
let version = 0;          // the scene version the server last gave us
let seq = 0;              // the last event we saw
let signature = "";       // what we last saved: elements' ids + versions, view position
let saveTimer = null;
let known = new Set();    // element ids we have (so an "added" event is applied once)

const stateEl = document.getElementById("state");
let stateTimer = null;
function say(text, ms = 2500) {
  stateEl.textContent = text;
  stateEl.classList.add("show");
  clearTimeout(stateTimer);
  if (ms) stateTimer = setTimeout(() => stateEl.classList.remove("show"), ms);
}

async function call(method, path, body) {
  const r = await fetch(path + (path.includes("?") ? "&" : "?") + "project=" + encodeURIComponent(PROJECT),
                        { method, headers, body: body ? JSON.stringify(body) : undefined });
  const data = await r.json().catch(() => ({ ok: false, message: r.statusText }));
  if (!r.ok || data.ok === false) throw new Error(data.message || ("HTTP " + r.status));
  return data;
}

function sceneSignature(elements, appState) {
  let s = elements.length + ":";
  for (const e of elements) s += e.id + "." + e.version + ";";
  const z = appState.zoom ? appState.zoom.value : 1;
  return s + "|" + Math.round(appState.scrollX) + "," + Math.round(appState.scrollY) + "," + z.toFixed(3);
}

function viewAppState(appState) {
  // only what the next opening needs: where you were, the background
  return { scrollX: appState.scrollX, scrollY: appState.scrollY, zoom: appState.zoom,
           viewBackgroundColor: appState.viewBackgroundColor };
}

async function save() {
  if (!api) return;
  const elements = api.getSceneElements();
  const appState = api.getAppState();
  const sig = sceneSignature(elements, appState);
  if (sig === signature) return;
  try {
    const r = await call("POST", "/board/api/scene", { scene: { elements, appState: viewAppState(appState), files: api.getFiles() }, version });
    version = r.version;
    signature = sig;
    known = new Set(elements.map(e => e.id));
  } catch (e) {
    say("没存上：" + e.message, 4000);
  }
}

function scheduleSave() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(save, 800);
}

function visible(elements, appState) {
  // the elements in the current view (scene coordinates); all of them when the view holds none
  const z = appState.zoom ? appState.zoom.value : 1;
  const left = -appState.scrollX, top = -appState.scrollY;
  const right = left + window.innerWidth / z, bottom = top + window.innerHeight / z;
  const inView = elements.filter(e => !e.isDeleted && e.x < right && e.x + e.width > left && e.y < bottom && e.y + e.height > top);
  return inView.length ? inView : elements.filter(e => !e.isDeleted);
}

function textsOf(elements) {
  return elements.filter(e => e.type === "text" && e.text && e.text.trim())
                 .sort((a, b) => (a.y - b.y) || (a.x - b.x)).map(e => e.text.trim());
}

async function snapshot(request) {
  if (!api) return;
  const elements = visible(api.getSceneElements(), api.getAppState());
  let png = "";
  if (elements.length) {
    const blob = await Ex.exportToBlob({ elements, appState: { ...api.getAppState(), exportBackground: true },
                                         files: api.getFiles(), mimeType: "image/png", maxWidthOrHeight: 1600 });
    png = await new Promise(res => { const fr = new FileReader(); fr.onload = () => res(fr.result); fr.readAsDataURL(blob); });
  }
  await call("POST", "/board/api/snapshot", { request, png, texts: textsOf(elements) });
}

async function look() {
  const b = document.getElementById("look");
  b.disabled = true;
  try {
    await snapshot("user");
    say("已经交给它看了");
  } catch (e) {
    say("没发出去：" + e.message, 4000);
  } finally {
    b.disabled = false;
  }
}

function applyAdded(ev) {
  const fresh = (ev.elements || []).filter(e => !known.has(e.id));
  if (!fresh.length) return;
  const files = Object.values(ev.files || {});
  if (files.length) api.addFiles(files);
  const elements = [...api.getSceneElements(), ...fresh];
  api.updateScene({ elements });
  for (const e of fresh) known.add(e.id);
  signature = "";  // ours now too: the next save includes them
  if (ev.version) version = ev.version;
  const first = fresh[0];
  api.scrollToContent(fresh, { fitToContent: false, animate: true });
  say(first && first.type === "image" ? "它放了一张图" : "它放了一张卡片");
}

async function reload() {
  const r = await call("GET", "/board/api/scene");
  version = r.version;
  const elements = r.scene.elements || [];
  known = new Set(elements.map(e => e.id));
  api.updateScene({ elements, appState: r.scene.appState || {} });
  if (r.scene.files) api.addFiles(Object.values(r.scene.files));
  signature = sceneSignature(elements, api.getAppState());
}

async function poll() {
  for (;;) {
    try {
      const r = await call("GET", `/board/api/events?since=${seq}&wait=25`);
      seq = r.seq;
      for (const ev of r.events || []) {
        if (ev.kind === "added") applyAdded(ev);
        else if (ev.kind === "cleared") { await reload(); say("白板清空了"); }
        else if (ev.kind === "snapshot") { try { await snapshot(ev.request); say("它看了一眼白板"); } catch (e) { say("截图没成：" + e.message, 4000); } }
      }
    } catch (e) {
      say("连不上后台服务：" + e.message, 0);
      await new Promise(res => setTimeout(res, 3000));
    }
  }
}

async function main() {
  if (!PROJECT || !TOKEN) { say("这页要由 voice-claude 打开（缺 project 或 token）", 0); return; }
  if (NAME) document.title = `${NAME} 白板 — voice-claude`;
  let initial = { elements: [], appState: {}, files: {} };
  try {
    const r = await call("GET", "/board/api/scene");
    version = r.version;
    seq = r.seq || 0;
    initial = { elements: r.scene.elements || [], appState: r.scene.appState || {}, files: r.scene.files || {} };
  } catch (e) {
    say("读不到白板：" + e.message, 0);
  }
  known = new Set(initial.elements.map(e => e.id));
  const root = ReactDOM.createRoot(document.getElementById("app"));
  root.render(React.createElement(Ex.Excalidraw, {
    langCode: "zh-CN",
    initialData: { ...initial, appState: { ...initial.appState, collaborators: new Map() } },
    excalidrawAPI: (a) => {
      api = a;
      signature = sceneSignature(api.getSceneElements(), api.getAppState());
      poll();
    },
    onChange: () => { if (api) scheduleSave(); },
    UIOptions: { canvasActions: { loadScene: false, saveToActiveFile: false, export: false, saveAsImage: true } },
  }));
  document.getElementById("look").addEventListener("click", look);
  window.addEventListener("beforeunload", () => { clearTimeout(saveTimer); save(); });
}

main();
