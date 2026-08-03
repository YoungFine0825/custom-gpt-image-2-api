// GPT-Image 配置节点安全扩展 (security extension)
//
// 两个职责 (two responsibilities)：
//
//   1) 防泄露 (no leak)：把「密钥」widget 的 widget.serialize 设为 false，
//      密钥永不写进 widgets_values —— 本地保存的 .json、导出、输出 PNG 内嵌的
//      workflow 都不含它。注意这是 widget 自身属性(控制持久化)，不是
//      options.serialize(控制是否随 prompt 发给后端执行)——后者不动，
//      密钥照常参与执行。两条路径互相独立。
//
//   2) 每个节点独立的本机存档 (per-node local profile)：密钥不进工作流，
//      重开工作流就没了，所以另存进浏览器 localStorage 并在载入后自动回填。
//
// 为什么改成「按节点」归档 (why per-node)：
//   v3.4.1 及以前存档按 base_url 归档(键 = 前缀 + base_url)，那是**所有配置
//   节点共享的一格**。于是：
//     - 两个配置节点填同一个 base_url 时，后改的密钥覆盖前一个的存档；
//       重开工作流两个节点都被回填成最后写入的那把密钥(状态被串改)。
//     - 「先填密钥、后填地址」时 base_url 还是空的 → 存档键算不出来 →
//       密钥根本没落盘 → 重开即丢。
//   现在每个配置节点在 node.properties 里持有一个 uuid(随工作流一起序列化、
//   在 configure 时还原)，存档键 = 前缀 + uuid，**写入只碰自己那一格**，
//   节点之间再不互相覆盖。复制/粘贴出的副本会继承同一个 uuid，故写入前做
//   去重 (dedup)：副本换用新 uuid 并继承一份原存档，此后各自独立。
//
// 回填优先级 (restore order)：
//   ① 本节点自己的存档；
//   ② 同 base_url 下最近写入的存档(新建节点填一个用过的地址即自动带出密钥，
//      纯便利路径，不构成共享状态——写入永远只写自己那一格)；
//   ③ v3.4.1 的旧版按-base_url 存档(**只读**，平滑迁移用，不再写入)。
//   三者都只在密钥 widget 为空时才填，绝不覆盖用户手输的值。
//
// 权衡 (tradeoff)：localStorage 是明文存在浏览器 profile 里，同源 JS 可读、
// 也留在磁盘。它严格优于「写进会被分享的工作流」，是社区处理前端密钥的
// 标准做法，但不是加密保险箱。想要更强隔离可改存服务端本地配置文件。

import { app } from "../../scripts/app.js";

// 配置节点内部 key（见 __init__.py 的 NODE_CLASS_MAPPINGS）。
const CONFIG_NODE = "ImageAPIConfig";
// widget 名（见 config_node.py 的 INPUT_TYPES）。
const SECRET_WIDGET = "密钥";
const BASEURL_WIDGET = "接口地址";

// 按节点归档的存档键前缀（值是 JSON: {base, key, ts}）。
const LS_NODE_PREFIX = "meomeo-dev.gpt-image.node::";
// v3.4.1 及以前的按-base_url 存档前缀（只读，迁移用）。
const LS_LEGACY_PREFIX = "meomeo-dev.gpt-image.apikey::";
// 节点身份存放处：litegraph 的 properties 会随工作流序列化、并在 configure
// 时逐键还原，是 ComfyUI 里给节点挂持久化附加状态的既有机制。
// 不用 node.id：它跨工作流会碰撞（见 docs/usage-and-security.md 第 6.3 节）。
const PROP_ID = "gptImageConfigId";

// 与后端 config_node.build 的归一化保持一致：去空白、去末尾斜杠。
// 保证「存」和「取」用的键一致，无论用户填了几个尾斜杠。
function normalizeBaseUrl(v) {
    return (v || "").trim().replace(/\/+$/, "");
}

function newId() {
    try {
        if (crypto?.randomUUID) return crypto.randomUUID();
    } catch (e) { /* 落到下面的兜底 */ }
    return "n" + Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
}

function readRecord(id) {
    if (!id) return null;
    try {
        const raw = localStorage.getItem(LS_NODE_PREFIX + id);
        if (!raw) return null;
        const rec = JSON.parse(raw);
        return rec && typeof rec === "object" ? rec : null;
    } catch (e) {
        return null;   // 存档损坏(手改过/旧格式)：当作没有，不影响使用
    }
}

function writeRecord(id, base, key) {
    if (!id) return;
    try {
        if (key) {
            localStorage.setItem(LS_NODE_PREFIX + id, JSON.stringify({
                base: normalizeBaseUrl(base), key, ts: Date.now(),
            }));
        } else {
            // 用户清空了密钥 → 删掉本机存档，避免残留。
            localStorage.removeItem(LS_NODE_PREFIX + id);
        }
    } catch (e) {
        console.error("[GPT-Image] 无法写入本机密钥存档：", e);
    }
}

/** 同 base_url 下最近写入的存档密钥（便利路径：新建节点填个用过的地址即带出密钥）。 */
function newestKeyForBase(base, excludeId) {
    const want = normalizeBaseUrl(base);
    if (!want) return null;
    let best = null;
    try {
        for (let i = 0; i < localStorage.length; i++) {
            const k = localStorage.key(i);
            if (!k || !k.startsWith(LS_NODE_PREFIX)) continue;
            if (excludeId && k === LS_NODE_PREFIX + excludeId) continue;
            const rec = readRecord(k.slice(LS_NODE_PREFIX.length));
            if (!rec?.key || normalizeBaseUrl(rec.base) !== want) continue;
            if (!best || (rec.ts || 0) > (best.ts || 0)) best = rec;
        }
    } catch (e) {
        return null;
    }
    return best?.key || null;
}

/** v3.4.1 旧版按-base_url 存档（只读，迁移用）。 */
function legacyKeyForBase(base) {
    const b = normalizeBaseUrl(base);
    if (!b) return null;
    try {
        return localStorage.getItem(LS_LEGACY_PREFIX + b);
    } catch (e) {
        return null;
    }
}

function liveConfigNodes() {
    const nodes = app.graph?._nodes || app.graph?.nodes || [];
    return Array.from(nodes).filter((n) => n?.type === CONFIG_NODE);
}

/** 取本节点的 uuid；没有就新建。与其它活节点撞号(复制/粘贴)时本节点改用新号。
 *
 * 复制节点时 litegraph 会连 properties 一起克隆，副本因此继承同一个 uuid ——
 * 若不处理，副本一改密钥就会写进原节点那一格(又变回互相覆盖)。这里在**写入前**
 * 做去重：把发现撞号的那个节点(通常就是后来粘贴的副本)换成新号，并继承一份
 * 原存档，于是副本开箱即用、此后各自独立。
 */
function ensureNodeId(node) {
    node.properties ??= {};
    let id = node.properties[PROP_ID];
    if (!id) {
        id = newId();
        node.properties[PROP_ID] = id;
        return id;
    }
    const twin = liveConfigNodes().find(
        (n) => n !== node && n.properties?.[PROP_ID] === id);
    if (twin) {
        const inherited = readRecord(id);
        id = newId();
        node.properties[PROP_ID] = id;
        if (inherited?.key) writeRecord(id, inherited.base, inherited.key);
    }
    return id;
}

app.registerExtension({
    name: "meomeo-dev.gpt-image.config-security",

    async beforeRegisterNodeDef(nodeType, nodeData /*, app */) {
        if (nodeData?.name !== CONFIG_NODE) return;

        const widgetsOf = (node) => ({
            baseW: node.widgets?.find((x) => x?.name === BASEURL_WIDGET),
            keyW: node.widgets?.find((x) => x?.name === SECRET_WIDGET),
        });

        // 从本机存档回填密钥：仅当密钥 widget 为空时才填，不覆盖用户手输的值。
        // 三级来源见文件头「回填优先级」。用到 ②/③ 时顺手落成本节点自己的存档，
        // 让旧版用户平滑迁移到按节点归档。
        const restoreKey = (node) => {
            const { baseW, keyW } = widgetsOf(node);
            if (!baseW || !keyW) return;
            if (keyW.value) return;              // 已有值，不覆盖
            const id = ensureNodeId(node);
            const own = readRecord(id);
            let key = own?.key || null;
            let needPersist = false;
            if (!key) {
                key = newestKeyForBase(baseW.value, id) || legacyKeyForBase(baseW.value);
                needPersist = !!key;
            }
            if (!key) return;
            node.__restoringKey = true;          // 防止回填触发保存回环
            keyW.value = key;
            node.__restoringKey = false;
            if (needPersist) writeRecord(id, baseW.value, key);
            app.graph?.setDirtyCanvas(true, true);
        };

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const ret = onNodeCreated ? onNodeCreated.apply(this, arguments) : undefined;
            try {
                const { baseW, keyW } = widgetsOf(this);

                if (keyW) {
                    // (1) 阻止密钥写进 widgets_values（保存/导出/PNG 内嵌都不含它）。
                    //     必须在 configure 应用 widgets_values 之前设好——ComfyUI 前端
                    //     读 widgets_values 时会跳过 serialize===false 的 widget，
                    //     写入时同样跳过，两边一致才不会串位。
                    keyW.serialize = false;

                    // 密钥变化 → 写进本节点自己的存档（回填过程中不重复写）。
                    const origKeyCb = keyW.callback;
                    keyW.callback = function (value) {
                        const r = origKeyCb ? origKeyCb.apply(this, arguments) : undefined;
                        if (!this.__restoringKey) {
                            writeRecord(ensureNodeId(this), baseW?.value, value);
                        }
                        return r;
                    }.bind(this);
                }

                if (baseW) {
                    // 地址变化 → ① 密钥为空时试着按新地址带出密钥（新建节点先填
                    // 地址、再自动带出对应密钥）；② 已有密钥则更新存档里的地址，
                    // 使存档始终反映本节点当前状态。
                    const origBaseCb = baseW.callback;
                    baseW.callback = function (value) {
                        const r = origBaseCb ? origBaseCb.apply(this, arguments) : undefined;
                        restoreKey(this);
                        const { keyW: kw } = widgetsOf(this);
                        if (kw?.value) writeRecord(ensureNodeId(this), value, kw.value);
                        return r;
                    }.bind(this);
                }
            } catch (e) {
                console.error("[GPT-Image] 配置节点安全扩展初始化失败：", e);
            }
            return ret;
        };

        // 载入工作流/粘贴节点后回填密钥。configure() 会先逐键还原 properties
        // （含本节点 uuid）、再套用 widgets_values，最后才调 onConfigure，
        // 故此时 uuid 与 base_url 都已就位。
        const onConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function (info) {
            const ret = onConfigure ? onConfigure.apply(this, arguments) : undefined;
            try {
                restoreKey(this);
            } catch (e) {
                console.error("[GPT-Image] 载入后回填密钥失败：", e);
            }
            return ret;
        };
    },
});
