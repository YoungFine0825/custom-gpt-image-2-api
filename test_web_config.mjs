// 配置节点前端扩展的无浏览器单测 (headless test for the config-node extension)。
//
//     node test_web_config.mjs        // 全部通过打印 ALL PASS
//
// 为什么需要它 (why)：密钥的存档/回填逻辑全在前端，而它的正确性取决于 ComfyUI
// 前端几个**互不对称**的行为(序列化跳过 serialize:false 但保留下标、configure
// 读取用压缩计数器、clone 在「尚未加进 graph」时就 configure)。这些只靠读代码
// 极易看漏——本文件把它们照搬成可执行的假 litegraph，直接驱动**真实的**
// web/gpt_image_config_security.js 跑用户会遇到的操作序列。
//
// 下面 FakeNode 的 serialize/configure/clone 三个方法是从实际安装的
// comfyui_frontend_package 打包产物里逐字对照抄下来的语义(非猜测)：
//   serialize()  : `if (w.serialize === false) continue; data.widgets_values[i] = w.value`
//                  —— 跳过不序列化的 widget，但**下标 i 仍是原始下标**(留空位)
//   configure()  : 先逐键还原 properties，再 `let t = 0; for (w of widgets)
//                  if (w.serialize !== false) w.value = widgets_values[t++]`
//                  —— 读取用**压缩计数器**，最后才调 onConfigure
//   clone()      : createNode → cloneObject(serialize()) → configure()
//                  —— **此时克隆体还没被加进 graph**
//   LGraph.configure() / 粘贴：**先把所有节点 add 进 graph，再逐个 configure**
//
// 注意 (caveat)：这是对上述语义的忠实模拟，不是真的 litegraph；它能锁住我们
// 依赖的那几条不变量，但不替代在真实 ComfyUI 里点一遍。

import { mkdirSync, copyFileSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const CONFIG_NODE = "ImageAPIConfig";
const BASE_W = "接口地址";
const KEY_W = "密钥";
const PROP_ID = "gptImageConfigId";
const LS_LEGACY_PREFIX = "meomeo-dev.gpt-image.apikey::";

// ── 把真实扩展放进 ComfyUI 的目录形状里，好让 `../../scripts/app.js` 能解析 ──
const root = join(tmpdir(), "gptimg-web-test-" + process.pid);
mkdirSync(join(root, "scripts"), { recursive: true });
mkdirSync(join(root, "extensions", "x"), { recursive: true });
writeFileSync(join(root, "scripts", "app.js"),
    "export const app = globalThis.__app;\n");
// GPTIMG_EXT 可指向别的实现（用来验证「这些用例确实能抓到旧版的 bug」）。
const extSrc = process.env.GPTIMG_EXT
    ? new URL("file://" + process.env.GPTIMG_EXT)
    : new URL("./web/gpt_image_config_security.js", import.meta.url);
copyFileSync(extSrc, join(root, "extensions", "x", "ext.mjs"));

// ── 假 localStorage ────────────────────────────────────────────────────────
const store = new Map();
globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => void store.set(k, String(v)),
    removeItem: (k) => void store.delete(k),
    key: (i) => Array.from(store.keys())[i] ?? null,
    get length() { return store.size; },
};

// ── 假 app / graph ────────────────────────────────────────────────────────
let ext = null;
const graph = {
    _nodes: [],
    add(node) { this._nodes.push(node); return node; },
    setDirtyCanvas() {},
};
globalThis.__app = {
    graph,
    registerExtension(e) { ext = e; },
};

const nodeType = { prototype: {} };
await import(join(root, "extensions", "x", "ext.mjs"));
await ext.beforeRegisterNodeDef(nodeType, { name: CONFIG_NODE });

// ── 假 litegraph 节点（三个方法的语义见文件头）────────────────────────────
class FakeNode {
    constructor(type = CONFIG_NODE) {
        this.type = type;
        this.properties = {};
        // widget 顺序与 config_node.py 的 INPUT_TYPES 一致：密钥必须在最后。
        this.widgets = [
            { name: BASE_W, value: "", callback: null },
            { name: KEY_W, value: "", callback: null },
        ];
        this.serialize_widgets = true;
        nodeType.prototype.onNodeCreated?.call(this);
    }

    get base() { return this.widgets.find((w) => w.name === BASE_W).value; }
    get key() { return this.widgets.find((w) => w.name === KEY_W).value; }
    get uuid() { return this.properties[PROP_ID]; }

    /** 模拟用户在 widget 里输入（litegraph 在用户交互时才调 callback）。 */
    type_(name, value) {
        const w = this.widgets.find((x) => x.name === name);
        w.value = value;
        w.callback?.(value);
    }

    serialize() {
        const data = { type: this.type };
        if (this.properties) data.properties = structuredClone(this.properties);
        if (this.widgets && this.serialize_widgets) {
            data.widgets_values = [];
            for (const [i, w] of this.widgets.entries()) {
                if (w.serialize === false) continue;       // 跳过，但下标不压缩
                data.widgets_values[i] = w.value ?? null;
            }
        }
        return data;
    }

    configure(info) {
        for (const k in info) {
            if (k === "properties") {
                for (const p in info.properties) this.properties[p] = info.properties[p];
                continue;
            }
            if (k === "widgets_values" || k === "type") continue;
            this[k] = info[k];
        }
        if (info.widgets_values) {
            let t = 0;                                     // 读取用压缩计数器
            for (const w of this.widgets ?? []) {
                if (w.serialize === false) continue;
                if (t >= info.widgets_values.length) break;
                w.value = info.widgets_values[t++];
            }
        }
        nodeType.prototype.onConfigure?.call(this, info);
    }

    /** LGraphNode.clone()：克隆体在 configure 时**还没被加进 graph**。 */
    clone() {
        const copy = new FakeNode(this.type);
        copy.configure(JSON.parse(JSON.stringify(this.serialize())));
        return copy;
    }
}

// ── 测试基建 ──────────────────────────────────────────────────────────────
let failures = 0;
function check(name, cond, detail) {
    if (cond) return;
    failures++;
    console.error("✗ " + name + (detail === undefined ? "" : "\n    " + detail));
}

/** 新建节点并加进 graph（对应从菜单拖一个节点出来）。 */
function addNode() {
    return graph.add(new FakeNode());
}

/** 保存工作流：序列化当前 graph 里的所有节点。 */
function save() {
    return JSON.parse(JSON.stringify(graph._nodes.map((n) => n.serialize())));
}

/** 刷新/重开工作流：LGraph.configure —— 先全部 add，再逐个 configure。 */
function reload(saved) {
    graph._nodes = [];
    const created = saved.map(() => graph.add(new FakeNode()));
    created.forEach((node, i) => node.configure(saved[i]));
    return created;
}

/** 粘贴：也是先 add 再 configure。 */
function paste(node) {
    const data = JSON.parse(JSON.stringify(node.serialize()));
    const copy = graph.add(new FakeNode());
    copy.configure(data);
    return copy;
}

function reset() {
    store.clear();
    graph._nodes = [];
}

// ── 场景 1：基本往返（密钥不进工作流，但刷新后能回填）──────────────────
reset();
{
    const a = addNode();
    a.type_(BASE_W, "https://gw-a.example.com/v1");
    a.type_(KEY_W, "sk-aaa");
    const saved = save();
    check("场景1 密钥不写进工作流 JSON",
        !JSON.stringify(saved).includes("sk-aaa"), JSON.stringify(saved));
    const [a2] = reload(saved);
    check("场景1 刷新后密钥自动回填", a2.key === "sk-aaa", a2.key);
    check("场景1 地址照常随工作流保存",
        a2.base === "https://gw-a.example.com/v1", a2.base);
}

// ── 场景 2：先填密钥、后填地址（v3.4.1 会静默丢失）────────────────────
reset();
{
    const a = addNode();
    a.type_(KEY_W, "sk-first");                  // 地址仍为空
    a.type_(BASE_W, "https://gw-a.example.com/v1");
    const [a2] = reload(save());
    check("场景2 先填密钥也不丢", a2.key === "sk-first", a2.key);
}

// ── 场景 3：两个节点、同一地址、不同密钥（v3.4.1 会互相覆盖）──────────
reset();
{
    const a = addNode();
    a.type_(BASE_W, "https://same.example.com/v1");
    a.type_(KEY_W, "sk-account-1");
    const b = addNode();
    b.type_(BASE_W, "https://same.example.com/v1");
    b.type_(KEY_W, "sk-account-2");
    check("场景3 两节点 uuid 不同", a.uuid && b.uuid && a.uuid !== b.uuid,
        `${a.uuid} / ${b.uuid}`);
    check("场景3 A 的密钥没被 B 覆盖(内存中)", a.key === "sk-account-1", a.key);
    const [a2, b2] = reload(save());
    check("场景3 刷新后 A 仍是自己的密钥", a2.key === "sk-account-1", a2.key);
    check("场景3 刷新后 B 仍是自己的密钥", b2.key === "sk-account-2", b2.key);
}

// ── 场景 4：Clone 节点 → 改密钥 → 刷新（用户报告的丢失场景）────────────
// 「clone 出来的应该是一个全新的节点，即便 URL 和密钥长得一样」。
reset();
{
    const a = addNode();
    a.type_(BASE_W, "https://same.example.com/v1");
    a.type_(KEY_W, "sk-original");

    const c = graph.add(a.clone());               // 菜单 Clone：configure 时尚未入图
    check("场景4 clone 得到全新 uuid", c.uuid && c.uuid !== a.uuid,
        `original=${a.uuid} clone=${c.uuid}`);

    c.type_(KEY_W, "sk-clone");                   // 在克隆体上配置自己的密钥
    check("场景4 改克隆体不动原节点(内存)", a.key === "sk-original", a.key);

    const [a2, c2] = reload(save());
    check("场景4 刷新后原节点保住自己的密钥", a2.key === "sk-original", a2.key);
    check("场景4 刷新后克隆体保住自己的密钥", c2.key === "sk-clone", c2.key);
}

// ── 场景 5：Clone 后不改任何东西就刷新（克隆体应开箱可用）──────────────
reset();
{
    const a = addNode();
    a.type_(BASE_W, "https://same.example.com/v1");
    a.type_(KEY_W, "sk-original");
    const c = graph.add(a.clone());
    check("场景5 clone 继承密钥(开箱可用)", c.key === "sk-original", c.key);
    const [a2, c2] = reload(save());
    check("场景5 刷新后原节点仍有密钥", a2.key === "sk-original", a2.key);
    check("场景5 刷新后克隆体仍有密钥", c2.key === "sk-original", c2.key);
}

// ── 场景 6：复制/粘贴（与 clone 不同：粘贴是先入图再 configure）────────
reset();
{
    const a = addNode();
    a.type_(BASE_W, "https://same.example.com/v1");
    a.type_(KEY_W, "sk-original");
    const p = paste(a);
    check("场景6 粘贴得到全新 uuid", p.uuid && p.uuid !== a.uuid,
        `original=${a.uuid} pasted=${p.uuid}`);
    p.type_(KEY_W, "sk-pasted");
    const [a2, p2] = reload(save());
    check("场景6 刷新后原节点保住自己的密钥", a2.key === "sk-original", a2.key);
    check("场景6 刷新后粘贴体保住自己的密钥", p2.key === "sk-pasted", p2.key);
}

// ── 场景 7：连续 clone 三份，各自独立 ──────────────────────────────────
reset();
{
    const a = addNode();
    a.type_(BASE_W, "https://same.example.com/v1");
    a.type_(KEY_W, "sk-0");
    const c1 = graph.add(a.clone());
    const c2 = graph.add(a.clone());
    const c3 = graph.add(c1.clone());             // 克隆体再 clone
    const ids = new Set([a, c1, c2, c3].map((n) => n.uuid));
    check("场景7 四个节点四个不同 uuid", ids.size === 4, [...ids].join(" "));
    c1.type_(KEY_W, "sk-1");
    c2.type_(KEY_W, "sk-2");
    c3.type_(KEY_W, "sk-3");
    const got = reload(save()).map((n) => n.key);
    check("场景7 刷新后四把密钥各自归位",
        JSON.stringify(got) === JSON.stringify(["sk-0", "sk-1", "sk-2", "sk-3"]),
        JSON.stringify(got));
}

// ── 场景 8：旧版(v3.4.1)按 base_url 的存档仍能迁移回填 ──────────────────
reset();
{
    localStorage.setItem(LS_LEGACY_PREFIX + "https://legacy.example.com/v1", "sk-legacy");
    const saved = [{
        type: CONFIG_NODE, properties: {},
        widgets_values: ["https://legacy.example.com/v1"],
    }];
    const [a] = reload(saved);
    check("场景8 旧存档迁移回填", a.key === "sk-legacy", a.key);
    check("场景8 迁移后落成本节点自己的存档", !!a.uuid, a.uuid);
}

// ── 场景 9：清空密钥会删掉本机存档（不残留）────────────────────────────
reset();
{
    const a = addNode();
    a.type_(BASE_W, "https://gw.example.com/v1");
    a.type_(KEY_W, "sk-temp");
    a.type_(KEY_W, "");
    const [a2] = reload(save());
    check("场景9 清空后不再回填", !a2.key, a2.key);
    check("场景9 存档已删除",
        ![...store.keys()].some((k) => store.get(k)?.includes("sk-temp")),
        [...store.entries()].join(" "));
}

// ── 收尾 ──────────────────────────────────────────────────────────────────
rmSync(root, { recursive: true, force: true });
if (failures) {
    console.error(`\n${failures} 项失败`);
    process.exit(1);
}
console.log("ALL PASS");
