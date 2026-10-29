import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

app.registerExtension({
    name: "StrataSimple.ControlPanel",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "StrataSimpleControlPanel") return;
        const original = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const ret = original?.apply(this, arguments);
            const node = this;
            let busy = false;
            const status = this.addWidget("text", "last_result", "Ready", () => {}, { serialize: false });
            async function send(action) {
                if (busy) return;
                if (action !== "status" && !window.confirm(`Strata ${action} 요청을 보내시겠습니까?`)) return;
                const value = (name) => node.widgets.find(w => w.name === name)?.value;
                busy = true;
                status.value = `${action}...`;
                node.setDirtyCanvas(true, true);
                try {
                    const response = await api.fetchApi("/strata_simple/control", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ base_url: value("base_url"), timeout_s: value("timeout_s"), api_key_env: value("api_key_env"), action })
                    });
                    const result = await response.json();
                    if (!response.ok || !result.ok) throw new Error(result.error || `HTTP ${response.status}`);
                    status.value = result.result;
                    console.info("[Strata Simple]", result.result);
                } catch (error) {
                    status.value = `ERROR: ${error.message}`;
                    window.alert(status.value);
                } finally {
                    busy = false;
                    node.setDirtyCanvas(true, true);
                }
            }
            for (const [label, action] of [["Status", "status"], ["Load", "load"], ["Unload", "unload"]]) {
                this.addWidget("button", label, null, () => send(action), { serialize: false });
            }
            this.setSize([420, Math.max(this.size[1], 260)]);
            return ret;
        };
    }
});
