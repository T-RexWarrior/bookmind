import { useEffect, useState } from "react";
import { useApp } from "../../store/appStore";
import { Button, Dialog, toast } from "../../components/ui/primitives";
import * as api from "../../api/client";

export function ProjectSettingsDialog({ open, onClose, onDelete }: { open: boolean; onClose: () => void; onDelete?: () => void }) {
  const { state, dispatch } = useApp();
  const project = state.activeProject;
  const [form, setForm] = useState({ name: "", goal: "", learning_scope: "", deadline: "", current_plan: "" });
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!project) return;
    setForm({
      name: project.name || "",
      goal: project.goal || "",
      learning_scope: project.learning_scope || "",
      deadline: project.deadline || "",
      current_plan: project.current_plan || "",
    });
  }, [project, open]);

  if (!project) return null;

  const save = async () => {
    if (!form.name.trim()) return;
    setSaving(true);
    try {
      await api.updateProject(project.project_id, form);
      const fresh = await api.getProject(project.project_id);
      dispatch({ type: "SET_ACTIVE_PROJECT", project: fresh });
      dispatch({ type: "SET_PROJECTS", projects: state.projects.map((item) => item.project_id === fresh.project_id ? fresh : item) });
      toast("已经帮你保存好啦");
      onClose();
    } catch (error) {
      toast((error as Error).message || "保存失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="学习空间设置"
      size="md"
      footer={<><Button onClick={onClose}>取消</Button><Button variant="primary" onClick={save} disabled={saving || !form.name.trim()}>{saving ? "保存中…" : "保存"}</Button></>}
    >
      <div className="form-stack">
        <label><span>空间名称</span><input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} placeholder="例如：数据结构" /></label>
        <label><span>学习目标</span><textarea value={form.goal} onChange={(event) => setForm({ ...form, goal: event.target.value })} placeholder="你希望通过这些资料理解或完成什么？" rows={2} /></label>
        <label><span>学习范围</span><input value={form.learning_scope} onChange={(event) => setForm({ ...form, learning_scope: event.target.value })} placeholder="例如：第 1–5 章，或整套资料" /></label>
        <div className="form-grid-2">
          <label><span>可选截止日期</span><input type="date" value={form.deadline} onChange={(event) => setForm({ ...form, deadline: event.target.value })} /></label>
          <label><span>本次学习计划</span><input value={form.current_plan} onChange={(event) => setForm({ ...form, current_plan: event.target.value })} placeholder="例如：先读第 2 章，再做一道巩固题" /></label>
        </div>
        <p className="form-help">这些内容只是帮你理清接下来怎么学，按自己的节奏填写就好。</p>
        {onDelete && (
          <div className="settings-danger">
            <div><strong>删除学习空间</strong><p>从首页移除这个空间；已形成的学习证据仍会保留。</p></div>
            <Button variant="danger" onClick={onDelete}>删除</Button>
          </div>
        )}
      </div>
    </Dialog>
  );
}
