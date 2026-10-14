const routes = [];
let outlet = null;
let current = null;
let afterRoute = null;

export function route(pattern, view) {
  const keys = [];
  const source = pattern.replace(/:(\w+)/g, (_, key) => {
    keys.push(key);
    return "([^/]+)";
  });
  routes.push({ pattern, keys, view, re: new RegExp(`^${source}/?$`) });
}

export function currentPath() {
  return location.hash.replace(/^#/, "") || "/chat";
}

export function navigate(path) {
  if (currentPath() === path) render();
  else location.hash = path;
}

export function replacePath(path) {
  history.replaceState(null, "", `#${path}`);
  afterRoute?.(path.split("?")[0]);
}

let listening = false;

export function startRouter(el, onRoute) {
  outlet = el;
  afterRoute = onRoute;
  if (!listening) {
    window.addEventListener("hashchange", render);
    listening = true;
  }
  render();
}

function render() {
  const [path, search = ""] = currentPath().split("?");
  const query = Object.fromEntries(new URLSearchParams(search));
  const match = routes.find((r) => r.re.test(path)) || routes[0];
  const found = path.match(match.re) || [];
  const params = {};
  match.keys.forEach((key, i) => {
    params[key] = decodeURIComponent(found[i + 1] || "");
  });
  try {
    current?.destroy?.();
  } catch (err) {
    console.warn(err);
  }
  current = match.view({ params, query, path });
  outlet.replaceChildren(current.el);
  afterRoute?.(path);
}
