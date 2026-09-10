import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import * as React from "react";
import ts from "typescript";

const require = createRequire(import.meta.url);

// Execute the production TS/TSX module with explicit service dependencies.
// This keeps interaction tests independent of a browser and a live data store.
export async function loadModule(relativePath, dependencies = {}) {
  const source = await readFile(new URL(relativePath, import.meta.url), "utf8");
  const { outputText } = ts.transpileModule(source, {
    fileName: relativePath,
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      jsx: ts.JsxEmit.ReactJSX,
      esModuleInterop: true,
    },
  });
  const module = { exports: {} };
  const resolve = (name) => dependencies[name] ?? require(name);
  new Function("require", "module", "exports", outputText)(resolve, module, module.exports);
  return module.exports;
}

export function componentHarness() {
  const slots = [];
  const effects = [];
  let cursor = 0;
  const hooks = {
    ...React,
    useState(initial) {
      const index = cursor++;
      if (!(index in slots)) {
        slots[index] = typeof initial === "function" ? initial() : initial;
      }
      return [slots[index], (next) => {
        slots[index] = typeof next === "function" ? next(slots[index]) : next;
      }];
    },
    useRef(initial) {
      const [value] = hooks.useState(() => ({ current: initial }));
      return value;
    },
    useMemo(factory, dependencies) {
      const index = cursor++;
      const prior = slots[index];
      if (!prior || !dependencies || dependencies.some((value, i) => !Object.is(value, prior.dependencies[i]))) {
        slots[index] = { value: factory(), dependencies };
      }
      return slots[index].value;
    },
    useCallback(callback, dependencies) { return hooks.useMemo(() => callback, dependencies); },
    useEffect(effect, dependencies) {
      const index = cursor++;
      const prior = slots[index];
      if (!prior || dependencies?.some((value, i) => !Object.is(value, prior[i]))) {
        effects.push(effect);
      }
      slots[index] = dependencies ?? [];
    },
  };
  return {
    hooks,
    render(Component, props = {}) {
      cursor = 0;
      return Component(props);
    },
    async settle() {
      effects.splice(0).forEach((effect) => effect());
      // Service operations in these tests are mocked promises, without timers.
      await new Promise((resolve) => setImmediate(resolve));
    },
  };
}

export function elements(node) {
  if (Array.isArray(node)) return node.flatMap(elements);
  if (!React.isValidElement(node)) return [];
  return [node, ...elements(node.props.children)];
}

export function renderedText(node) {
  if (Array.isArray(node)) return node.map(renderedText).join("");
  if (React.isValidElement(node)) return renderedText(node.props.children);
  return typeof node === "string" || typeof node === "number" ? String(node) : "";
}
