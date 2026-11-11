"use strict";
(() => {
  // packages/foundation/src/index.ts
  var parse = (value) => value === "light" || value === "dark" ? value : "system";
  function initColorMode({ storageKey: storageKey2, target = document.documentElement }) {
    const win = target.ownerDocument.defaultView;
    const media = win.matchMedia("(prefers-color-scheme: dark)");
    let mode = "system";
    try {
      mode = parse(win.localStorage.getItem(storageKey2));
    } catch {
    }
    let disposed = false;
    const listeners = /* @__PURE__ */ new Set();
    const resolved = () => mode === "system" ? media.matches ? "dark" : "light" : mode;
    const apply = () => {
      target.dataset.fastuiTheme = resolved();
      listeners.forEach((listener) => listener({ mode, resolved: resolved() }));
    };
    const systemChange = () => {
      if (mode === "system") apply();
    };
    const storageChange = (event) => {
      let storage;
      try {
        storage = win.localStorage;
      } catch {
        return;
      }
      if (event.storageArea !== storage || event.key !== storageKey2 && event.key !== null) return;
      mode = parse(event.newValue);
      apply();
    };
    media.addEventListener("change", systemChange);
    win.addEventListener("storage", storageChange);
    apply();
    return {
      get mode() {
        return mode;
      },
      get resolved() {
        return resolved();
      },
      setMode(value) {
        if (disposed) return;
        mode = parse(value);
        try {
          win.localStorage.setItem(storageKey2, mode);
        } catch {
        }
        apply();
      },
      subscribe(listener) {
        if (disposed) return () => {
        };
        listeners.add(listener);
        listener({ mode, resolved: resolved() });
        return () => {
          listeners.delete(listener);
        };
      },
      dispose() {
        disposed = true;
        media.removeEventListener("change", systemChange);
        win.removeEventListener("storage", storageChange);
        listeners.clear();
      }
    };
  }

  // packages/foundation/src/bootstrap.ts
  var storageKey = document.currentScript.dataset.storageKey;
  if (!storageKey) throw new Error("FastUI color-mode.js requires data-storage-key on its script element.");
  initColorMode({ storageKey });
})();
